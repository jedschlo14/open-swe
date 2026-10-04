"""Runs inside a thread's sandbox and streams the browser's private X display.

Each connection is one live-view viewer. The server connects through a sandbox
tunnel, proves it knows the session's token, and from then on receives the display
as H.264 (``ffmpeg`` capturing the display, one encoder per viewer so every viewer
starts on a keyframe) and sends back the controller's input, which is injected into
the display through XTest, and resize requests, which set the display's size
through RandR.

Standard library only, plus ``ffmpeg`` and ``xrandr`` on PATH and libX11 and libXtst:
it runs in whatever image the sandbox booted.

Frames to the server are ``kind (1 byte) | length (4 bytes, big endian) | payload``:
``S`` carries ``{"width", "height"}`` and starts a new encode, ``V`` carries a chunk of
the Annex B byte stream, which begins with parameter sets and a keyframe after ``S``.
Messages from the server are one JSON object per line.

Usage:
    display_stream.py <display> <width> <height> <frame_rate> <token_path>
"""

import asyncio
import ctypes
import hmac
import json
import re
import signal
import struct
import sys
from pathlib import Path

_MIN_SIZE = 64
_MAX_SIZE = 4096
_MAX_LINE = 64 * 1024
_HELLO_TIMEOUT = 10
_READ_CHUNK = 64 * 1024
_KEYFRAME_INTERVAL_SECONDS = 1
_MAX_RATE_KBPS = 8000
_SPARE_KEYCODES = 16
_RESIZE_GRACE_SECONDS = 2
_REMAP_SETTLE_SECONDS = 0.02
_BUTTON_UP, _BUTTON_DOWN, _BUTTON_LEFT, _BUTTON_RIGHT = 4, 5, 6, 7
_OUTPUT = re.compile(r"^(\S+) connected", re.MULTILINE)


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class XInput:
    """XTest input for one display, with keysyms the keymap lacks bound to spare keycodes."""

    def __init__(self, display: str) -> None:
        self._x11 = ctypes.CDLL("libX11.so.6")
        self._xtst = ctypes.CDLL("libXtst.so.6")
        x11, xtst = self._x11, self._xtst
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XDisplayKeycodes.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        x11.XGetKeyboardMapping.restype = ctypes.POINTER(ctypes.c_ulong)
        x11.XGetKeyboardMapping.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ubyte,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        x11.XChangeKeyboardMapping.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_int,
        ]
        x11.XFree.argtypes = [ctypes.c_void_p]
        xtst.XTestFakeMotionEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        xtst.XTestFakeButtonEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        xtst.XTestFakeKeyEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        handle = x11.XOpenDisplay(display.encode())
        if not handle:
            raise RuntimeError(f"cannot open display {display}")
        self._dpy = ctypes.c_void_p(handle)
        self._direct: dict[int, int] = {}
        self._spare: list[int] = []
        self._bound: dict[int, int] = {}
        self._next_spare = 0
        self._per_keycode = 0
        self._first = 0
        self._read_keymap()

    def _read_keymap(self) -> None:
        low, high, per = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        self._x11.XDisplayKeycodes(self._dpy, ctypes.byref(low), ctypes.byref(high))
        count = high.value - low.value + 1
        table = self._x11.XGetKeyboardMapping(self._dpy, low.value, count, ctypes.byref(per))
        self._first, self._per_keycode = low.value, per.value
        direct: dict[int, int] = {}
        spare: list[int] = []
        for offset in range(count):
            symbols = [table[offset * per.value + level] for level in range(per.value)]
            keycode = low.value + offset
            if symbols[0] and symbols[0] not in direct:
                direct[symbols[0]] = keycode
            if not any(symbols):
                spare.append(keycode)
        self._x11.XFree(table)
        self._direct = direct
        self._spare = spare[-_SPARE_KEYCODES:]

    async def _keycode(self, keysym: int) -> int | None:
        keycode = self._direct.get(keysym) or self._bound.get(keysym)
        if keycode is not None:
            return keycode
        if not self._spare:
            return None
        spare = self._spare[self._next_spare % len(self._spare)]
        self._next_spare += 1
        self._bound = {sym: code for sym, code in self._bound.items() if code != spare}
        row = (ctypes.c_ulong * self._per_keycode)(*([keysym] * self._per_keycode))
        self._x11.XChangeKeyboardMapping(self._dpy, spare, self._per_keycode, row, 1)
        self._x11.XSync(self._dpy, 0)
        self._bound[keysym] = spare
        await asyncio.sleep(_REMAP_SETTLE_SECONDS)
        return spare

    def move(self, x: int, y: int) -> None:
        self._xtst.XTestFakeMotionEvent(self._dpy, -1, x, y, 0)
        self._x11.XFlush(self._dpy)

    def button(self, code: int, down: bool) -> None:
        self._xtst.XTestFakeButtonEvent(self._dpy, code, 1 if down else 0, 0)
        self._x11.XFlush(self._dpy)

    def scroll(self, right: int, up: int) -> None:
        for ticks, positive, negative in (
            (up, _BUTTON_UP, _BUTTON_DOWN),
            (right, _BUTTON_RIGHT, _BUTTON_LEFT),
        ):
            for _ in range(abs(ticks)):
                code = positive if ticks > 0 else negative
                self.button(code, True)
                self.button(code, False)

    async def key(self, keysym: int, down: bool) -> int | None:
        """Press or release ``keysym``; returns the keycode used, or ``None`` if it has none."""
        keycode = await self._keycode(keysym)
        if keycode is None:
            return None
        self._xtst.XTestFakeKeyEvent(self._dpy, keycode, 1 if down else 0, 0)
        self._x11.XFlush(self._dpy)
        return keycode

    def release_keycode(self, keycode: int) -> None:
        self._xtst.XTestFakeKeyEvent(self._dpy, keycode, 0, 0)
        self._x11.XFlush(self._dpy)


class Screen:
    """The display's size, changed through RandR; every encode restarts when it changes."""

    def __init__(self, display: str, width: int, height: int, rate: int) -> None:
        self.display = display
        self.rate = rate
        self.width = width
        self.height = height
        self.changed = asyncio.Event()
        self._lock = asyncio.Lock()
        self._output: str | None = None

    async def _xrandr(self, *args: str) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            "xrandr",
            "--display",
            self.display,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        return process.returncode or 0, output.decode(errors="replace")

    async def resize(self, width: int, height: int) -> None:
        if not (
            _MIN_SIZE <= width <= _MAX_SIZE
            and _MIN_SIZE <= height <= _MAX_SIZE
            and width % 2 == 0
            and height % 2 == 0
        ):
            raise ValueError(f"unsupported size {width}x{height}")
        async with self._lock:
            if self._output is None:
                code, text = await self._xrandr("--query")
                found = _OUTPUT.search(text)
                if code != 0 or found is None:
                    raise RuntimeError(f"xrandr found no output: {text.strip()}")
                self._output = found.group(1)
            name = f"{width}x{height}"
            total_width, total_height = width + 160, height + 30
            clock = total_width * total_height * self.rate / 1e6
            await self._xrandr(
                "--newmode",
                name,
                f"{clock:.2f}",
                *(str(value) for value in (width, width + 48, width + 80, total_width)),
                *(str(value) for value in (height, height + 3, height + 8, total_height)),
                "+hsync",
                "-vsync",
            )
            await self._xrandr("--addmode", self._output, name)
            code, text = await self._xrandr("--output", self._output, "--mode", name)
            if code != 0:
                raise RuntimeError(f"xrandr could not set {name}: {text.strip()}")
            if (width, height) != (self.width, self.height):
                self.width, self.height = width, height
                changed, self.changed = self.changed, asyncio.Event()
                changed.set()


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.terminate()
    await process.wait()


async def _pump(process: asyncio.subprocess.Process, writer: asyncio.StreamWriter) -> None:
    assert process.stdout is not None
    while True:
        chunk = await process.stdout.read(_READ_CHUNK)
        if not chunk:
            return
        writer.write(b"V" + struct.pack(">I", len(chunk)) + chunk)
        await writer.drain()


async def _stream(screen: Screen, writer: asyncio.StreamWriter) -> None:
    while True:
        changed, width, height = screen.changed, screen.width, screen.height
        head = json.dumps({"width": width, "height": height}).encode()
        writer.write(b"S" + struct.pack(">I", len(head)) + head)
        gop = str(screen.rate * _KEYFRAME_INTERVAL_SECONDS)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            *("-hide_banner", "-loglevel", "error", "-nostdin"),
            *("-f", "x11grab", "-framerate", str(screen.rate)),
            *("-video_size", f"{width}x{height}", "-draw_mouse", "1", "-i", screen.display),
            *("-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"),
            *("-pix_fmt", "yuv420p", "-crf", "24"),
            *("-maxrate", f"{_MAX_RATE_KBPS}k", "-bufsize", f"{_MAX_RATE_KBPS // 2}k"),
            *("-g", gop, "-keyint_min", gop, "-sc_threshold", "0"),
            *("-x264-params", "aud=1:repeat-headers=1"),
            *("-f", "h264", "pipe:1"),
            stdout=asyncio.subprocess.PIPE,
            stderr=sys.stderr,
        )
        pump = asyncio.create_task(_pump(process, writer))
        resized = asyncio.create_task(changed.wait())
        try:
            await asyncio.wait({pump, resized}, return_when=asyncio.FIRST_COMPLETED)
            if not resized.done():
                await asyncio.wait({resized}, timeout=_RESIZE_GRACE_SECONDS)
            if not resized.done():
                raise RuntimeError("ffmpeg stopped")
        finally:
            pump.cancel()
            resized.cancel()
            await asyncio.gather(pump, resized, return_exceptions=True)
            await _terminate(process)


async def _apply(message: dict[str, object], screen: Screen, keys: XInput, held: set[int]) -> None:
    kind = message.get("type")
    if kind == "resize":
        width, height = message.get("width"), message.get("height")
        if isinstance(width, int) and isinstance(height, int):
            await screen.resize(width, height)
        return
    if kind == "key":
        keysym, down = message.get("keysym"), message.get("down")
        if isinstance(keysym, int) and isinstance(down, bool):
            keycode = await keys.key(keysym, down)
            if keycode is not None and down:
                held.add(keycode)
            elif keycode is not None:
                held.discard(keycode)
        return
    x, y = message.get("x"), message.get("y")
    if not (isinstance(x, int) and isinstance(y, int)):
        return
    keys.move(x, y)
    if kind == "button":
        code, down = message.get("code"), message.get("down")
        if code in (1, 2, 3) and isinstance(down, bool):
            keys.button(int(code), down)
    elif kind == "scroll":
        right, up = message.get("right"), message.get("up")
        if isinstance(right, int) and isinstance(up, int):
            keys.scroll(right, up)


async def _serve(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    screen: Screen,
    keys: XInput,
    token: str,
) -> None:
    hello = json.loads(await asyncio.wait_for(reader.readline(), _HELLO_TIMEOUT))
    if not hmac.compare_digest(str(hello.get("token", "")), token):
        raise PermissionError("bad token")
    held: set[int] = set()
    streaming = asyncio.create_task(_stream(screen, writer))
    try:
        while not streaming.done():
            line = asyncio.create_task(reader.readline())
            await asyncio.wait({line, streaming}, return_when=asyncio.FIRST_COMPLETED)
            if not line.done():
                line.cancel()
                break
            raw = line.result()
            if not raw:
                break
            try:
                message = json.loads(raw)
                if isinstance(message, dict):
                    await _apply(message, screen, keys, held)
            except (ValueError, RuntimeError) as exc:
                _log(f"input refused: {exc}")
    finally:
        streaming.cancel()
        results = await asyncio.gather(streaming, return_exceptions=True)
        for keycode in held:
            keys.release_keycode(keycode)
        outcome = results[0]
        if isinstance(outcome, Exception) and not isinstance(outcome, asyncio.CancelledError):
            raise outcome


async def main(display: str, width: int, height: int, rate: int, token_path: str) -> None:
    token = Path(token_path).read_text().strip()
    screen = Screen(display, width, height, rate)
    await screen.resize(width, height)
    keys = XInput(display)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await _serve(reader, writer, screen, keys, token)
        except (ConnectionError, asyncio.IncompleteReadError) as exc:
            _log(f"viewer disconnected: {type(exc).__name__}")
        except Exception as exc:
            _log(f"viewer ended: {type(exc).__name__}: {exc}")
        finally:
            writer.close()

    main_task = asyncio.current_task()
    if main_task is not None:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, main_task.cancel)
    server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=_MAX_LINE)
    port = server.sockets[0].getsockname()[1]
    print(f"ready {port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(
        main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5])
    )
