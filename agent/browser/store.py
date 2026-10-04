"""Every read and write of the ``browser_session`` table.

Each transition is a conditional ``UPDATE`` on the state it leaves, so two
replicas racing the same transition agree on one winner without holding locks
across sandbox calls.
"""

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from pydantic import JsonValue
from sqlalchemy import text

from agent.browser.models import (
    BrowserSession,
    FailureReason,
    HandbackNotice,
    PageRef,
    PendingConfirmation,
    StopReason,
)
from agent.database import postgres

_COLUMNS = (
    "session_id, thread_id, sandbox_id, state, failure_reason, stop_reason, stream_port,"
    " idle_timeout_seconds, started_by, created_at, updated_at, last_activity_at, warned_at,"
    " ended_at, controller, controller_login, lease_epoch, approved_endpoints,"
    " allowed_endpoints, page_refs, pending_confirmation, handoff, agent_inflight,"
    " handback_notice"
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
    thread_id: str,
    *,
    started_by: str,
    idle_timeout_seconds: int,
    approved_endpoints: Sequence[str] = (),
) -> tuple[BrowserSession, bool]:
    """Insert a ``starting`` session, or return the active one; ``True`` when inserted."""
    inserted = await _one(
        f"""
        INSERT INTO browser_session
            (session_id, thread_id, state, idle_timeout_seconds, started_by, approved_endpoints)
        VALUES
            (:session_id, :thread_id, 'starting', :idle_timeout_seconds, :started_by,
             :approved_endpoints)
        ON CONFLICT (thread_id) WHERE {_ACTIVE} DO NOTHING
        RETURNING {_COLUMNS}
        """,
        {
            "session_id": uuid.uuid4().hex[:16],
            "thread_id": thread_id,
            "idle_timeout_seconds": idle_timeout_seconds,
            "started_by": started_by,
            "approved_endpoints": list(approved_endpoints),
        },
    )
    if inserted is not None:
        return inserted, True
    existing = await active(thread_id)
    if existing is None:
        # The conflicting session ended between the insert and this read.
        return await claim_start(
            thread_id,
            started_by=started_by,
            idle_timeout_seconds=idle_timeout_seconds,
            approved_endpoints=approved_endpoints,
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


async def mark_ready(session_id: str, *, stream_port: int | None) -> BrowserSession | None:
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


async def claim_agent_action(session_id: str) -> BrowserSession | None:
    """Start an agent action under the lease; ``None`` unless the agent holds it, settled.

    The returned row's ``lease_epoch`` fences the action: a takeover bumps it, and
    the broker drops whatever the action does after that. ``handback_notice`` is
    handed over exactly once.
    """
    async with postgres.transaction() as conn:
        held = (
            (
                await conn.execute(
                    text(
                        f"""
                        SELECT {_COLUMNS} FROM browser_session
                        WHERE session_id = :session_id AND state = 'ready'
                            AND controller = 'agent' AND handoff = 'none'
                        FOR UPDATE
                        """
                    ),
                    {"session_id": session_id},
                )
            )
            .mappings()
            .first()
        )
        if held is None:
            return None
        await conn.execute(
            text(
                """
                UPDATE browser_session
                SET last_activity_at = clock_timestamp(), warned_at = NULL,
                    updated_at = clock_timestamp(), agent_inflight = agent_inflight + 1,
                    handback_notice = NULL
                WHERE session_id = :session_id
                """
            ),
            {"session_id": session_id},
        )
    return BrowserSession.model_validate(dict(held))


async def release_agent_action(session_id: str) -> None:
    await _one(
        f"""
        UPDATE browser_session SET agent_inflight = greatest(agent_inflight - 1, 0)
        WHERE session_id = :session_id
        RETURNING {_COLUMNS}
        """,
        {"session_id": session_id},
    )


async def lease_epoch(session_id: str) -> int | None:
    session = await _one(
        f"SELECT {_COLUMNS} FROM browser_session WHERE session_id = :session_id",
        {"session_id": session_id},
    )
    return session.lease_epoch if session is not None and session.state == "ready" else None


async def begin_takeover(session_id: str, login: str) -> BrowserSession | None:
    """Give ``login`` the lease and fence out the agent; ``None`` unless the agent held it."""
    return await _one(
        f"""
        UPDATE browser_session
        SET controller = 'user', controller_login = :login, lease_epoch = lease_epoch + 1,
            handoff = 'takeover', pending_confirmation = NULL, updated_at = clock_timestamp()
        WHERE session_id = :session_id AND state = 'ready' AND controller = 'agent'
            AND handoff = 'none'
        RETURNING {_COLUMNS}
        """,
        {"session_id": session_id, "login": login},
    )


async def begin_handback(session_id: str) -> BrowserSession | None:
    """Revoke the person's input and forget page refs; ``None`` unless a person held the lease."""
    return await _one(
        f"""
        UPDATE browser_session
        SET controller = 'agent', lease_epoch = lease_epoch + 1, handoff = 'handback',
            page_refs = NULL, pending_confirmation = NULL, updated_at = clock_timestamp()
        WHERE session_id = :session_id AND state = 'ready' AND controller = 'user'
            AND handoff = 'none'
        RETURNING {_COLUMNS}
        """,
        {"session_id": session_id},
    )


async def finish_handoff(
    session_id: str, *, notice: HandbackNotice | None = None
) -> BrowserSession | None:
    return await _one(
        f"""
        UPDATE browser_session
        SET handoff = 'none',
            controller_login = CASE WHEN controller = 'agent' THEN NULL ELSE controller_login END,
            handback_notice = COALESCE(CAST(:notice AS jsonb), handback_notice),
            updated_at = clock_timestamp()
        WHERE session_id = :session_id AND handoff <> 'none'
        RETURNING {_COLUMNS}
        """,
        {"session_id": session_id, "notice": notice.model_dump_json() if notice else None},
    )


async def allow_endpoints(session_id: str, endpoints: Sequence[str]) -> BrowserSession | None:
    return await _one(
        f"""
        UPDATE browser_session
        SET allowed_endpoints = ARRAY(
                SELECT DISTINCT unnest(allowed_endpoints || CAST(:endpoints AS text[]))
                ORDER BY 1
            ),
            updated_at = clock_timestamp()
        WHERE session_id = :session_id AND state = 'ready'
        RETURNING {_COLUMNS}
        """,
        {"session_id": session_id, "endpoints": list(endpoints)},
    )


async def record_refs(session_id: str, refs: Mapping[str, PageRef]) -> None:
    await _one(
        f"""
        UPDATE browser_session SET page_refs = CAST(:refs AS jsonb)
        WHERE session_id = :session_id AND state = 'ready'
        RETURNING {_COLUMNS}
        """,
        {
            "session_id": session_id,
            "refs": json.dumps({key: ref.model_dump() for key, ref in refs.items()}),
        },
    )


async def request_confirmation(
    session_id: str, *, operation: dict[str, JsonValue], reason: str, description: str
) -> PendingConfirmation | None:
    """Hold ``operation`` for a person's approval, replacing any earlier request."""
    pending = PendingConfirmation(
        confirmation_id=uuid.uuid4().hex[:12],
        operation=operation,
        reason=reason,
        description=description,
        status="pending",
        requested_at=datetime.now(UTC),
    )
    updated = await _one(
        f"""
        UPDATE browser_session SET pending_confirmation = CAST(:pending AS jsonb),
            updated_at = clock_timestamp()
        WHERE session_id = :session_id AND state = 'ready'
        RETURNING {_COLUMNS}
        """,
        {"session_id": session_id, "pending": pending.model_dump_json()},
    )
    return pending if updated is not None else None


async def decide_confirmation(
    session_id: str, confirmation_id: str, *, approve: bool, login: str
) -> BrowserSession | None:
    """Approve or deny a pending request; ``None`` when no such request is pending."""
    return await _one(
        f"""
        UPDATE browser_session
        SET pending_confirmation = CASE WHEN :approve
                THEN pending_confirmation
                    || jsonb_build_object('status', 'approved', 'decided_by', CAST(:login AS text))
                ELSE NULL END,
            updated_at = clock_timestamp()
        WHERE session_id = :session_id AND state = 'ready'
            AND pending_confirmation ->> 'confirmation_id' = :confirmation_id
            AND pending_confirmation ->> 'status' = 'pending'
        RETURNING {_COLUMNS}
        """,
        {
            "session_id": session_id,
            "confirmation_id": confirmation_id,
            "approve": approve,
            "login": login,
        },
    )


async def consume_confirmation(
    session_id: str, confirmation_id: str, operation: dict[str, JsonValue]
) -> bool:
    """Spend an approval on exactly the operation it was granted for, once."""
    consumed = await _one(
        f"""
        UPDATE browser_session SET pending_confirmation = NULL, updated_at = clock_timestamp()
        WHERE session_id = :session_id AND state = 'ready'
            AND pending_confirmation ->> 'confirmation_id' = :confirmation_id
            AND pending_confirmation ->> 'status' = 'approved'
            AND pending_confirmation -> 'operation' = CAST(:operation AS jsonb)
        RETURNING {_COLUMNS}
        """,
        {
            "session_id": session_id,
            "confirmation_id": confirmation_id,
            "operation": json.dumps(operation),
        },
    )
    return consumed is not None
