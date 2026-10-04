"""The wire to a session's mirror bridge: length-prefixed frames over a sandbox tunnel."""

import asyncio
import json
import struct
from collections.abc import Mapping

KIND_EVENTS = b"E"
KIND_JSON = b"J"
KIND_ASSET_RESPONSE = b"B"
KIND_HELLO = b"H"
KIND_INPUT = b"I"
KIND_CONTROL = b"C"
KIND_ASSET_REQUEST = b"Q"
_HEADER_BYTES = 5


async def read_frame(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    header = await reader.readexactly(_HEADER_BYTES)
    return header[:1], await reader.readexactly(struct.unpack(">I", header[1:])[0])


def write_frame(writer: asyncio.StreamWriter, kind: bytes, payload: bytes) -> None:
    writer.write(kind + struct.pack(">I", len(payload)) + payload)


def write_json(writer: asyncio.StreamWriter, kind: bytes, body: Mapping[str, object]) -> None:
    write_frame(writer, kind, json.dumps(body).encode())
