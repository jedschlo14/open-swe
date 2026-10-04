"""The live view's translation layer between the dashboard viewer and the display helper."""

import asyncio

import pytest

from agent.browser import display, engine
from agent.browser.live import messages
from agent.browser.live.actions import with_scheme
from agent.browser.live.h264 import AccessUnitSplitter
from agent.browser.live.keysyms import keysym_for
from agent.browser.live.stream import VideoBacklog, WheelAccumulator, to_helper
from agent.browser.models import BrowserSession

SCREEN = (2160, 1344)
AUD = b"\x00\x00\x00\x01\x09\x10"
SPS = b"\x00\x00\x00\x01\x67\x64\x00\x2a\xac"
KEYFRAME = AUD + SPS + b"\x00\x00\x00\x01\x68\xee\x3c\x80" + b"\x00\x00\x01\x65\x88\x84"
DELTA = AUD + b"\x00\x00\x01\x41\x9a\x24"


@pytest.fixture(autouse=True)
def _dashboard_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_JWT_SECRET", "dashboard-secret")


def _session(session_id: str = "000000000000004e") -> BrowserSession:
    return BrowserSession.model_construct(session_id=session_id)


def test_the_stream_is_cut_into_frames_wherever_the_chunks_fall() -> None:
    stream = KEYFRAME + DELTA + DELTA + KEYFRAME
    splitter = AccessUnitSplitter()
    units = [
        unit
        for index in range(0, len(stream), 7)
        for unit in splitter.feed(stream[index : index + 7])
    ]

    assert [unit.keyframe for unit in units] == [True, False, False]
    assert b"".join(unit.data for unit in units) == KEYFRAME + DELTA + DELTA
    assert units[0].codec == "avc1.64002A"
    assert units[1].codec is None
    assert [unit.keyframe for unit in splitter.feed(AUD)] == [True]


def test_a_slow_viewer_skips_to_the_next_keyframe_instead_of_falling_behind() -> None:
    async def scenario() -> list[bool]:
        splitter = AccessUnitSplitter()
        frames = splitter.feed(KEYFRAME + DELTA * 5 + KEYFRAME + DELTA + AUD)
        backlog = VideoBacklog(limit=3)
        for frame in frames:
            backlog.push(frame)
        return [(await backlog.pop()).keyframe for _ in range(2)]

    assert asyncio.run(scenario()) == [True, False]


def test_a_new_encode_discards_frames_of_the_old_one() -> None:
    async def scenario() -> bool:
        backlog = VideoBacklog()
        splitter = AccessUnitSplitter()
        for frame in splitter.feed(KEYFRAME + DELTA + AUD):
            backlog.push(frame)
        backlog.restart()
        for frame in splitter.feed(DELTA + KEYFRAME + AUD):
            backlog.push(frame)
        return (await backlog.pop()).keyframe

    assert asyncio.run(scenario()) is True


def test_pointer_events_are_scaled_to_the_display_and_kept_on_it() -> None:
    wheel = WheelAccumulator()
    move = messages.MouseMessage(type="mouse", action="move", x=100, y=50)
    edge = messages.MouseMessage(type="mouse", action="down", x=9_000, y=9_000, button=2)

    assert to_helper(move, SCREEN, wheel) == [{"type": "move", "x": 150, "y": 75}]
    assert to_helper(edge, SCREEN, wheel) == [
        {"type": "button", "x": 2159, "y": 1343, "code": 3, "down": True}
    ]


def test_wheel_deltas_add_up_to_whole_clicks_and_scroll_the_page_the_same_way() -> None:
    wheel = WheelAccumulator()
    scroll = messages.MouseMessage(type="mouse", action="wheel", x=10, y=10, dy=40)

    assert to_helper(scroll, SCREEN, wheel) == []
    [event] = to_helper(scroll, SCREEN, wheel)
    assert event["type"] == "scroll" and event["up"] == -1
    assert to_helper(scroll, SCREEN, wheel) == []


def test_keys_become_keysyms_and_characters_the_display_lacks_are_not_mapped() -> None:
    assert keysym_for("a", "KeyA") == ord("a")
    assert keysym_for("Enter", "Enter") == 0xFF0D
    assert keysym_for("Shift", "ShiftRight") == 0xFFE2
    assert keysym_for("世", "KeyX") is None
    assert keysym_for("Dead", "KeyE") is None
    typed = messages.KeyMessage(type="key", action="down", key="Enter", code="Enter")
    assert to_helper(typed, SCREEN, WheelAccumulator()) == [
        {"type": "key", "keysym": 0xFF0D, "down": True}
    ]


def test_every_session_has_its_own_display_and_helper_token() -> None:
    first, second = _session("000000000000004e"), _session("000000000000004f")

    assert display.display_name(first) != display.display_name(second)
    assert display.token(first) != display.token(second)
    assert display.token(first) == display.token(first)


def test_the_display_is_sized_on_an_even_grid() -> None:
    width, height = display.screen_for(1013, 601, engine.RENDER_SCALE)

    assert width % 2 == 0 and height % 2 == 0
    assert (width, height) == (round(1008 * 1.5), round(592 * 1.5))


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
