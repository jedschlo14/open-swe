"""Executes typed browser operations for the agent, enforcing policy in the executor.

Every operation is checked here, never left to the caller: the lease (only the
current controller acts), the egress allowlist, and the confirmation gate for
sensitive actions, whose approvals only a thread writer can grant.
"""

import logging
from typing import Literal

from langsmith.sandbox import AsyncSandbox
from pydantic import BaseModel, JsonValue

from agent.browser import engine, manager, policy, store
from agent.browser.models import BrowserSession, HandbackNotice, PageRef
from agent.browser.ops import BrowserOp, ClickOp, NavigateOp, ScreenshotOp, SnapshotOp, commands

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
    handback: HandbackNotice | None = None


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
    found = await _session_for(thread_id, op, workspace_slug)
    if isinstance(found, BrowserOutcome):
        return found
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
            outcome = await _execute(sandbox, session, op, confirmation_id)
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
    if isinstance(op, SnapshotOp | ScreenshotOp):
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
        return BrowserOutcome(status="ok", image_base64=await engine.screenshot(sandbox, session))
    data: dict[str, JsonValue] = {}
    for index, args in enumerate(commands(op)):
        if index and not await _still_held(session):
            raise _Superseded
        data = await engine.run_command(sandbox, session, args)
    if isinstance(op, SnapshotOp):
        await store.record_refs(session.session_id, _refs(data))
        return BrowserOutcome(
            status="ok", url=_text(data, "origin"), snapshot=_text(data, "snapshot")
        )
    if isinstance(op, NavigateOp):
        return BrowserOutcome(status="ok", url=_text(data, "url"), title=_text(data, "title"))
    return BrowserOutcome(status="ok")


def _describe(op: BrowserOp) -> str:
    """A short, human-readable account of the held action for the confirmation card."""
    fields = op.model_dump(exclude={"action"}, exclude_none=True, exclude_defaults=True)
    details = ", ".join(f"{key}={value!r}" for key, value in fields.items())
    return f"{op.action}({details})"
