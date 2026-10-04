"""Runs inside a thread's sandbox and carries one viewer's WebRTC datagrams over TCP.

neko's WebRTC media is UDP, and the sandbox accepts no inbound UDP. Each TCP
connection to this helper is one viewer: the server reaches it through a sandbox
tunnel, and every frame it sends (``length (2 bytes, big endian) | datagram``) is
forwarded to neko's UDP port from a socket of its own, with neko's replies framed
back the same way. A connection whose peer stops reading drops datagrams instead of
queueing them, so a stalled link never builds up delay.

Standard library only.

Usage:
    udp_bridge.py <tcp_port> <neko_udp_port>
"""

import asyncio
import socket
import struct
import subprocess
import sys

_MAX_FLOWS = 8
_MAX_BACKLOG_BYTES = 256 * 1024
_HEADER = struct.Struct(">H")


def _neko_host() -> str:
    """The address neko's UDP port answers on: it binds each interface address, not loopback."""
    try:
        output = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except OSError, subprocess.SubprocessError:
        return "127.0.0.1"
    for line in output.splitlines():
        fields = line.split()
        if "inet" in fields:
            return fields[fields.index("inet") + 1].split("/")[0]
    return "127.0.0.1"


async def _serve(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target: tuple[str, int],
    flows: set[socket.socket],
) -> None:
    if len(flows) >= _MAX_FLOWS:
        writer.close()
        return
    loop = asyncio.get_running_loop()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setblocking(False)
    udp.bind((target[0], 0))
    flows.add(udp)

    async def to_neko() -> None:
        while True:
            (length,) = _HEADER.unpack(await reader.readexactly(_HEADER.size))
            await loop.sock_sendto(udp, await reader.readexactly(length), target)

    async def to_server() -> None:
        while True:
            datagram, _ = await loop.sock_recvfrom(udp, 65535)
            if writer.transport.get_write_buffer_size() > _MAX_BACKLOG_BYTES:
                continue
            writer.write(_HEADER.pack(len(datagram)) + datagram)
            await writer.drain()

    tasks = [asyncio.create_task(to_neko()), asyncio.create_task(to_server())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        flows.discard(udp)
        udp.close()
        writer.close()


async def main(tcp_port: int, neko_port: int) -> None:
    target = (_neko_host(), neko_port)
    flows: set[socket.socket] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _serve(reader, writer, target, flows)

    server = await asyncio.start_server(handle, "127.0.0.1", tcp_port)
    print(f"ready {tcp_port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), int(sys.argv[2])))
