"""Executes typed browser operations for the agent, enforcing policy in the executor.

Every operation is checked here, never left to the caller: the lease (only the
current controller acts), the egress allowlist, and the confirmation gate for
sensitive actions, whose approvals only a thread writer can grant.
"""

import asyncio
import base64
import logging
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Literal

from langsmith.sandbox import AsyncSandbox
from pydantic import BaseModel, JsonValue

from agent.browser import engine, manager, policy, sign_ins, store
from agent.browser.models import BrowserSession, HandbackNotice, PageRef
from agent.browser.ops import (
    BrowserOp,
    ClickOp,
    FlowStep,
    NavigateOp,
    RecordStartOp,
    RecordStopOp,
    ScreenshotOp,
    SnapshotOp,
    WaitOp,
    commands,
)

logger = logging.getLogger(__name__)

OutcomeStatus = Literal[
    "ok",
    "error",
    "refused",
    "confirmation_required",
    "user_in_control",
    "no_session",
    "unavailable",
]


class BrowserOutcome(BaseModel):
    """What one operation did, in the shape the agent tools report."""

    status: OutcomeStatus
    message: str | None = None
    url: str | None = None
    title: str | None = None
    snapshot: str | None = None
    confirmation_id: str | None = None
    image_base64: str | None = None
    image_mime_type: str | None = None
    handback: HandbackNotice | None = None


_THREAD_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

RECORDING_LEAD_IN_SECONDS = 1.0
RECORDING_TAIL_SECONDS = 1.5
MAX_FLOW_WAIT_MS = 5_000


@asynccontextmanager
async def _one_at_a_time(thread_id: str) -> AsyncIterator[None]:
    """Queue a thread's browser calls in arrival order, so parallel tool calls never interleave."""
    lock = _THREAD_LOCKS.get(thread_id)
    if lock is None:
        lock = _THREAD_LOCKS[thread_id] = asyncio.Lock()
    async with lock:
        yield


def _text(data: dict[str, JsonValue], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _refs(data: dict[str, JsonValue]) -> dict[str, PageRef]:
    raw = data.get("refs")
    if not isinstance(raw, dict):
        return {}
    refs: dict[str, PageRef] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            role, name = value.get("role"), value.get("name")
            refs[key] = PageRef(
                role=role if isinstance(role, str) else "",
                name=name if isinstance(name, str) else "",
            )
    return refs


def _not_ready(session: BrowserSession) -> BrowserOutcome:
    if session.state == "failed":
        return BrowserOutcome(
            status="unavailable",
            message=f"The browser session failed ({session.failure_reason}).",
        )
    return BrowserOutcome(status="unavailable", message=f"The browser is {session.state}.")


async def _session_for(
    thread_id: str, op: BrowserOp, workspace_slug: str | None
) -> BrowserSession | BrowserOutcome:
    if isinstance(op, NavigateOp):
        try:
            return await manager.ensure_session(
                thread_id, started_by="agent", workspace_slug=workspace_slug
            )
        except manager.BrowserUnsupportedError as exc:
            return BrowserOutcome(status="unavailable", message=str(exc))
    session = await manager.current(thread_id)
    if session is None or not session.active:
        return BrowserOutcome(
            status="no_session", message="No browser is open. Navigate to a page first."
        )
    return session


async def execute(
    thread_id: str,
    op: BrowserOp,
    *,
    workspace_slug: str | None,
    confirmation_id: str | None = None,
) -> BrowserOutcome:
    """Run ``op`` on the thread's browser as the agent."""
    async with _one_at_a_time(thread_id):
        found = await _session_for(thread_id, op, workspace_slug)
        if isinstance(found, BrowserOutcome):
            return found
        return await _as_agent(
            thread_id,
            found,
            lambda sandbox, session: _execute(sandbox, session, op, confirmation_id),
        )


async def record_flow(
    thread_id: str,
    steps: Sequence[FlowStep],
    *,
    workspace_slug: str | None,
    pause_ms: int,
) -> BrowserOutcome:
    """Record ``steps`` run back to back as one short clip, discarding it if any step fails."""
    if any(isinstance(step, WaitOp) and step.milliseconds > MAX_FLOW_WAIT_MS for step in steps):
        return BrowserOutcome(
            status="error",
            message=f"A wait in a recording is at most {MAX_FLOW_WAIT_MS} ms; keep clips short.",
        )
    async with _one_at_a_time(thread_id):
        found = await manager.current(thread_id)
        if found is None or not found.active:
            return BrowserOutcome(
                status="no_session", message="No browser is open. Navigate to a page first."
            )

        async def run(sandbox: AsyncSandbox, session: BrowserSession) -> BrowserOutcome:
            await engine.start_recording(sandbox, session)
            try:
                await asyncio.sleep(RECORDING_LEAD_IN_SECONDS)
                for number, step in enumerate(steps, start=1):
                    outcome = await _execute(sandbox, session, step, None)
                    if outcome.status != "ok":
                        await engine.discard_recording(sandbox, session)
                        return _failed_step(number, step, outcome)
                    await asyncio.sleep(pause_ms / 1000)
                await asyncio.sleep(RECORDING_TAIL_SECONDS)
            except Exception:
                await engine.discard_recording(sandbox, session)
                raise
            return await _stopped_recording(sandbox, session)

        return await _as_agent(thread_id, found, run)


def _failed_step(number: int, step: FlowStep, outcome: BrowserOutcome) -> BrowserOutcome:
    reason = outcome.message or outcome.status
    if outcome.status == "confirmation_required":
        reason = f"{reason} needs a person's approval; do it with browser_act outside a recording"
    return BrowserOutcome(
        status="refused" if outcome.status == "confirmation_required" else outcome.status,
        message=f"Step {number} ({step.action}) failed: {reason}. The recording was discarded.",
    )


async def _as_agent(
    thread_id: str,
    found: BrowserSession,
    run: Callable[[AsyncSandbox, BrowserSession], Awaitable[BrowserOutcome]],
) -> BrowserOutcome:
    """Run ``run`` on the session's browser under the agent's lease, fenced against takeover."""
    if found.state != "ready" or found.sandbox_id is None:
        return _not_ready(found)
    session = await store.claim_agent_action(found.session_id)
    if session is None or session.sandbox_id is None:
        latest = await store.latest(thread_id)
        if latest is not None and latest.active and latest.controller == "user":
            return BrowserOutcome(
                status="user_in_control",
                message=(
                    f"{latest.controller_login or 'A person'} is controlling the browser. "
                    "Wait for them to hand it back."
                ),
            )
        return _not_ready(latest or found)
    try:
        async with engine.connected(session.sandbox_id) as sandbox:
            outcome = await run(sandbox, session)
    except engine.SandboxLostError:
        await manager.fail(session, "sandbox_lost")
        return BrowserOutcome(
            status="unavailable", message="The sandbox the browser ran in is gone."
        )
    except engine.EngineCommandError as exc:
        outcome = BrowserOutcome(status="error", message=str(exc))
    except _Superseded:
        outcome = _TAKEN_OVER
    finally:
        await store.release_agent_action(session.session_id)
    if outcome.status != "user_in_control" and not await _still_held(session):
        outcome = _TAKEN_OVER
    if session.handback_notice is not None:
        outcome = outcome.model_copy(update={"handback": session.handback_notice})
    return outcome


class _Superseded(Exception):
    """A person took control while this action was running."""


_TAKEN_OVER = BrowserOutcome(
    status="user_in_control",
    message=(
        "A person took control of the browser while this action ran, so its result was "
        "discarded. Wait for them to hand it back."
    ),
)


async def _still_held(session: BrowserSession) -> bool:
    return await store.lease_epoch(session.session_id) == session.lease_epoch


async def _decide(sandbox: AsyncSandbox, session: BrowserSession, op: BrowserOp) -> policy.Decision:
    if isinstance(op, NavigateOp):
        return policy.navigation(op, session.approved_endpoints)
    if isinstance(op, SnapshotOp | ScreenshotOp | RecordStartOp | RecordStopOp):
        return policy.Allow()
    label = None
    if isinstance(op, ClickOp) and op.x is not None and op.y is not None:
        label = await engine.element_label(sandbox, session, op.x, op.y)
    page = await engine.run_command(sandbox, session, ["get", "url"])
    return policy.action(
        op, page_url=_text(page, "url") or "", refs=session.page_refs or {}, target_label=label
    )


async def _execute(
    sandbox: AsyncSandbox,
    session: BrowserSession,
    op: BrowserOp,
    confirmation_id: str | None,
) -> BrowserOutcome:
    decision = await _decide(sandbox, session, op)
    if isinstance(decision, policy.Refuse):
        return BrowserOutcome(status="refused", message=decision.reason)
    operation = op.model_dump(mode="json")
    if isinstance(decision, policy.Confirm):
        approved = confirmation_id is not None and await store.consume_confirmation(
            session.session_id, confirmation_id, operation
        )
        if not approved:
            pending = await store.request_confirmation(
                session.session_id,
                operation=operation,
                reason=decision.reason,
                description=_describe(op),
            )
            if pending is None:
                return _not_ready(session)
            logger.info(
                "Browser action awaits confirmation",
                extra={"browser_session_id": session.session_id, "browser_action": op.action},
            )
            return BrowserOutcome(
                status="confirmation_required",
                confirmation_id=pending.confirmation_id,
                message=decision.reason,
            )
    if decision.endpoints:
        widened = await store.allow_endpoints(session.session_id, decision.endpoints)
        if widened is None:
            return _not_ready(session)
        await engine.write_allowlist(sandbox, widened, widened.allowed_endpoints)
    logger.info(
        "Browser action",
        extra={"browser_session_id": session.session_id, "browser_action": op.action},
    )
    if isinstance(op, ScreenshotOp):
        image = await engine.screenshot(sandbox, session, image_format=op.format)
        return BrowserOutcome(
            status="ok",
            image_base64=base64.b64encode(image).decode(),
            image_mime_type="image/png" if op.format == "png" else "image/jpeg",
        )
    if isinstance(op, RecordStartOp):
        await engine.start_recording(sandbox, session)
        return BrowserOutcome(
            status="ok",
            message=(
                f"Recording. It keeps the first {engine.MAX_RECORDING_SECONDS} seconds and "
                "is discarded if a person takes control. Stop it with browser_record_stop."
            ),
        )
    if isinstance(op, RecordStopOp):
        return await _stopped_recording(sandbox, session)
    recording = await engine.recording_active(sandbox, session)
    if recording:
        await engine.keep_recording_mask(sandbox, session)
    data: dict[str, JsonValue] = {}
    for index, args in enumerate(commands(op)):
        if index and not await _still_held(session):
            raise _Superseded
        data = await engine.run_command(sandbox, session, args)
    if recording:
        await engine.keep_recording_mask(sandbox, session)
    if isinstance(op, SnapshotOp):
        await store.record_refs(session.session_id, _refs(data))
        return BrowserOutcome(
            status="ok", url=_text(data, "origin"), snapshot=_text(data, "snapshot")
        )
    if isinstance(op, NavigateOp):
        return BrowserOutcome(status="ok", url=_text(data, "url"), title=_text(data, "title"))
    return BrowserOutcome(status="ok")


async def _stopped_recording(sandbox: AsyncSandbox, session: BrowserSession) -> BrowserOutcome:
    sheet = await engine.stop_recording(sandbox, session)
    if sheet is None:
        return BrowserOutcome(
            status="error",
            message=(
                "No recording is in progress. One is discarded when a person takes "
                "control of the browser."
            ),
        )
    return BrowserOutcome(
        status="ok",
        message=(
            f"Recorded {sheet.seconds:.0f} seconds; the image is one frame per second. "
            "Check it, then publish with browser_publish_recording."
        ),
        image_base64=base64.b64encode(sheet.image).decode(),
        image_mime_type="image/jpeg",
    )


class RecordingExport(BaseModel):
    """A finished recording encoded for publishing."""

    data: bytes
    recording_format: engine.RecordingFormat


async def export_recording(
    thread_id: str, recording_format: engine.RecordingFormat
) -> tuple[BrowserOutcome, RecordingExport | None]:
    """Encode the thread's finished recording as ``recording_format``."""
    exported: list[RecordingExport] = []

    async def run(sandbox: AsyncSandbox, session: BrowserSession) -> BrowserOutcome:
        data = await engine.export_recording(sandbox, session, recording_format)
        exported.append(RecordingExport(data=data, recording_format=recording_format))
        return BrowserOutcome(status="ok")

    async with _one_at_a_time(thread_id):
        found = await manager.current(thread_id)
        if found is None or not found.active:
            return BrowserOutcome(status="no_session", message="No browser is open."), None
        outcome = await _as_agent(thread_id, found, run)
    return outcome, exported[0] if exported and outcome.status == "ok" else None


async def restore_sign_in(
    thread_id: str, origin: str, *, owner: str, workspace_slug: str | None
) -> BrowserOutcome:
    """Restore ``owner``'s saved sign-in for ``origin`` into the thread's browser."""
    target = sign_ins.canonical_origin(origin)
    endpoint = policy.endpoint_of(target) if target else None
    if target is None or endpoint is None:
        return BrowserOutcome(
            status="refused", message="Saved sign-ins are for external http(s) origins only."
        )
    saved = await sign_ins.load(owner, target)
    if saved is None:
        available = sign_ins.origins(await sign_ins.list_for(owner))
        return BrowserOutcome(
            status="error",
            message=f"No saved sign-in for {target}. Saved: {', '.join(available) or 'none'}.",
        )
    record, state = saved
    if record.status == "expired":
        return _EXPIRED
    async with _one_at_a_time(thread_id):
        try:
            found = await manager.ensure_session(
                thread_id, started_by="agent", workspace_slug=workspace_slug
            )
        except manager.BrowserUnsupportedError as exc:
            return BrowserOutcome(status="unavailable", message=str(exc))
        if endpoint.key not in found.approved_endpoints:
            return BrowserOutcome(
                status="refused", message=f"{endpoint.host} is no longer an approved endpoint."
            )

        async def run(sandbox: AsyncSandbox, session: BrowserSession) -> BrowserOutcome:
            if not await engine.apply_sign_in(sandbox, session, target, state):
                await sign_ins.mark_expired(record.sign_in_id)
                return _EXPIRED
            await sign_ins.mark_used(record.sign_in_id)
            logger.info(
                "Browser sign-in restored",
                extra={"browser_session_id": session.session_id, "sign_in_id": record.sign_in_id},
            )
            return BrowserOutcome(
                status="ok", message=f"Signed in to {target} with a saved sign-in."
            )

        return await _as_agent(thread_id, found, run)


_EXPIRED = BrowserOutcome(
    status="error",
    message="The saved sign-in expired. Ask the user to sign in again by taking control, then save it.",
)


def _describe(op: BrowserOp) -> str:
    """A short, human-readable account of the held action for the confirmation card."""
    fields = op.model_dump(exclude={"action"}, exclude_none=True, exclude_defaults=True)
    details = ", ".join(f"{key}={value!r}" for key, value in fields.items())
    return f"{op.action}({details})"
