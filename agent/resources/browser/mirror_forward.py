"""Exposes the mirror bridge's Unix socket on sandbox loopback.

The bridge runs in the browser's network namespace, which the sandbox's main
namespace cannot route into, but a Unix socket is a file both can open. The
dashboard relay reaches this listener through a sandbox tunnel. Standard library only.

Usage: mirror_forward.py <socket_path>
"""

import asyncio
import contextlib
import sys


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(1 << 16):
            writer.write(chunk)
            await writer.drain()
    except ConnectionError:
        print("a forwarded connection ended", file=sys.stderr, flush=True)
    finally:
        writer.close()


async def main(socket_path: str) -> None:
    async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter):
        try:
            bridge_reader, bridge_writer = await asyncio.open_unix_connection(
                socket_path, limit=1 << 24
            )
        except OSError:
            print("the mirror bridge is not accepting connections", file=sys.stderr, flush=True)
            client_writer.close()
            return
        done, pending = await asyncio.wait(
            {
                asyncio.create_task(pipe(client_reader, bridge_writer)),
                asyncio.create_task(pipe(bridge_reader, client_writer)),
            },
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(ConnectionError):
                task.result()
        bridge_writer.close()
        client_writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=1 << 24)
    print(f"ready {server.sockets[0].getsockname()[1]}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
