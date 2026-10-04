"""Splits the display helper's H.264 byte stream into access units the viewer can decode.

The encoder writes Annex B with an access unit delimiter in front of every frame, so a
frame ends where the next delimiter begins. Each unit says whether a decoder may start
from it and, when it carries a sequence parameter set, the codec string for it.
"""

from dataclasses import dataclass

_START = b"\x00\x00\x01"
_AUD = b"\x00\x00\x01\x09"
_NAL_IDR = 5
_NAL_SPS = 7
_MAX_BUFFER_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class AccessUnit:
    data: bytes
    keyframe: bool
    codec: str | None


def _unit_start(buffer: bytearray, delimiter: int) -> int:
    return delimiter - 1 if delimiter > 0 and buffer[delimiter - 1] == 0 else delimiter


def describe(unit: bytes | bytearray) -> AccessUnit:
    """The keyframe flag and codec string of one access unit."""
    keyframe = False
    codec: str | None = None
    index = unit.find(_START)
    while index != -1:
        header = index + 3
        if header >= len(unit):
            break
        nal_type = unit[header] & 0x1F
        if nal_type == _NAL_IDR:
            keyframe = True
        elif nal_type == _NAL_SPS and codec is None and header + 3 < len(unit):
            codec = f"avc1.{unit[header + 1]:02X}{unit[header + 2]:02X}{unit[header + 3]:02X}"
        index = unit.find(_START, header)
    return AccessUnit(bytes(unit), keyframe, codec)


class AccessUnitSplitter:
    """Accumulates stream chunks and yields each access unit once it is complete."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, chunk: bytes) -> list[AccessUnit]:
        self._buffer += chunk
        units: list[AccessUnit] = []
        first = self._buffer.find(_AUD)
        if first != -1:
            del self._buffer[: _unit_start(self._buffer, first)]
        while first != -1:
            following = self._buffer.find(_AUD, len(_AUD))
            if following == -1:
                break
            end = _unit_start(self._buffer, following)
            units.append(describe(self._buffer[:end]))
            del self._buffer[:end]
        if len(self._buffer) > _MAX_BUFFER_BYTES:
            raise ValueError("the video stream has no access unit delimiters")
        return units
