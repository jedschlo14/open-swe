"""Talks to a session's display helper through the sandbox tunnel."""

import asyncio
import json
import struct
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from types import TracebackType

from pydantic import JsonValue

from agent.browser import display
from agent.browser.models import BrowserSession

_MAX_FRAME_BYTES = 16 * 1024 * 1024
_HEADER = struct.Struct(">cI")


@dataclass(frozen=True)
class Screen:
    """A new encode has started at this size, in device pixels."""

    width: int
    height: int


@dataclass(frozen=True)
class Video:
    """A chunk of the encode's Annex B byte stream."""

    data: bytes


type Frame = Screen | Video


class DisplayClient:
    """One viewer's connection to the helper: its video out, input and resizes in."""

    def __init__(self, session: BrowserSession, port: int) -> None:
        self._session = session
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> DisplayClient:
        self._reader, self._writer = await asyncio.open_connection("127.0.0.1", self._port)
        await self.send({"token": display.token(self._session)})
        return self

    async def __aexit__(
        self,
        _type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if self._writer is not None:
            self._writer.close()
            await self._writer.wait_closed()

    async def send(self, message: Mapping[str, JsonValue]) -> None:
        if self._writer is None:
            raise display.DisplayError("the viewer has not connected")
        self._writer.write(json.dumps(message).encode() + b"\n")
        await self._writer.drain()

    async def frames(self) -> AsyncIterator[Frame]:
        if self._reader is None:
            raise display.DisplayError("the viewer has not connected")
        while True:
            try:
                header = await self._reader.readexactly(_HEADER.size)
            except asyncio.IncompleteReadError:
                return
            kind, length = _HEADER.unpack(header)
            if length > _MAX_FRAME_BYTES:
                raise display.DisplayError("the display helper sent an oversized frame")
            payload = await self._reader.readexactly(length)
            yield _decode(kind, payload)


def _decode(kind: bytes, payload: bytes) -> Frame:
    if kind == b"V":
        return Video(payload)
    if kind == b"S":
        size = json.loads(payload)
        width, height = size.get("width"), size.get("height")
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            return Screen(width, height)
    raise display.DisplayError("the display helper sent an unreadable frame")
