"""Runs a session's virtual display and the neko server that streams it, inside the sandbox.

neko (``m1k1o/neko``, Apache-2.0) captures an X display and serves it as WebRTC
video, with the cursor image on a data channel and input injected back through
XTest. Per session this starts Xorg (dummy video driver, so the screen can be any
size), openbox (so Chromium's kiosk window follows the screen), and ``neko serve``
bound to sandbox loopback. Chromium itself is launched by ``agent-browser`` on the
display, in the browser's network namespace; the display's filesystem socket is
reachable from there, unlike an abstract one.

neko's REST and WebSocket API is reachable only through the server's sandbox
tunnel. Its admin token is derived from the dashboard secret and the session id,
so no process other than this server can mint it without reading the sandbox.
"""

import hashlib
import hmac
import json
import shlex
from functools import cache
from importlib import resources

from langsmith.sandbox import AsyncSandbox

from agent.browser import turn
from agent.browser.models import BrowserSession
from agent.config import ENV

NEKO_ROOT = "/opt/neko"
NEKO_BINARY = f"{NEKO_ROOT}/bin/neko"
SCREEN_RATE = 30
_START_TIMEOUT_SECONDS = 60
_PROBE_TIMEOUT_SECONDS = 30

_INSTALLED_CHECK = (
    f"test -x {NEKO_BINARY} && command -v Xorg >/dev/null && command -v openbox >/dev/null"
    f" && command -v curl >/dev/null && command -v python3 >/dev/null"
    f" && ! ldd {NEKO_BINARY} 2>/dev/null | grep -q 'not found'"
)


class NekoError(RuntimeError):
    """The display or the neko server could not be started."""


@cache
def _resource(name: str) -> str:
    return resources.files("agent.resources").joinpath("browser", name).read_text()


def display_name(session: BrowserSession) -> str:
    """The session's own X display, distinct from any other the sandbox runs."""
    return f":{100 + int(session.session_id, 16) % 900}"


def x_socket(session: BrowserSession) -> str:
    return f"/tmp/.X11-unix/X{display_name(session).lstrip(':')}"


def api_token(session: BrowserSession) -> str:
    """The session's neko admin token, derived rather than stored."""
    secret = ENV.DASHBOARD_JWT_SECRET.get()
    if not secret:
        raise NekoError("DASHBOARD_JWT_SECRET is not configured")
    return hmac.new(
        secret.encode(), f"browser-neko:{session.session_id}".encode(), hashlib.sha256
    ).hexdigest()


def screen_for(width: int, height: int, scale: float) -> tuple[int, int]:
    """The display size, in device pixels, that gives a page ``width`` by ``height`` CSS pixels.

    neko rounds a screen's width down to a multiple of 8, so CSS sizes are first
    snapped to multiples of 16, which stay on that grid at the 1.5 scale.
    """
    css_width, css_height = (max(value // 16, 1) * 16 for value in (width, height))
    return round(css_width * scale), round(css_height * scale)


async def installed(sandbox: AsyncSandbox) -> bool:
    """Whether the sandbox image carries neko, Xorg, and openbox."""
    result = await sandbox.run(_INSTALLED_CHECK, timeout=_PROBE_TIMEOUT_SECONDS)
    return result.exit_code == 0


def environment(
    session: BrowserSession, port: int, size: tuple[int, int], backend: turn.IceServer
) -> dict[str, str]:
    """The ``NEKO_*`` configuration for one session's server."""
    ice = json.dumps([backend.as_json()])
    width, height = size
    return {
        "NEKO_SERVER_BIND": f"127.0.0.1:{port}",
        "NEKO_SERVER_METRICS": "false",
        "NEKO_DESKTOP_DISPLAY": display_name(session),
        "NEKO_DESKTOP_SCREEN": f"{width}x{height}@{SCREEN_RATE}",
        "NEKO_DESKTOP_INPUT_ENABLED": "false",
        "NEKO_DESKTOP_UPLOAD_DROP": "false",
        "NEKO_CAPTURE_VIDEO_DISPLAY": display_name(session),
        "NEKO_CAPTURE_VIDEO_CODEC": "h264",
        "NEKO_CAPTURE_MICROPHONE_ENABLED": "false",
        "NEKO_MEMBER_PROVIDER": "object",
        "NEKO_MEMBER_OBJECT_USERS": "[]",
        "NEKO_SESSION_API_TOKEN": api_token(session),
        "NEKO_SESSION_COOKIE_ENABLED": "false",
        "NEKO_SESSION_IMPLICIT_HOSTING": "true",
        "NEKO_WEBRTC_ICESERVERS_FRONTEND": ice,
        "NEKO_WEBRTC_ICESERVERS_BACKEND": ice,
        "NEKO_WEBRTC_NAT1TO1": "127.0.0.1",
        "NEKO_PLUGINS_ENABLED": "false",
    }


async def _free_port(sandbox: AsyncSandbox) -> int:
    result = await sandbox.run(
        'python3 -c \'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0));'
        " print(s.getsockname()[1])'",
        timeout=_PROBE_TIMEOUT_SECONDS,
    )
    port = result.stdout.strip()
    if result.exit_code != 0 or not port.isdigit():
        raise NekoError("could not find a free port for the live view")
    return int(port)


async def start(
    sandbox: AsyncSandbox,
    session: BrowserSession,
    directory: str,
    size: tuple[int, int],
    backend: turn.IceServer,
) -> int:
    """Start the session's display, window manager, and neko; return neko's sandbox-loopback port."""
    port = await _free_port(sandbox)
    await sandbox.write(f"{directory}/xorg.conf", _resource("xorg.conf"))
    await sandbox.write(f"{directory}/openbox.xml", _resource("openbox.xml"))
    folder, display, socket = (
        shlex.quote(directory),
        shlex.quote(display_name(session)),
        shlex.quote(x_socket(session)),
    )
    xorg = (
        f"nohup Xorg {display} -config {folder}/xorg.conf -noreset -nolisten tcp"
        f" -logfile {folder}/xorg.log >{folder}/xorg.out 2>&1 </dev/null &"
        f" echo $! >{folder}/xorg.pid;"
    )
    started = await sandbox.run(
        f"{xorg}"
        f" for _ in $(seq 100); do [ -S {socket} ] && break; sleep 0.1; done;"
        f" [ -S {socket} ] || {{ tail -n 20 {folder}/xorg.out {folder}/xorg.log >&2; exit 1; }};"
        f" DISPLAY={display} nohup openbox --config-file {folder}/openbox.xml"
        f" >{folder}/openbox.log 2>&1 </dev/null & echo $! >{folder}/openbox.pid;"
        f" nohup {NEKO_BINARY} serve >{folder}/neko.log 2>&1 </dev/null & echo $! >{folder}/neko.pid;"
        f" for _ in $(seq {_START_TIMEOUT_SECONDS * 10});"
        f" do curl -sf http://127.0.0.1:{port}/health >/dev/null && exit 0; sleep 0.1; done;"
        f" tail -n 20 {folder}/neko.log >&2; exit 1",
        env={**environment(session, port, size, backend), "HOME": directory},
        timeout=_START_TIMEOUT_SECONDS + 30,
    )
    if started.exit_code != 0:
        raise NekoError(started.stderr.strip() or "the live view display did not start")
    return port


def stop_command(session: BrowserSession, directory: str) -> str:
    """A shell fragment that stops the session's display processes and removes their sockets."""
    folder = shlex.quote(directory)
    lock = shlex.quote(f"/tmp/.X{display_name(session).lstrip(':')}-lock")
    return (
        f"for pid in {folder}/neko.pid {folder}/openbox.pid {folder}/xorg.pid;"
        ' do [ -f "$pid" ] && kill $(cat "$pid") 2>/dev/null; done;'
        f" rm -f {shlex.quote(x_socket(session))} {lock};"
    )
