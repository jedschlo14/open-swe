"""Drives ``agent-browser`` inside a thread's LangSmith sandbox.

The browser runs in a network namespace that has only loopback. Its sole way
out is the egress proxy (``agent/resources/browser/egress_proxy.py``), which
admits the session's allowlist and nothing else, so a dead proxy means no
network rather than an open one. Every command enters that namespace, so a
daemon that has to be respawned is respawned inside it too.

Chromium runs headless. The live view is the page's own DOM: a bridge in the
browser's namespace (``agent/resources/browser/mirror_bridge.py``) records it
over DevTools and carries a viewer's input back, and a forwarder in the main
namespace exposes the bridge on sandbox loopback. When the bridge cannot start,
the session simply has no live view; the agent is unaffected.

Every call goes through ``sandbox.run`` with the server's own credentials; the
browser's DevTools port stays on the namespace's loopback.
"""

import asyncio
import io
import json
import logging
import math
import re
import shlex
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Literal

from langsmith.sandbox import AsyncSandbox, ResourceNotFoundError
from PIL import Image
from pydantic import JsonValue

from agent.browser.models import BrowserSession
from agent.browser.policy import allowed_domains, endpoint_of
from agent.browser.sign_ins import (
    SignInCookie,
    SignInState,
    canonical_origin,
    parse_cookies,
    parse_local_storage,
)

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
RENDER_SCALE = 1.5
_SCREENSHOT_QUALITY = 70
RECORDING_FPS = 10
MAX_RECORDING_SECONDS = 30
_SHEET_FRAME_WIDTH = 288
_SHEET_COLUMNS = 5
_ENCODE_TIMEOUT_SECONDS = 180
_LAUNCH_TIMEOUT_SECONDS = 120
_MIRROR_WAIT_SECONDS = 30
_COMMAND_TIMEOUT_SECONDS = 60
_ELEMENT_LABEL_SCRIPT = (
    "(() => {{ const e = document.elementFromPoint({x}, {y}); if (!e) return null;"
    " const t = e.closest('button,a,input,select,textarea,label,[role]') || e;"
    " return (t.getAttribute('aria-label') || t.innerText || t.value || t.title || '')"
    ".trim().slice(0, 200); }})()"
)
_SELECTION_SCRIPT = (
    "(() => { const a = document.activeElement;"
    " if (a && typeof a.selectionStart === 'number' && a.selectionEnd > a.selectionStart)"
    " return a.value.slice(a.selectionStart, a.selectionEnd);"
    " return String(getSelection()); })()"
)
MAX_TEXT_CHARS = 20_000
MAX_SELECTION_CHARS = 100_000
_PASSWORD_FIELD_SCRIPT = "!!document.querySelector('input[type=password]')"
_STORE_LOCAL_STORAGE_SCRIPT = (
    "(() => {{ for (const [k, v] of Object.entries(JSON.parse({items}))) "
    "localStorage.setItem(k, v); }})()"
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


@cache
def _mirror_resource(*path: str) -> str:
    return resources.files("agent.resources").joinpath("browser", *path).read_text()


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
    *,
    wait_seconds: int = 5,
    in_namespace: bool = False,
) -> str:
    """Start one helper process in the background and return its ``ready`` line.

    ``in_namespace`` runs it inside the browser's network namespace, with the
    environment ``agent-browser`` needs to find the session's daemon.
    """
    directory = session_dir(session)
    argv = ["python3", f"{directory}/{script}", *args]
    if in_namespace:
        variables = [f"{key}={value}" for key, value in _env(session).items()]
        argv = ["ip", "netns", "exec", session.network_namespace, "env", *variables, *argv]
    command = shlex.join(argv)
    log = shlex.quote(f"{directory}/{name}.log")
    pid = shlex.quote(f"{directory}/{name}.pid")
    started = await sandbox.run(
        f"nohup {command} >{log} 2>&1 </dev/null & echo $! >{pid};"
        f" for _ in $(seq {wait_seconds * 10}); do line=$(grep -m1 '^ready' {log}) && echo \"$line\""
        f" && exit 0; sleep 0.1; done; cat {log} >&2; exit 1",
        timeout=_COMMAND_TIMEOUT_SECONDS,
    )
    if started.exit_code != 0:
        raise EngineCommandError(started.stderr.strip() or f"browser {name} did not start")
    return started.stdout.strip()


async def _start_mirror(sandbox: AsyncSandbox, session: BrowserSession) -> int:
    """Start the bridge in the browser's namespace and its loopback forwarder; returns the port."""
    directory = session_dir(session)
    socket = f"{directory}/mirror.sock"
    for name, path in (
        ("mirror_bridge.py", ("mirror_bridge.py",)),
        ("mirror_page.js", ("mirror_page.js",)),
        ("rrweb-record.js", ("vendor", "rrweb-record.umd.min.cjs")),
        ("mirror_forward.py", ("mirror_forward.py",)),
    ):
        await sandbox.write(f"{directory}/{name}", _mirror_resource(*path))
    await _start_helper(
        sandbox,
        session,
        "mirror",
        "mirror_bridge.py",
        [socket, f"{directory}/rrweb-record.js", f"{directory}/mirror_page.js"],
        wait_seconds=_MIRROR_WAIT_SECONDS,
        in_namespace=True,
    )
    ready = await _start_helper(sandbox, session, "forward", "mirror_forward.py", [socket])
    port = ready.removeprefix("ready ").strip()
    if not port.isdigit():
        raise EngineCommandError("the live view forwarder did not report a port")
    return int(port)


async def launch(sandbox: AsyncSandbox, session: BrowserSession) -> int | None:
    """Launch the session's isolated browser; returns the live view forwarder's sandbox port.

    ``None`` means the live view could not start, so the browser runs for the agent only.
    """
    await check_engine(sandbox)
    await _isolate(sandbox, session)
    await run_command(sandbox, session, ["get", "url"], timeout=_LAUNCH_TIMEOUT_SECONDS)
    await set_viewport(sandbox, session, VIEWPORT_WIDTH, VIEWPORT_HEIGHT)
    try:
        return await _start_mirror(sandbox, session)
    except EngineCommandError:
        logger.warning(
            "The browser live view did not start",
            exc_info=True,
            extra={"browser_session_id": session.session_id},
        )
        return None


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
) -> None:
    await run_command(
        sandbox, session, ["set", "viewport", str(width), str(height), str(RENDER_SCALE)]
    )


async def element_label(
    sandbox: AsyncSandbox, session: BrowserSession, x: int, y: int
) -> str | None:
    """The visible name of the element at a viewport point, for the confirmation gate."""
    data = await run_command(sandbox, session, ["eval", _ELEMENT_LABEL_SCRIPT.format(x=x, y=y)])
    result = data.get("result")
    return result if isinstance(result, str) else None


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
_RECORDING_MASK_ID = "open-swe-recording-mask"
_MASK_ON = (
    "(() => {{ if (document.getElementById('{id}')) return true;"
    " const s = document.createElement('style'); s.id = '{id}';"
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


RecordingFormat = Literal["mp4", "gif", "webp"]

_VIDEO_FILTER = r"scale=min(1280\,iw):-2"
_ANIMATION_SCALE = r"fps=8,scale=min(960\,iw):-2:flags=lanczos"
_ANIMATION_ARGS: dict[RecordingFormat, tuple[str, ...]] = {
    "mp4": (
        "-vf",
        _VIDEO_FILTER,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "30",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
    ),
    "gif": (
        "-vf",
        _ANIMATION_SCALE + ",split[a][b];[a]palettegen=max_colors=128[p];"
        "[b][p]paletteuse=dither=bayer:bayer_scale=4",
    ),
    "webp": (
        "-vf",
        _ANIMATION_SCALE,
        "-c:v",
        "libwebp_anim",
        "-quality",
        "60",
        "-loop",
        "0",
    ),
}


@dataclass(frozen=True)
class RecordingSheet:
    """A tiled one-frame-per-second preview of a recording, and how long it ran."""

    image: bytes
    seconds: float


def _recording_file(session: BrowserSession) -> str:
    return f"{session_dir(session)}/recording.mp4"


def _recording_marker(session: BrowserSession) -> str:
    return f"{session_dir(session)}/recording.active"


async def recording_active(sandbox: AsyncSandbox, session: BrowserSession) -> bool:
    result = await sandbox.run(f"test -f {shlex.quote(_recording_marker(session))}")
    return result.exit_code == 0


async def keep_recording_mask(sandbox: AsyncSandbox, session: BrowserSession) -> None:
    """Hide password and autofilled fields on the current page for as long as it is recorded."""
    try:
        await run_command(sandbox, session, ["eval", _MASK_ON.format(id=_RECORDING_MASK_ID)])
    except EngineCommandError:
        logger.warning(
            "Could not mask the page being recorded",
            exc_info=True,
            extra={"browser_session_id": session.session_id},
        )


async def start_recording(sandbox: AsyncSandbox, session: BrowserSession) -> None:
    """Begin recording the page, replacing any earlier recording, with sensitive fields hidden."""
    await sandbox.run(f"rm -f {shlex.quote(_recording_file(session))}")
    await keep_recording_mask(sandbox, session)
    await run_command(
        sandbox,
        session,
        ["record", "restart", _recording_file(session), "--fps", str(RECORDING_FPS)],
    )
    await sandbox.run(f"date +%s >{shlex.quote(_recording_marker(session))}")


async def _end_recording(sandbox: AsyncSandbox, session: BrowserSession) -> None:
    await sandbox.run(f"rm -f {shlex.quote(_recording_marker(session))}")
    try:
        await run_command(sandbox, session, ["eval", _MASK_OFF.format(id=_RECORDING_MASK_ID)])
    except EngineCommandError:
        logger.warning(
            "Could not remove the recording mask",
            exc_info=True,
            extra={"browser_session_id": session.session_id},
        )


async def discard_recording(sandbox: AsyncSandbox, session: BrowserSession) -> bool:
    """Stop any recording and delete it; returns whether one was running."""
    if not await recording_active(sandbox, session):
        return False
    try:
        await run_command(sandbox, session, ["record", "stop"])
    except EngineCommandError:
        logger.warning(
            "Stopping the recording failed; deleting it anyway",
            exc_info=True,
            extra={"browser_session_id": session.session_id},
        )
    finally:
        await sandbox.run(f"rm -f {shlex.quote(_recording_file(session))}")
        await _end_recording(sandbox, session)
    return True


async def stop_recording(sandbox: AsyncSandbox, session: BrowserSession) -> RecordingSheet | None:
    """Finish the recording and return a preview sheet, or ``None`` when none was running."""
    if not await recording_active(sandbox, session):
        return None
    try:
        await run_command(sandbox, session, ["record", "stop"])
    except EngineCommandError:
        await sandbox.run(f"rm -f {shlex.quote(_recording_file(session))}")
        raise
    finally:
        await _end_recording(sandbox, session)
    return await _contact_sheet(sandbox, session)


async def _contact_sheet(sandbox: AsyncSandbox, session: BrowserSession) -> RecordingSheet:
    video = shlex.quote(_recording_file(session))
    probe = await sandbox.run(
        f"ffprobe -v error -show_entries format=duration -of csv=p=0 {video}",
        timeout=_COMMAND_TIMEOUT_SECONDS,
    )
    try:
        seconds = float(probe.stdout.strip())
    except ValueError:
        raise EngineCommandError("the recording is empty or unreadable") from None
    frames = min(max(math.ceil(seconds), 1), MAX_RECORDING_SECONDS)
    columns = min(_SHEET_COLUMNS, frames)
    rows = math.ceil(frames / columns)
    sheet = f"{session_dir(session)}/sheet.jpg"
    made = await sandbox.run(
        f"ffmpeg -v error -y -i {video} -vf"
        f" fps=1,scale={_SHEET_FRAME_WIDTH}:-2,tile={columns}x{rows}"
        f" -frames:v 1 -update 1 -q:v 5 {shlex.quote(sheet)}",
        timeout=_ENCODE_TIMEOUT_SECONDS,
    )
    if made.exit_code != 0:
        raise EngineCommandError(made.stderr.strip() or "could not preview the recording")
    try:
        return RecordingSheet(image=await sandbox.read(sheet), seconds=seconds)
    finally:
        await sandbox.run(f"rm -f {shlex.quote(sheet)}")


async def export_recording(
    sandbox: AsyncSandbox, session: BrowserSession, recording_format: RecordingFormat
) -> bytes:
    """The stopped recording encoded as ``recording_format``, at most 30 seconds long."""
    source = _recording_file(session)
    output = f"{session_dir(session)}/export.{recording_format}"
    quoted = shlex.quote(output)
    args = shlex.join(_ANIMATION_ARGS[recording_format])
    try:
        made = await sandbox.run(
            f"test -f {shlex.quote(source)} && ffmpeg -v error -y -i {shlex.quote(source)}"
            f" -t {MAX_RECORDING_SECONDS} {args} {quoted}",
            timeout=_ENCODE_TIMEOUT_SECONDS,
        )
        if made.exit_code != 0:
            raise EngineCommandError(made.stderr.strip() or "there is no finished recording")
        return await sandbox.read(output)
    finally:
        await sandbox.run(f"rm -f {quoted}")


async def close(sandbox: AsyncSandbox, session: BrowserSession) -> None:
    """Close the browser, take down its network and helpers, and delete everything it wrote."""
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
        f"for pid in {directory}/forward.pid {directory}/mirror.pid {directory}/proxy.pid;"
        ' do [ -f "$pid" ] && kill $(cat "$pid") 2>/dev/null; done;'
        f" ip netns delete {namespace} 2>/dev/null; rm -rf {directory}; true"
    )


async def current_origin(sandbox: AsyncSandbox, session: BrowserSession) -> str | None:
    """The canonical origin of the page the browser is on, when it is an external one."""
    page = await run_command(sandbox, session, ["get", "url"])
    url = page.get("url")
    return canonical_origin(url) if isinstance(url, str) else None


async def capture_sign_in(
    sandbox: AsyncSandbox, session: BrowserSession, origin: str
) -> SignInState:
    """The current page's cookies for ``origin``'s host and its localStorage."""
    endpoint = endpoint_of(origin)
    host = endpoint.host if endpoint is not None else ""
    cookies = parse_cookies(await run_command(sandbox, session, ["cookies", "get"]), host)
    storage = parse_local_storage(await run_command(sandbox, session, ["storage", "local"]))
    return SignInState(cookies=cookies, local_storage=storage)


def _cookie_args(cookie: SignInCookie, origin: str) -> list[str]:
    args = ["cookies", "set", cookie.name, cookie.value, "--path", cookie.path]
    args += ["--domain", cookie.domain] if cookie.domain.startswith(".") else ["--url", origin]
    if cookie.http_only:
        args.append("--httpOnly")
    if cookie.secure:
        args.append("--secure")
    if cookie.same_site is not None:
        args += ["--sameSite", cookie.same_site]
    if cookie.expires is not None:
        args += ["--expires", str(cookie.expires)]
    return args


async def apply_sign_in(
    sandbox: AsyncSandbox, session: BrowserSession, origin: str, state: SignInState
) -> bool:
    """Restore a saved sign-in and report whether the page now looks signed in.

    The browser's allowlist forbids loading saved state, so cookies are set one at
    a time and localStorage is written once the origin's page has loaded. A
    failed restore leaves nothing behind.
    """
    skipped = 0
    for cookie in state.cookies:
        # agent-browser reads a leading dash as a flag, so such values cannot be passed.
        if cookie.name.startswith("-") or cookie.value.startswith("-"):
            skipped += 1
            continue
        await run_command(sandbox, session, _cookie_args(cookie, origin))
    if skipped:
        logger.warning(
            "Saved sign-in cookies skipped", extra={"browser_session_id": session.session_id}
        )
    await run_command(sandbox, session, ["open", origin], timeout=_LAUNCH_TIMEOUT_SECONDS)
    if state.local_storage:
        items = json.dumps(json.dumps(state.local_storage))
        await run_command(
            sandbox, session, ["eval", _STORE_LOCAL_STORAGE_SCRIPT.format(items=items)]
        )
        await run_command(sandbox, session, ["reload"])
    on_origin = await current_origin(sandbox, session) == origin
    has_password_field = (
        await run_command(sandbox, session, ["eval", _PASSWORD_FIELD_SCRIPT])
    ).get("result")
    if on_origin and has_password_field is False:
        return True
    await run_command(sandbox, session, ["cookies", "clear"])
    if on_origin:
        await run_command(sandbox, session, ["eval", "localStorage.clear()"])
    return False
