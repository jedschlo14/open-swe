"""Relays a thread browser's live view to one dashboard viewer.

The server opens a LangSmith tunnel to the mirror bridge in the sandbox and copies
what it records to the viewer, so the provider URL never reaches the client. The
viewer rebuilds the page from those events as a real document. Access is checked on
connect and rechecked while the view is open; the connection closes when the viewer
loses access or the session it was opened for ends. Input passes only for a viewer
who holds the lease, checked again for every press, release, wheel tick, and key.

To the viewer, a JSON array is a batch of page events and a JSON object is a message
(page state, a reset, fonts, a notice, clipboard text, the resource ticket).
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping

from fastapi import WebSocket, WebSocketDisconnect
from langsmith.sandbox import AsyncSandbox

from agent.browser import engine, manager
from agent.browser.live import bridge, messages
from agent.browser.live.actions import PersonActions
from agent.browser.live.gate import Authorizer, ControlGate, ViewportSync
from agent.browser.models import BrowserSession
from agent.dashboard.oauth import BrowserTicket, issue_browser_asset_ticket

logger = logging.getLogger(__name__)

SUBPROTOCOL = "open-swe-browser"
_SLOTS = asyncio.Semaphore(40)
_RECHECK_SECONDS = 5.0


class Outbound:
    """Everything bound for one viewer, sent one message at a time."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._lock = asyncio.Lock()
        self._background: set[asyncio.Task[None]] = set()

    async def text(self, payload: str) -> None:
        async with self._lock:
            await self._websocket.send_text(payload)

    def message(self, payload: Mapping[str, object]) -> None:
        task = asyncio.create_task(self.text(json.dumps(payload)))
        self._background.add(task)
        task.add_done_callback(self._background.discard)


async def _read_bridge(reader: asyncio.StreamReader, outbound: Outbound) -> None:
    while True:
        kind, payload = await bridge.read_frame(reader)
        if kind in (bridge.KIND_EVENTS, bridge.KIND_JSON):
            await outbound.text(payload.decode())


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
            case (
                messages.MouseMessage()
                | messages.KeyMessage()
                | messages.PasteMessage()
                | messages.ChoiceMessage()
            ):
                bridge.write_json(writer, bridge.KIND_INPUT, message.model_dump(exclude_none=True))
            case messages.CopyMessage():
                await actions.copy(message)
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
    outbound = Outbound(websocket)
    await outbound.text(
        json.dumps(
            {
                "type": "assets",
                "ticket": issue_browser_asset_ticket(
                    login=ticket.login, email=ticket.email, thread_id=session.thread_id
                ),
            }
        )
    )
    bridge.write_json(writer, bridge.KIND_HELLO, {"role": "view"})
    bridge.write_json(writer, bridge.KIND_CONTROL, {"type": "fonts"})
    tasks = {
        asyncio.create_task(_read_bridge(reader, outbound)),
        asyncio.create_task(_watch(session, authorize, websocket)),
    }
    if ticket.role == "control":
        gate = ControlGate(session, ticket.login, authorize)
        queue: asyncio.Queue[Callable[[], Awaitable[None]]] = asyncio.Queue()
        actions = PersonActions(sandbox, session, outbound.message)

        async def resize(width: int, height: int) -> None:
            try:
                await engine.set_viewport(sandbox, session, width, height)
            except engine.EngineCommandError:
                logger.warning(
                    "Browser resize failed",
                    exc_info=True,
                    extra={"browser_session_id": session.session_id},
                )

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
