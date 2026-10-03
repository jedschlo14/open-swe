"""Dashboard API for a thread's browser session.

Reading the session's status needs thread-read access; starting, stopping,
keeping it alive, and confirming a held action need thread-write access, checked
on every request.
"""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Response, WebSocket
from pydantic import BaseModel, ConfigDict, JsonValue
from pydantic.alias_generators import to_camel

from agent.browser import lease, live, manager, store
from agent.browser.models import BrowserSession, BrowserSessionView
from agent.dashboard.deps import SESSION_DEP
from agent.dashboard.oauth import (
    BrowserTicket,
    BrowserTicketRole,
    decode_browser_ticket,
    issue_browser_ticket,
)
from agent.threads.summary import assert_thread_readable, thread_is_postable
from agent.utils.json_types import thread_metadata
from agent.utils.thread_ops import langgraph_client, langgraph_url

router = APIRouter(tags=["browser"])


class ConfirmationDecision(BaseModel):
    approve: bool


class BrowserLiveConnection(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    url: str
    protocol: str
    ticket: str
    role: BrowserTicketRole


async def _metadata(thread_id: str) -> dict[str, JsonValue]:
    try:
        thread = await langgraph_client().threads.get(thread_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, "thread not found") from exc
    return thread_metadata(thread)


async def _readable(thread_id: str, session: dict[str, Any]) -> dict[str, JsonValue]:
    metadata = await _metadata(thread_id)
    assert_thread_readable(metadata, session["sub"], session.get("email"))
    return metadata


async def _writable(thread_id: str, session: dict[str, Any]) -> dict[str, JsonValue]:
    metadata = await _readable(thread_id, session)
    if not thread_is_postable(metadata, session["sub"], session.get("email")):
        raise HTTPException(403, "only people who can message this thread can control its browser")
    return metadata


def _workspace(metadata: dict[str, JsonValue]) -> str | None:
    value = metadata.get("workspace") or metadata.get("environment")
    return value if isinstance(value, str) else None


def _view(
    session: BrowserSession | None,
    metadata: dict[str, JsonValue],
    viewer: dict[str, Any] | None = None,
) -> BrowserSessionView:
    return BrowserSessionView.of(
        session,
        now=datetime.now(UTC),
        supported=manager.browser_supported(metadata),
        viewer=viewer["sub"] if viewer else None,
    )


@router.get("/threads/{thread_id}/browser")
async def api_browser_status(
    thread_id: str, session: dict[str, Any] = SESSION_DEP
) -> BrowserSessionView:
    metadata = await _readable(thread_id, session)
    return _view(await manager.current(thread_id), metadata, session)


@router.post("/threads/{thread_id}/browser/start")
async def api_browser_start(
    thread_id: str, session: dict[str, Any] = SESSION_DEP
) -> BrowserSessionView:
    metadata = await _writable(thread_id, session)
    try:
        started = await manager.ensure_session(
            thread_id, started_by=session["sub"], workspace_slug=_workspace(metadata)
        )
    except manager.BrowserUnsupportedError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _view(started, metadata, session)


@router.post("/threads/{thread_id}/browser/stop")
async def api_browser_stop(
    thread_id: str, session: dict[str, Any] = SESSION_DEP
) -> BrowserSessionView:
    metadata = await _writable(thread_id, session)
    stopped = await manager.stop_session(thread_id, "requested")
    return _view(stopped or await manager.current(thread_id), metadata, session)


@router.post("/threads/{thread_id}/browser/keepalive")
async def api_browser_keepalive(
    thread_id: str, session: dict[str, Any] = SESSION_DEP
) -> BrowserSessionView:
    metadata = await _writable(thread_id, session)
    kept = await manager.keep_alive(thread_id)
    return _view(kept or await manager.current(thread_id), metadata, session)


@router.post("/threads/{thread_id}/browser/confirmations/{confirmation_id}")
async def api_browser_confirm(
    thread_id: str,
    confirmation_id: str,
    decision: ConfirmationDecision,
    session: dict[str, Any] = SESSION_DEP,
) -> BrowserSessionView:
    """Approve or deny the action the agent is waiting on; only a thread writer may decide."""
    metadata = await _writable(thread_id, session)
    active = await store.active(thread_id)
    if active is None:
        raise HTTPException(409, "no browser session is running")
    decided = await store.decide_confirmation(
        active.session_id, confirmation_id, approve=decision.approve, login=session["sub"]
    )
    if decided is None:
        raise HTTPException(409, "that action is no longer waiting for confirmation")
    return _view(decided, metadata, session)


def _role(metadata: dict[str, JsonValue], login: str, email: str | None) -> BrowserTicketRole:
    return "control" if thread_is_postable(metadata, login, email) else "view"


def _live_url(thread_id: str) -> str:
    parsed = urlsplit(langgraph_url())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(500, "invalid LangGraph URL for the browser live view")
    path = (
        f"{parsed.path.rstrip('/')}/dashboard/api/threads/{quote(thread_id, safe='')}/browser/live"
    )
    return urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc, path, "", ""))


@router.post("/threads/{thread_id}/browser/live/connect")
async def api_browser_live_connect(
    thread_id: str, response: Response, session: dict[str, Any] = SESSION_DEP
) -> BrowserLiveConnection:
    """Issue a one-minute ticket for the live view; readers watch, writers may later control."""
    metadata = await _readable(thread_id, session)
    current = await manager.current(thread_id)
    if current is None or current.state != "ready":
        raise HTTPException(409, "the browser is not running")
    role = _role(metadata, session["sub"], session.get("email"))
    response.headers["Cache-Control"] = "no-store"
    return BrowserLiveConnection(
        url=_live_url(thread_id),
        protocol=live.SUBPROTOCOL,
        ticket=issue_browser_ticket(
            login=session["sub"], email=session.get("email"), thread_id=thread_id, role=role
        ),
        role=role,
    )


def _ticket(websocket: WebSocket, thread_id: str) -> BrowserTicket:
    offered = [
        value.strip()
        for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if value.strip()
    ]
    if len(offered) != 2 or offered[0] != live.SUBPROTOCOL:
        raise HTTPException(401, "invalid browser ticket")
    return decode_browser_ticket(offered[1], thread_id=thread_id)


@router.websocket("/threads/{thread_id}/browser/live")
async def api_browser_live(websocket: WebSocket, thread_id: str) -> None:
    try:
        ticket = _ticket(websocket, thread_id)
    except HTTPException as exc:
        await websocket.close(code=1008, reason=str(exc.detail)[:123])
        return

    async def authorize() -> BrowserTicketRole | None:
        try:
            metadata = await _readable(thread_id, {"sub": ticket.login, "email": ticket.email})
        except HTTPException:
            return None
        if ticket.role == "control" and _role(metadata, ticket.login, ticket.email) == "control":
            return "control"
        return "view"

    if await authorize() is None:
        await websocket.close(code=1008, reason="thread not found")
        return
    current = await manager.current(thread_id)
    if current is None or current.state != "ready":
        await websocket.close(code=1008, reason="the browser is not running")
        return
    await websocket.accept(subprotocol=live.SUBPROTOCOL)
    await live.relay(websocket, current, ticket, authorize)


@router.post("/threads/{thread_id}/browser/takeover")
async def api_browser_takeover(
    thread_id: str, session: dict[str, Any] = SESSION_DEP
) -> BrowserSessionView:
    """Take the lease from the agent once its in-flight action settles."""
    metadata = await _writable(thread_id, session)
    try:
        taken = await lease.take_control(thread_id, session["sub"])
    except lease.LeaseConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _view(taken, metadata, session)


@router.post("/threads/{thread_id}/browser/handback")
async def api_browser_handback(
    thread_id: str, session: dict[str, Any] = SESSION_DEP
) -> BrowserSessionView:
    """Return the lease to the agent; any thread writer may, so control never strands."""
    metadata = await _writable(thread_id, session)
    try:
        returned = await lease.hand_back(thread_id, session["sub"])
    except lease.LeaseConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _view(returned, metadata, session)
