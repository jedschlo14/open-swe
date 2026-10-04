"""Drives ``agent-browser`` inside a thread's LangSmith sandbox.

The browser runs in a network namespace that has only loopback. Its sole way
out is the egress proxy (``agent/resources/browser/egress_proxy.py``), which
admits the session's allowlist and nothing else, so a dead proxy means no
network rather than an open one. Every command enters that namespace, so a
daemon that has to be respawned is respawned inside it too.

Every call goes through ``sandbox.run`` with the server's own credentials; the
browser's DevTools and stream ports stay on the namespace's loopback.
"""

import asyncio
import base64
import io
import json
import logging
import re
import shlex
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from functools import cache
from importlib import resources

from langsmith.sandbox import AsyncSandbox, ResourceNotFoundError
from PIL import Image
from pydantic import JsonValue

from agent.browser.models import BrowserSession
from agent.browser.policy import allowed_domains

logger = logging.getLogger(__name__)

NAMESPACE = "open-swe"
BROWSER_ROOT = "/tmp/open-swe-browser"
MIN_ENGINE_VERSION = (0, 37, 0)
PROXY_PORT = 3128
MAX_OUTPUT_CHARS = 40_000
# The daemon's own idle shutdown is a backstop for a server that never sweeps.
DAEMON_IDLE_GRACE_MS = 10 * 60 * 1000
VIEWPORT_WIDTH = 1440
VIEWPORT_HEIGHT = 900
# Rendered at twice the viewport so the live view stays sharp on high-density (4K) displays.
DEVICE_SCALE_FACTOR = 2
_SCREENSHOT_QUALITY = 70
_LAUNCH_TIMEOUT_SECONDS = 120
_COMMAND_TIMEOUT_SECONDS = 60
_ELEMENT_LABEL_SCRIPT = (
    "(() => {{ const e = document.elementFromPoint({x}, {y}); if (!e) return null;"
    " const t = e.closest('button,a,input,select,textarea,label,[role]') || e;"
    " return (t.getAttribute('aria-label') || t.innerText || t.value || t.title || '')"
    ".trim().slice(0, 200); }})()"
)


class SandboxLostError(RuntimeError):
    """The session's sandbox no longer exists."""


class EngineMissingError(RuntimeError):
    """``agent-browser`` is absent from the sandbox or older than the pinned minimum."""


class EgressUnavailableError(RuntimeError):
    """The sandbox cannot isolate the browser's network, so the browser must not start."""


class EngineCommandError(RuntimeError):
    """An ``agent-browser`` command failed."""


@cache
def _proxy_script() -> str:
    return resources.files("agent.resources").joinpath("browser", "egress_proxy.py").read_text()


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


def session_dir(session: BrowserSession) -> str:
    return f"{BROWSER_ROOT}/{session.session_id}"


def _env(session: BrowserSession) -> dict[str, str]:
    return {
        "AGENT_BROWSER_NAMESPACE": NAMESPACE,
        "AGENT_BROWSER_SESSION": session.daemon_session,
        "AGENT_BROWSER_IDLE_TIMEOUT_MS": str(
            session.idle_timeout_seconds * 1000 + DAEMON_IDLE_GRACE_MS
        ),
        "AGENT_BROWSER_PROXY": f"http://127.0.0.1:{PROXY_PORT}",
        # Loopback goes through the proxy too: the namespace's own loopback is empty.
        "AGENT_BROWSER_PROXY_BYPASS": "<-loopback>",
        "AGENT_BROWSER_ALLOWED_DOMAINS": allowed_domains(session.approved_endpoints),
        "AGENT_BROWSER_MAX_OUTPUT": str(MAX_OUTPUT_CHARS),
        "AGENT_BROWSER_STREAM_QUALITY": "80",
        # Both caps must be set, or the stream downscales frames to the viewport's CSS size.
        "AGENT_BROWSER_STREAM_MAX_WIDTH": str(VIEWPORT_WIDTH * DEVICE_SCALE_FACTOR),
        "AGENT_BROWSER_STREAM_MAX_HEIGHT": str(VIEWPORT_HEIGHT * DEVICE_SCALE_FACTOR),
    }


async def run_command(
    sandbox: AsyncSandbox,
    session: BrowserSession,
    args: Sequence[str],
    *,
    timeout: int = _COMMAND_TIMEOUT_SECONDS,
) -> dict[str, JsonValue]:
    """Run one ``agent-browser --json`` command in the browser's namespace and return its ``data``.

    Output goes through a file because the first command spawns the daemon,
    which would otherwise inherit the pipe and hold the command open.
    """
    command = shlex.join(
        ["ip", "netns", "exec", session.network_namespace, "agent-browser", "--json", *args]
    )
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


async def write_allowlist(
    sandbox: AsyncSandbox, session: BrowserSession, endpoints: Sequence[str]
) -> None:
    allowed = sorted({*session.approved_endpoints, *endpoints})
    await sandbox.write(f"{session_dir(session)}/allow.json", json.dumps(allowed))


async def _isolate(sandbox: AsyncSandbox, session: BrowserSession) -> None:
    directory = session_dir(session)
    namespace = shlex.quote(session.network_namespace)
    setup = await sandbox.run(
        f"mkdir -p {shlex.quote(directory)} && chmod 700 {shlex.quote(directory)}"
        " && command -v python3 >/dev/null"
        f" && (ip netns list | grep -qx {namespace} || ip netns add {namespace})"
        f" && ip netns exec {namespace} ip link set lo up",
        timeout=_COMMAND_TIMEOUT_SECONDS,
    )
    if setup.exit_code != 0:
        raise EgressUnavailableError(setup.stderr.strip() or "network isolation failed")
    await sandbox.write(f"{directory}/egress_proxy.py", _proxy_script())
    await write_allowlist(sandbox, session, session.allowed_endpoints)
    try:
        await _start_helper(
            sandbox,
            session,
            "proxy",
            [session.network_namespace, str(PROXY_PORT), f"{directory}/allow.json"],
        )
    except EngineCommandError as exc:
        raise EgressUnavailableError(str(exc)) from exc


async def _start_helper(
    sandbox: AsyncSandbox, session: BrowserSession, mode: str, args: Sequence[str]
) -> str:
    """Start one ``egress_proxy.py`` mode in the background and return its ``ready`` line."""
    directory = session_dir(session)
    command = shlex.join(["python3", f"{directory}/egress_proxy.py", mode, *args])
    log = shlex.quote(f"{directory}/{mode}.log")
    pid = shlex.quote(f"{directory}/{mode}.pid")
    started = await sandbox.run(
        f"nohup {command} >{log} 2>&1 </dev/null & echo $! >{pid};"
        f" for _ in $(seq 50); do line=$(grep -m1 '^ready' {log}) && echo \"$line\" && exit 0;"
        f" sleep 0.1; done; cat {log} >&2; exit 1",
        timeout=_COMMAND_TIMEOUT_SECONDS,
    )
    if started.exit_code != 0:
        raise EngineCommandError(started.stderr.strip() or f"browser {mode} did not start")
    return started.stdout.strip()


async def launch(sandbox: AsyncSandbox, session: BrowserSession) -> int:
    """Launch the session's isolated browser and return the sandbox-loopback port of its stream."""
    await check_engine(sandbox)
    await _isolate(sandbox, session)
    await run_command(sandbox, session, ["get", "url"], timeout=_LAUNCH_TIMEOUT_SECONDS)
    # Without an explicit viewport the page is shorter than the size the stream reports,
    # so frames are stretched and live-view clicks land below the pointer.
    await run_command(
        sandbox,
        session,
        ["set", "viewport", str(VIEWPORT_WIDTH), str(VIEWPORT_HEIGHT), str(DEVICE_SCALE_FACTOR)],
    )
    port = _stream_port(await run_command(sandbox, session, ["stream", "status"]))
    if port is None:
        port = _stream_port(await run_command(sandbox, session, ["stream", "enable"]))
    if port is None:
        raise EngineCommandError("the browser stream did not report a port")
    ready = await _start_helper(sandbox, session, "forward", [session.network_namespace, str(port)])
    forwarded = ready.removeprefix("ready ").strip()
    if not forwarded.isdigit():
        raise EngineCommandError("the stream forward did not report a port")
    return int(forwarded)


async def element_label(
    sandbox: AsyncSandbox, session: BrowserSession, x: int, y: int
) -> str | None:
    """The visible name of the element at a viewport point, for the confirmation gate."""
    data = await run_command(sandbox, session, ["eval", _ELEMENT_LABEL_SCRIPT.format(x=x, y=y)])
    result = data.get("result")
    return result if isinstance(result, str) else None


def _to_viewport_pixels(image: bytes) -> bytes:
    """Scale a device-pixel JPEG capture down to viewport pixels, the space clicks use."""
    with Image.open(io.BytesIO(image)) as source:
        size = (
            max(round(source.width / DEVICE_SCALE_FACTOR), 1),
            max(round(source.height / DEVICE_SCALE_FACTOR), 1),
        )
        if size == source.size:
            return image
        scaled = source.convert("RGB").resize(size, Image.Resampling.LANCZOS)
    output = io.BytesIO()
    scaled.save(output, "JPEG", quality=_SCREENSHOT_QUALITY)
    return output.getvalue()


async def screenshot(sandbox: AsyncSandbox, session: BrowserSession) -> str:
    """Capture the viewport as JPEG, delete the file, and return it base64-encoded."""
    path = f"{session_dir(session)}/capture.jpg"
    await run_command(
        sandbox,
        session,
        [
            "screenshot",
            "--screenshot-format",
            "jpeg",
            "--screenshot-quality",
            str(_SCREENSHOT_QUALITY),
            path,
        ],
    )
    try:
        image = await asyncio.to_thread(_to_viewport_pixels, await sandbox.read(path))
        return base64.b64encode(image).decode()
    finally:
        await sandbox.run(f"rm -f {shlex.quote(path)}")


async def close(sandbox: AsyncSandbox, session: BrowserSession) -> None:
    """Close the browser, take down its network, and delete everything it wrote to disk."""
    try:
        await run_command(sandbox, session, ["close"])
    except EngineCommandError:
        logger.warning(
            "Browser close failed; removing its files anyway",
            exc_info=True,
            extra={"browser_session_id": session.session_id},
        )
    directory = shlex.quote(session_dir(session))
    namespace = shlex.quote(session.network_namespace)
    await sandbox.run(
        f"for pid in {directory}/proxy.pid {directory}/forward.pid;"
        ' do [ -f "$pid" ] && kill $(cat "$pid") 2>/dev/null; done;'
        f" ip netns delete {namespace} 2>/dev/null; rm -rf {directory}; true"
    )
