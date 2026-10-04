"""Relays a thread browser's live view, served by neko over WebRTC, to one dashboard viewer.

The viewer's browser and neko exchange video, and neko sends the cursor image, over a
WebRTC connection. With a TURN relay configured it goes through the relay, and the
TURN credential the viewer receives is minted for that connection. Without one it
goes through a loopback UDP port on this server and the sandbox tunnel (see
``local_media``), for a viewer on this machine. Everything else passes through this
socket: signaling, page state, and the controller's input. The viewer never talks to
neko.

Access is checked on connect and rechecked while the view is open. Signaling is
open to any viewer; input, resizing, and toolbar actions pass only for a viewer who
holds the lease, checked again for every press, release, wheel tick, and key.
neko's own events are forwarded from an allowlist, rebuilt from known fields.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack

import aiohttp
from fastapi import WebSocket, WebSocketDisconnect
from langsmith.sandbox import AsyncSandbox
from pydantic import JsonValue

from agent.browser import engine, manager, neko, turn
from agent.browser.live import local_media, messages
from agent.browser.live.actions import PersonActions
from agent.browser.live.gate import Authorizer, ControlGate, ViewportSync
from agent.browser.live.keysyms import keysym_for
from agent.browser.models import BrowserSession
from agent.browser.neko_client import NekoApi, NekoEvent
from agent.dashboard.oauth import BrowserTicket

logger = logging.getLogger(__name__)

SUBPROTOCOL = "open-swe-browser"
_SLOTS = asyncio.Semaphore(40)
_RECHECK_SECONDS = 5.0
_PAGE_POLL_SECONDS = 1.0
_MOUSE_BUTTONS = {0: 1, 1: 2, 2: 3}
# One X wheel click scrolls this many CSS pixels in Chromium at the 1.5 render scale.
_WHEEL_TICK_PIXELS = 120.0
_MAX_WHEEL_TICKS = 10
_FIRST_UNMAPPED_CODE_POINT = 0x80

type Outbound = Callable[[Mapping[str, JsonValue]], None]


def to_viewer(
    event: NekoEvent, viewer_ice: turn.IceServer | None, local_port: int | None = None
) -> dict[str, JsonValue] | None:
    """What neko's event means to the dashboard viewer, or ``None`` when it is not for them.

    With ``viewer_ice`` the viewer gets only relay candidates and its own TURN credential.
    With ``local_port`` it gets neko's candidates rewritten to the loopback port the server
    listens on for its media. With neither, signaling is not offered.
    """
    kind, payload = event.get("event"), event.get("payload")
    if not isinstance(payload, dict):
        return None
    sdp = payload.get("sdp")
    if kind == "signal/provide" and isinstance(sdp, str):
        if viewer_ice is not None:
            return {
                "type": "signal",
                "event": "provide",
                "sdp": sdp,
                "iceServers": [viewer_ice.as_json()],
                "relayOnly": True,
            }
        if local_port is not None:
            return {
                "type": "signal",
                "event": "provide",
                "sdp": local_media.localize_sdp(sdp, local_port),
                "iceServers": [],
                "relayOnly": False,
            }
        return None
    if kind == "signal/restart" and isinstance(sdp, str):
        if viewer_ice is None and local_port is not None:
            sdp = local_media.localize_sdp(sdp, local_port)
        return {"type": "signal", "event": "restart", "sdp": sdp}
    if kind == "signal/candidate":
        candidate = payload.get("candidate")
        if not isinstance(candidate, str):
            return None
        if viewer_ice is not None:
            if " typ relay" not in candidate:
                return None
        elif local_port is not None:
            candidate = local_media.localize_candidate(candidate, local_port) or ""
        if not candidate:
            return None
        return {
            "type": "signal",
            "event": "candidate",
            "candidate": {
                "candidate": candidate,
                "sdpMid": payload.get("sdpMid") or "",
                "sdpMLineIndex": payload.get("sdpMLineIndex") or 0,
            },
        }
    if kind == "screen/updated":
        return _screen(payload)
    if kind == "system/init":
        size = payload.get("screen_size")
        return _screen(size) if isinstance(size, dict) else None
    return None


def _screen(size: Mapping[str, JsonValue]) -> dict[str, JsonValue] | None:
    width, height = size.get("width"), size.get("height")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        return {"type": "screen", "width": width, "height": height, "scale": engine.RENDER_SCALE}
    return None


class WheelAccumulator:
    """Turns pixel wheel deltas into whole X wheel clicks, carrying what is left over.

    A trackpad sends many tiny deltas and a mouse a few large ones; both add up to the
    same distance instead of rounding each event to a full click or to nothing.
    """

    def __init__(self) -> None:
        self._x = 0.0
        self._y = 0.0

    def ticks(self, dx: float, dy: float) -> tuple[int, int]:
        """Clicks for neko: positive is right and up, the opposite of a browser's ``deltaY``."""
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


def to_neko(
    message: messages.MouseMessage | messages.KeyMessage | messages.SignalMessage,
    screen: tuple[int, int],
    wheel: WheelAccumulator,
) -> list[tuple[str, dict[str, JsonValue]]]:
    """The neko events one viewer message becomes; CSS pixels become display pixels."""
    match message:
        case messages.SignalMessage():
            return _signal(message)
        case messages.KeyMessage():
            keysym = keysym_for(message.key, message.code)
            if keysym is None:
                return []
            return [(f"control/key{message.action}", {"keysym": keysym})]
        case messages.MouseMessage():
            x = min(round(message.x * engine.RENDER_SCALE), screen[0] - 1)
            y = min(round(message.y * engine.RENDER_SCALE), screen[1] - 1)
            at: dict[str, JsonValue] = {"x": max(x, 0), "y": max(y, 0)}
            match message.action:
                case "move":
                    return [("control/move", at)]
                case "down" | "up":
                    code = _MOUSE_BUTTONS[message.button]
                    return [(f"control/button{message.action}", {**at, "code": code})]
                case "wheel":
                    right, up = wheel.ticks(message.dx, message.dy)
                    if right == 0 and up == 0:
                        return []
                    return [
                        ("control/move", at),
                        ("control/scroll", {"delta_x": right, "delta_y": up, "control_key": False}),
                    ]
    return []


def _signal(message: messages.SignalMessage) -> list[tuple[str, dict[str, JsonValue]]]:
    match message.event:
        case "request":
            return [("signal/request", {"video": {}, "audio": {"disabled": True}})]
        case "restart":
            return [("signal/restart", {})]
        case "answer":
            return [("signal/answer", {"sdp": message.sdp})] if message.sdp else []
        case "candidate":
            if message.candidate is None:
                return []
            return [("signal/candidate", message.candidate.model_dump(mode="json"))]
    return []


def _is_unmapped_character(message: messages.KeyMessage) -> bool:
    """A typed character outside ASCII, which the display has no keycode for; it is inserted as text."""
    return len(message.key) == 1 and ord(message.key) >= _FIRST_UNMAPPED_CODE_POINT


class _Outbox:
    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    def message(self, payload: Mapping[str, JsonValue]) -> None:
        self._queue.put_nowait(json.dumps(payload))

    async def run(self) -> None:
        while True:
            await self._websocket.send_text(await self._queue.get())


class _Viewer:
    """Everything one connection carries between the dashboard viewer and neko."""

    def __init__(
        self,
        websocket: WebSocket,
        session: BrowserSession,
        ticket: BrowserTicket,
        authorize: Authorizer,
        sandbox: AsyncSandbox,
        api: NekoApi,
        media: local_media.LocalMedia | None,
    ) -> None:
        self._websocket = websocket
        self._session = session
        self._ticket = ticket
        self._authorize = authorize
        self._sandbox = sandbox
        self._api = api
        self._outbox = _Outbox(websocket)
        self._screen = neko.screen_for(
            engine.VIEWPORT_WIDTH, engine.VIEWPORT_HEIGHT, engine.RENDER_SCALE
        )
        self._ice = (
            turn.mint(session.session_id, ttl_seconds=turn.VIEWER_TTL_SECONDS)
            if media is None
            else None
        )
        self._local_port = media.port if media is not None else None
        self._wheel = WheelAccumulator()
        self._queue: asyncio.Queue[Callable[[], Awaitable[None]]] = asyncio.Queue()
        self._gate = (
            ControlGate(session, ticket.login, authorize) if ticket.role == "control" else None
        )
        self._viewport = (
            ViewportSync(session, self._gate, self._resize) if self._gate is not None else None
        )

    async def _from_neko(self) -> None:
        socket = await self._api.events()
        async for frame in socket:
            if frame.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                event = json.loads(frame.data)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "system/disconnect":
                await self._websocket.close(code=1011, reason="Live view disconnected")
                return
            outgoing = to_viewer(event, self._ice, self._local_port)
            if outgoing is not None:
                if outgoing["type"] == "screen":
                    width, height = outgoing["width"], outgoing["height"]
                    if isinstance(width, int) and isinstance(height, int):
                        self._screen = (width, height)
                self._outbox.message(outgoing)

    async def _from_viewer(self) -> None:
        actions = PersonActions(self._sandbox, self._session, self._outbox.message)
        while True:
            message = messages.parse(await self._websocket.receive_text())
            if message is None:
                continue
            if isinstance(message, messages.SignalMessage):
                await self._forward(to_neko(message, self._screen, self._wheel))
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
                    await self._forward(to_neko(message, self._screen, self._wheel))
                case messages.CopyMessage():
                    await actions.copy(message)
                case messages.PasteMessage():
                    self._queue.put_nowait(lambda m=message: actions.paste(m))
                case messages.NavigateMessage():
                    self._queue.put_nowait(lambda m=message: actions.navigate(m))

    async def _forward(self, events: list[tuple[str, dict[str, JsonValue]]]) -> None:
        for event, payload in events:
            await self._api.send(event, payload)

    async def _resize(self, width: int, height: int) -> None:
        size = neko.screen_for(width, height, engine.RENDER_SCALE)
        try:
            await self._api.set_screen(*size, neko.SCREEN_RATE)
        except neko.NekoError, aiohttp.ClientError:
            logger.warning(
                "Browser resize failed",
                exc_info=True,
                extra={"browser_session_id": self._session.session_id},
            )

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
            asyncio.create_task(self._from_neko()),
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


def _mode_problem(websocket: WebSocket) -> str | None:
    """Why this viewer cannot be served in the session's media mode, or ``None`` if it can."""
    if neko.media_mode() == "relay":
        return None
    if local_media.is_local_viewer(
        websocket.client.host if websocket.client else None, websocket.headers
    ):
        return None
    return neko.LOCAL_ONLY_MESSAGE


async def relay(
    websocket: WebSocket, session: BrowserSession, ticket: BrowserTicket, authorize: Authorizer
) -> None:
    """Serve one accepted live-view connection until either side ends it."""
    if session.sandbox_id is None or session.stream_port is None:
        await websocket.close(code=1011, reason="The browser has no live view")
        return
    problem = _mode_problem(websocket)
    if problem is not None:
        await websocket.close(code=1008, reason=problem)
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
        async with AsyncExitStack() as stack:
            tunnel = await stack.enter_async_context(
                await sandbox.tunnel(remote_port=session.stream_port, local_port=0)
            )
            media = (
                await stack.enter_async_context(local_media.LocalMedia(sandbox, session))
                if neko.media_mode() == "local"
                else None
            )
            api = await stack.enter_async_context(NekoApi(session, tunnel.local_port))
            await api.join(ticket.role)
            logger.info(
                "Browser live view opened",
                extra={
                    "browser_session_id": session.session_id,
                    "browser_role": ticket.role,
                    "browser_media": "local" if media is not None else "relay",
                },
            )
            await _Viewer(websocket, session, ticket, authorize, sandbox, api, media).serve()
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
