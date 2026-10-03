"""Every read and write of the ``browser_session`` table.

Each transition is a conditional ``UPDATE`` on the state it leaves, so two
replicas racing the same transition agree on one winner without holding locks
across sandbox calls.
"""

import uuid
from collections.abc import Mapping

from sqlalchemy import text

from agent.browser.models import BrowserSession, FailureReason, StopReason
from agent.database import postgres

_COLUMNS = (
    "session_id, thread_id, sandbox_id, state, failure_reason, stop_reason, stream_port,"
    " idle_timeout_seconds, started_by, created_at, updated_at, last_activity_at, warned_at,"
    " ended_at"
)
_ACTIVE = "state IN ('starting', 'ready', 'stopping')"


async def _one(statement: str, parameters: Mapping[str, object]) -> BrowserSession | None:
    async with postgres.transaction() as conn:
        row = (await conn.execute(text(statement), parameters)).mappings().first()
    return BrowserSession.model_validate(dict(row)) if row is not None else None


async def latest(thread_id: str) -> BrowserSession | None:
    """The thread's most recent session, active or ended."""
    return await _one(
        f"""
        SELECT {_COLUMNS} FROM browser_session
        WHERE thread_id = :thread_id
        ORDER BY ({_ACTIVE}) DESC, created_at DESC
        LIMIT 1
        """,
        {"thread_id": thread_id},
    )


async def active(thread_id: str) -> BrowserSession | None:
    return await _one(
        f"SELECT {_COLUMNS} FROM browser_session WHERE thread_id = :thread_id AND {_ACTIVE}",
        {"thread_id": thread_id},
    )


async def claim_start(
    thread_id: str, *, started_by: str, idle_timeout_seconds: int
) -> tuple[BrowserSession, bool]:
    """Insert a ``starting`` session, or return the active one; ``True`` when inserted."""
    inserted = await _one(
        f"""
        INSERT INTO browser_session (session_id, thread_id, state, idle_timeout_seconds, started_by)
        VALUES (:session_id, :thread_id, 'starting', :idle_timeout_seconds, :started_by)
        ON CONFLICT (thread_id) WHERE {_ACTIVE} DO NOTHING
        RETURNING {_COLUMNS}
        """,
        {
            "session_id": uuid.uuid4().hex[:16],
            "thread_id": thread_id,
            "idle_timeout_seconds": idle_timeout_seconds,
            "started_by": started_by,
        },
    )
    if inserted is not None:
        return inserted, True
    existing = await active(thread_id)
    if existing is None:
        # The conflicting session ended between the insert and this read.
        return await claim_start(
            thread_id, started_by=started_by, idle_timeout_seconds=idle_timeout_seconds
        )
    return existing, False


async def bind_sandbox(session_id: str, sandbox_id: str) -> BrowserSession | None:
    return await _one(
        f"""
        UPDATE browser_session SET sandbox_id = :sandbox_id, updated_at = clock_timestamp()
        WHERE session_id = :session_id AND state = 'starting'
        RETURNING {_COLUMNS}
        """,
        {"session_id": session_id, "sandbox_id": sandbox_id},
    )


async def mark_ready(session_id: str, *, stream_port: int) -> BrowserSession | None:
    return await _one(
        f"""
        UPDATE browser_session
        SET state = 'ready', stream_port = :stream_port, updated_at = clock_timestamp(),
            last_activity_at = clock_timestamp()
        WHERE session_id = :session_id AND state = 'starting'
        RETURNING {_COLUMNS}
        """,
        {"session_id": session_id, "stream_port": stream_port},
    )


async def fail(session_id: str, reason: FailureReason) -> BrowserSession | None:
    """Fail a session that is still active; ``None`` when it already ended."""
    return await _one(
        f"""
        UPDATE browser_session
        SET state = 'failed', failure_reason = :reason, updated_at = clock_timestamp(),
            ended_at = clock_timestamp()
        WHERE session_id = :session_id AND {_ACTIVE}
        RETURNING {_COLUMNS}
        """,
        {"session_id": session_id, "reason": reason},
    )


async def begin_stop(session_id: str, reason: StopReason) -> BrowserSession | None:
    """Move a starting or ready session to ``stopping``; ``None`` when another stop won."""
    return await _one(
        f"""
        UPDATE browser_session
        SET state = 'stopping', stop_reason = :reason, updated_at = clock_timestamp()
        WHERE session_id = :session_id AND state IN ('starting', 'ready')
        RETURNING {_COLUMNS}
        """,
        {"session_id": session_id, "reason": reason},
    )


async def finish_stop(session_id: str) -> BrowserSession | None:
    return await _one(
        f"""
        UPDATE browser_session
        SET state = 'stopped', updated_at = clock_timestamp(), ended_at = clock_timestamp()
        WHERE session_id = :session_id AND state = 'stopping'
        RETURNING {_COLUMNS}
        """,
        {"session_id": session_id},
    )


async def touch(session_id: str) -> BrowserSession | None:
    """Restart a ready session's idle clock."""
    return await _one(
        f"""
        UPDATE browser_session
        SET last_activity_at = clock_timestamp(), warned_at = NULL, updated_at = clock_timestamp()
        WHERE session_id = :session_id AND state = 'ready'
        RETURNING {_COLUMNS}
        """,
        {"session_id": session_id},
    )


async def mark_warned(session_id: str) -> BrowserSession | None:
    """Record the expiry warning once; ``None`` when it was already recorded."""
    return await _one(
        f"""
        UPDATE browser_session SET warned_at = clock_timestamp(), updated_at = clock_timestamp()
        WHERE session_id = :session_id AND state = 'ready' AND warned_at IS NULL
        RETURNING {_COLUMNS}
        """,
        {"session_id": session_id},
    )
