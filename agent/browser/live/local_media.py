"""Carries a viewer's WebRTC media over the sandbox tunnel when no TURN relay is configured.

The sandbox accepts no inbound UDP and a laptop has no public address, so there is
nothing for a relay to reach. But the server already holds a TCP tunnel into the
sandbox, and a viewer on the server's own machine can reach the server's loopback. So
neko is told to offer one loopback UDP candidate, this module listens there for the
viewer's browser, and each datagram crosses the tunnel inside a TCP frame to a bridge
in the sandbox (``agent/resources/browser/udp_bridge.py``), which hands it to neko.

Frames are ``length (2 bytes, big endian) | datagram`` in both directions. A flow whose
tunnel backs up drops datagrams rather than queueing them. Because only the viewer's
own machine can connect to the loopback socket, this works only for a viewer on the
server's machine; ``is_local_viewer`` is how the server tells.
"""

import asyncio
import ipaddress
import logging
import struct
from collections.abc import Mapping
from contextlib import AsyncExitStack
from types import TracebackType
from urllib.parse import urlsplit

from langsmith.sandbox import AsyncSandbox

from agent.browser import neko
from agent.browser.models import BrowserSession

logger = logging.getLogger(__name__)

_MAX_FLOWS = 4
_MAX_BACKLOG_BYTES = 256 * 1024
_HEADER = struct.Struct(">H")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.strip().strip("[]")).is_loopback
    except ValueError:
        return False


def is_local_viewer(client_host: str | None, headers: Mapping[str, str]) -> bool:
    """Whether the request comes from a browser on the server's own machine.

    The client address, every hop a proxy reports, and the host the browser typed must
    all be loopback; one that is not means the browser is somewhere loopback cannot reach.
    """
    if client_host is None or not _is_loopback(client_host):
        return False
    forwarded = headers.get("x-forwarded-for", "")
    if any(part.strip() and not _is_loopback(part) for part in forwarded.split(",")):
        return False
    hostname = urlsplit(f"//{headers.get('host', '')}").hostname or ""
    return hostname.lower() in _LOOPBACK_HOSTS


class LocalMedia(asyncio.DatagramProtocol):
    """A loopback UDP port for one viewer, tunnelled to the session's bridge in the sandbox."""

    def __init__(self, sandbox: AsyncSandbox, session: BrowserSession) -> None:
        self._sandbox = sandbox
        self._session = session
        self._stack = AsyncExitStack()
        self._tunnel_port = 0
        self._transport: asyncio.DatagramTransport | None = None
        self._flows: dict[tuple[str, int], asyncio.Task[None]] = {}
        self._writers: dict[tuple[str, int], asyncio.StreamWriter] = {}
        self.port = 0

    async def __aenter__(self) -> LocalMedia:
        stream_port = self._session.stream_port
        if stream_port is None:
            raise neko.NekoError("the browser has no live view")
        tunnel = await self._stack.enter_async_context(
            await self._sandbox.tunnel(remote_port=neko.bridge_port(stream_port), local_port=0)
        )
        self._tunnel_port = tunnel.local_port
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: self, local_addr=("127.0.0.1", 0)
        )
        self._stack.push_async_callback(self._close_flows)
        self.port = transport.get_extra_info("sockname")[1]
        return self

    async def __aexit__(
        self,
        _type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if self._transport is not None:
            self._transport.close()
        await self._stack.aclose()

    async def _close_flows(self) -> None:
        for task in self._flows.values():
            task.cancel()
        await asyncio.gather(*self._flows.values(), return_exceptions=True)

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        if isinstance(transport, asyncio.DatagramTransport):
            self._transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        writer = self._writers.get(addr)
        if writer is not None:
            if writer.transport.get_write_buffer_size() <= _MAX_BACKLOG_BYTES:
                writer.write(_HEADER.pack(len(data)) + data)
            return
        if addr not in self._flows and len(self._flows) < _MAX_FLOWS:
            self._flows[addr] = asyncio.create_task(self._flow(addr, data))

    async def _flow(self, addr: tuple[str, int], first: bytes) -> None:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", self._tunnel_port)
        except OSError:
            logger.warning(
                "The live view bridge could not be reached",
                exc_info=True,
                extra={"browser_session_id": self._session.session_id},
            )
            self._flows.pop(addr, None)
            return
        self._writers[addr] = writer
        writer.write(_HEADER.pack(len(first)) + first)
        try:
            while True:
                (length,) = _HEADER.unpack(await reader.readexactly(_HEADER.size))
                datagram = await reader.readexactly(length)
                if self._transport is not None:
                    self._transport.sendto(datagram, addr)
        except asyncio.IncompleteReadError, ConnectionError:
            logger.debug(
                "The live view bridge closed",
                extra={"browser_session_id": self._session.session_id},
            )
        finally:
            self._writers.pop(addr, None)
            self._flows.pop(addr, None)
            writer.close()


def localize_candidate(candidate: str, port: int) -> str | None:
    """``candidate`` pointed at this viewer's loopback port, or ``None`` if it is not a UDP host one."""
    line = candidate.strip()
    prefix = "a=" if line.startswith("a=") else ""
    fields = line.removeprefix("a=").split()
    if len(fields) < 8 or fields[2].lower() != "udp" or fields[6] != "typ" or fields[7] != "host":
        return None
    fields[4], fields[5] = "127.0.0.1", str(port)
    return prefix + " ".join(fields)


def localize_sdp(sdp: str, port: int) -> str:
    """``sdp`` with its candidates replaced by the one loopback UDP candidate the viewer can reach."""
    lines: list[str] = []
    seen: set[str] = set()
    for line in sdp.splitlines(keepends=True):
        if not line.startswith("a=candidate:"):
            lines.append(line)
            continue
        localized = localize_candidate(line, port)
        if localized is None:
            continue
        fields = localized.split()
        key = " ".join(fields[1:2] + fields[4:6])
        if key in seen:
            continue
        seen.add(key)
        lines.append(localized + line[len(line.rstrip("\r\n")) :])
    return "".join(lines)
