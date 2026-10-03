"""Starts, checks, and stops a thread's browser session.

A session is bound to the sandbox it launched in. When the thread's sandbox
changes or disappears the session fails as ``sandbox_lost``; it is never
relaunched behind anyone's back, because the page state is gone with it.
"""

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal

from langgraph_sdk.errors import NotFoundError
from langsmith.sandbox import AsyncSandbox
from pydantic import JsonValue

from agent.bridge.store import Bridge
from agent.browser import cron, engine, store
from agent.browser.models import IDLE_WARNING_LEAD, BrowserSession, FailureReason, StopReason
from agent.config import ENV
from agent.utils.json_types import thread_metadata
from agent.utils.thread_ops import langgraph_client

logger = logging.getLogger(__name__)

_START_WAIT = timedelta(seconds=150)
_START_POLL_SECONDS = 1.0
_STALE_START = timedelta(minutes=5)
_STALE_STOP = timedelta(minutes=2)
_STALE_HANDOFF = timedelta(minutes=1)

SweepOutcome = Literal[
    "no_session", "stale_start", "stale_stop", "thread_closed", "lost", "expired", "warned", "ok"
]


class BrowserUnsupportedError(RuntimeError):
    """The thread's sandbox cannot host a browser session."""


def _sandbox_id(metadata: Mapping[str, JsonValue]) -> str | None:
    value = metadata.get("sandbox_id")
    return value if isinstance(value, str) and value and value != "__creating__" else None


def browser_supported(metadata: Mapping[str, JsonValue]) -> bool:
    """Cloud threads only: a bridged thread's sandbox is the user's own machine."""
    return (
        ENV.SANDBOX_TYPE.get() == "langsmith" and Bridge.bridge_id_of(_sandbox_id(metadata)) is None
    )


async def get_sandbox_metadata(thread_id: str) -> dict[str, JsonValue]:
    """The live thread's metadata, which names the sandbox it is bound to now."""
    return thread_metadata(await langgraph_client().threads.get(thread_id))


async def _session_settings(workspace_slug: str | None) -> tuple[int, list[str]]:
    """The workspace's idle timeout in seconds and its approved external endpoints."""
    from agent.dashboard.workspace_settings import get_workspace_settings

    settings = await get_workspace_settings(workspace_slug)
    return settings.browser_idle_timeout_minutes * 60, settings.browser_approved_dev_endpoints


async def ensure_session(
    thread_id: str, *, started_by: str, workspace_slug: str | None
) -> BrowserSession:
    """Return the thread's live session, starting one when there is none.

    Concurrent callers converge on one session: the partial unique index lets
    exactly one insert win, and everyone else waits for that launch to settle.
    """
    metadata = await get_sandbox_metadata(thread_id)
    if not browser_supported(metadata):
        raise BrowserUnsupportedError("Browser sessions need a cloud sandbox")
    idle_timeout_seconds, approved_endpoints = await _session_settings(workspace_slug)
    session, created = await store.claim_start(
        thread_id,
        started_by=started_by,
        idle_timeout_seconds=idle_timeout_seconds,
        approved_endpoints=approved_endpoints,
    )
    if not created:
        if session.state == "starting":
            return await _await_start(session)
        if session.state == "ready":
            return await check_session(session)
        return session
    try:
        await cron.ensure_sweep_cron(thread_id)
    except Exception:
        # The daemon's own idle shutdown still bounds the browser; only the record lingers.
        logger.warning(
            "Could not schedule the browser idle sweep",
            exc_info=True,
            extra={"browser_session_id": session.session_id},
        )
    return await _launch(session, workspace_slug)


async def _await_start(session: BrowserSession) -> BrowserSession:
    deadline = datetime.now(UTC) + _START_WAIT
    current: BrowserSession | None = session
    while current is not None and current.state == "starting" and datetime.now(UTC) < deadline:
        await asyncio.sleep(_START_POLL_SECONDS)
        current = await store.latest(session.thread_id)
    return current or session


async def _launch(session: BrowserSession, workspace_slug: str | None) -> BrowserSession:
    from agent.sandboxes.lifecycle import ensure_sandbox_for_thread

    try:
        backend = await ensure_sandbox_for_thread(session.thread_id, workspace_slug=workspace_slug)
    except Exception:
        logger.warning(
            "Could not get a sandbox for the browser",
            exc_info=True,
            extra={"browser_session_id": session.session_id},
        )
        return await fail(session, "launch_failed")
    sandbox_id = backend.id
    if Bridge.bridge_id_of(sandbox_id) is not None:
        return await fail(session, "sandbox_unsupported")
    bound = await store.bind_sandbox(session.session_id, sandbox_id)
    if bound is None:
        return await store.latest(session.thread_id) or session
    try:
        async with engine.connected(sandbox_id) as sandbox:
            try:
                port = await engine.launch(sandbox, bound)
            except Exception:
                await _discard(sandbox, bound)
                raise
            ready = await store.mark_ready(session.session_id, stream_port=port)
            if ready is None:
                # Stopped while launching: whoever stopped it may have closed too early.
                await engine.close(sandbox, bound)
                return await store.latest(session.thread_id) or bound
            return ready
    except engine.SandboxLostError:
        return await fail(bound, "sandbox_lost")
    except engine.EgressUnavailableError:
        logger.warning(
            "The sandbox cannot isolate the browser's network",
            exc_info=True,
            extra={"browser_session_id": session.session_id},
        )
        return await fail(bound, "egress_unavailable")
    except engine.EngineMissingError:
        logger.warning(
            "agent-browser is missing or too old in the sandbox",
            exc_info=True,
            extra={"browser_session_id": session.session_id},
        )
        return await fail(bound, "engine_missing")
    except Exception:
        logger.warning(
            "Browser launch failed",
            exc_info=True,
            extra={"browser_session_id": session.session_id},
        )
        return await fail(bound, "launch_failed")


async def _discard(sandbox: AsyncSandbox, session: BrowserSession) -> None:
    try:
        await engine.close(sandbox, session)
    except Exception:
        logger.warning(
            "Could not clean up after a failed browser launch",
            exc_info=True,
            extra={"browser_session_id": session.session_id},
        )


async def fail(session: BrowserSession, reason: FailureReason) -> BrowserSession:
    logger.info(
        "Browser session failed",
        extra={"browser_session_id": session.session_id, "browser_failure": reason},
    )
    return await store.fail(session.session_id, reason) or (
        await store.latest(session.thread_id) or session
    )


async def check_session(session: BrowserSession) -> BrowserSession:
    """Fail a ready session whose thread no longer points at the sandbox it launched in."""
    if session.state != "ready":
        return session
    try:
        current = _sandbox_id(await get_sandbox_metadata(session.thread_id))
    except NotFoundError:
        current = None
    if current is not None and current == session.sandbox_id:
        return session
    return await fail(session, "sandbox_lost")


async def current(thread_id: str) -> BrowserSession | None:
    """The thread's latest session, after checking that a ready one still has its sandbox."""
    session = await store.latest(thread_id)
    return await check_session(session) if session is not None else None


async def keep_alive(thread_id: str) -> BrowserSession | None:
    session = await store.active(thread_id)
    if session is None or session.state != "ready":
        return session
    return await store.touch(session.session_id)


async def stop_session(thread_id: str, reason: StopReason) -> BrowserSession | None:
    """Stop the thread's active session and delete its files; ``None`` when none was active."""
    session = await store.active(thread_id)
    if session is None:
        return None
    if session.state == "stopping":
        return session
    stopping = await store.begin_stop(session.session_id, reason)
    if stopping is None:
        return await store.latest(thread_id)
    if stopping.sandbox_id is not None and reason != "sandbox_recreated":
        try:
            async with engine.connected(stopping.sandbox_id) as sandbox:
                await engine.close(sandbox, stopping)
        except engine.SandboxLostError:
            logger.info(
                "Browser sandbox was already gone at stop",
                extra={"browser_session_id": stopping.session_id},
            )
        except Exception:
            logger.warning(
                "Could not close the browser in its sandbox",
                exc_info=True,
                extra={"browser_session_id": stopping.session_id},
            )
    logger.info(
        "Browser session stopped",
        extra={"browser_session_id": stopping.session_id, "browser_stop_reason": reason},
    )
    return await store.finish_stop(stopping.session_id) or stopping


async def stop_browser_for_thread(thread_id: str, reason: StopReason) -> None:
    """Best-effort stop for thread lifecycle hooks, which must not fail over the browser."""
    from agent.database import postgres

    if not postgres.configured():
        return
    try:
        await stop_session(thread_id, reason)
    except Exception:
        logger.warning(
            "Could not stop the thread's browser",
            exc_info=True,
            extra={"agent_thread_id": thread_id, "browser_stop_reason": reason},
        )


async def _thread_closed(thread_id: str) -> bool:
    try:
        thread = await langgraph_client().threads.get(thread_id)
    except NotFoundError:
        return True
    metadata = thread.get("metadata") if isinstance(thread, Mapping) else None
    return isinstance(metadata, Mapping) and metadata.get("resolved") is True


async def sweep(thread_id: str) -> dict[str, JsonValue]:
    """One idle-sweep tick for a thread; deletes the thread's cron once nothing is active."""
    session = await store.active(thread_id)
    if session is None:
        await cron.delete_sweep_cron(thread_id)
        return {"status": "no_session"}
    outcome = await _sweep_session(session, datetime.now(UTC))
    return {"status": outcome, "browser_session_id": session.session_id}


async def _sweep_session(session: BrowserSession, now: datetime) -> SweepOutcome:
    if session.state == "starting":
        if now - session.updated_at < _STALE_START:
            return "ok"
        await fail(session, "launch_failed")
        return "stale_start"
    if session.state == "stopping":
        if now - session.updated_at < _STALE_STOP:
            return "ok"
        await store.finish_stop(session.session_id)
        return "stale_stop"
    if session.handoff != "none" and now - session.updated_at >= _STALE_HANDOFF:
        await store.finish_handoff(session.session_id)
    if await _thread_closed(session.thread_id):
        await stop_session(session.thread_id, "thread_closed")
        return "thread_closed"
    if (await check_session(session)).state != "ready":
        return "lost"
    if now >= session.expires_at:
        await stop_session(session.thread_id, "idle_timeout")
        return "expired"
    if now >= session.expires_at - IDLE_WARNING_LEAD and session.warned_at is None:
        await store.mark_warned(session.session_id)
        return "warned"
    return "ok"
