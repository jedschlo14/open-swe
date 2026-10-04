"""What a dashboard viewer may send over the live view socket, validated at the edge.

Anything that does not parse is dropped. A viewer without the lease has every
one of these ignored by the gate, so parsing never grants anything.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from agent.browser import engine

_COORDINATE = Annotated[float, Field(ge=0, le=10_000)]
_DELTA = Annotated[float, Field(ge=-10_000, le=10_000)]


class _Message(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)


class MouseMessage(_Message):
    type: Literal["mouse"]
    action: Literal["move", "down", "up", "wheel"]
    x: _COORDINATE
    y: _COORDINATE
    button: Annotated[int, Field(ge=0, le=2)] = 0
    dx: _DELTA = 0
    dy: _DELTA = 0

    @property
    def discrete(self) -> bool:
        return self.action != "move"


class KeyMessage(_Message):
    type: Literal["key"]
    action: Literal["down", "up"]
    key: Annotated[str, Field(max_length=32)]
    code: Annotated[str, Field(max_length=32)]


class ResizeMessage(_Message):
    type: Literal["resize"]
    width: Annotated[int, Field(ge=1, le=20_000)]
    height: Annotated[int, Field(ge=1, le=20_000)]

    @property
    def size(self) -> tuple[int, int]:
        return engine.clamp_viewport(self.width, self.height)


class NavigateMessage(_Message):
    type: Literal["navigate"]
    action: Literal["back", "forward", "reload", "go"]
    url: Annotated[str, Field(max_length=4096)] = ""


class CopyMessage(_Message):
    type: Literal["copy"]


class PasteMessage(_Message):
    type: Literal["paste"]
    text: Annotated[str, Field(max_length=engine.MAX_TEXT_CHARS)]


ClientMessage = (
    MouseMessage | KeyMessage | ResizeMessage | NavigateMessage | CopyMessage | PasteMessage
)
_ADAPTER: TypeAdapter[ClientMessage] = TypeAdapter(
    Annotated[ClientMessage, Field(discriminator="type")]
)


def parse(raw: str) -> ClientMessage | None:
    try:
        return _ADAPTER.validate_json(raw)
    except ValidationError:
        return None
