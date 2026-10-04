"""Drives ``agent-browser`` inside a thread's LangSmith sandbox.

The browser runs in a network namespace that has only loopback. Its sole way
out is the egress proxy (``agent/resources/browser/egress_proxy.py``), which
admits the session's allowlist and nothing else, so a dead proxy means no
network rather than an open one. Every command enters that namespace, so a
daemon that has to be respawned is respawned inside it too.

Chromium runs headed on a private virtual display served by neko (see
``agent.browser.neko``) when the sandbox image carries it and a TURN relay is
configured, so the live view streams real video over WebRTC and native widgets
such as ``<select>`` popups appear. Otherwise it runs headless and the session
simply has no live view; the agent is unaffected.

Every call goes through ``sandbox.run`` with the server's own credentials; the
browser's DevTools port stays on the namespace's loopback.
"""

import asyncio
import io
import json
import logging
import re
import shlex
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from functools import cache
from importlib import resources

from langsmith.sandbox import AsyncSandbox, ResourceNotFoundError
from PIL import Image
from pydantic import JsonValue

from agent.browser import neko, turn
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
MIN_VIEWPORT = (320, 240)
MAX_VIEWPORT = (1920, 1200)
VIEWPORT_STEP = 16
RENDER_SCALE = 1.5
_SCREENSHOT_QUALITY = 70
_LAUNCH_TIMEOUT_SECONDS = 120
_COMMAND_TIMEOUT_SECONDS = 60
_ELEMENT_LABEL_SCRIPT = (
    "(() => {{ const e = document.elementFromPoint({x}, {y}); if (!e) return null;"
    " const t = e.closest('button,a,input,select,textarea,label,[role]') || e;"
    " return (t.getAttribute('aria-label') || t.innerText || t.value || t.title || '')"
    ".trim().slice(0, 200); }})()"
)
_PAGE_STATE_SCRIPT = (
    "JSON.stringify([location.href, document.title, document.readyState !== 'complete'])"
)
_SELECTION_SCRIPT = (
    "(() => { const a = document.activeElement;"
    " if (a && typeof a.selectionStart === 'number' && a.selectionEnd > a.selectionStart)"
    " return a.value.slice(a.selectionStart, a.selectionEnd);"
    " return String(getSelection()); })()"
)
MAX_TEXT_CHARS = 20_000
MAX_SELECTION_CHARS = 100_000


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


def _env(session: BrowserSession, *, headed: bool) -> dict[str, str]:
    env = {
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
    }
    if headed:
        env |= {
            "AGENT_BROWSER_HEADED": "1",
            "AGENT_BROWSER_NO_XVFB": "1",
            "DISPLAY": neko.display_name(session),
            "AGENT_BROWSER_ARGS": ",".join(
                (f"--force-device-scale-factor={RENDER_SCALE}", "--kiosk", "--disable-infobars")
            ),
        }
    return env


async def run_command(
    sandbox: AsyncSandbox,
    session: BrowserSession,
    args: Sequence[str],
    *,
    timeout: int = _COMMAND_TIMEOUT_SECONDS,
    headed: bool | None = None,
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
    live = session.live_view if headed is None else headed
    result = await sandbox.run(script, env=_env(session, headed=live), timeout=timeout)
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
            "egress_proxy.py",
            ["proxy", session.network_namespace, str(PROXY_PORT), f"{directory}/allow.json"],
        )
    except EngineCommandError as exc:
        raise EgressUnavailableError(str(exc)) from exc


async def _start_helper(
    sandbox: AsyncSandbox,
    session: BrowserSession,
    name: str,
    script: str,
    args: Sequence[str],
) -> str:
    """Start one helper process in the background and return its ``ready`` line."""
    directory = session_dir(session)
    command = shlex.join(["python3", f"{directory}/{script}", *args])
    log = shlex.quote(f"{directory}/{name}.log")
    pid = shlex.quote(f"{directory}/{name}.pid")
    started = await sandbox.run(
        f"nohup {command} >{log} 2>&1 </dev/null & echo $! >{pid};"
        f" for _ in $(seq 50); do line=$(grep -m1 '^ready' {log}) && echo \"$line\""
        f" && exit 0; sleep 0.1; done; cat {log} >&2; exit 1",
        timeout=_COMMAND_TIMEOUT_SECONDS,
    )
    if started.exit_code != 0:
        raise EngineCommandError(started.stderr.strip() or f"browser {name} did not start")
    return started.stdout.strip()


async def launch(sandbox: AsyncSandbox, session: BrowserSession) -> int | None:
    """Launch the session's isolated browser; returns neko's sandbox-loopback port.

    ``None`` means the browser has no live view (neko is not in the sandbox image or
    no TURN relay is configured), so it runs headless and only the agent uses it.
    """
    await check_engine(sandbox)
    live = await _live_view_available(sandbox, session)
    await _isolate(sandbox, session)
    port: int | None = None
    if live:
        try:
            port = await neko.start(
                sandbox,
                session,
                session_dir(session),
                neko.screen_for(VIEWPORT_WIDTH, VIEWPORT_HEIGHT, RENDER_SCALE),
                turn.mint(session.session_id, ttl_seconds=turn.BACKEND_TTL_SECONDS),
            )
        except neko.NekoError as exc:
            raise EngineCommandError(str(exc)) from exc
    await run_command(
        sandbox, session, ["get", "url"], timeout=_LAUNCH_TIMEOUT_SECONDS, headed=live
    )
    if not live:
        await set_viewport(sandbox, session, VIEWPORT_WIDTH, VIEWPORT_HEIGHT, headed=False)
    return port


async def _live_view_available(sandbox: AsyncSandbox, session: BrowserSession) -> bool:
    if not turn.configured():
        logger.info(
            "No TURN relay is configured; the browser runs without a live view",
            extra={"browser_session_id": session.session_id},
        )
        return False
    if not await neko.installed(sandbox):
        logger.info(
            "neko is not in the sandbox image; the browser runs without a live view",
            extra={"browser_session_id": session.session_id},
        )
        return False
    return True


def clamp_viewport(width: int, height: int) -> tuple[int, int]:
    """``width`` by ``height`` limited to the sizes the browser supports."""
    return (
        min(max(width, MIN_VIEWPORT[0]), MAX_VIEWPORT[0]),
        min(max(height, MIN_VIEWPORT[1]), MAX_VIEWPORT[1]),
    )


async def set_viewport(
    sandbox: AsyncSandbox,
    session: BrowserSession,
    width: int,
    height: int,
    *,
    headed: bool | None = None,
) -> None:
    """Size a headless page; a headed page follows the display, which neko resizes."""
    await run_command(
        sandbox,
        session,
        ["set", "viewport", str(width), str(height), str(RENDER_SCALE)],
        headed=headed,
    )


async def element_label(
    sandbox: AsyncSandbox, session: BrowserSession, x: int, y: int
) -> str | None:
    """The visible name of the element at a viewport point, for the confirmation gate."""
    data = await run_command(sandbox, session, ["eval", _ELEMENT_LABEL_SCRIPT.format(x=x, y=y)])
    result = data.get("result")
    return result if isinstance(result, str) else None


async def page_state(
    sandbox: AsyncSandbox, session: BrowserSession
) -> tuple[str, str, bool] | None:
    """The page's URL, title, and whether it is still loading."""
    data = await run_command(sandbox, session, ["eval", _PAGE_STATE_SCRIPT])
    result = data.get("result")
    if not isinstance(result, str):
        return None
    try:
        url, title, loading = json.loads(result)
    except ValueError:
        return None
    if isinstance(url, str) and isinstance(title, str) and isinstance(loading, bool):
        return url, title, loading
    return None


async def selection_text(sandbox: AsyncSandbox, session: BrowserSession) -> str:
    """The page's selected text, for a person's copy."""
    data = await run_command(sandbox, session, ["eval", _SELECTION_SCRIPT])
    result = data.get("result")
    return result[:MAX_SELECTION_CHARS] if isinstance(result, str) else ""


async def insert_text(sandbox: AsyncSandbox, session: BrowserSession, text: str) -> None:
    """Insert text at the focused element as a whole, for a person's paste or composed input."""
    await run_command(sandbox, session, ["keyboard", "inserttext", text[:MAX_TEXT_CHARS]])


def _to_viewport_pixels(image: bytes) -> bytes:
    """Scale a device-pixel capture down to viewport pixels, the space clicks use."""
    with Image.open(io.BytesIO(image)) as source:
        size = (
            max(round(source.width / RENDER_SCALE), 1),
            max(round(source.height / RENDER_SCALE), 1),
        )
        if size == source.size:
            return image
        image_format = source.format or "JPEG"
        if image_format == "JPEG":
            source = source.convert("RGB")
        scaled = source.resize(size, Image.Resampling.LANCZOS)
    output = io.BytesIO()
    if image_format == "JPEG":
        scaled.save(output, image_format, quality=_SCREENSHOT_QUALITY)
    else:
        scaled.save(output, image_format)
    return output.getvalue()


_MASK_ID = "open-swe-capture-mask"
_MASK_ON = (
    "(() => {{ const s = document.createElement('style'); s.id = '{id}';"
    " s.textContent = 'input[type=password]{{visibility:hidden!important}}"
    " input:-webkit-autofill{{-webkit-text-fill-color:transparent!important;"
    "color:transparent!important}}'; document.documentElement.appendChild(s); return true; }})()"
)
_MASK_OFF = "document.getElementById('{id}')?.remove() ?? true"


async def screenshot(
    sandbox: AsyncSandbox, session: BrowserSession, *, image_format: str = "jpeg"
) -> bytes:
    """Capture the viewport with password and autofilled fields hidden; the file is deleted after."""
    extension = "png" if image_format == "png" else "jpg"
    path = f"{session_dir(session)}/capture.{extension}"
    await run_command(sandbox, session, ["eval", _MASK_ON.format(id=_MASK_ID)])
    try:
        options = (
            ["--screenshot-format", "png"]
            if extension == "png"
            else [
                "--screenshot-format",
                "jpeg",
                "--screenshot-quality",
                str(_SCREENSHOT_QUALITY),
            ]
        )
        await run_command(sandbox, session, ["screenshot", *options, path])
    finally:
        await run_command(sandbox, session, ["eval", _MASK_OFF.format(id=_MASK_ID)])
    try:
        return await asyncio.to_thread(_to_viewport_pixels, await sandbox.read(path))
    finally:
        await sandbox.run(f"rm -f {shlex.quote(path)}")


async def close(sandbox: AsyncSandbox, session: BrowserSession) -> None:
    """Close the browser, take down its network and display, and delete everything it wrote."""
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
        f"{neko.stop_command(session, session_dir(session))}"
        f" [ -f {directory}/proxy.pid ] && kill $(cat {directory}/proxy.pid) 2>/dev/null;"
        f" ip netns delete {namespace} 2>/dev/null; rm -rf {directory}; true"
    )
