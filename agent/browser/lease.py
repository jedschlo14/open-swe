"""Takeover and handback of a thread browser's single lease.

Takeover first fences the agent out (the epoch bump makes the broker discard
whatever an in-flight action does next), then waits a bounded time for actions
already running to settle before the person's input is let through. Handback
revokes the person's input, forgets page refs so the agent cannot act on stale
ones, and leaves the agent a notice of the page it is getting back.
Disconnecting changes nothing; stopping or failing the session ends the lease.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from pydantic import JsonValue

from agent.browser import engine, manager, store
from agent.browser.models import BrowserSession, HandbackNotice

logger = logging.getLogger(__name__)

SETTLE_TIMEOUT = timedelta(seconds=10)
_SETTLE_POLL_SECONDS = 0.25


class LeaseConflictError(RuntimeError):
    """The lease is not in the state the transition starts from."""


async def _ready(thread_id: str) -> BrowserSession:
    session = await manager.current(thread_id)
    if session is None or session.state != "ready":
        raise LeaseConflictError("The browser is not running.")
    return session


async def take_control(thread_id: str, login: str) -> BrowserSession:
    session = await _ready(thread_id)
    taken = await store.begin_takeover(session.session_id, login)
    if taken is None:
        holder = session.controller_login or "Someone"
        raise LeaseConflictError(
            f"{holder} is already in control."
            if session.controller == "user"
            else "Control is changing hands; try again."
        )
    deadline = datetime.now(UTC) + SETTLE_TIMEOUT
    current: BrowserSession | None = taken
    while current is not None and current.agent_inflight > 0 and datetime.now(UTC) < deadline:
        await asyncio.sleep(_SETTLE_POLL_SECONDS)
        current = await store.latest(thread_id)
    if current is not None and current.agent_inflight > 0:
        # Its results are already fenced off by the epoch; waiting longer only blocks the person.
        logger.info(
            "Took browser control before the agent's action settled",
            extra={"browser_session_id": session.session_id},
        )
    await _discard_recording(taken)
    logger.info("Browser control taken", extra={"browser_session_id": session.session_id})
    return await store.finish_handoff(session.session_id) or taken


async def _discard_recording(session: BrowserSession) -> None:
    """End any recording before a person's input is let through, so it is never captured."""
    if session.sandbox_id is None:
        return
    try:
        async with engine.connected(session.sandbox_id) as sandbox:
            if await engine.discard_recording(sandbox, session):
                logger.info(
                    "Discarded the recording for a takeover",
                    extra={"browser_session_id": session.session_id},
                )
    except engine.EngineCommandError, engine.SandboxLostError:
        logger.warning(
            "Could not discard the recording for a takeover",
            exc_info=True,
            extra={"browser_session_id": session.session_id},
        )


def _text(data: dict[str, JsonValue], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


async def _page(session: BrowserSession) -> tuple[str | None, str | None]:
    if session.sandbox_id is None:
        return None, None
    try:
        async with engine.connected(session.sandbox_id) as sandbox:
            url_data, title_data = await asyncio.gather(
                engine.run_command(sandbox, session, ["get", "url"]),
                engine.run_command(sandbox, session, ["get", "title"]),
            )
            url, title = _text(url_data, "url"), _text(title_data, "title")
    except engine.EngineCommandError, engine.SandboxLostError:
        logger.warning(
            "Could not read the page for the handback notice",
            exc_info=True,
            extra={"browser_session_id": session.session_id},
        )
        return None, None
    return url, title


async def hand_back(thread_id: str, login: str) -> BrowserSession:
    session = await _ready(thread_id)
    returning = await store.begin_handback(session.session_id)
    if returning is None:
        raise LeaseConflictError("The agent already has control.")
    url, title = await _page(returning)
    notice = HandbackNotice(returned_by=login, url=url, title=title)
    logger.info("Browser control handed back", extra={"browser_session_id": session.session_id})
    return await store.finish_handoff(session.session_id, notice=notice) or returning
