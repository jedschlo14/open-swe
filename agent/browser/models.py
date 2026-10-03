"""The browser session record and the states it moves through."""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue
from pydantic.alias_generators import to_camel

SessionState = Literal["starting", "ready", "stopping", "stopped", "failed"]
ACTIVE_STATES: tuple[SessionState, ...] = ("starting", "ready", "stopping")
FailureReason = Literal[
    "sandbox_lost", "sandbox_unsupported", "engine_missing", "egress_unavailable", "launch_failed"
]
StopReason = Literal["requested", "thread_closed", "idle_timeout", "sandbox_recreated"]
Controller = Literal["agent", "user"]

IDLE_WARNING_LEAD = timedelta(minutes=5)


class PageRef(BaseModel):
    """An element the last snapshot named, as the confirmation gate sees it."""

    role: str
    name: str


_API_CONFIG = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PendingConfirmation(BaseModel):
    """A sensitive action held until a thread writer approves exactly this operation."""

    model_config = _API_CONFIG

    confirmation_id: str
    operation: dict[str, JsonValue]
    reason: str
    description: str
    status: Literal["pending", "approved"]
    requested_at: datetime
    decided_by: str | None = None


class BrowserSession(BaseModel):
    """One ``browser_session`` row."""

    session_id: str
    thread_id: str
    sandbox_id: str | None
    state: SessionState
    failure_reason: FailureReason | None
    stop_reason: StopReason | None
    stream_port: int | None
    idle_timeout_seconds: int
    started_by: str
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    warned_at: datetime | None
    ended_at: datetime | None
    controller: Controller
    controller_login: str | None
    lease_epoch: int
    approved_endpoints: list[str]
    allowed_endpoints: list[str]
    page_refs: dict[str, PageRef] | None
    pending_confirmation: PendingConfirmation | None

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def expires_at(self) -> datetime:
        return self.last_activity_at + timedelta(seconds=self.idle_timeout_seconds)

    @property
    def daemon_session(self) -> str:
        """The ``agent-browser`` session name, unique per session so profiles never mix."""
        return f"osw-{self.session_id}"

    @property
    def network_namespace(self) -> str:
        """The network namespace the browser runs in; its only way out is the egress proxy."""
        return f"osw-{self.session_id}"


class BrowserSessionView(BaseModel):
    """What the dashboard shows for a thread's browser."""

    model_config = _API_CONFIG

    state: SessionState | None
    session_id: str | None = None
    failure_reason: FailureReason | None = None
    stop_reason: StopReason | None = None
    started_by: str | None = None
    started_at: datetime | None = None
    expires_at: datetime | None = None
    expiry_warning: bool = False
    supported: bool = True
    controller: Controller | None = None
    pending_confirmation: PendingConfirmation | None = None

    @classmethod
    def of(
        cls, session: BrowserSession | None, *, now: datetime, supported: bool
    ) -> BrowserSessionView:
        if session is None:
            return cls(state=None, supported=supported)
        expires_at = session.expires_at if session.state == "ready" else None
        return cls(
            state=session.state,
            session_id=session.session_id,
            failure_reason=session.failure_reason,
            stop_reason=session.stop_reason,
            started_by=session.started_by,
            started_at=session.created_at,
            expires_at=expires_at,
            expiry_warning=expires_at is not None and now >= expires_at - IDLE_WARNING_LEAD,
            supported=supported,
            controller=session.controller if session.active else None,
            pending_confirmation=session.pending_confirmation if session.active else None,
        )
