"""Relays a thread browser's live view to one dashboard viewer.

The server opens a LangSmith tunnel to the stream forward in the sandbox and
copies frames to the viewer, so neither the provider URL nor DevTools ever
reaches the client. Access is checked on connect and rechecked while the view
is open; the connection closes when the viewer loses access or the session it
was opened for ends.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import JsonValue
from websockets.asyncio.client import ClientConnection
from websockets.typing import Origin

from agent.browser import manager
from agent.browser.models import BrowserSession
from agent.dashboard.oauth import BrowserTicket, BrowserTicketRole

logger = logging.getLogger(__name__)

SUBPROTOCOL = "open-swe-browser"
_SLOTS = asyncio.Semaphore(40)
_RECHECK_SECONDS = 5.0
_MAX_FPS = 30
_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
# Console output is page content nobody asked to keep; viewers see the page itself.
_RELAYED_TYPES = frozenset({"frame", "status", "tabs", "url"})

type Authorizer = Callable[[], Awaitable[BrowserTicketRole | None]]


def _viewer_message(raw: str) -> dict[str, JsonValue] | None:
    """A viewer message the relay may pass upstream for a view-only viewer."""
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


async def _upstream(websocket: WebSocket, upstream: ClientConnection) -> None:
    while True:
        message = _viewer_message(await websocket.receive_text())
        if message is not None:
            await upstream.send(json.dumps(message))


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
                tasks = {
                    asyncio.create_task(_downstream(upstream, websocket)),
                    asyncio.create_task(_upstream(websocket, upstream)),
                    asyncio.create_task(_watch(session, authorize, websocket)),
                }
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
