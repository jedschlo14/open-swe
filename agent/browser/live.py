"""Relays a thread browser's live view to one dashboard viewer.

The server opens a LangSmith tunnel to the stream forward in the sandbox and
copies frames to the viewer, so neither the provider URL nor DevTools ever
reaches the client. Access is checked on connect and rechecked while the view
is open; the connection closes when the viewer loses access or the session it
was opened for ends. Input passes only for a viewer who holds the lease, checked
again for every press, release, and key. The controller also receives the page's
cursor, looked up under their pointer, so hovering a link looks like hovering a link.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from langsmith.sandbox import AsyncSandbox
from pydantic import JsonValue
from websockets.asyncio.client import ClientConnection
from websockets.typing import Origin

from agent.browser import engine, manager, store
from agent.browser.models import BrowserSession
from agent.dashboard.oauth import BrowserTicket, BrowserTicketRole

logger = logging.getLogger(__name__)

SUBPROTOCOL = "open-swe-browser"
_SLOTS = asyncio.Semaphore(40)
_RECHECK_SECONDS = 5.0
_MAX_FPS = 10
_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
# Console output is page content nobody asked to keep; viewers see the page itself.
_RELAYED_TYPES = frozenset({"frame", "status", "tabs", "url"})
_MOUSE_EVENTS = frozenset({"mousePressed", "mouseReleased", "mouseMoved", "mouseWheel"})
_KEY_EVENTS = frozenset({"keyDown", "keyUp", "char"})
_MOUSE_BUTTONS = frozenset({"none", "left", "middle", "right"})
# Pointer moves arrive by the dozen per second; they reuse a check this recent.
_MOVE_CHECK_SECONDS = 2.0
_ACTIVITY_EVERY_SECONDS = 30.0
# Each cursor lookup is a command in the sandbox, so lookups run one at a time with a rest between.
_CURSOR_PROBE_REST_SECONDS = 0.1

type Authorizer = Callable[[], Awaitable[BrowserTicketRole | None]]


def _viewer_message(raw: str) -> dict[str, JsonValue] | None:
    """A message any viewer may pass upstream: frame pacing, never input."""
    try:
        message = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(message, dict):
        return None
    kind = message.get("type")
    if kind == "ack":
        seq = message.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0:
            return {"type": "ack", "seq": seq}
    if kind == "config":
        fps = message.get("maxFps")
        if isinstance(fps, int) and not isinstance(fps, bool) and 1 <= fps <= _MAX_FPS:
            return {"type": "config", "maxFps": fps}
    return None


async def _downstream(upstream: ClientConnection, websocket: WebSocket) -> None:
    async for raw in upstream:
        if not isinstance(raw, str):
            continue
        try:
            kind = json.loads(raw).get("type")
        except ValueError, AttributeError:
            continue
        if kind in _RELAYED_TYPES:
            await websocket.send_text(raw)


def _number(value: JsonValue, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if low <= value <= high else None


def _input_message(raw: str) -> tuple[dict[str, JsonValue], bool] | None:
    """A controller's input event rebuilt from known fields, and whether it is a discrete action."""
    try:
        message = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(message, dict):
        return None
    kind, event = message.get("type"), message.get("eventType")
    modifiers = message.get("modifiers")
    clean: dict[str, JsonValue] = {
        "type": kind if isinstance(kind, str) else "",
        "eventType": event if isinstance(event, str) else "",
        "modifiers": modifiers if isinstance(modifiers, int) and 0 <= modifiers < 16 else 0,
    }
    if kind == "input_mouse" and event in _MOUSE_EVENTS:
        x, y = _number(message.get("x"), 0, 10_000), _number(message.get("y"), 0, 10_000)
        button = message.get("button", "none")
        clicks = message.get("clickCount", 0)
        if x is None or y is None or button not in _MOUSE_BUTTONS:
            return None
        clean |= {
            "x": x,
            "y": y,
            "button": button,
            "clickCount": clicks if isinstance(clicks, int) and 0 <= clicks <= 3 else 0,
            "deltaX": _number(message.get("deltaX", 0), -10_000, 10_000) or 0,
            "deltaY": _number(message.get("deltaY", 0), -10_000, 10_000) or 0,
        }
        return clean, event != "mouseMoved"
    if kind == "input_keyboard" and event in _KEY_EVENTS:
        for field, limit in (("key", 32), ("code", 32), ("text", 8)):
            value = message.get(field)
            if isinstance(value, str) and len(value) <= limit:
                clean[field] = value
        return clean, True
    return None


class _ControlGate:
    """Decides, per input event, whether this viewer still holds the browser's lease."""

    def __init__(self, session: BrowserSession, login: str, authorize: Authorizer) -> None:
        self._session = session
        self._login = login
        self._authorize = authorize
        self._checked_at = float("-inf")
        self._allowed = False
        self._active_at = float("-inf")

    async def allows(self, discrete: bool) -> bool:
        now = time.monotonic()
        if discrete or now - self._checked_at >= _MOVE_CHECK_SECONDS:
            current = await store.latest(self._session.thread_id)
            self._allowed = (
                current is not None
                and current.session_id == self._session.session_id
                and current.user_controls(self._login)
                and await self._authorize() == "control"
            )
            self._checked_at = now
        if self._allowed and discrete and now - self._active_at >= _ACTIVITY_EVERY_SECONDS:
            self._active_at = now
            await store.touch(self._session.session_id)
        return self._allowed


class _CursorProbe:
    """Tells the controller which cursor the page shows under their pointer.

    DevTools has no cursor event and the stream carries only pixels, so the
    page is asked at the pointer's latest position, newest position first.
    """

    def __init__(
        self, sandbox: AsyncSandbox, session: BrowserSession, websocket: WebSocket
    ) -> None:
        self._sandbox = sandbox
        self._session = session
        self._websocket = websocket
        self._target: tuple[int, int] | None = None
        self._wake = asyncio.Event()

    def move(self, x: float, y: float) -> None:
        self._target = (round(x), round(y))
        self._wake.set()

    async def run(self) -> None:
        sent: str | None = None
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._target is None:
                continue
            try:
                cursor = await engine.cursor_at(self._sandbox, self._session, *self._target)
            except Exception:
                logger.debug(
                    "Browser cursor lookup failed",
                    exc_info=True,
                    extra={"browser_session_id": self._session.session_id},
                )
                cursor = None
            if cursor is not None and cursor != sent:
                sent = cursor
                await self._websocket.send_text(json.dumps({"type": "cursor", "cursor": cursor}))
            await asyncio.sleep(_CURSOR_PROBE_REST_SECONDS)


async def _upstream(
    websocket: WebSocket,
    upstream: ClientConnection,
    gate: _ControlGate | None,
    probe: _CursorProbe | None,
) -> None:
    while True:
        raw = await websocket.receive_text()
        message = _viewer_message(raw)
        if message is not None:
            await upstream.send(json.dumps(message))
            continue
        if gate is None:
            continue
        event = _input_message(raw)
        if event is not None and await gate.allows(event[1]):
            await upstream.send(json.dumps(event[0]))
            x, y = event[0].get("x"), event[0].get("y")
            if probe is not None and isinstance(x, float) and isinstance(y, float):
                probe.move(x, y)


async def _watch(session: BrowserSession, authorize: Authorizer, websocket: WebSocket) -> None:
    while True:
        await asyncio.sleep(_RECHECK_SECONDS)
        if await authorize() is None:
            await websocket.close(code=1008, reason="Access to this thread changed")
            return
        current = await manager.current(session.thread_id)
        if current is None or current.session_id != session.session_id or current.state != "ready":
            await websocket.close(code=1000, reason="The browser session ended")
            return


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

    gate = _ControlGate(session, ticket.login, authorize) if ticket.role == "control" else None
    client = None
    try:
        client, sandbox = await connect_async_langsmith_sandbox(session.sandbox_id)
        async with await sandbox.tunnel(remote_port=session.stream_port, local_port=0) as tunnel:
            async with websockets.connect(
                f"ws://127.0.0.1:{tunnel.local_port}/?pacing=ack&maxFps={_MAX_FPS}",
                origin=Origin("http://localhost"),
                max_size=_MAX_MESSAGE_BYTES,
            ) as upstream:
                logger.info(
                    "Browser live view opened",
                    extra={"browser_session_id": session.session_id, "browser_role": ticket.role},
                )
                probe = _CursorProbe(sandbox, session, websocket) if gate is not None else None
                tasks = {
                    asyncio.create_task(_downstream(upstream, websocket)),
                    asyncio.create_task(_upstream(websocket, upstream, gate, probe)),
                    asyncio.create_task(_watch(session, authorize, websocket)),
                }
                if probe is not None:
                    tasks.add(asyncio.create_task(probe.run()))
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    task.result()
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
