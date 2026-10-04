"""The live view's translation layer between the dashboard viewer and neko."""

import base64
import hashlib
import hmac

import pytest

from agent.browser import engine, neko, turn
from agent.browser.live import messages
from agent.browser.live.actions import with_scheme
from agent.browser.live.keysyms import keysym_for
from agent.browser.live.webrtc import WheelAccumulator, to_neko, to_viewer
from agent.browser.models import BrowserSession
from agent.browser.neko_client import _profile
from agent.config import ENV

SCREEN = (2160, 1344)
RELAY = "candidate:1 1 udp 41885439 203.0.113.9 50000 typ relay raddr 0.0.0.0 rport 0"
HOST = "candidate:2 1 udp 2130706431 127.0.0.1 59047 typ host"


@pytest.fixture(autouse=True)
def _turn_and_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "BROWSER_TURN_URLS", "turn:turn.example.com:3478, turns:turn.example.com:443"
    )
    monkeypatch.setenv("BROWSER_TURN_SECRET", "relay-secret")
    monkeypatch.setenv("DASHBOARD_JWT_SECRET", "dashboard-secret")


def _session(session_id: str = "000000000000004e") -> BrowserSession:
    return BrowserSession.model_construct(session_id=session_id)


def test_the_viewer_only_learns_relay_candidates_and_its_own_turn_credential() -> None:
    ice = turn.mint("abc", ttl_seconds=60)
    candidate = {"event": "signal/candidate", "payload": {"candidate": HOST, "sdpMid": "0"}}

    assert to_viewer(candidate, ice) is None
    relayed = to_viewer({**candidate, "payload": {"candidate": RELAY, "sdpMid": "0"}}, ice)
    assert relayed is not None and relayed["event"] == "candidate"

    provide = to_viewer(
        {
            "event": "signal/provide",
            "payload": {"sdp": "v=0", "iceservers": [{"urls": ["stun:x"]}]},
        },
        ice,
    )
    assert provide == {
        "type": "signal",
        "event": "provide",
        "sdp": "v=0",
        "iceServers": [ice.as_json()],
        "relayOnly": True,
    }
    roster = {"event": "system/init", "payload": {"sessions": {"someone": {}}}}
    assert to_viewer(roster, ice) is None


def test_turn_credentials_are_scoped_to_the_session_and_expire() -> None:
    ice = turn.mint("abc", ttl_seconds=60, now=1_000)
    expires, session_id = ice.username.split(":")

    assert (expires, session_id) == ("1060", "abc")
    expected = hmac.new(b"relay-secret", ice.username.encode(), hashlib.sha1).digest()
    assert ice.credential == base64.b64encode(expected).decode()
    assert ice.urls == ("turn:turn.example.com:3478", "turns:turn.example.com:443")


def test_without_turn_nothing_is_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BROWSER_TURN_SECRET")

    assert not turn.configured()
    with pytest.raises(RuntimeError):
        turn.mint("abc", ttl_seconds=60)
    assert ENV.BROWSER_TURN_URLS.get()


def test_only_a_control_ticket_gets_a_member_that_may_host() -> None:
    assert _profile("control")["can_host"] is True
    assert _profile("view")["can_host"] is False
    assert _profile("control")["is_admin"] is False


def test_pointer_events_are_scaled_to_the_display_and_kept_on_it() -> None:
    wheel = WheelAccumulator()
    move = messages.MouseMessage(type="mouse", action="move", x=100, y=50)
    edge = messages.MouseMessage(type="mouse", action="down", x=9_000, y=9_000, button=2)

    assert to_neko(move, SCREEN, wheel) == [("control/move", {"x": 150, "y": 75})]
    assert to_neko(edge, SCREEN, wheel) == [
        ("control/buttondown", {"x": 2159, "y": 1343, "code": 3})
    ]


def test_wheel_deltas_add_up_to_whole_clicks_and_scroll_the_page_the_same_way() -> None:
    wheel = WheelAccumulator()
    scroll = messages.MouseMessage(type="mouse", action="wheel", x=10, y=10, dy=40)

    assert to_neko(scroll, SCREEN, wheel) == []
    [_, (event, payload)] = to_neko(scroll, SCREEN, wheel)
    assert event == "control/scroll" and payload["delta_y"] == -1


def test_keys_become_keysyms_and_characters_the_display_lacks_are_not_mapped() -> None:
    assert keysym_for("a", "KeyA") == ord("a")
    assert keysym_for("Enter", "Enter") == 0xFF0D
    assert keysym_for("Shift", "ShiftRight") == 0xFFE2
    assert keysym_for("世", "KeyX") is None
    assert keysym_for("Dead", "KeyE") is None
    typed = messages.KeyMessage(type="key", action="down", key="Enter", code="Enter")
    assert to_neko(typed, SCREEN, WheelAccumulator()) == [("control/keydown", {"keysym": 0xFF0D})]


def test_every_session_has_its_own_display_and_admin_token() -> None:
    first, second = _session("000000000000004e"), _session("000000000000004f")

    assert neko.display_name(first) != neko.display_name(second)
    assert neko.api_token(first) != neko.api_token(second)
    assert neko.api_token(first) == neko.api_token(first)


def test_the_display_is_sized_on_the_grid_neko_can_set() -> None:
    width, height = neko.screen_for(1013, 601, engine.RENDER_SCALE)

    assert width % 8 == 0
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
