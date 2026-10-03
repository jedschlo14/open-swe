"""Drives ``agent-browser`` inside a thread's LangSmith sandbox.

Every call goes through ``sandbox.run`` with the server's own credentials; the
browser's DevTools and stream ports stay on the sandbox's loopback.
"""

import json
import logging
import re
import shlex
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager

from langsmith.sandbox import AsyncSandbox, ResourceNotFoundError
from pydantic import JsonValue

from agent.browser.models import BrowserSession

logger = logging.getLogger(__name__)

NAMESPACE = "open-swe"
BROWSER_ROOT = "/tmp/open-swe-browser"
MIN_ENGINE_VERSION = (0, 37, 0)
# The daemon's own idle shutdown is a backstop for a server that never sweeps.
DAEMON_IDLE_GRACE_MS = 10 * 60 * 1000
_LAUNCH_TIMEOUT_SECONDS = 120
_COMMAND_TIMEOUT_SECONDS = 60


class SandboxLostError(RuntimeError):
    """The session's sandbox no longer exists."""


class EngineMissingError(RuntimeError):
    """``agent-browser`` is absent from the sandbox or older than the pinned minimum."""


class EngineCommandError(RuntimeError):
    """An ``agent-browser`` command failed."""


@asynccontextmanager
async def connected(sandbox_id: str) -> AsyncIterator[AsyncSandbox]:
    from agent.sandboxes.providers.langsmith import connect_async_langsmith_sandbox

    try:
        client, sandbox = await connect_async_langsmith_sandbox(sandbox_id)
    except ResourceNotFoundError as exc:
        raise SandboxLostError(sandbox_id) from exc
    try:
        yield sandbox
    finally:
        await client.aclose()


def _env(session: BrowserSession) -> dict[str, str]:
    return {
        "AGENT_BROWSER_NAMESPACE": NAMESPACE,
        "AGENT_BROWSER_SESSION": session.daemon_session,
        "AGENT_BROWSER_IDLE_TIMEOUT_MS": str(
            session.idle_timeout_seconds * 1000 + DAEMON_IDLE_GRACE_MS
        ),
    }


def session_dir(session: BrowserSession) -> str:
    return f"{BROWSER_ROOT}/{session.session_id}"


async def run_command(
    sandbox: AsyncSandbox,
    session: BrowserSession,
    args: Sequence[str],
    *,
    timeout: int = _COMMAND_TIMEOUT_SECONDS,
) -> dict[str, JsonValue]:
    """Run one ``agent-browser --json`` command and return its ``data``.

    Output goes through a file because the first command spawns the daemon,
    which would otherwise inherit the pipe and hold the command open.
    """
    command = shlex.join(["agent-browser", "--json", *args])
    script = (
        f'out=$(mktemp) && {command} >"$out" 2>&1 </dev/null; rc=$?; '
        'cat "$out"; rm -f "$out"; exit $rc'
    )
    result = await sandbox.run(script, env=_env(session), timeout=timeout)
    payload = _last_json_line(result.stdout)
    if payload is None:
        raise EngineCommandError(f"agent-browser {args[0]} printed no result")
    if payload.get("success") is not True:
        error = payload.get("error")
        raise EngineCommandError(error if isinstance(error, str) else f"{args[0]} failed")
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _last_json_line(stdout: str) -> dict[str, JsonValue] | None:
    for line in reversed(stdout.strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def parse_version(output: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


async def check_engine(sandbox: AsyncSandbox) -> None:
    result = await sandbox.run("agent-browser --version", timeout=_COMMAND_TIMEOUT_SECONDS)
    version = parse_version(result.stdout) if result.exit_code == 0 else None
    if version is None or version < MIN_ENGINE_VERSION:
        raise EngineMissingError(result.stdout.strip() or result.stderr.strip())


def _stream_port(data: Mapping[str, JsonValue]) -> int | None:
    port = data.get("port")
    return port if isinstance(port, int) and not isinstance(port, bool) and port > 0 else None


async def launch(sandbox: AsyncSandbox, session: BrowserSession) -> int:
    """Launch the session's browser on a blank page and return its loopback stream port."""
    await check_engine(sandbox)
    directory = shlex.quote(session_dir(session))
    await sandbox.run(f"mkdir -p {directory} && chmod 700 {directory}")
    await run_command(sandbox, session, ["open", "about:blank"], timeout=_LAUNCH_TIMEOUT_SECONDS)
    port = _stream_port(await run_command(sandbox, session, ["stream", "status"]))
    if port is None:
        port = _stream_port(await run_command(sandbox, session, ["stream", "enable"]))
    if port is None:
        raise EngineCommandError("the browser stream did not report a port")
    return port


async def close(sandbox: AsyncSandbox, session: BrowserSession) -> None:
    """Close the browser and delete everything the session wrote to disk."""
    try:
        await run_command(sandbox, session, ["close"])
    except EngineCommandError:
        logger.warning(
            "Browser close failed; removing its files anyway",
            exc_info=True,
            extra={"browser_session_id": session.session_id},
        )
    await sandbox.run(f"rm -rf {shlex.quote(session_dir(session))}")
