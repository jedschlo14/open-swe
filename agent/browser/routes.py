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

from agent.browser import engine, lease, live, manager, sign_ins, store
from agent.browser.models import BrowserSession, BrowserSessionView
from agent.browser.policy import endpoint_of
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


class HandbackRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    save_sign_in: bool = False


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
    login = viewer["sub"] if viewer else None
    view = BrowserSessionView.of(
        session,
        now=datetime.now(UTC),
        supported=manager.browser_supported(metadata),
        viewer=login,
    )
    if session and view.viewer_controls and _owns_private_thread(metadata, login):
        view.can_save_sign_in = True
        view.approved_endpoints = list(session.approved_endpoints)
    return view


def _owns_private_thread(metadata: dict[str, JsonValue], login: str | None) -> bool:
    owner = metadata.get("owner_login")
    return (
        metadata.get("visibility") == "private"
        and login is not None
        and isinstance(owner, str)
        and owner.lower() == login.lower()
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
    if not current.live_view:
        raise HTTPException(409, "this browser has no live view")
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
    thread_id: str,
    request: HandbackRequest | None = None,
    session: dict[str, Any] = SESSION_DEP,
) -> BrowserSessionView:
    """Return the lease to the agent, first saving the page's sign-in when asked.

    Any thread writer may hand back, so control never strands; a failed save keeps
    the caller in control so they can retry or hand back without it.
    """
    metadata = await _writable(thread_id, session)
    if request is not None and request.save_sign_in:
        await _save_sign_in(thread_id, metadata, session["sub"])
    try:
        returned = await lease.hand_back(thread_id, session["sub"])
    except lease.LeaseConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _view(returned, metadata, session)


@router.get("/browser/saved-sign-ins")
async def api_browser_saved_sign_ins(
    session: dict[str, Any] = SESSION_DEP,
) -> list[sign_ins.SavedSignIn]:
    """The caller's saved sign-ins, without their contents."""
    return await sign_ins.list_for(session["sub"])


@router.delete("/browser/saved-sign-ins/{sign_in_id}", status_code=204)
async def api_browser_delete_saved_sign_in(
    sign_in_id: str, session: dict[str, Any] = SESSION_DEP
) -> Response:
    """Revoke a saved sign-in; the next restore finds nothing."""
    if not await sign_ins.delete(session["sub"], sign_in_id):
        raise HTTPException(404, "saved sign-in not found")
    return Response(status_code=204)


@router.post("/threads/{thread_id}/browser/saved-sign-in")
async def api_browser_save_sign_in(
    thread_id: str, session: dict[str, Any] = SESSION_DEP
) -> sign_ins.SavedSignIn:
    """Save the sign-in on the page the caller is controlling, for reuse in their private threads."""
    metadata = await _writable(thread_id, session)
    return await _save_sign_in(thread_id, metadata, session["sub"])


async def _save_sign_in(
    thread_id: str, metadata: dict[str, JsonValue], login: str
) -> sign_ins.SavedSignIn:
    if not _owns_private_thread(metadata, login):
        raise HTTPException(403, "sign-ins can only be saved from a private thread you own")
    current = await manager.current(thread_id)
    if current is None or current.sandbox_id is None or not current.user_controls(login):
        raise HTTPException(409, "take control of the browser before saving a sign-in")
    try:
        async with engine.connected(current.sandbox_id) as sandbox:
            origin = await engine.current_origin(sandbox, current)
            endpoint = endpoint_of(origin) if origin else None
            if origin is None or endpoint is None or endpoint.key not in current.approved_endpoints:
                raise HTTPException(
                    409, "open a page on an approved external endpoint, then sign in there"
                )
            state = await engine.capture_sign_in(sandbox, current, origin)
    except engine.SandboxLostError as exc:
        raise HTTPException(409, "the browser's sandbox is gone") from exc
    except engine.EngineCommandError as exc:
        raise HTTPException(502, f"could not read the browser's sign-in: {exc}") from exc
    if not state.cookies and not state.local_storage:
        raise HTTPException(409, "this page has no sign-in to save; sign in first")
    if len(state.model_dump_json()) > sign_ins.MAX_STATE_CHARS:
        raise HTTPException(413, "this sign-in is too large to save")
    return await sign_ins.save(login, origin, state)
