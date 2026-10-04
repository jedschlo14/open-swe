"""Runs inside a thread's sandbox and carries every byte in or out of the browser's network.

The browser lives in a network namespace that has nothing but loopback.

``proxy`` mode is the browser's only way out: it listens on that namespace's
loopback, makes every outbound connection from the sandbox's own namespace, and
admits only the ``host:port`` pairs in the allowlist file, which it rereads per
connection so the broker can widen it without a restart. When this process is
gone the browser has no network.

Standard library only: it runs in whatever image the sandbox booted.

Usage:
    egress_proxy.py proxy <netns> <listen_port> <allowlist_path>
"""

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

_HEADER_LIMIT = 64 * 1024
_CONNECT_TIMEOUT = 15
_HOP_BY_HOP = {b"proxy-connection", b"connection", b"keep-alive", b"proxy-authorization"}


def _socket_in(netns: str) -> socket.socket:
    """A TCP socket that belongs to ``netns``; a socket keeps the namespace it was made in."""
    own = os.open("/proc/self/ns/net", os.O_RDONLY)
    target = os.open(f"/var/run/netns/{netns}", os.O_RDONLY)
    try:
        os.setns(target, os.CLONE_NEWNET)
        try:
            made = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        finally:
            os.setns(own, os.CLONE_NEWNET)
    finally:
        os.close(own)
        os.close(target)
    made.setblocking(False)
    return made


def _listen_in(netns: str, port: int) -> socket.socket:
    listener = _socket_in(netns)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(128)
    return listener


def _allowed(allowlist: Path, host: str, port: int) -> bool:
    try:
        entries = json.loads(allowlist.read_text())
    except (OSError, ValueError) as exc:
        print(f"allowlist unreadable: {exc}", file=sys.stderr, flush=True)
        return False
    if not isinstance(entries, list):
        return False
    target = f"{host.lower().strip('[]')}:{port}"
    return any(isinstance(entry, str) and entry.lower() == target for entry in entries)


def _split_authority(authority: str, default_port: int) -> tuple[str, int] | None:
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        port_text = rest.removeprefix(":")
    else:
        host, sep, port_text = authority.rpartition(":")
        if not sep:
            host, port_text = authority, ""
    if not host:
        return None
    try:
        port = int(port_text) if port_text else default_port
    except ValueError:
        return None
    return (host, port) if 0 < port < 65536 else None


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except ConnectionError, asyncio.IncompleteReadError:
        pass
    finally:
        writer.close()


async def _refuse(writer: asyncio.StreamWriter, status: bytes) -> None:
    writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    await writer.drain()
    writer.close()


async def _handle(
    allowlist: Path, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
) -> None:
    try:
        head = await client_reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError:
        client_writer.close()
        return
    request_line, *header_lines = head[:-4].split(b"\r\n")
    parts = request_line.split(b" ")
    if len(parts) != 3:
        await _refuse(client_writer, b"400 Bad Request")
        return
    method, target, version = parts
    connect = method == b"CONNECT"
    if connect:
        endpoint = _split_authority(target.decode("latin-1"), 443)
        path = b""
    else:
        url = target.decode("latin-1")
        scheme, sep, rest = url.partition("://")
        if not sep or scheme.lower() != "http":
            await _refuse(client_writer, b"400 Bad Request")
            return
        authority, slash, remainder = rest.partition("/")
        endpoint = _split_authority(authority, 80)
        path = ("/" + remainder if slash else "/").encode("latin-1")
    if endpoint is None:
        await _refuse(client_writer, b"400 Bad Request")
        return
    host, port = endpoint
    if not _allowed(allowlist, host, port):
        print(f"denied {host}:{port}", file=sys.stderr, flush=True)
        await _refuse(client_writer, b"403 Forbidden")
        return
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(host.strip("[]"), port), _CONNECT_TIMEOUT
        )
    except OSError, TimeoutError:
        await _refuse(client_writer, b"502 Bad Gateway")
        return
    if connect:
        client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await client_writer.drain()
    else:
        # One request per connection, so a reused proxy connection can never
        # carry a second request to a host this check did not see.
        headers = [
            line
            for line in header_lines
            if line.split(b":", 1)[0].strip().lower() not in _HOP_BY_HOP
        ]
        upstream_writer.write(
            b"\r\n".join([b" ".join([method, path, version]), *headers, b"Connection: close"])
            + b"\r\n\r\n"
        )
        await upstream_writer.drain()
    await asyncio.gather(
        _pipe(client_reader, upstream_writer), _pipe(upstream_reader, client_writer)
    )


async def _serve(netns: str, port: int, allowlist: Path) -> None:
    listener = _listen_in(netns, port)
    server = await asyncio.start_server(
        lambda reader, writer: _handle(allowlist, reader, writer),
        sock=listener,
        limit=_HEADER_LIMIT,
    )
    print("ready", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    if sys.argv[1] == "proxy":
        asyncio.run(_serve(sys.argv[2], int(sys.argv[3]), Path(sys.argv[4])))
    else:
        raise SystemExit(f"unknown mode {sys.argv[1]!r}")
