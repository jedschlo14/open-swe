"""The live view's wire: what a viewer may send and what the bridge replays to a late viewer."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from agent.browser.live import messages
from agent.browser.live.actions import with_scheme

_BRIDGE = Path(__file__).parents[2] / "agent" / "resources" / "browser" / "mirror_bridge.py"


@pytest.fixture(scope="module")
def bridge() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mirror_bridge", _BRIDGE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Writer:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.frames.append(data)


def _event(kind: int, **data: object) -> str:
    return json.dumps({"type": kind, "data": data, "timestamp": 1})


def test_a_late_viewer_replays_from_the_last_snapshot(bridge: ModuleType) -> None:
    mirror = bridge.Mirror("", "")
    meta, snapshot = _event(4, href="http://localhost/"), _event(2, node={})
    mirror.ingest("n4" + meta)
    mirror.ingest("n2" + snapshot)
    mirror.ingest("n3" + _event(3, source=0))
    mirror.ingest("c2" + _event(2, node={"fresh": True}))
    mirror.ingest("n3" + _event(3, source=1))
    viewer = bridge.Viewer(_Writer())

    mirror.send_state(viewer)

    sent = [frame for frame in viewer.writer.frames if frame[:1] == bridge.KIND_EVENTS]
    events = json.loads(sent[0][5:])
    assert [event["type"] for event in events] == [4, 2, 3]
    assert events[1]["data"] == {"node": {"fresh": True}}


def test_a_press_carries_the_element_it_landed_on() -> None:
    press = messages.parse(
        json.dumps(
            {
                "type": "mouse",
                "action": "down",
                "x": 10,
                "y": 20,
                "anchor": {"id": 7, "fx": 0.25, "fy": 0.5},
            }
        )
    )
    assert isinstance(press, messages.MouseMessage)
    assert press.model_dump(exclude_none=True)["anchor"] == {"id": 7, "fx": 0.25, "fy": 0.5}
    assert messages.parse(json.dumps({"type": "choice", "id": -1, "value": "x"})) is None


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("localhost:3000/docs", "http://localhost:3000/docs"),
        ("127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("example.com", "https://example.com"),
        ("http://localhost:5173", "http://localhost:5173"),
    ],
)
def test_the_address_bar_adds_a_scheme(typed: str, expected: str) -> None:
    assert with_scheme(typed) == expected


class _Cdp:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []

    async def send(
        self, method: str, params: dict[str, object] | None, session: str | None
    ) -> dict[str, object]:
        self.sent.append((method, params or {}))
        return {}


@pytest.mark.asyncio
async def test_pasted_text_reaches_the_page_in_order_with_keys(bridge: ModuleType) -> None:
    mirror = bridge.Mirror("", "")
    cdp = _Cdp()
    mirror.cdp = cdp
    await mirror.input({"type": "paste", "text": "--user@example.com"})
    await mirror.input({"type": "key", "action": "down", "key": "Tab", "code": "Tab"})

    assert [method for method, _ in cdp.sent] == ["Input.insertText", "Input.dispatchKeyEvent"]
    assert cdp.sent[0][1] == {"text": "--user@example.com"}
