"""Serves one browser session's virtual display: H.264 video, cursor shape, and input.

Runs in the sandbox's main network namespace next to the browser's display
(the browser's own namespace has no route to it, only to the X socket file). It
listens on sandbox loopback; the dashboard relay reaches it through a sandbox
tunnel and speaks a length-prefixed protocol (``kind`` byte, 4-byte big-endian
length, payload). Standard library plus ctypes only, so a sandbox needs just
``ffmpeg`` and the X client libraries that Chromium already requires.

Usage: live_helper.py <display> <render_scale> <css_width> <css_height>

Helper to relay: ``V`` video access unit (flag byte, 3 codec bytes on key
frames, Annex-B data), ``C`` cursor JSON (PNG as base64), ``S`` status JSON.
Relay to helper: ``I`` input JSON, ``R`` resize JSON (CSS pixels).
Encoding runs only while a relay is connected.
"""

import asyncio
import base64
import ctypes
import json
import struct
import sys
import time
import zlib
from collections.abc import Iterator

KIND_VIDEO = b"V"
KIND_CURSOR = b"C"
KIND_STATUS = b"S"
KIND_INPUT = b"I"
KIND_RESIZE = b"R"
MAX_PAYLOAD = 64 * 1024

FPS = 30
KEYFRAME_SECONDS = 1
BITRATE = "4M"
ENCODER_GRACE_SECONDS = 5.0
CURSOR_POLL_SECONDS = 1 / 30
WINDOW_WAIT_SECONDS = 20.0
CLIENT_BACKLOG_BYTES = 1_500_000
MIN_CSS = (320, 240)
MAX_CSS = (1920, 1200)
WHEEL_TICK_CSS_PIXELS = 72.0

NAL_AUD, NAL_SPS, NAL_IDR = 9, 7, 5

_NAMED_KEYS = {
    "Enter": 0xFF0D,
    "Backspace": 0xFF08,
    "Tab": 0xFF09,
    "Escape": 0xFF1B,
    "Delete": 0xFFFF,
    "Insert": 0xFF63,
    "Home": 0xFF50,
    "End": 0xFF57,
    "PageUp": 0xFF55,
    "PageDown": 0xFF56,
    "ArrowLeft": 0xFF51,
    "ArrowUp": 0xFF52,
    "ArrowRight": 0xFF53,
    "ArrowDown": 0xFF54,
    "CapsLock": 0xFFE5,
    "ContextMenu": 0xFF67,
    **{f"F{n}": 0xFFBD + n for n in range(1, 13)},
}
_MODIFIER_CODES = {
    "ShiftLeft": 0xFFE1,
    "ShiftRight": 0xFFE2,
    "ControlLeft": 0xFFE3,
    "ControlRight": 0xFFE4,
    "AltLeft": 0xFFE9,
    "AltRight": 0xFFEA,
    "MetaLeft": 0xFFEB,
    "MetaRight": 0xFFEC,
}
_BUTTONS = {0: 1, 1: 2, 2: 3}


def keysym_for(key: str, code: str) -> int | None:
    """The X keysym a browser key event stands for, or ``None`` when it has none."""
    if code in _MODIFIER_CODES:
        return _MODIFIER_CODES[code]
    if key in _NAMED_KEYS:
        return _NAMED_KEYS[key]
    if len(key) == 1:
        point = ord(key)
        return point if point < 0x100 else 0x1000000 | point
    return None


def split_access_units(buffer: bytearray) -> Iterator[bytes]:
    """Pop every complete H.264 access unit off ``buffer``, split at access-unit delimiters.

    The last unit stays buffered: it is only known to be complete once the next
    delimiter starts. Needs the encoder to emit one (``aud=1``).
    """
    starts: list[int] = []
    index = buffer.find(b"\x00\x00\x01")
    while index != -1:
        if index + 3 < len(buffer) and buffer[index + 3] & 0x1F == NAL_AUD:
            starts.append(index - 1 if index > 0 and buffer[index - 1] == 0 else index)
        index = buffer.find(b"\x00\x00\x01", index + 3)
    for begin, end in zip(starts, starts[1:], strict=False):
        yield bytes(buffer[begin:end])
    if len(starts) > 1:
        del buffer[: starts[-1]]


def nal_types(unit: bytes) -> list[int]:
    types: list[int] = []
    index = unit.find(b"\x00\x00\x01")
    while index != -1 and index + 3 < len(unit):
        types.append(unit[index + 3] & 0x1F)
        index = unit.find(b"\x00\x00\x01", index + 3)
    return types


def codec_bytes(unit: bytes) -> bytes | None:
    """Profile, constraint, and level bytes from the unit's SPS, which name its ``avc1`` codec."""
    index = unit.find(b"\x00\x00\x01")
    while index != -1 and index + 7 <= len(unit):
        if unit[index + 3] & 0x1F == NAL_SPS:
            return unit[index + 4 : index + 7]
        index = unit.find(b"\x00\x00\x01", index + 3)
    return None


def png(width: int, height: int, rgba: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    stride = width * 4
    raw = b"".join(b"\x00" + rgba[row * stride : (row + 1) * stride] for row in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def unpremultiply(argb: list[int]) -> bytes:
    out = bytearray()
    for value in argb:
        alpha = (value >> 24) & 0xFF
        red, green, blue = (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF
        if 0 < alpha < 255:
            red, green, blue = (min(255, channel * 255 // alpha) for channel in (red, green, blue))
        out += bytes((red, green, blue, alpha))
    return bytes(out)


def even(value: float) -> int:
    return max(2, round(value) // 2 * 2)


class _Attributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong),
        ("klass", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


class _CursorImage(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_short),
        ("y", ctypes.c_short),
        ("width", ctypes.c_ushort),
        ("height", ctypes.c_ushort),
        ("xhot", ctypes.c_ushort),
        ("yhot", ctypes.c_ushort),
        ("cursor_serial", ctypes.c_ulong),
        ("pixels", ctypes.POINTER(ctypes.c_ulong)),
        ("atom", ctypes.c_ulong),
        ("name", ctypes.c_char_p),
    ]


class Display:
    """One X connection: window lookup and resize, cursor image, and synthetic input."""

    def __init__(self, name: str) -> None:
        self._x = ctypes.CDLL("libX11.so.6")
        self._fixes = ctypes.CDLL("libXfixes.so.3")
        self._tst = ctypes.CDLL("libXtst.so.6")
        x = self._x
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XQueryTree.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        x.XGetWindowAttributes.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_Attributes),
        ]
        x.XMoveResizeWindow.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        x.XFree.argtypes = [ctypes.c_void_p]
        x.XFlush.argtypes = [ctypes.c_void_p]
        x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XKeysymToKeycode.restype = ctypes.c_ubyte
        x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x.XDisplayKeycodes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        x.XChangeKeyboardMapping.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_int,
        ]
        self._fixes.XFixesGetCursorImage.restype = ctypes.POINTER(_CursorImage)
        self._fixes.XFixesGetCursorImage.argtypes = [ctypes.c_void_p]
        self._tst.XTestFakeMotionEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        self._tst.XTestFakeButtonEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        self._tst.XTestFakeKeyEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        self._display = x.XOpenDisplay(name.encode())
        if not self._display:
            raise RuntimeError(f"cannot open display {name}")
        self._root = x.XDefaultRootWindow(self._display)
        low, high = ctypes.c_int(), ctypes.c_int()
        x.XDisplayKeycodes(self._display, ctypes.byref(low), ctypes.byref(high))
        self._scratch_keycode = high.value
        self._held: dict[str, int] = {}

    def toplevel(self) -> int | None:
        """The browser's window: the largest mapped, managed child of the root."""
        root, parent = ctypes.c_ulong(), ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint()
        if not self._x.XQueryTree(
            self._display,
            self._root,
            ctypes.byref(root),
            ctypes.byref(parent),
            ctypes.byref(children),
            ctypes.byref(count),
        ):
            return None
        best, best_area = None, 0
        for index in range(count.value):
            attributes = _Attributes()
            self._x.XGetWindowAttributes(self._display, children[index], ctypes.byref(attributes))
            area = attributes.width * attributes.height
            if (
                attributes.map_state == 2
                and not attributes.override_redirect
                and area > best_area
                and attributes.width > 64
            ):
                best, best_area = children[index], area
        if children:
            self._x.XFree(children)
        return best

    def place(self, window: int, width: int, height: int) -> None:
        self._x.XMoveResizeWindow(self._display, window, 0, 0, width, height)
        self._x.XFlush(self._display)

    def cursor(self) -> tuple[int, int, int, int, int, bytes] | None:
        """The pointer's serial, size, hotspot, and RGBA pixels."""
        image = self._fixes.XFixesGetCursorImage(self._display)
        if not image:
            return None
        try:
            contents = image.contents
            count = contents.width * contents.height
            pixels = [contents.pixels[index] & 0xFFFFFFFF for index in range(count)]
            return (
                int(contents.cursor_serial),
                contents.width,
                contents.height,
                contents.xhot,
                contents.yhot,
                unpremultiply(pixels),
            )
        finally:
            self._x.XFree(ctypes.cast(image, ctypes.c_void_p))

    def move(self, x: int, y: int) -> None:
        self._tst.XTestFakeMotionEvent(self._display, -1, x, y, 0)
        self._x.XFlush(self._display)

    def button(self, button: int, pressed: bool) -> None:
        self._tst.XTestFakeButtonEvent(self._display, button, 1 if pressed else 0, 0)
        self._x.XFlush(self._display)

    def key(self, code: str, keysym: int, pressed: bool) -> None:
        """Press or release a key; a release uses the keycode its press did."""
        if pressed:
            keycode = self._x.XKeysymToKeycode(self._display, keysym)
            if keycode == 0:
                keycode = self._scratch_keycode
                symbols = (ctypes.c_ulong * 1)(keysym)
                self._x.XChangeKeyboardMapping(self._display, keycode, 1, symbols, 1)
                self._x.XSync(self._display, 0)
            self._held[code] = keycode
        else:
            keycode = self._held.pop(code, 0)
            if keycode == 0:
                return
        self._tst.XTestFakeKeyEvent(self._display, keycode, 1 if pressed else 0, 0)
        self._x.XFlush(self._display)

    def release_all(self) -> None:
        for code in list(self._held):
            self._tst.XTestFakeKeyEvent(self._display, self._held.pop(code), 0, 0)
        self._x.XFlush(self._display)


class Client:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.needs_key = True

    def send(self, kind: bytes, payload: bytes) -> None:
        self.writer.write(kind + struct.pack(">I", len(payload)) + payload)

    @property
    def backlog(self) -> int:
        return self.writer.transport.get_write_buffer_size()


class Helper:
    def __init__(self, display: str, scale: float, css_width: int, css_height: int) -> None:
        self.display_name = display
        self.scale = scale
        self.css = (css_width, css_height)
        self.x = Display(display)
        self.window: int | None = None
        self.clients: set[Client] = set()
        self.encoder: asyncio.subprocess.Process | None = None
        self.encoder_task: asyncio.Task[None] | None = None
        self.stop_task: asyncio.Task[None] | None = None
        self.cursor_serial = -1
        self.last_cursor: bytes | None = None
        self.wheel = [0.0, 0.0]

    @property
    def size(self) -> tuple[int, int]:
        return even(self.css[0] * self.scale), even(self.css[1] * self.scale)

    def status(self) -> bytes:
        width, height = self.size
        return json.dumps(
            {"width": width, "height": height, "cssWidth": self.css[0], "cssHeight": self.css[1]}
        ).encode()

    async def attach_window(self) -> None:
        deadline = time.monotonic() + WINDOW_WAIT_SECONDS
        while time.monotonic() < deadline:
            self.window = self.x.toplevel()
            if self.window is not None:
                self.fit()
                return
            await asyncio.sleep(0.2)
        raise RuntimeError("the browser window never appeared")

    def fit(self) -> None:
        if self.window is not None:
            self.x.place(self.window, *self.size)

    async def resize(self, css_width: int, css_height: int) -> None:
        css_width = min(max(css_width, MIN_CSS[0]), MAX_CSS[0])
        css_height = min(max(css_height, MIN_CSS[1]), MAX_CSS[1])
        if (css_width, css_height) == self.css:
            return
        self.css = (css_width, css_height)
        self.window = self.x.toplevel() or self.window
        self.fit()
        self.broadcast(KIND_STATUS, self.status())
        if self.encoder is not None:
            await self.stop_encoder()
            await self.start_encoder()

    def broadcast(self, kind: bytes, payload: bytes) -> None:
        for client in self.clients:
            client.send(kind, payload)

    async def start_encoder(self) -> None:
        if self.encoder is not None:
            return
        width, height = self.size
        self.encoder = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "x11grab",
            "-draw_mouse",
            "0",
            "-framerate",
            str(FPS),
            "-video_size",
            f"{width}x{height}",
            "-i",
            f"{self.display_name}.0+0,0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-profile:v",
            "baseline",
            "-pix_fmt",
            "yuv420p",
            "-bf",
            "0",
            "-b:v",
            BITRATE,
            "-maxrate",
            BITRATE,
            "-bufsize",
            "2M",
            "-x264-params",
            f"aud=1:repeat-headers=1:keyint={FPS * KEYFRAME_SECONDS}:scenecut=0",
            "-f",
            "h264",
            "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=sys.stderr,
            limit=8 * 1024 * 1024,
        )
        for client in self.clients:
            client.needs_key = True
        self.encoder_task = asyncio.create_task(self.pump(self.encoder))

    async def stop_encoder(self) -> None:
        process, task = self.encoder, self.encoder_task
        self.encoder = self.encoder_task = None
        if process is not None and process.returncode is None:
            process.kill()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if process is not None:
            await process.wait()

    async def pump(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        buffer = bytearray()
        while True:
            chunk = await process.stdout.read(262_144)
            if not chunk:
                break
            buffer += chunk
            for unit in split_access_units(buffer):
                self.deliver(unit)
        if self.encoder is process:
            print("video encoder exited", file=sys.stderr, flush=True)
            self.encoder = None
            if self.clients:
                await asyncio.sleep(1)
                await self.start_encoder()

    def deliver(self, unit: bytes) -> None:
        key = NAL_IDR in nal_types(unit)
        codec = codec_bytes(unit) if key else None
        payload = b"\x01" + codec + unit if key and codec is not None else b"\x00" + unit
        for client in self.clients:
            if client.needs_key and not key:
                continue
            if client.backlog > CLIENT_BACKLOG_BYTES and not key:
                client.needs_key = True
                continue
            if key and client.backlog > CLIENT_BACKLOG_BYTES * 2:
                continue
            client.needs_key = False
            client.send(KIND_VIDEO, payload)

    async def poll_cursor(self) -> None:
        while True:
            await asyncio.sleep(CURSOR_POLL_SECONDS)
            if not self.clients:
                continue
            image = self.x.cursor()
            if image is None or image[0] == self.cursor_serial:
                continue
            serial, width, height, hot_x, hot_y, rgba = image
            self.cursor_serial = serial
            self.last_cursor = json.dumps(
                {
                    "width": width,
                    "height": height,
                    "hotX": hot_x,
                    "hotY": hot_y,
                    "png": base64.b64encode(png(width, height, rgba)).decode(),
                }
            ).encode()
            self.broadcast(KIND_CURSOR, self.last_cursor)

    def input(self, message: dict[str, object]) -> None:
        kind = message.get("type")
        if kind == "mouse":
            self.mouse(message)
        elif kind == "key":
            self.keyboard(message)

    def mouse(self, message: dict[str, object]) -> None:
        action = message.get("action")
        x, y = message.get("x"), message.get("y")
        if isinstance(x, int | float) and isinstance(y, int | float):
            self.x.move(round(x * self.scale), round(y * self.scale))
        if action == "wheel":
            self.scroll(message.get("dx"), message.get("dy"))
            return
        button = (
            _BUTTONS.get(message.get("button")) if isinstance(message.get("button"), int) else 1
        )
        if action in ("down", "up") and button is not None:
            self.x.button(button, action == "down")

    def scroll(self, dx: object, dy: object) -> None:
        for axis, delta, negative, positive in ((1, dy, 4, 5), (0, dx, 6, 7)):
            if not isinstance(delta, int | float):
                continue
            self.wheel[axis] += delta
            tick = WHEEL_TICK_CSS_PIXELS * self.scale
            while abs(self.wheel[axis]) >= tick / 2:
                step = tick if self.wheel[axis] > 0 else -tick
                self.wheel[axis] -= step
                button = positive if step > 0 else negative
                self.x.button(button, True)
                self.x.button(button, False)

    def keyboard(self, message: dict[str, object]) -> None:
        key, code, action = message.get("key"), message.get("code"), message.get("action")
        if not isinstance(key, str) or not isinstance(code, str) or action not in ("down", "up"):
            return
        keysym = keysym_for(key, code)
        if keysym is not None:
            self.x.key(code or key, keysym, action == "down")

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = Client(writer)
        self.clients.add(client)
        if self.stop_task is not None:
            self.stop_task.cancel()
            self.stop_task = None
        client.send(KIND_STATUS, self.status())
        if self.last_cursor is not None:
            client.send(KIND_CURSOR, self.last_cursor)
        await self.start_encoder()
        try:
            while True:
                header = await reader.readexactly(5)
                length = struct.unpack(">I", header[1:])[0]
                if length > MAX_PAYLOAD:
                    break
                payload = await reader.readexactly(length)
                try:
                    message = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if header[:1] == KIND_INPUT:
                    self.input(message)
                elif header[:1] == KIND_RESIZE:
                    width, height = message.get("width"), message.get("height")
                    if isinstance(width, int) and isinstance(height, int):
                        await self.resize(width, height)
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            self.clients.discard(client)
            if not self.clients:
                self.x.release_all()
                self.stop_task = asyncio.create_task(self.stop_when_idle())
            writer.close()

    async def stop_when_idle(self) -> None:
        await asyncio.sleep(ENCODER_GRACE_SECONDS)
        if not self.clients:
            await self.stop_encoder()

    async def serve(self) -> None:
        await self.attach_window()
        server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        asyncio.create_task(self.poll_cursor())
        print(f"ready {port}", flush=True)
        async with server:
            await server.serve_forever()


def main() -> None:
    display, scale, width, height = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4]
    asyncio.run(Helper(display, scale, int(width), int(height)).serve())


if __name__ == "__main__":
    main()
