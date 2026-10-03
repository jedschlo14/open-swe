"""Browser session lifecycle against a real migrated schema.

Starts converge through the partial unique index and stops race through
conditional updates, so these run against PostgreSQL rather than a double.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from agent.browser import engine, manager, routes, store
from agent.browser.models import BrowserSession
from agent.database import postgres

THREAD = "thread-1"


class FakeEngine:
    def __init__(self) -> None:
        self.launches = 0
        self.closes = 0

    @asynccontextmanager
    async def connected(self, sandbox_id: str) -> AsyncIterator[str]:
        yield sandbox_id

    async def launch(self, sandbox: str, session: BrowserSession) -> int:
        self.launches += 1
        await asyncio.sleep(0.2)
        return 9222

    async def close(self, sandbox: str, session: BrowserSession) -> None:
        self.closes += 1


@dataclass
class Sandbox:
    metadata: dict[str, str]
    engine: FakeEngine


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch, registry_db: None) -> Sandbox:
    bound = {"sandbox_id": "sb-1"}
    fake = FakeEngine()
    monkeypatch.setenv("SANDBOX_TYPE", "langsmith")
    monkeypatch.setattr(manager, "get_sandbox_metadata", AsyncMock(side_effect=lambda _: bound))
    monkeypatch.setattr(manager, "_idle_timeout_seconds", AsyncMock(return_value=3600))
    monkeypatch.setattr(manager, "_thread_closed", AsyncMock(return_value=False))
    monkeypatch.setattr(manager, "_START_POLL_SECONDS", 0.02)
    monkeypatch.setattr(manager.cron, "ensure_sweep_cron", AsyncMock(return_value="cron-1"))
    monkeypatch.setattr(
        "agent.sandboxes.lifecycle.ensure_sandbox_for_thread",
        AsyncMock(side_effect=lambda *_, **__: SimpleNamespace(id=bound["sandbox_id"])),
    )
    monkeypatch.setattr(engine, "connected", fake.connected)
    monkeypatch.setattr(engine, "launch", fake.launch)
    monkeypatch.setattr(engine, "close", fake.close)
    return Sandbox(metadata=bound, engine=fake)


async def test_concurrent_starts_launch_one_browser(sandbox: Sandbox) -> None:
    sessions = await asyncio.gather(
        *(
            manager.ensure_session(THREAD, started_by=login, workspace_slug=None)
            for login in ("alice", "bob", "carol")
        )
    )

    assert {session.session_id for session in sessions} == {sessions[0].session_id}
    assert {session.state for session in sessions} == {"ready"}
    assert sandbox.engine.launches == 1


async def test_a_lost_sandbox_fails_the_session_instead_of_relaunching(
    sandbox: Sandbox,
) -> None:
    started = await manager.ensure_session(THREAD, started_by="alice", workspace_slug=None)
    sandbox.metadata["sandbox_id"] = "sb-2"

    current = await manager.current(THREAD)

    assert current is not None
    assert current.session_id == started.session_id
    assert (current.state, current.failure_reason) == ("failed", "sandbox_lost")
    assert sandbox.engine.launches == 1


async def test_an_idle_session_is_stopped_by_the_sweep(sandbox: Sandbox) -> None:
    started = await manager.ensure_session(THREAD, started_by="alice", workspace_slug=None)
    async with postgres.transaction() as conn:
        await conn.execute(
            text(
                "UPDATE browser_session SET last_activity_at = clock_timestamp() - interval '2 hours'"
                " WHERE session_id = :session_id"
            ),
            {"session_id": started.session_id},
        )

    assert (await manager.sweep(THREAD))["status"] == "expired"
    ended = await store.latest(THREAD)
    assert ended is not None
    assert (ended.state, ended.stop_reason) == ("stopped", "idle_timeout")
    assert sandbox.engine.closes == 1


async def test_an_admin_can_watch_a_private_thread_but_not_start_its_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONFIGURED_ADMINS", "admin")
    thread = {"metadata": {"source": "dashboard", "visibility": "private", "owner_login": "owner"}}
    client = SimpleNamespace(threads=SimpleNamespace(get=AsyncMock(return_value=thread)))
    monkeypatch.setattr(routes, "langgraph_client", lambda: client)
    ensure = AsyncMock()
    monkeypatch.setattr(manager, "ensure_session", ensure)
    monkeypatch.setattr(manager, "current", AsyncMock(return_value=None))
    admin = {"sub": "admin", "email": None}

    status = await routes.api_browser_status(THREAD, admin)
    with pytest.raises(HTTPException) as refused:
        await routes.api_browser_start(THREAD, admin)

    assert status.state is None
    assert refused.value.status_code == 403
    ensure.assert_not_awaited()
