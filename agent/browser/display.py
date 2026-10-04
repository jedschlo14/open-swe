"""Runs a session's virtual display inside the sandbox and names what streams it.

Per session this starts Xorg (dummy video driver, so the screen can be any size) and
openbox (so Chromium's kiosk window follows the screen). The helper that captures the
display as H.264 and injects input (``agent/resources/browser/display_stream.py``) is
started by the engine next to the egress proxy. Chromium itself is launched by
``agent-browser`` on the display, in the browser's network namespace; the display's
filesystem socket is reachable from there, unlike an abstract one.

The helper is reachable only through the server's sandbox tunnel and answers only to a
token derived from the dashboard secret and the session id, so no process other than
this server can mint it without reading the sandbox.
"""

import hashlib
import hmac
import logging
import shlex
from functools import cache
from importlib import resources

from langsmith.sandbox import AsyncSandbox

from agent.browser.models import BrowserSession
from agent.config import ENV

logger = logging.getLogger(__name__)

FRAME_RATE = 30
HELPER_SCRIPT = "display_stream.py"
_START_TIMEOUT_SECONDS = 60
_PROBE_TIMEOUT_SECONDS = 30
_INSTALL_TIMEOUT_SECONDS = 600

_INSTALLED_CHECK = (
    "command -v Xorg >/dev/null && command -v openbox >/dev/null"
    " && command -v xrandr >/dev/null && command -v python3 >/dev/null"
    " && command -v ffmpeg >/dev/null"
    " && ffmpeg -hide_banner -devices 2>/dev/null | grep -q x11grab"
    " && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libx264"
    ' && python3 -c \'import ctypes; ctypes.CDLL("libX11.so.6"); ctypes.CDLL("libXtst.so.6")\''
)
_INSTALL = (
    "export DEBIAN_FRONTEND=noninteractive;"
    ' if [ "$(id -u)" -eq 0 ]; then SUDO=; else SUDO="sudo -n"; fi;'
    " command -v apt-get >/dev/null && $SUDO apt-get update -qq"
    " && $SUDO apt-get install -y -qq --no-install-recommends"
    " xserver-xorg-core xserver-xorg-video-dummy openbox x11-xserver-utils"
    " ffmpeg python3 libx11-6 libxtst6"
)


class DisplayError(RuntimeError):
    """The display could not be started."""


@cache
def _resource(name: str) -> str:
    return resources.files("agent.resources").joinpath("browser", name).read_text()


def helper_script() -> str:
    return _resource(HELPER_SCRIPT)


def display_name(session: BrowserSession) -> str:
    """The session's own X display, distinct from any other the sandbox runs."""
    return f":{100 + int(session.session_id, 16) % 900}"


def x_socket(session: BrowserSession) -> str:
    return f"/tmp/.X11-unix/X{display_name(session).lstrip(':')}"


def token(session: BrowserSession) -> str:
    """The session's helper token, derived rather than stored."""
    secret = ENV.DASHBOARD_JWT_SECRET.get()
    if not secret:
        raise DisplayError("DASHBOARD_JWT_SECRET is not configured")
    return hmac.new(
        secret.encode(), f"browser-display:{session.session_id}".encode(), hashlib.sha256
    ).hexdigest()


def screen_for(width: int, height: int, scale: float) -> tuple[int, int]:
    """The display size, in device pixels, that gives a page ``width`` by ``height`` CSS pixels.

    H.264 and RandR want even sizes, so CSS sizes are first snapped to multiples of 16,
    which stay even at the 1.5 scale.
    """
    css_width, css_height = (max(value // 16, 1) * 16 for value in (width, height))
    return round(css_width * scale), round(css_height * scale)


async def installed(sandbox: AsyncSandbox) -> bool:
    """Whether the sandbox carries what the live view needs."""
    result = await sandbox.run(_INSTALLED_CHECK, timeout=_PROBE_TIMEOUT_SECONDS)
    return result.exit_code == 0


async def ensure_installed(sandbox: AsyncSandbox) -> bool:
    """Whether the sandbox can show the browser, installing the packages if they are absent."""
    if await installed(sandbox):
        return True
    try:
        result = await sandbox.run(_INSTALL, timeout=_INSTALL_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("Installing the browser display packages failed", exc_info=True)
        return False
    if result.exit_code != 0:
        logger.warning(
            "Installing the browser display packages failed",
            extra={"exit_code": result.exit_code, "stderr": result.stderr[-500:]},
        )
        return False
    return await installed(sandbox)


async def start_x(sandbox: AsyncSandbox, session: BrowserSession, directory: str) -> None:
    """Start the session's X server and window manager."""
    await sandbox.write(f"{directory}/xorg.conf", _resource("xorg.conf"))
    await sandbox.write(f"{directory}/openbox.xml", _resource("openbox.xml"))
    folder, display, socket = (
        shlex.quote(directory),
        shlex.quote(display_name(session)),
        shlex.quote(x_socket(session)),
    )
    started = await sandbox.run(
        f"nohup Xorg {display} -config {folder}/xorg.conf -noreset -nolisten tcp"
        f" -logfile {folder}/xorg.log >{folder}/xorg.out 2>&1 </dev/null &"
        f" echo $! >{folder}/xorg.pid;"
        f" for _ in $(seq 100); do [ -S {socket} ] && break; sleep 0.1; done;"
        f" [ -S {socket} ] || {{ tail -n 20 {folder}/xorg.out {folder}/xorg.log >&2; exit 1; }};"
        f" DISPLAY={display} nohup openbox --config-file {folder}/openbox.xml"
        f" >{folder}/openbox.log 2>&1 </dev/null & echo $! >{folder}/openbox.pid",
        timeout=_START_TIMEOUT_SECONDS,
    )
    if started.exit_code != 0:
        raise DisplayError(started.stderr.strip() or "the live view display did not start")


def stop_command(session: BrowserSession, directory: str) -> str:
    """A shell fragment that stops the session's display processes and removes their sockets."""
    folder = shlex.quote(directory)
    lock = shlex.quote(f"/tmp/.X{display_name(session).lstrip(':')}-lock")
    return (
        f"for pid in {folder}/display.pid {folder}/openbox.pid {folder}/xorg.pid;"
        ' do [ -f "$pid" ] && kill $(cat "$pid") 2>/dev/null; done;'
        f" rm -f {shlex.quote(x_socket(session))} {lock};"
    )
