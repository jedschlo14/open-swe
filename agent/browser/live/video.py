"""Relays a thread browser's live view to one dashboard viewer.

The server opens a LangSmith tunnel to the live helper in the sandbox and copies
its H.264 video, cursor shape, and status to the viewer, so the provider URL never
reaches the client. Access is checked on connect and rechecked while the view is
open; the connection closes when the viewer loses access or the session it was
opened for ends. Input passes only for a viewer who holds the lease, checked again
for every press, release, wheel tick, and key.

To the viewer, video is a binary message (a flag byte, three codec bytes on key
frames, then Annex-B data); everything else is JSON text.
"""

import asyncio
import json
import logging
import struct
from collections import deque
from collections.abc import Awaitable, Callable, Mapping

from fastapi import WebSocket, WebSocketDisconnect
from langsmith.sandbox import AsyncSandbox

from agent.browser import engine, manager
from agent.browser.live import messages
from agent.browser.live.actions import PersonActions
from agent.browser.live.gate import Authorizer, ControlGate, ViewportSync
from agent.browser.models import BrowserSession
from agent.dashboard.oauth import BrowserTicket

logger = logging.getLogger(__name__)

SUBPROTOCOL = "open-swe-browser"
_SLOTS = asyncio.Semaphore(40)
_RECHECK_SECONDS = 5.0
_PAGE_POLL_SECONDS = 1.0
_MAX_QUEUED_FRAMES = 6
_HELPER_VIDEO, _HELPER_CURSOR, _HELPER_STATUS = b"V", b"C", b"S"
_HELPER_INPUT, _HELPER_RESIZE = b"I", b"R"
_KEY_FRAME = 1
_HELPER_TEXT_TYPES = {_HELPER_CURSOR: "cursor", _HELPER_STATUS: "status"}


class Outbox:
    """Everything bound for one viewer; slow viewers lose video frames, never messages.

    A viewer that falls behind skips to the next key frame instead of replaying a
    backlog, so lag stays bounded without corrupting the picture.
    """

    def __init__(self) -> None:
        self._messages: deque[str] = deque()
        self._frames: deque[bytes] = deque()
        self._need_key = True
        self._wake = asyncio.Event()

    def message(self, payload: Mapping[str, object]) -> None:
        self._messages.append(json.dumps(payload))
        self._wake.set()

    def frame(self, data: bytes) -> None:
        if data[0] & _KEY_FRAME:
            self._frames.clear()
            self._need_key = False
        elif self._need_key:
            return
        elif len(self._frames) >= _MAX_QUEUED_FRAMES:
            self._frames.clear()
            self._need_key = True
            return
        self._frames.append(data)
        self._wake.set()

    async def next(self) -> str | bytes:
        while True:
            if self._messages:
                return self._messages.popleft()
            if self._frames:
                return self._frames.popleft()
            self._wake.clear()
            await self._wake.wait()


async def _read_helper(reader: asyncio.StreamReader, outbox: Outbox) -> None:
    while True:
        header = await reader.readexactly(5)
        payload = await reader.readexactly(struct.unpack(">I", header[1:])[0])
        kind = header[:1]
        if kind == _HELPER_VIDEO:
            outbox.frame(payload)
        elif kind in _HELPER_TEXT_TYPES:
            try:
                body = json.loads(payload)
            except ValueError:
                continue
            if isinstance(body, dict):
                outbox.message({"type": _HELPER_TEXT_TYPES[kind], **body})


async def _send_outbox(websocket: WebSocket, outbox: Outbox) -> None:
    while True:
        item = await outbox.next()
        if isinstance(item, bytes):
            await websocket.send_bytes(item)
        else:
            await websocket.send_text(item)


def _helper_send(writer: asyncio.StreamWriter, kind: bytes, body: dict[str, object]) -> None:
    payload = json.dumps(body).encode()
    writer.write(kind + struct.pack(">I", len(payload)) + payload)


async def _run_person_actions(
    queue: asyncio.Queue[Callable[[], Awaitable[None]]], session: BrowserSession
) -> None:
    while True:
        action = await queue.get()
        try:
            await action()
        except engine.EngineCommandError:
            logger.warning(
                "Browser person action failed",
                exc_info=True,
                extra={"browser_session_id": session.session_id},
            )


async def _poll_page(sandbox: AsyncSandbox, session: BrowserSession, outbox: Outbox) -> None:
    sent: tuple[str, str, bool] | None = None
    while True:
        try:
            state = await engine.page_state(sandbox, session)
        except engine.EngineCommandError:
            logger.debug(
                "Browser page state lookup failed",
                exc_info=True,
                extra={"browser_session_id": session.session_id},
            )
            state = None
        if state is not None and state != sent:
            sent = state
            url, title, loading = state
            outbox.message({"type": "page", "url": url, "title": title, "loading": loading})
        await asyncio.sleep(_PAGE_POLL_SECONDS)


async def _upstream(
    websocket: WebSocket,
    writer: asyncio.StreamWriter,
    gate: ControlGate,
    viewport: ViewportSync,
    actions: PersonActions,
    queue: asyncio.Queue[Callable[[], Awaitable[None]]],
) -> None:
    while True:
        message = messages.parse(await websocket.receive_text())
        if message is None:
            continue
        if isinstance(message, messages.ResizeMessage):
            viewport.request(message.size)
            continue
        discrete = message.discrete if isinstance(message, messages.MouseMessage) else True
        if not await gate.allows(discrete):
            continue
        match message:
            case messages.MouseMessage() | messages.KeyMessage():
                _helper_send(writer, _HELPER_INPUT, message.model_dump())
            case messages.CopyMessage():
                await actions.copy(message)
            case messages.PasteMessage():
                queue.put_nowait(lambda m=message: actions.paste(m))
            case messages.NavigateMessage():
                queue.put_nowait(lambda m=message: actions.navigate(m))
        await writer.drain()


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


async def _serve(
    websocket: WebSocket,
    session: BrowserSession,
    ticket: BrowserTicket,
    authorize: Authorizer,
    sandbox: AsyncSandbox,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    outbox = Outbox()
    tasks = {
        asyncio.create_task(_read_helper(reader, outbox)),
        asyncio.create_task(_send_outbox(websocket, outbox)),
        asyncio.create_task(_watch(session, authorize, websocket)),
        asyncio.create_task(_poll_page(sandbox, session, outbox)),
    }
    if ticket.role == "control":
        gate = ControlGate(session, ticket.login, authorize)
        queue: asyncio.Queue[Callable[[], Awaitable[None]]] = asyncio.Queue()
        actions = PersonActions(sandbox, session, outbox.message)

        async def resize(width: int, height: int) -> None:
            _helper_send(writer, _HELPER_RESIZE, {"width": width, "height": height})
            await writer.drain()

        viewport = ViewportSync(session, gate, resize)
        tasks |= {
            asyncio.create_task(_upstream(websocket, writer, gate, viewport, actions, queue)),
            asyncio.create_task(viewport.run()),
            asyncio.create_task(_run_person_actions(queue, session)),
        }
    else:
        tasks.add(asyncio.create_task(_discard_viewer_messages(websocket)))
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()


async def _discard_viewer_messages(websocket: WebSocket) -> None:
    while True:
        await websocket.receive_text()


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
            reader, writer = await asyncio.open_connection("127.0.0.1", tunnel.local_port)
            logger.info(
                "Browser live view opened",
                extra={"browser_session_id": session.session_id, "browser_role": ticket.role},
            )
            try:
                await _serve(websocket, session, ticket, authorize, sandbox, reader, writer)
            finally:
                writer.close()
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
