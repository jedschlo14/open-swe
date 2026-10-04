"""Mirrors one browser page for the dashboard: records it with rrweb and carries input back.

Runs in the browser's own network namespace, because the browser's DevTools port
is only on that namespace's loopback. It listens on a Unix socket, which the
sandbox's main namespace can reach (``mirror_forward.py`` exposes it on sandbox
loopback). Standard library only: it runs in whatever image the sandbox booted.

Wire protocol on the socket: ``kind`` byte, 4-byte big-endian length, payload.

To a viewer: ``E`` a JSON array of rrweb events, ``J`` a JSON control message
(page state, viewport, a reset notice), ``B`` an asset response.
From a viewer: ``H`` hello (``role``), ``I`` input, ``C`` control, ``Q`` asset request.

Usage: mirror_bridge.py <socket_path> <record_js> <page_js>
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import struct
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

KIND_EVENTS = b"E"
KIND_JSON = b"J"
KIND_ASSET_RESPONSE = b"B"
KIND_HELLO = b"H"
KIND_INPUT = b"I"
KIND_CONTROL = b"C"
KIND_ASSET_REQUEST = b"Q"

MAX_VIEWER_MESSAGE = 1024 * 1024
MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_FONT_BYTES = 32 * 1024 * 1024
FONT_SCAN_SECONDS = 20
FONT_FALLBACK_LIMIT = 24
FONT_SCHEME = "font:"
ASSET_TIMEOUT_SECONDS = 30
FLUSH_SECONDS = 0.008
SLOW_VIEWER_BYTES = 16 * 1024 * 1024
TAIL_LIMIT_BYTES = 4 * 1024 * 1024
TAIL_LIMIT_EVENTS = 4000
CHECKOUT_REST_SECONDS = 3.0
BINDING = "__openSweEmit"
DOUBLE_CLICK_SECONDS = 0.5
DOUBLE_CLICK_PIXELS = 5

EVENT_FULL_SNAPSHOT = "2"
EVENT_META = "4"
EVENT_CUSTOM = "5"

_MODIFIER_BITS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}
_NAMED_KEY_CODES = {
    "Backspace": 8,
    "Tab": 9,
    "Enter": 13,
    "Escape": 27,
    "Space": 32,
    "PageUp": 33,
    "PageDown": 34,
    "End": 35,
    "Home": 36,
    "ArrowLeft": 37,
    "ArrowUp": 38,
    "ArrowRight": 39,
    "ArrowDown": 40,
    "Insert": 45,
    "Delete": 46,
    "Semicolon": 186,
    "Equal": 187,
    "Comma": 188,
    "Minus": 189,
    "Period": 190,
    "Slash": 191,
    "Backquote": 192,
    "BracketLeft": 219,
    "Backslash": 220,
    "BracketRight": 221,
    "Quote": 222,
    "ContextMenu": 93,
    "CapsLock": 20,
    "ShiftLeft": 16,
    "ShiftRight": 16,
    "ControlLeft": 17,
    "ControlRight": 17,
    "AltLeft": 18,
    "AltRight": 18,
    "MetaLeft": 91,
    "MetaRight": 92,
}
_MOUSE_BUTTONS = ("left", "middle", "right")
_MOUSE_MASKS = (1, 4, 2)


def key_code(code: str) -> int:
    """The Windows virtual key code Chromium expects for a ``KeyboardEvent.code``."""
    if code in _NAMED_KEY_CODES:
        return _NAMED_KEY_CODES[code]
    if code.startswith("Key") and len(code) == 4:
        return ord(code[3])
    if code.startswith("Digit") and len(code) == 6:
        return ord(code[5])
    if code.startswith("Numpad") and code[6:].isdigit():
        return 96 + int(code[6:])
    if code.startswith("F") and code[1:].isdigit() and 1 <= int(code[1:]) <= 24:
        return 111 + int(code[1:])
    return 0


_FONT_WEIGHTS = ((0, 100), (40, 200), (50, 300), (55, 350), (80, 400), (100, 500))
_FONT_WEIGHTS += ((180, 600), (200, 700), (205, 800), (210, 900))
_FONT_WIDTHS = (
    (50, "50%"),
    (63, "62.5%"),
    (75, "75%"),
    (87, "87.5%"),
    (100, "100%"),
    (113, "112.5%"),
    (125, "125%"),
    (150, "150%"),
    (200, "200%"),
)
_GENERIC_FAMILIES = ("serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui")
_METRIC_ALIASES = {
    "arial": "Liberation Sans",
    "helvetica": "Liberation Sans",
    "helvetica neue": "Liberation Sans",
    "times new roman": "Liberation Serif",
    "times": "Liberation Serif",
    "courier new": "Liberation Mono",
    "courier": "Liberation Mono",
}
_FONT_TYPES = {
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".ttc": "font/collection",
    ".otc": "font/collection",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def nearest(value: int, table: tuple[tuple[int, str | int], ...]) -> str | int:
    return min(table, key=lambda entry: abs(entry[0] - value))[1]


async def fontconfig(*args: str) -> str:
    """Output of a fontconfig command, empty when fontconfig is not installed or fails."""
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(process.communicate(), timeout=FONT_SCAN_SECONDS)
    except OSError, TimeoutError:
        print("fontconfig did not answer", file=sys.stderr, flush=True)
        return ""
    return out.decode("utf-8", "replace") if process.returncode == 0 else ""


class FontCatalog:
    """The fonts this browser has, so a viewer can draw the page with the same ones."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.message: dict[str, object] | None = None

    async def load(self) -> dict[str, object]:
        if self.message is None:
            self.message = await self.scan()
        return self.message

    async def scan(self) -> dict[str, object]:
        listing = await fontconfig(
            "fc-list", "--format", "%{file}|%{index}|%{family[0]}|%{weight}|%{slant}|%{width}\\n"
        )
        families: dict[str, list[dict[str, object]]] = {}
        seen: set[tuple[str, str, int, int, int]] = set()
        for line in listing.splitlines():
            parts = line.split("|")
            if len(parts) != 6 or not parts[2]:
                continue
            path, index, family, weight, slant, width = parts
            suffix = Path(path).suffix.lower()
            if suffix not in _FONT_TYPES or (suffix in (".ttc", ".otc") and index != "0"):
                continue
            try:
                numbers = (int(weight), int(slant), int(width))
            except ValueError:
                continue
            key = (path, family, *numbers)
            if key in seen:
                continue
            seen.add(key)
            identifier = hashlib.sha1(path.encode()).hexdigest()[:16]
            self.files[identifier] = path
            families.setdefault(family, []).append(
                {
                    "id": identifier,
                    "weight": str(nearest(numbers[0], _FONT_WEIGHTS)),
                    "style": "italic" if numbers[1] >= 100 else "normal",
                    "stretch": str(nearest(numbers[2], _FONT_WIDTHS)),
                    "ranges": None,
                }
            )
        names = {family.lower(): family for family in families}
        for alias, target in _METRIC_ALIASES.items():
            if target in families:
                names.setdefault(alias, target)
        generics: dict[str, str] = {}
        for generic in _GENERIC_FAMILIES:
            matched = (await fontconfig("fc-match", "-f", "%{family[0]}", generic)).strip()
            if matched in families:
                generics[generic] = matched
        order = await fontconfig("fc-match", "-s", "-f", "%{family[0]}\\n", "sans-serif")
        fallbacks: list[str] = []
        for family in order.splitlines():
            if family in families and family not in fallbacks:
                fallbacks.append(family)
        return {
            "type": "fonts",
            "families": families,
            "names": names,
            "generics": generics,
            "fallbacks": fallbacks[:FONT_FALLBACK_LIMIT],
        }

    def read(self, identifier: str) -> tuple[int, dict[str, str], bytes]:
        path = self.files.get(identifier)
        if path is None:
            return 404, {}, b""
        try:
            with open(path, "rb") as handle:
                body = handle.read(MAX_FONT_BYTES + 1)
        except OSError:
            print("a font file could not be read", file=sys.stderr, flush=True)
            return 404, {}, b""
        if len(body) > MAX_FONT_BYTES:
            return 413, {}, b""
        content_type = _FONT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")
        return 200, {"Content-Type": content_type}, body


class CdpError(RuntimeError):
    """A DevTools command failed or the connection to the browser ended."""


class Cdp:
    """A minimal DevTools-protocol client over a WebSocket, with flattened sessions."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, object]]] = {}
        self.on_event: Callable[[str, dict[str, object], str | None], None] = lambda *_: None
        self.closed = asyncio.Event()
        self._task = asyncio.create_task(self._read_loop())

    @classmethod
    async def connect(cls, url: str) -> Cdp:
        parts = urlsplit(url)
        reader, writer = await asyncio.open_connection(
            parts.hostname, parts.port, limit=1024 * 1024
        )
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        writer.write(
            (
                f"GET {path} HTTP/1.1\r\nHost: {parts.hostname}:{parts.port}\r\n"
                f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        head = await reader.readuntil(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            writer.close()
            raise CdpError(f"the browser refused the DevTools connection: {head[:80]!r}")
        return cls(reader, writer)

    async def _frame(self) -> tuple[int, bytes, bool]:
        header = await self._reader.readexactly(2)
        opcode, masked_length = header[0] & 0x0F, header[1] & 0x7F
        fin = bool(header[0] & 0x80)
        if masked_length == 126:
            (length,) = struct.unpack(">H", await self._reader.readexactly(2))
        elif masked_length == 127:
            (length,) = struct.unpack(">Q", await self._reader.readexactly(8))
        else:
            length = masked_length
        return opcode, await self._reader.readexactly(length), fin

    async def _read_loop(self) -> None:
        message = bytearray()
        try:
            while True:
                opcode, payload, fin = await self._frame()
                if opcode == 8:
                    break
                if opcode == 9:
                    self._write_frame(10, payload)
                    continue
                if opcode in (1, 2, 0):
                    message += payload
                    if fin:
                        self._dispatch(bytes(message))
                        message.clear()
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            self.closed.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CdpError("the browser's DevTools connection closed"))
            self._pending.clear()
            self._writer.close()

    def _dispatch(self, raw: bytes) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            print("unreadable DevTools message", file=sys.stderr, flush=True)
            return
        if not isinstance(message, dict):
            return
        identifier = message.get("id")
        if isinstance(identifier, int):
            future = self._pending.pop(identifier, None)
            if future is None or future.done():
                return
            error = message.get("error")
            if isinstance(error, dict):
                future.set_exception(CdpError(str(error.get("message", error))))
            else:
                result = message.get("result")
                future.set_result(result if isinstance(result, dict) else {})
            return
        method, params = message.get("method"), message.get("params")
        if isinstance(method, str):
            session = message.get("sessionId")
            self.on_event(
                method,
                params if isinstance(params, dict) else {},
                session if isinstance(session, str) else None,
            )

    def _write_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = struct.pack(">BB", 0x80 | opcode, 0x80 | length)
        elif length < 65536:
            header = struct.pack(">BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack(">BBQ", 0x80 | opcode, 0x80 | 127, length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._writer.write(header + mask + masked)

    async def send(
        self, method: str, params: dict[str, object] | None = None, session: str | None = None
    ) -> dict[str, object]:
        if self.closed.is_set():
            raise CdpError("the browser's DevTools connection closed")
        self._next_id += 1
        identifier = self._next_id
        message: dict[str, object] = {"id": identifier, "method": method}
        if params:
            message["params"] = params
        if session is not None:
            message["sessionId"] = session
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        self._write_frame(1, json.dumps(message).encode())
        await self._writer.drain()
        return await future

    def close(self) -> None:
        self._writer.close()
        self._task.cancel()


_INTERNAL_SCHEMES = (
    "chrome:",
    "chrome-untrusted:",
    "chrome-extension:",
    "devtools:",
    "chrome-search:",
)


def is_mirrorable(url: str) -> bool:
    return bool(url) and not url.startswith(_INTERNAL_SCHEMES)


def frame(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack(">I", len(payload)) + payload


class Viewer:
    """One connected dashboard viewer and the events waiting to reach it."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.pending: list[bytes] = []
        self.resync = False
        self.watching = False

    def write(self, kind: bytes, payload: bytes) -> None:
        self.writer.write(frame(kind, payload))

    def backlog(self) -> int:
        return self.writer.transport.get_write_buffer_size()


class Mirror:
    def __init__(self, record_js: str, page_js: str) -> None:
        recorder = "\n".join(
            line for line in record_js.splitlines() if not line.startswith("//# sourceMappingURL")
        )
        self.source = (
            "(function(){const define=undefined,exports=undefined,module=undefined;"
            f"{recorder}\n}}).call(window);\n{page_js}"
        )
        self.cdp: Cdp | None = None
        self.page_session: str | None = None
        self.page_target: str | None = None
        self.frame_id: str | None = None
        self.sessions: dict[str, str] = {}
        self.page_sessions: dict[str, str] = {}
        self.urls: dict[str, str] = {}
        self.titles: dict[str, str] = {}
        self.viewers: set[Viewer] = set()
        self.base: list[bytes] = []
        self.tail: list[bytes] = []
        self.tail_bytes = 0
        self.meta: bytes | None = None
        self.custom: dict[str, bytes] = {}
        self.page: dict[str, object] = {"url": "", "title": "", "loading": False}
        self.checkout_at = 0.0
        self.fonts = FontCatalog()
        self.pressed = 0
        self.modifiers = 0
        self.last_press: tuple[float, float, float, int] = (0.0, 0.0, 0.0, 0)
        self.click_count = 0
        self.flusher: asyncio.Task[None] | None = None

    async def command(
        self, method: str, params: dict[str, object] | None = None, *, session: str | None = None
    ) -> dict[str, object]:
        if self.cdp is None:
            raise CdpError("not connected to the browser")
        return await self.cdp.send(method, params, session or self.page_session)

    async def attach(self, cdp: Cdp) -> None:
        """Watch one DevTools connection until it ends."""
        self.cdp = cdp
        cdp.on_event = self.handle_event
        self.sessions.clear()
        self.page_sessions.clear()
        self.page_session = self.page_target = self.frame_id = None
        await cdp.send("Target.setDiscoverTargets", {"discover": True})
        await cdp.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )
        await cdp.closed.wait()
        self.cdp = None
        self.page_session = None

    async def setup_session(self, session: str, target_id: str, *, is_page: bool) -> None:
        if self.cdp is None:
            return
        if is_page:
            self.page_sessions[session] = target_id
            self.choose_page()
        try:
            await self.cdp.send("Runtime.enable", None, session)
            await self.cdp.send("Runtime.addBinding", {"name": BINDING}, session)
            await self.cdp.send("Page.enable", None, session)
            await self.cdp.send(
                "Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
                session,
            )
            await self.cdp.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": self.source, "runImmediately": True},
                session,
            )
        except CdpError:
            print("could not set up a page for mirroring", file=sys.stderr, flush=True)
        finally:
            with contextlib.suppress(CdpError):
                await self.cdp.send("Runtime.runIfWaitingForDebugger", None, session)

    def choose_page(self) -> None:
        """Mirror the first ordinary page; browser-internal pages are never shown."""
        for session, target_id in self.page_sessions.items():
            if is_mirrorable(self.urls.get(target_id, "")):
                if self.page_session != session:
                    self.show_page(session, target_id)
                return
        if self.page_session is not None:
            self.page_session = self.page_target = self.frame_id = None
            self.reset_log()
            self.broadcast_json({"type": "reset"})

    def reset_log(self) -> None:
        self.base, self.tail, self.tail_bytes, self.meta = [], [], 0, None
        self.custom.clear()

    def show_page(self, session: str, target_id: str) -> None:
        self.page_session, self.page_target, self.frame_id = session, target_id, None
        self.reset_log()
        self.broadcast_json({"type": "reset"})
        self.update_page(url=self.urls.get(target_id, ""), title=self.titles.get(target_id, ""))
        asyncio.create_task(self.find_frame(session))

    async def find_frame(self, session: str) -> None:
        with contextlib.suppress(CdpError):
            tree = await self.command("Page.getFrameTree", session=session)
            frame_tree = tree.get("frameTree")
            frame_info = frame_tree.get("frame") if isinstance(frame_tree, dict) else None
            if isinstance(frame_info, dict) and isinstance(frame_info.get("id"), str):
                self.frame_id = frame_info["id"]

    def handle_event(self, method: str, params: dict[str, object], session: str | None) -> None:
        if method == "Target.attachedToTarget":
            child, info = params.get("sessionId"), params.get("targetInfo")
            if isinstance(child, str) and isinstance(info, dict) and child not in self.sessions:
                kind, target_id = info.get("type"), info.get("targetId")
                if isinstance(target_id, str):
                    self.sessions[child] = target_id
                    self.urls[target_id] = str(info.get("url", ""))
                    self.titles[target_id] = str(info.get("title", ""))
                    if kind in ("page", "iframe"):
                        asyncio.create_task(
                            self.setup_session(child, target_id, is_page=kind == "page")
                        )
                    else:
                        asyncio.create_task(self.resume(child))
        elif method == "Target.targetInfoChanged":
            info = params.get("targetInfo")
            target_id = info.get("targetId") if isinstance(info, dict) else None
            if isinstance(info, dict) and isinstance(target_id, str):
                self.urls[target_id] = str(info.get("url", ""))
                self.titles[target_id] = str(info.get("title", ""))
                if target_id in self.page_sessions.values():
                    self.choose_page()
                if target_id == self.page_target:
                    self.update_page(url=info.get("url"), title=info.get("title"))
        elif method == "Target.detachedFromTarget":
            gone = params.get("sessionId")
            if isinstance(gone, str):
                self.sessions.pop(gone, None)
                self.page_sessions.pop(gone, None)
                if gone == self.page_session:
                    self.page_session = self.page_target = self.frame_id = None
                    self.choose_page()
        elif method == "Runtime.bindingCalled" and session is not None:
            if params.get("name") == BINDING and session == self.page_session:
                payload = params.get("payload")
                if isinstance(payload, str):
                    self.ingest(payload)
        elif method in ("Page.frameStartedLoading", "Page.frameStoppedLoading"):
            if session == self.page_session:
                self.update_page(loading=method == "Page.frameStartedLoading")

    async def resume(self, session: str) -> None:
        if self.cdp is None:
            return
        with contextlib.suppress(CdpError):
            await self.cdp.send("Runtime.runIfWaitingForDebugger", None, session)

    def update_page(
        self, *, url: object = None, title: object = None, loading: object = None
    ) -> None:
        changed = False
        for key, value in (("url", url), ("title", title), ("loading", loading)):
            if value is not None and self.page.get(key) != value:
                self.page[key] = value
                changed = True
        if changed:
            self.broadcast_json({"type": "page", **self.page})

    def ingest(self, payload: str) -> None:
        checkout, kind, body = payload[0] == "c", payload[1], payload[2:].encode()
        if kind == EVENT_META:
            self.meta = body
            return
        if kind == EVENT_FULL_SNAPSHOT:
            if self.meta is None:
                return
            self.base = [self.meta, body]
            self.tail, self.tail_bytes = [], 0
            if checkout:
                return
            self.deliver([self.meta, body])
            return
        if kind == EVENT_CUSTOM:
            try:
                data = json.loads(body).get("data", {})
                tag = data.get("tag") if isinstance(data, dict) else None
            except ValueError:
                tag = None
            if isinstance(tag, str):
                self.custom[tag] = body
        self.tail.append(body)
        self.tail_bytes += len(body)
        self.deliver([body])
        if self.tail_bytes > TAIL_LIMIT_BYTES or len(self.tail) > TAIL_LIMIT_EVENTS:
            asyncio.create_task(self.checkout())

    async def checkout(self) -> None:
        """Ask the page for a fresh snapshot so a late viewer replays little."""
        now = time.monotonic()
        if now - self.checkout_at < CHECKOUT_REST_SECONDS:
            return
        self.checkout_at = now
        with contextlib.suppress(CdpError):
            await self.command(
                "Runtime.evaluate", {"expression": "window.__openSweMirror?.snapshot()"}
            )

    def deliver(self, events: list[bytes]) -> None:
        for viewer in self.viewers:
            if viewer.watching and not viewer.resync:
                viewer.pending.extend(events)

    def broadcast_json(self, message: dict[str, object]) -> None:
        payload = json.dumps(message).encode()
        for viewer in self.viewers:
            if viewer.watching:
                viewer.write(KIND_JSON, payload)

    def send_state(self, viewer: Viewer) -> None:
        viewer.write(KIND_JSON, json.dumps({"type": "page", **self.page}).encode())
        events = [*self.base, *self.custom.values(), *self.tail] if self.base else []
        if events:
            viewer.write(KIND_EVENTS, b"[" + b",".join(events) + b"]")

    async def flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            for viewer in list(self.viewers):
                if not viewer.watching:
                    continue
                if viewer.resync:
                    if viewer.backlog() > 0:
                        continue
                    viewer.resync = False
                    viewer.pending.clear()
                    viewer.write(KIND_JSON, json.dumps({"type": "reset"}).encode())
                    self.send_state(viewer)
                    continue
                if not viewer.pending:
                    continue
                if viewer.backlog() > SLOW_VIEWER_BYTES:
                    viewer.pending.clear()
                    viewer.resync = True
                    continue
                batch, viewer.pending = viewer.pending, []
                viewer.write(KIND_EVENTS, b"[" + b",".join(batch) + b"]")

    async def input(self, message: dict[str, object]) -> None:
        kind = message.get("type")
        if kind == "mouse":
            await self.mouse(message)
        elif kind == "key":
            await self.key(message)
        elif kind == "choice":
            await self.choice(message)

    async def locate(self, message: dict[str, object]) -> tuple[float, float] | None:
        anchor = message.get("anchor")
        if not isinstance(anchor, dict):
            return None
        node, fx, fy = anchor.get("id"), anchor.get("fx"), anchor.get("fy")
        if not isinstance(node, int) or not isinstance(fx, int | float):
            return None
        if not isinstance(fy, int | float):
            return None
        result = await self.command(
            "Runtime.evaluate",
            {
                "expression": (
                    f"window.__openSweMirror?.locate({node}, {float(fx)}, {float(fy)}) ?? null"
                ),
                "returnByValue": True,
            },
        )
        value = result.get("result")
        found = value.get("value") if isinstance(value, dict) else None
        if (
            isinstance(found, list)
            and len(found) == 2
            and all(isinstance(part, int | float) for part in found)
        ):
            return float(found[0]), float(found[1])
        return None

    async def mouse(self, message: dict[str, object]) -> None:
        action = message.get("action")
        x, y = message.get("x"), message.get("y")
        if not isinstance(x, int | float) or not isinstance(y, int | float):
            return
        point = (float(x), float(y))
        if action in ("down", "up"):
            point = await self.locate(message) or point
        raw_button = message.get("button")
        button = raw_button if isinstance(raw_button, int) and 0 <= raw_button <= 2 else 0
        params: dict[str, object] = {"x": point[0], "y": point[1], "modifiers": self.modifiers}
        if action == "move":
            params |= {"type": "mouseMoved", "button": "none", "buttons": self.pressed}
        elif action == "down":
            now = time.monotonic()
            last_at, last_x, last_y, last_button = self.last_press
            near = abs(point[0] - last_x) <= DOUBLE_CLICK_PIXELS
            near = near and abs(point[1] - last_y) <= DOUBLE_CLICK_PIXELS
            repeat = now - last_at <= DOUBLE_CLICK_SECONDS and near and last_button == button
            self.click_count = self.click_count + 1 if repeat else 1
            self.last_press = (now, point[0], point[1], button)
            self.pressed |= _MOUSE_MASKS[button]
            params |= {
                "type": "mousePressed",
                "button": _MOUSE_BUTTONS[button],
                "buttons": self.pressed,
                "clickCount": self.click_count,
            }
        elif action == "up":
            self.pressed &= ~_MOUSE_MASKS[button]
            params |= {
                "type": "mouseReleased",
                "button": _MOUSE_BUTTONS[button],
                "buttons": self.pressed,
                "clickCount": self.click_count,
            }
        elif action == "wheel":
            dx, dy = message.get("dx"), message.get("dy")
            params |= {
                "type": "mouseWheel",
                "deltaX": float(dx) if isinstance(dx, int | float) else 0.0,
                "deltaY": float(dy) if isinstance(dy, int | float) else 0.0,
            }
        else:
            return
        await self.command("Input.dispatchMouseEvent", params)

    async def key(self, message: dict[str, object]) -> None:
        key, code, action = message.get("key"), message.get("code"), message.get("action")
        if not isinstance(key, str) or not isinstance(code, str) or action not in ("down", "up"):
            return
        bit = _MODIFIER_BITS.get(key, 0)
        if bit:
            self.modifiers = self.modifiers | bit if action == "down" else self.modifiers & ~bit
        virtual = key_code(code)
        params: dict[str, object] = {
            "key": key,
            "code": code,
            "modifiers": self.modifiers,
            "windowsVirtualKeyCode": virtual,
            "nativeVirtualKeyCode": virtual,
        }
        printable = len(key) == 1 and not self.modifiers & (_MODIFIER_BITS["Control"] | 4 | 1)
        if action == "up":
            params["type"] = "keyUp"
        elif printable:
            params |= {"type": "keyDown", "text": key, "unmodifiedText": key}
        else:
            params["type"] = "rawKeyDown"
        await self.command("Input.dispatchKeyEvent", params)

    async def choice(self, message: dict[str, object]) -> None:
        node, value = message.get("id"), message.get("value")
        if isinstance(node, int) and isinstance(value, str):
            await self.command(
                "Runtime.evaluate",
                {"expression": (f"window.__openSweMirror?.setChoice({node}, {json.dumps(value)})")},
            )

    async def control(self, viewer: Viewer, message: dict[str, object]) -> None:
        if message.get("type") == "fonts":
            viewer.write(KIND_JSON, json.dumps(await self.fonts.load()).encode())

    async def asset(self, request: int, url: str) -> bytes:
        """One response frame for ``url``, fetched the way the page itself would."""
        status, headers, body = 502, {}, b""
        try:
            if url.startswith(FONT_SCHEME):
                status, headers, body = self.fonts.read(url.removeprefix(FONT_SCHEME))
            else:
                status, headers, body = await asyncio.wait_for(
                    self.fetch(url), timeout=ASSET_TIMEOUT_SECONDS
                )
        except CdpError, TimeoutError:
            print("asset fetch failed", file=sys.stderr, flush=True)
        head = json.dumps(headers).encode()
        return struct.pack(">IHI", request, status, len(head)) + head + body

    async def fetch(self, url: str) -> tuple[int, dict[str, str], bytes]:
        if self.frame_id is None or urlsplit(url).scheme not in ("http", "https"):
            return 400, {}, b""
        loaded = await self.command(
            "Network.loadNetworkResource",
            {
                "frameId": self.frame_id,
                "url": url,
                "options": {"disableCache": False, "includeCredentials": True},
            },
        )
        resource = loaded.get("resource")
        if not isinstance(resource, dict) or resource.get("success") is not True:
            return 502, {}, b""
        status = resource.get("httpStatusCode")
        raw_headers = resource.get("headers")
        headers = (
            {str(name): str(value) for name, value in raw_headers.items()}
            if isinstance(raw_headers, dict)
            else {}
        )
        handle = resource.get("stream")
        body = bytearray()
        if isinstance(handle, str):
            try:
                while True:
                    chunk = await self.command("IO.read", {"handle": handle, "size": 1 << 20})
                    data = chunk.get("data")
                    if isinstance(data, str):
                        body += (
                            base64.b64decode(data) if chunk.get("base64Encoded") else data.encode()
                        )
                    if len(body) > MAX_ASSET_BYTES:
                        return 413, {}, b""
                    if chunk.get("eof") is True:
                        break
            finally:
                with contextlib.suppress(CdpError):
                    await self.command("IO.close", {"handle": handle})
        return (status if isinstance(status, int) else 200), headers, bytes(body)

    async def serve_viewer(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        viewer = Viewer(writer)
        self.viewers.add(viewer)
        tasks: set[asyncio.Task[None]] = set()
        try:
            while True:
                header = await reader.readexactly(5)
                length = struct.unpack(">I", header[1:])[0]
                if length > MAX_VIEWER_MESSAGE:
                    break
                payload = await reader.readexactly(length)
                kind = header[:1]
                if kind == KIND_ASSET_REQUEST:
                    request = struct.unpack(">I", payload[:4])[0]
                    url = payload[4:].decode("utf-8", "replace")
                    task = asyncio.create_task(self.answer_asset(viewer, request, url))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                    continue
                try:
                    message = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if kind == KIND_HELLO and message.get("role") == "view":
                    viewer.watching = True
                    self.send_state(viewer)
                elif kind == KIND_INPUT:
                    try:
                        await self.input(message)
                    except CdpError:
                        print("input failed", file=sys.stderr, flush=True)
                elif kind == KIND_CONTROL:
                    await self.control(viewer, message)
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            self.viewers.discard(viewer)
            for task in tasks:
                task.cancel()
            if not any(other.watching for other in self.viewers):
                self.pressed = 0
            writer.close()

    async def answer_asset(self, viewer: Viewer, request: int, url: str) -> None:
        viewer.write(KIND_ASSET_RESPONSE, await self.asset(request, url))

    async def connect_forever(self, resolve: Callable[[], Awaitable[str]]) -> None:
        while True:
            try:
                cdp = await Cdp.connect(await resolve())
                await self.attach(cdp)
            except OSError, CdpError, asyncio.IncompleteReadError:
                print("lost the browser; retrying", file=sys.stderr, flush=True)
            await asyncio.sleep(1)


async def resolve_cdp_url() -> str:
    """The browser's DevTools address, from the ``agent-browser`` daemon that launched it."""
    process = await asyncio.create_subprocess_exec(
        "agent-browser",
        "--json",
        "get",
        "cdp-url",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        stdin=asyncio.subprocess.DEVNULL,
    )
    out, _ = await process.communicate()
    for line in reversed(out.decode().strip().splitlines()):
        with contextlib.suppress(ValueError):
            data = json.loads(line).get("data")
            url = data.get("cdpUrl") if isinstance(data, dict) else None
            if isinstance(url, str):
                return url
    raise CdpError("agent-browser did not report a DevTools address")


async def serve(socket_path: str, record_js: str, page_js: str) -> None:
    mirror = Mirror(record_js, page_js)
    Path(socket_path).unlink(missing_ok=True)
    server = await asyncio.start_unix_server(mirror.serve_viewer, path=socket_path, limit=1 << 24)
    os.chmod(socket_path, 0o600)
    mirror.flusher = asyncio.create_task(mirror.flush_loop())
    connecting = asyncio.create_task(mirror.connect_forever(resolve_cdp_url))
    print("ready", flush=True)
    async with server:
        await connecting


def main() -> None:
    socket_path, record_path, page_path = sys.argv[1:4]
    asyncio.run(serve(socket_path, Path(record_path).read_text(), Path(page_path).read_text()))


if __name__ == "__main__":
    main()
