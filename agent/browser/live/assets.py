"""Loads a live view's page resources through the session's own browser.

A viewer's replay of the page asks for stylesheets, images, and fonts over HTTP.
Each is fetched by the sandboxed browser, so it travels the browser's egress
proxy and carries its cookies, and never from the server or the viewer. One
tunnel per session answers every viewer, and closes when it goes quiet.
"""

import asyncio
import json
import logging
import struct
from contextlib import AsyncExitStack
from dataclasses import dataclass

from agent.browser.live import bridge
from agent.browser.models import BrowserSession

logger = logging.getLogger(__name__)

_IDLE_SECONDS = 60.0
_REQUEST_TIMEOUT_SECONDS = 45.0
_CONCURRENT_REQUESTS = 12
_RESPONSE_HEAD = struct.Struct(">IHI")


class AssetUnavailableError(RuntimeError):
    """The browser could not be asked for the resource."""


@dataclass(frozen=True)
class Asset:
    status: int
    headers: dict[str, str]
    body: bytes


class _Channel:
    def __init__(self, session: BrowserSession) -> None:
        self._session = session
        self._stack = AsyncExitStack()
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future[Asset]] = {}
        self._next_request = 0
        self._last_used = asyncio.get_running_loop().time()
        self._tasks: set[asyncio.Task[None]] = set()
        self._slots = asyncio.Semaphore(_CONCURRENT_REQUESTS)
        self.closed = False

    async def open(self) -> None:
        from agent.sandboxes.providers.langsmith import connect_async_langsmith_sandbox

        sandbox_id, port = self._session.sandbox_id, self._session.stream_port
        if sandbox_id is None or port is None:
            raise AssetUnavailableError("the browser has no live view")
        try:
            client, sandbox = await connect_async_langsmith_sandbox(sandbox_id)
            self._stack.push_async_callback(client.aclose)
            tunnel = await self._stack.enter_async_context(
                await sandbox.tunnel(remote_port=port, local_port=0)
            )
            reader, self._writer = await asyncio.open_connection("127.0.0.1", tunnel.local_port)
        except BaseException:
            await self._stack.aclose()
            raise
        self._tasks = {
            asyncio.create_task(self._read(reader)),
            asyncio.create_task(self._reap()),
        }

    async def _read(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                kind, payload = await bridge.read_frame(reader)
                if kind != bridge.KIND_ASSET_RESPONSE or len(payload) < _RESPONSE_HEAD.size:
                    continue
                request, status, head_length = _RESPONSE_HEAD.unpack_from(payload)
                start = _RESPONSE_HEAD.size
                try:
                    headers = json.loads(payload[start : start + head_length])
                except ValueError:
                    headers = {}
                future = self._pending.pop(request, None)
                if future is not None and not future.done():
                    future.set_result(
                        Asset(
                            status=status,
                            headers=headers if isinstance(headers, dict) else {},
                            body=payload[start + head_length :],
                        )
                    )
        except asyncio.IncompleteReadError, ConnectionError, OSError:
            logger.debug(
                "Browser asset channel ended",
                extra={"browser_session_id": self._session.session_id},
            )
        finally:
            await self.close()

    async def _reap(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(_IDLE_SECONDS / 4)
            if not self._pending and loop.time() - self._last_used > _IDLE_SECONDS:
                await self.close()
                return

    async def fetch(self, url: str) -> Asset:
        writer = self._writer
        if writer is None or self.closed:
            raise AssetUnavailableError("the asset channel is closed")
        async with self._slots:
            self._last_used = asyncio.get_running_loop().time()
            self._next_request = (self._next_request + 1) % 2**32
            request = self._next_request
            future: asyncio.Future[Asset] = asyncio.get_running_loop().create_future()
            self._pending[request] = future
            bridge.write_frame(
                writer, bridge.KIND_ASSET_REQUEST, struct.pack(">I", request) + url.encode()
            )
            try:
                await writer.drain()
                return await asyncio.wait_for(future, timeout=_REQUEST_TIMEOUT_SECONDS)
            except (TimeoutError, ConnectionError) as exc:
                raise AssetUnavailableError("the browser did not answer in time") from exc
            finally:
                self._pending.pop(request, None)
                self._last_used = asyncio.get_running_loop().time()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if _channels.get(self._session.session_id) is self:
            del _channels[self._session.session_id]
        for future in self._pending.values():
            if not future.done():
                future.set_exception(AssetUnavailableError("the asset channel closed"))
        self._pending.clear()
        if self._writer is not None:
            self._writer.close()
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        await self._stack.aclose()


_channels: dict[str, _Channel] = {}
_opening = asyncio.Lock()


async def fetch(session: BrowserSession, url: str) -> Asset:
    """One resource as the session's browser sees it: its status, headers, and body."""
    async with _opening:
        channel = _channels.get(session.session_id)
        if channel is None or channel.closed:
            channel = _Channel(session)
            await channel.open()
            _channels[session.session_id] = channel
    return await channel.fetch(url)
