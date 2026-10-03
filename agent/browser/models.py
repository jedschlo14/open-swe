"""The browser session record and the states it moves through."""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

SessionState = Literal["starting", "ready", "stopping", "stopped", "failed"]
ACTIVE_STATES: tuple[SessionState, ...] = ("starting", "ready", "stopping")
FailureReason = Literal["sandbox_lost", "sandbox_unsupported", "engine_missing", "launch_failed"]
StopReason = Literal["requested", "thread_closed", "idle_timeout", "sandbox_recreated"]

IDLE_WARNING_LEAD = timedelta(minutes=5)


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


class BrowserSessionView(BaseModel):
    """What the dashboard shows for a thread's browser."""

    state: SessionState | None
    session_id: str | None = None
    failure_reason: FailureReason | None = None
    stop_reason: StopReason | None = None
    started_by: str | None = None
    started_at: datetime | None = None
    expires_at: datetime | None = None
    expiry_warning: bool = False
    supported: bool = True

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
        )
