"""The live view's transport: what a slow viewer drops and what the sandbox helper emits."""

import asyncio
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from agent.browser.live.actions import with_scheme
from agent.browser.live.video import Outbox

_HELPER = Path(__file__).parents[2] / "agent" / "resources" / "browser" / "live_helper.py"


@pytest.fixture(scope="module")
def helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("live_helper", _HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unit(*nal_types: int) -> bytes:
    return b"".join(
        b"\x00\x00\x00\x01" + bytes([0x60 | nal_type, 0x42, 0xC0, 0x28, 1, 2])
        for nal_type in nal_types
    )


async def _drain(outbox: Outbox) -> list[str | bytes]:
    items: list[str | bytes] = []
    while True:
        try:
            items.append(await asyncio.wait_for(outbox.next(), 0.01))
        except TimeoutError:
            return items


async def test_a_slow_viewer_skips_to_the_next_key_frame_but_never_loses_messages() -> None:
    outbox = Outbox()
    outbox.frame(b"\x01key")
    for index in range(10):
        outbox.frame(b"\x00" + bytes([index]))
    outbox.message({"type": "page", "url": "http://localhost:3000/"})
    outbox.frame(b"\x00late")
    outbox.frame(b"\x01fresh")

    assert await _drain(outbox) == [
        json.dumps({"type": "page", "url": "http://localhost:3000/"}),
        b"\x01fresh",
    ]


async def test_a_new_viewer_gets_no_delta_frames_before_a_key_frame() -> None:
    outbox = Outbox()
    outbox.frame(b"\x00delta")
    outbox.frame(b"\x01key")

    assert await _drain(outbox) == [b"\x01key"]


def test_access_units_split_at_delimiters_and_the_last_stays_buffered(helper: ModuleType) -> None:
    first, second, third = _unit(9, 7, 8, 5), _unit(9, 1), _unit(9, 1)
    buffer = bytearray(first + second + third)

    assert list(helper.split_access_units(buffer)) == [first, second]
    assert bytes(buffer) == third
    assert helper.nal_types(first) == [9, 7, 8, 5]
    assert helper.codec_bytes(first) == bytes([0x42, 0xC0, 0x28])


def test_browser_keys_map_to_x_keysyms(helper: ModuleType) -> None:
    assert helper.keysym_for("a", "KeyA") == ord("a")
    assert helper.keysym_for("Enter", "Enter") == 0xFF0D
    assert helper.keysym_for("Shift", "ShiftRight") == 0xFFE2
    assert helper.keysym_for("世", "KeyX") == 0x1000000 | ord("世")
    assert helper.keysym_for("Dead", "KeyE") is None


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
