"""Browser session lifecycle against a real migrated schema.

Starts converge through the partial unique index and stops race through
conditional updates, so these run against PostgreSQL rather than a double.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import JsonValue
from sqlalchemy import text

from agent.browser import broker, engine, lease, live, manager, routes, store
from agent.browser.models import BrowserSession
from agent.browser.ops import ClickOp, NavigateOp, SnapshotOp
from agent.database import postgres

THREAD = "thread-1"


class FakeEngine:
    def __init__(self) -> None:
        self.launches = 0
        self.closes = 0
        self.commands: list[list[str]] = []
        self.on_command: Callable[[list[str]], Awaitable[None]] | None = None

    @asynccontextmanager
    async def connected(self, sandbox_id: str) -> AsyncIterator[str]:
        yield sandbox_id

    async def launch(self, sandbox: str, session: BrowserSession) -> int:
        self.launches += 1
        await asyncio.sleep(0.2)
        return 9222

    async def close(self, sandbox: str, session: BrowserSession) -> None:
        self.closes += 1

    async def run_command(
        self, sandbox: str, session: BrowserSession, args: list[str]
    ) -> dict[str, JsonValue]:
        self.commands.append(args)
        if self.on_command is not None:
            await self.on_command(args)
        if args[0] == "get":
            return {"url": "http://localhost:3000/settings", "title": "Settings"}
        if args[0] == "snapshot":
            return {"refs": {"e1": {"role": "button", "name": "Delete account"}}}
        return {}

    async def write_allowlist(
        self, sandbox: str, session: BrowserSession, endpoints: list[str]
    ) -> None:
        return None


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
    monkeypatch.setattr(manager, "_session_settings", AsyncMock(return_value=(3600, [])))
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
    monkeypatch.setattr(engine, "run_command", fake.run_command)
    monkeypatch.setattr(engine, "write_allowlist", fake.write_allowlist)
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


async def test_a_sensitive_click_waits_for_one_approval_from_a_thread_writer(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    await broker.execute(THREAD, NavigateOp(url="http://localhost:3000/"), workspace_slug=None)
    await broker.execute(THREAD, SnapshotOp(), workspace_slug=None)
    click = ClickOp(ref="e1")

    held = await broker.execute(THREAD, click, workspace_slug=None)
    assert held.status == "confirmation_required"
    assert held.confirmation_id is not None
    unapproved = await broker.execute(
        THREAD, click, workspace_slug=None, confirmation_id=held.confirmation_id
    )
    assert unapproved.status == "confirmation_required"
    assert ["click", "@e1"] not in sandbox.engine.commands

    pending = await store.active(THREAD)
    assert pending is not None and pending.pending_confirmation is not None
    thread = {"metadata": {"source": "dashboard", "visibility": "public", "owner_login": "alice"}}
    client = SimpleNamespace(threads=SimpleNamespace(get=AsyncMock(return_value=thread)))
    monkeypatch.setattr(routes, "langgraph_client", lambda: client)
    await routes.api_browser_confirm(
        THREAD,
        pending.pending_confirmation.confirmation_id,
        routes.ConfirmationDecision(approve=True),
        {"sub": "alice", "email": None},
    )
    confirmation_id = pending.pending_confirmation.confirmation_id

    done = await broker.execute(THREAD, click, workspace_slug=None, confirmation_id=confirmation_id)
    again = await broker.execute(
        THREAD, click, workspace_slug=None, confirmation_id=confirmation_id
    )

    assert done.status == "ok"
    assert again.status == "confirmation_required"
    assert sandbox.engine.commands.count(["click", "@e1"]) == 1


async def test_navigation_outside_the_sandbox_is_refused(sandbox: Sandbox) -> None:
    refused = await broker.execute(
        THREAD, NavigateOp(url="https://example.com/"), workspace_slug=None
    )

    assert refused.status == "refused"
    assert not any(args[0] == "open" for args in sandbox.engine.commands)


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


def test_a_view_only_viewer_cannot_send_input_to_the_browser() -> None:
    click = {"type": "input_mouse", "eventType": "mousePressed", "x": 4, "y": 4}

    assert live._viewer_message(json.dumps(click)) is None
    assert live._viewer_message(json.dumps({"type": "ack", "seq": 7})) == {"type": "ack", "seq": 7}


async def test_a_takeover_fences_out_the_agent_until_the_person_hands_back(
    sandbox: Sandbox,
) -> None:
    await broker.execute(THREAD, NavigateOp(url="http://localhost:3000/"), workspace_slug=None)
    await broker.execute(THREAD, SnapshotOp(), workspace_slug=None)
    taken = await lease.take_control(THREAD, "alice")
    gate = live._ControlGate(taken, "alice", AsyncMock(return_value="control"))

    blocked = await broker.execute(THREAD, ClickOp(ref="e1"), workspace_slug=None)
    assert blocked.status == "user_in_control"
    assert await gate.allows(True)

    await lease.hand_back(THREAD, "alice")
    stale = await broker.execute(THREAD, ClickOp(ref="e1"), workspace_slug=None)

    assert not await gate.allows(True)
    assert stale.status == "refused"
    assert stale.handback is not None
    assert (stale.handback.returned_by, stale.handback.url) == (
        "alice",
        "http://localhost:3000/settings",
    )
    assert ["click", "@e1"] not in sandbox.engine.commands


async def test_an_agent_action_interrupted_by_a_takeover_is_discarded(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = await broker.execute(
        THREAD, NavigateOp(url="http://localhost:3000/"), workspace_slug=None
    )
    assert started.status == "ok"
    monkeypatch.setattr(engine, "element_label", AsyncMock(return_value="Docs"))
    session = await store.active(THREAD)
    assert session is not None

    async def take_over_on_first_move(args: list[str]) -> None:
        if args[:2] == ["mouse", "move"]:
            await store.begin_takeover(session.session_id, "alice")

    sandbox.engine.on_command = take_over_on_first_move
    interrupted = await broker.execute(THREAD, ClickOp(x=10, y=10), workspace_slug=None)

    assert interrupted.status == "user_in_control"
    assert ["mouse", "down"] not in sandbox.engine.commands
    settled = await store.active(THREAD)
    assert settled is not None and settled.agent_inflight == 0
