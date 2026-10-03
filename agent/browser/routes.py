"""Dashboard API for a thread's browser session.

Reading the session's status needs thread-read access; starting, stopping,
keeping it alive, and confirming a held action need thread-write access, checked
on every request.
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, JsonValue

from agent.browser import manager, store
from agent.browser.models import BrowserSession, BrowserSessionView
from agent.dashboard.deps import SESSION_DEP
from agent.threads.summary import assert_thread_readable, thread_is_postable
from agent.utils.json_types import thread_metadata
from agent.utils.thread_ops import langgraph_client

router = APIRouter(tags=["browser"])


class ConfirmationDecision(BaseModel):
    approve: bool


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


def _view(session: BrowserSession | None, metadata: dict[str, JsonValue]) -> BrowserSessionView:
    return BrowserSessionView.of(
        session, now=datetime.now(UTC), supported=manager.browser_supported(metadata)
    )


@router.get("/threads/{thread_id}/browser")
async def api_browser_status(
    thread_id: str, session: dict[str, Any] = SESSION_DEP
) -> BrowserSessionView:
    metadata = await _readable(thread_id, session)
    return _view(await manager.current(thread_id), metadata)


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
    return _view(started, metadata)


@router.post("/threads/{thread_id}/browser/stop")
async def api_browser_stop(
    thread_id: str, session: dict[str, Any] = SESSION_DEP
) -> BrowserSessionView:
    metadata = await _writable(thread_id, session)
    stopped = await manager.stop_session(thread_id, "requested")
    return _view(stopped or await manager.current(thread_id), metadata)


@router.post("/threads/{thread_id}/browser/keepalive")
async def api_browser_keepalive(
    thread_id: str, session: dict[str, Any] = SESSION_DEP
) -> BrowserSessionView:
    metadata = await _writable(thread_id, session)
    kept = await manager.keep_alive(thread_id)
    return _view(kept or await manager.current(thread_id), metadata)


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
    return _view(decided, metadata)
