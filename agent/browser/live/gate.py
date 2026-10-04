"""Per-viewer checks that decide whether an input event may reach the browser."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from agent.browser import store
from agent.browser.models import BrowserSession
from agent.dashboard.oauth import BrowserTicketRole

logger = logging.getLogger(__name__)

type Authorizer = Callable[[], Awaitable[BrowserTicketRole | None]]

_MOVE_CHECK_SECONDS = 2.0
_ACTIVITY_EVERY_SECONDS = 30.0
_ACCESS_CHECK_SECONDS = 1.0
_VIEWPORT_REST_SECONDS = 0.3
_VIEWPORT_RETRY_SECONDS = 3.0


class ControlGate:
    """Decides, per input event, whether this viewer still holds the browser's lease.

    Pointer moves arrive by the dozen per second and reuse a check a moment old;
    every press, release, wheel tick, and key reads the lease again. Thread
    access lives in another service and is rechecked at most once a second.
    """

    def __init__(self, session: BrowserSession, login: str, authorize: Authorizer) -> None:
        self._session = session
        self._login = login
        self._authorize = authorize
        self._checked_at = float("-inf")
        self._access_checked_at = float("-inf")
        self._may_control = False
        self._allowed = False
        self._active_at = float("-inf")

    async def allows(self, discrete: bool) -> bool:
        now = time.monotonic()
        if discrete or now - self._checked_at >= _MOVE_CHECK_SECONDS:
            current = await store.latest(self._session.thread_id)
            holds_lease = (
                current is not None
                and current.session_id == self._session.session_id
                and current.user_controls(self._login)
            )
            if holds_lease and now - self._access_checked_at >= _ACCESS_CHECK_SECONDS:
                self._may_control = await self._authorize() == "control"
                self._access_checked_at = now
            self._allowed = holds_lease and self._may_control
            self._checked_at = now
        if self._allowed and discrete and now - self._active_at >= _ACTIVITY_EVERY_SECONDS:
            self._active_at = now
            await store.touch(self._session.session_id)
        return self._allowed

    async def allows_resize(self) -> bool:
        """Whether this viewer may resize the page: they hold it, or the agent is between actions."""
        current = await store.latest(self._session.thread_id)
        if current is None or current.session_id != self._session.session_id:
            return False
        agent_idle = (
            current.state == "ready"
            and current.controller == "agent"
            and current.handoff == "none"
            and current.agent_inflight == 0
        )
        return (current.user_controls(self._login) or agent_idle) and (
            await self._authorize() == "control"
        )


class ViewportSync:
    """Applies a controller's latest panel size to the page.

    A size that is refused, because someone else holds the page or the agent is
    mid-action, stays pending and is retried, so it lands once the page is free.
    """

    def __init__(
        self,
        session: BrowserSession,
        gate: ControlGate,
        apply: Callable[[int, int], Awaitable[None]],
    ) -> None:
        self._session = session
        self._gate = gate
        self._apply = apply
        self._target: tuple[int, int] | None = None
        self._wake = asyncio.Event()

    def request(self, size: tuple[int, int]) -> None:
        self._target = size
        self._wake.set()

    async def run(self) -> None:
        while True:
            if self._target is None:
                await self._wake.wait()
                self._wake.clear()
                continue
            target = self._target
            if not await self._gate.allows_resize():
                try:
                    await asyncio.wait_for(self._wake.wait(), _VIEWPORT_RETRY_SECONDS)
                except TimeoutError:
                    continue
                self._wake.clear()
                continue
            if self._target == target:
                self._target = None
            try:
                await self._apply(*target)
            except OSError:
                logger.warning(
                    "Browser resize failed",
                    exc_info=True,
                    extra={"browser_session_id": self._session.session_id},
                )
            await asyncio.sleep(_VIEWPORT_REST_SECONDS)
