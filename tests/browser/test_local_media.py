"""Live view media without a TURN relay: who may use it, and the datagrams that cross the tunnel."""

import asyncio
import importlib.util
import struct
from importlib import resources
from types import ModuleType

import pytest

from agent.browser import neko
from agent.browser.live import local_media
from agent.browser.live.webrtc import to_viewer

HOST = "candidate:1 1 udp 2130706431 127.0.0.1 52002 typ host"
OFFER = (
    "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
    "a=ice-lite\r\n"
    "a=candidate:1 1 udp 2130706431 127.0.0.1 52002 typ host\r\n"
    "a=candidate:1 1 udp 2130706431 127.0.0.1 52002 typ host\r\n"
    "a=candidate:2 1 tcp 1 10.0.0.5 9 typ host tcptype passive\r\n"
    "a=candidate:3 1 udp 1694498815 203.0.113.9 40000 typ srflx raddr 0.0.0.0 rport 0\r\n"
    "a=end-of-candidates\r\n"
)


@pytest.mark.parametrize(
    ("client", "headers", "local"),
    [
        ("127.0.0.1", {"host": "localhost:2024"}, True),
        ("::1", {"host": "[::1]:2024"}, True),
        ("127.0.0.1", {"host": "127.0.0.1:2024", "x-forwarded-for": "127.0.0.1"}, True),
        ("127.0.0.1", {"host": "open-swe.example.com"}, False),
        ("127.0.0.1", {"host": "localhost:2024", "x-forwarded-for": "198.51.100.4"}, False),
        ("198.51.100.4", {"host": "localhost:2024"}, False),
        (None, {"host": "localhost:2024"}, False),
    ],
)
def test_only_a_browser_on_the_servers_own_machine_may_use_the_local_bridge(
    client: str | None, headers: dict[str, str], local: bool
) -> None:
    assert local_media.is_local_viewer(client, headers) is local


def test_the_viewer_is_offered_one_loopback_udp_candidate_at_the_servers_port() -> None:
    localized = local_media.localize_sdp(OFFER, 61234)

    assert localized.count("a=candidate:") == 1
    assert "a=candidate:1 1 udp 2130706431 127.0.0.1 61234 typ host\r\n" in localized
    assert "a=ice-lite" in localized and "a=end-of-candidates" in localized
    assert local_media.localize_candidate(HOST, 61234) == (
        "candidate:1 1 udp 2130706431 127.0.0.1 61234 typ host"
    )
    assert local_media.localize_candidate("candidate:3 1 udp 1 203.0.113.9 4 typ srflx", 1) is None


def test_local_signaling_carries_no_ice_servers_and_does_not_force_relays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROWSER_TURN_URLS", "turn:turn.example.com:3478")
    monkeypatch.setenv("BROWSER_TURN_SECRET", "relay-secret")
    provide = {"event": "signal/provide", "payload": {"sdp": OFFER}}

    offered = to_viewer(provide, None, 61234)
    assert offered is not None
    assert offered["iceServers"] == [] and offered["relayOnly"] is False
    assert to_viewer(provide, None, None) is None
    candidate = {"event": "signal/candidate", "payload": {"candidate": HOST, "sdpMid": "0"}}
    assert to_viewer(candidate, None, 61234) is not None
    relay_only = {"event": "signal/candidate", "payload": {"candidate": HOST}}
    assert to_viewer(relay_only, neko.turn.mint("abc", ttl_seconds=60), None) is None


def test_without_turn_media_goes_through_the_local_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BROWSER_TURN_URLS", raising=False)
    monkeypatch.delenv("BROWSER_TURN_SECRET", raising=False)
    assert neko.media_mode() == "local"
    monkeypatch.setenv("BROWSER_TURN_URLS", "turn:turn.example.com:3478")
    monkeypatch.setenv("BROWSER_TURN_SECRET", "relay-secret")
    assert neko.media_mode() == "relay"


def _bridge_module() -> ModuleType:
    source = resources.files("agent.resources").joinpath("browser", "udp_bridge.py")
    spec = importlib.util.spec_from_file_location("udp_bridge", str(source))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Echo(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.received.append(data)
        assert self.transport is not None
        self.transport.sendto(b"re:" + data, addr)


def test_the_sandbox_bridge_hands_datagrams_to_neko_and_returns_its_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _bridge_module()
    monkeypatch.setattr(bridge, "_neko_host", lambda: "127.0.0.1")

    async def scenario() -> tuple[list[bytes], bytes]:
        loop = asyncio.get_running_loop()
        echo = _Echo()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: echo, local_addr=("127.0.0.1", 0)
        )
        neko_port = transport.get_extra_info("sockname")[1]
        probe = await asyncio.start_server(lambda *_: None, "127.0.0.1", 0)
        tcp_port = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()
        server = asyncio.create_task(bridge.main(tcp_port, neko_port))
        try:
            for _ in range(50):
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", tcp_port)
                    break
                except OSError:
                    await asyncio.sleep(0.05)
            writer.write(struct.pack(">H", 5) + b"hello")
            reply_length = struct.unpack(">H", await asyncio.wait_for(reader.readexactly(2), 5))[0]
            reply = await reader.readexactly(reply_length)
            writer.close()
            return echo.received, reply
        finally:
            server.cancel()
            transport.close()

    received, reply = asyncio.run(scenario())
    assert received == [b"hello"]
    assert reply == b"re:hello"
