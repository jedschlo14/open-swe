"""Browser calls queue per thread, and a scripted recording runs its steps as one clip."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError

from agent.browser import broker, engine, manager, policy
from agent.browser.broker import BrowserOutcome
from agent.browser.ops import (
    ActOp,
    ClickOp,
    FindOp,
    FlowStep,
    NavigateOp,
    PressOp,
    SnapshotOp,
    WaitOp,
    commands,
)


def test_find_runs_the_semantic_locator_command() -> None:
    fill = FindOp(by="placeholder", value="Message", do="fill", text="hello")
    role = FindOp(by="role", value="button", name="Send")

    assert commands(fill) == [["find", "placeholder", "Message", "fill", "hello"]]
    assert commands(role) == [["find", "role", "button", "click", "--name", "Send"]]


@pytest.mark.parametrize(
    "fields",
    [
        {"by": "text", "value": "-rf"},
        {"by": "text", "value": "Send", "name": "Send"},
        {"by": "text", "value": "Send", "do": "fill"},
        {"by": "text", "value": "Send", "text": "typed"},
    ],
)
def test_find_rejects_arguments_the_cli_would_misread(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ActOp).validate_python({"action": "find", **fields})


def test_a_find_click_on_a_sensitive_label_of_an_external_page_needs_approval() -> None:
    op = FindOp(by="role", value="button", name="Delete account")

    external = policy.action(op, page_url="https://example.com/", refs={}, target_label=None)
    local = policy.action(op, page_url="http://localhost:3000/", refs={}, target_label=None)

    assert external.verdict == "confirm"
    assert local.verdict == "allow"


def test_a_recording_step_cannot_be_a_dialog_or_a_nested_recording() -> None:
    adapter = TypeAdapter(FlowStep)

    with pytest.raises(ValidationError):
        adapter.validate_python({"action": "dialog", "accept": True})
    with pytest.raises(ValidationError):
        adapter.validate_python({"action": "record_start"})


async def test_parallel_calls_for_one_thread_run_one_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = 0
    peak = 0
    order: list[str] = []

    async def as_agent(thread_id, found, run):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        order.append(found)
        await asyncio.sleep(0.01)
        running -= 1
        return BrowserOutcome(status="ok")

    async def session_for(thread_id, op, workspace_slug):
        return op.action

    monkeypatch.setattr(broker, "_as_agent", as_agent)
    monkeypatch.setattr(broker, "_session_for", session_for)
    ops = [SnapshotOp(), ClickOp(x=1, y=1), WaitOp(milliseconds=5), PressOp(key="Enter")]

    await asyncio.gather(*(broker.execute("t1", op, workspace_slug=None) for op in ops))

    assert peak == 1
    assert order == ["snapshot", "click", "wait", "press"]


class _Recorder:
    def __init__(self, fail_on: str | None = None) -> None:
        self.events: list[str] = []
        self.fail_on = fail_on

    async def start(self, sandbox, session) -> None:
        self.events.append("start")

    async def discard(self, sandbox, session) -> bool:
        self.events.append("discard")
        return True

    async def stop(self, sandbox, session):
        self.events.append("stop")
        return SimpleNamespace(image=b"sheet", seconds=6.0)

    async def execute(self, sandbox, session, step, confirmation_id):
        self.events.append(step.action)
        if step.action == self.fail_on:
            return BrowserOutcome(status="error", message="no such element")
        return BrowserOutcome(status="ok")


def _install(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    async def as_agent(thread_id, found, run):
        return await run("sandbox", "session")

    monkeypatch.setattr(broker, "_as_agent", as_agent)
    monkeypatch.setattr(broker, "_execute", recorder.execute)
    monkeypatch.setattr(broker, "RECORDING_LEAD_IN_SECONDS", 0)
    monkeypatch.setattr(broker, "RECORDING_TAIL_SECONDS", 0)
    monkeypatch.setattr(manager, "current", AsyncMock(return_value=SimpleNamespace(active=True)))
    monkeypatch.setattr(engine, "start_recording", recorder.start)
    monkeypatch.setattr(engine, "discard_recording", recorder.discard)
    monkeypatch.setattr(engine, "stop_recording", recorder.stop)


STEPS: list[FlowStep] = [
    FindOp(by="label", value="Message", do="fill", text="draft"),
    ClickOp(x=10, y=20),
    NavigateOp(url="http://localhost:3000/"),
]


async def test_a_recording_runs_its_steps_between_one_start_and_one_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    _install(monkeypatch, recorder)

    outcome = await broker.record_flow("t1", STEPS, workspace_slug=None, pause_ms=0)

    assert outcome.status == "ok" and outcome.image_base64 is not None
    assert recorder.events == ["start", "find", "click", "navigate", "stop"]


async def test_a_failing_step_discards_the_recording_and_names_the_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder(fail_on="click")
    _install(monkeypatch, recorder)

    outcome = await broker.record_flow("t1", STEPS, workspace_slug=None, pause_ms=0)

    assert outcome.status == "error"
    assert "Step 2 (click)" in (outcome.message or "")
    assert recorder.events == ["start", "find", "click", "discard"]


async def test_a_long_wait_is_refused_before_recording_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    _install(monkeypatch, recorder)

    outcome = await broker.record_flow(
        "t1", [WaitOp(milliseconds=20_000)], workspace_slug=None, pause_ms=0
    )

    assert outcome.status == "error"
    assert recorder.events == []
