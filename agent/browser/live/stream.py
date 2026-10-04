"""Relays a thread browser's live view to one dashboard viewer.

The sandbox's display helper encodes the browser's private display as H.264. This
relay reads it through a sandbox tunnel, cuts it into frames, and sends each as one
binary message over the viewer's socket: a flag byte (1 for a frame a decoder can
start from) and the frame. Text messages carry everything else: the display's size,
the codec, page state, and the controller's input back. The viewer never talks to the
sandbox.

Access is checked on connect and rechecked while the view is open. Input, resizing,
and toolbar actions pass only for a viewer who holds the lease, checked again for
every press, release, wheel tick, and key. A viewer that cannot keep up skips frames
to the next keyframe rather than falling behind.
"""

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping

from fastapi import WebSocket, WebSocketDisconnect
from langsmith.sandbox import AsyncSandbox
from pydantic import JsonValue

from agent.browser import display, engine, manager
from agent.browser.live import messages
from agent.browser.live.actions import PersonActions
from agent.browser.live.display_client import DisplayClient, Screen, Video
from agent.browser.live.gate import Authorizer, ControlGate, ViewportSync
from agent.browser.live.h264 import AccessUnit, AccessUnitSplitter
from agent.browser.live.keysyms import keysym_for
from agent.browser.models import BrowserSession
from agent.dashboard.oauth import BrowserTicket

logger = logging.getLogger(__name__)

SUBPROTOCOL = "open-swe-browser"
_SLOTS = asyncio.Semaphore(40)
_RECHECK_SECONDS = 5.0
_PAGE_POLL_SECONDS = 1.0
_MAX_BACKLOG_FRAMES = 45
_MOUSE_BUTTONS = {0: 1, 1: 2, 2: 3}
# One X wheel click scrolls this many CSS pixels in Chromium at the 1.5 render scale.
_WHEEL_TICK_PIXELS = 120.0
_MAX_WHEEL_TICKS = 10
_FIRST_UNMAPPED_CODE_POINT = 0x80

type HelperMessage = dict[str, JsonValue]


class WheelAccumulator:
    """Turns pixel wheel deltas into whole X wheel clicks, carrying what is left over.

    A trackpad sends many tiny deltas and a mouse a few large ones; both add up to the
    same distance instead of rounding each event to a full click or to nothing.
    """

    def __init__(self) -> None:
        self._x = 0.0
        self._y = 0.0

    def ticks(self, dx: float, dy: float) -> tuple[int, int]:
        """Clicks for the display: positive is right and up, the opposite of a browser's ``deltaY``."""
        self._x += dx
        self._y += dy
        right = _take(self._x)
        down = _take(self._y)
        self._x -= right * _WHEEL_TICK_PIXELS
        self._y -= down * _WHEEL_TICK_PIXELS
        return right, -down


def _take(pixels: float) -> int:
    ticks = round(pixels / _WHEEL_TICK_PIXELS)
    return min(max(ticks, -_MAX_WHEEL_TICKS), _MAX_WHEEL_TICKS)


def to_helper(
    message: messages.MouseMessage | messages.KeyMessage,
    screen: tuple[int, int],
    wheel: WheelAccumulator,
) -> list[HelperMessage]:
    """The helper messages one viewer message becomes; CSS pixels become display pixels."""
    match message:
        case messages.KeyMessage():
            keysym = keysym_for(message.key, message.code)
            if keysym is None:
                return []
            return [{"type": "key", "keysym": keysym, "down": message.action == "down"}]
        case messages.MouseMessage():
            x = min(round(message.x * engine.RENDER_SCALE), screen[0] - 1)
            y = min(round(message.y * engine.RENDER_SCALE), screen[1] - 1)
            at: HelperMessage = {"x": max(x, 0), "y": max(y, 0)}
            match message.action:
                case "move":
                    return [{"type": "move", **at}]
                case "down" | "up":
                    code = _MOUSE_BUTTONS[message.button]
                    return [
                        {"type": "button", **at, "code": code, "down": message.action == "down"}
                    ]
                case "wheel":
                    right, up = wheel.ticks(message.dx, message.dy)
                    if right == 0 and up == 0:
                        return []
                    return [{"type": "scroll", **at, "right": right, "up": up}]
    return []


def _is_unmapped_character(message: messages.KeyMessage) -> bool:
    """A typed character outside ASCII, which the display has no keycode for; it is inserted as text."""
    return len(message.key) == 1 and ord(message.key) >= _FIRST_UNMAPPED_CODE_POINT


class VideoBacklog:
    """Frames waiting for a slow viewer; past a limit it drops them and resumes at a keyframe."""

    def __init__(self, limit: int = _MAX_BACKLOG_FRAMES) -> None:
        self._limit = limit
        self._frames: deque[AccessUnit] = deque()
        self._needs_keyframe = True
        self._ready = asyncio.Event()

    def restart(self) -> None:
        """Forget everything queued: a new encode began and the old frames no longer fit."""
        self._frames.clear()
        self._needs_keyframe = True

    def push(self, frame: AccessUnit) -> None:
        if self._needs_keyframe and not frame.keyframe:
            return
        if len(self._frames) >= self._limit:
            self._frames.clear()
            self._needs_keyframe = True
            if not frame.keyframe:
                return
        self._needs_keyframe = False
        self._frames.append(frame)
        self._ready.set()

    async def pop(self) -> AccessUnit:
        while not self._frames:
            self._ready.clear()
            await self._ready.wait()
        return self._frames.popleft()


class _Outbox:
    """The viewer's socket as a single writer: text messages queue, video waits its turn."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._lock = asyncio.Lock()

    def message(self, payload: Mapping[str, JsonValue]) -> None:
        self._queue.put_nowait(json.dumps(payload))

    async def send_now(self, text: str | None, frame: bytes) -> None:
        """Send ``text`` (when given) and then ``frame`` with nothing in between."""
        async with self._lock:
            if text is not None:
                await self._websocket.send_text(text)
            await self._websocket.send_bytes(frame)

    async def run(self) -> None:
        while True:
            text = await self._queue.get()
            async with self._lock:
                await self._websocket.send_text(text)


class _Viewer:
    """Everything one connection carries between the dashboard viewer and the display helper."""

    def __init__(
        self,
        websocket: WebSocket,
        session: BrowserSession,
        ticket: BrowserTicket,
        authorize: Authorizer,
        sandbox: AsyncSandbox,
        client: DisplayClient,
    ) -> None:
        self._websocket = websocket
        self._session = session
        self._ticket = ticket
        self._authorize = authorize
        self._sandbox = sandbox
        self._client = client
        self._outbox = _Outbox(websocket)
        self._screen = display.screen_for(
            engine.VIEWPORT_WIDTH, engine.VIEWPORT_HEIGHT, engine.RENDER_SCALE
        )
        self._encode = 0
        self._splitter = AccessUnitSplitter()
        self._backlog = VideoBacklog()
        self._wheel = WheelAccumulator()
        self._queue: asyncio.Queue[Callable[[], Awaitable[None]]] = asyncio.Queue()
        self._gate = (
            ControlGate(session, ticket.login, authorize) if ticket.role == "control" else None
        )
        self._viewport = (
            ViewportSync(session, self._gate, self._resize) if self._gate is not None else None
        )

    async def _from_display(self) -> None:
        async for frame in self._client.frames():
            if isinstance(frame, Screen):
                self._screen = (frame.width, frame.height)
                self._encode += 1
                self._splitter.reset()
                self._backlog.restart()
                self._outbox.message(
                    {
                        "type": "screen",
                        "width": frame.width,
                        "height": frame.height,
                        "scale": engine.RENDER_SCALE,
                    }
                )
            elif isinstance(frame, Video):
                for unit in self._splitter.feed(frame.data):
                    self._backlog.push(unit)

    async def _to_viewer(self) -> None:
        announced: str | None = None
        encode = self._encode
        while True:
            unit = await self._backlog.pop()
            if encode != self._encode:
                encode, announced = self._encode, None
            announcement: str | None = None
            if unit.codec is not None and unit.codec != announced:
                announced = unit.codec
                announcement = json.dumps({"type": "video", "codec": unit.codec})
            if announced is None:
                continue
            flag = b"\x01" if unit.keyframe else b"\x00"
            await self._outbox.send_now(announcement, flag + unit.data)

    async def _from_viewer(self) -> None:
        actions = PersonActions(self._sandbox, self._session, self._outbox.message)
        while True:
            message = messages.parse(await self._websocket.receive_text())
            if message is None:
                continue
            gate = self._gate
            if gate is None:
                continue
            if isinstance(message, messages.ResizeMessage):
                if self._viewport is not None:
                    self._viewport.request(message.size)
                continue
            discrete = message.discrete if isinstance(message, messages.MouseMessage) else True
            if not await gate.allows(discrete):
                continue
            match message:
                case messages.KeyMessage() if _is_unmapped_character(message):
                    if message.action == "down":
                        self._queue.put_nowait(lambda m=message: actions.paste_text(m.key))
                case messages.MouseMessage() | messages.KeyMessage():
                    for event in to_helper(message, self._screen, self._wheel):
                        await self._client.send(event)
                case messages.CopyMessage():
                    await actions.copy(message)
                case messages.PasteMessage():
                    self._queue.put_nowait(lambda m=message: actions.paste(m))
                case messages.NavigateMessage():
                    self._queue.put_nowait(lambda m=message: actions.navigate(m))

    async def _resize(self, width: int, height: int) -> None:
        size = display.screen_for(width, height, engine.RENDER_SCALE)
        await self._client.send({"type": "resize", "width": size[0], "height": size[1]})

    async def _run_person_actions(self) -> None:
        while True:
            action = await self._queue.get()
            try:
                await action()
            except engine.EngineCommandError:
                logger.warning(
                    "Browser person action failed",
                    exc_info=True,
                    extra={"browser_session_id": self._session.session_id},
                )

    async def _poll_page(self) -> None:
        sent: tuple[str, str, bool] | None = None
        while True:
            try:
                state = await engine.page_state(self._sandbox, self._session)
            except engine.EngineCommandError:
                logger.debug(
                    "Browser page state lookup failed",
                    exc_info=True,
                    extra={"browser_session_id": self._session.session_id},
                )
                state = None
            if state is not None and state != sent:
                sent = state
                url, title, loading = state
                self._outbox.message(
                    {"type": "page", "url": url, "title": title, "loading": loading}
                )
            await asyncio.sleep(_PAGE_POLL_SECONDS)

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(_RECHECK_SECONDS)
            if await self._authorize() is None:
                await self._websocket.close(code=1008, reason="Access to this thread changed")
                return
            current = await manager.current(self._session.thread_id)
            if (
                current is None
                or current.session_id != self._session.session_id
                or current.state != "ready"
            ):
                await self._websocket.close(code=1000, reason="The browser session ended")
                return

    async def serve(self) -> None:
        tasks = {
            asyncio.create_task(self._from_display()),
            asyncio.create_task(self._to_viewer()),
            asyncio.create_task(self._from_viewer()),
            asyncio.create_task(self._outbox.run()),
            asyncio.create_task(self._watch()),
            asyncio.create_task(self._poll_page()),
        }
        if self._viewport is not None:
            tasks |= {
                asyncio.create_task(self._viewport.run()),
                asyncio.create_task(self._run_person_actions()),
            }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()


async def relay(
    websocket: WebSocket, session: BrowserSession, ticket: BrowserTicket, authorize: Authorizer
) -> None:
    """Serve one accepted live-view connection until either side ends it."""
    if session.sandbox_id is None or session.stream_port is None:
        await websocket.close(code=1011, reason="The browser has no live view")
        return
    try:
        await asyncio.wait_for(_SLOTS.acquire(), timeout=0.01)
    except TimeoutError:
        await websocket.close(code=1013, reason="Live view capacity reached")
        return
    from agent.sandboxes.providers.langsmith import connect_async_langsmith_sandbox

    client = None
    try:
        client, sandbox = await connect_async_langsmith_sandbox(session.sandbox_id)
        async with await sandbox.tunnel(remote_port=session.stream_port, local_port=0) as tunnel:
            async with DisplayClient(session, tunnel.local_port) as helper:
                logger.info(
                    "Browser live view opened",
                    extra={"browser_session_id": session.session_id, "browser_role": ticket.role},
                )
                await _Viewer(websocket, session, ticket, authorize, sandbox, helper).serve()
        await websocket.close(code=1000, reason="The browser session ended")
    except WebSocketDisconnect:
        logger.debug(
            "Browser live viewer disconnected", extra={"browser_session_id": session.session_id}
        )
    except Exception:
        logger.warning(
            "Browser live view failed",
            exc_info=True,
            extra={"browser_session_id": session.session_id},
        )
        try:
            await websocket.close(code=1011, reason="Live view disconnected")
        except RuntimeError:
            logger.debug(
                "Browser live view was already closed",
                extra={"browser_session_id": session.session_id},
            )
    finally:
        if client is not None:
            await client.aclose()
        _SLOTS.release()
