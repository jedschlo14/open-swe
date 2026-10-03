"""The typed operations the broker executes, and nothing else.

There is deliberately no ``eval``, upload, download, or tab operation: each
operation maps to a fixed ``agent-browser`` argv built here, never to text the
caller wrote.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_REF_PATTERN = r"^@?e\d{1,6}$"
Ref = Annotated[str, Field(pattern=_REF_PATTERN, description="An element ref from the snapshot.")]
Coordinate = Annotated[int, Field(ge=0, le=10_000)]


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NavigateOp(_Op):
    action: Literal["navigate"] = "navigate"
    url: str = Field(max_length=4096)
    allow_origins: tuple[str, ...] = Field(default=(), max_length=10)


class SnapshotOp(_Op):
    action: Literal["snapshot"] = "snapshot"
    interactive_only: bool = True


class ClickOp(_Op):
    action: Literal["click"] = "click"
    ref: Ref | None = None
    x: Coordinate | None = None
    y: Coordinate | None = None

    @model_validator(mode="after")
    def _one_target(self) -> ClickOp:
        if (self.ref is None) == (self.x is None or self.y is None):
            raise ValueError("click takes either a ref or both x and y")
        return self


class FillOp(_Op):
    action: Literal["fill"] = "fill"
    ref: Ref
    text: str = Field(max_length=10_000)


class SelectOp(_Op):
    action: Literal["select"] = "select"
    ref: Ref
    values: tuple[str, ...] = Field(min_length=1, max_length=20)


class PressOp(_Op):
    action: Literal["press"] = "press"
    key: str = Field(pattern=r"^[A-Za-z0-9+]{1,40}$", description="For example Enter, Tab.")


class ScrollOp(_Op):
    action: Literal["scroll"] = "scroll"
    direction: Literal["up", "down", "left", "right"]
    pixels: int = Field(default=600, ge=1, le=20_000)


class WaitOp(_Op):
    action: Literal["wait"] = "wait"
    milliseconds: int = Field(ge=1, le=30_000)


class BackOp(_Op):
    action: Literal["back"] = "back"


class ReloadOp(_Op):
    action: Literal["reload"] = "reload"


class DialogOp(_Op):
    action: Literal["dialog"] = "dialog"
    accept: bool
    text: str | None = Field(default=None, max_length=1_000)


class ScreenshotOp(_Op):
    action: Literal["screenshot"] = "screenshot"


ActOp = Annotated[
    ClickOp | FillOp | SelectOp | PressOp | ScrollOp | WaitOp | BackOp | ReloadOp | DialogOp,
    Field(discriminator="action"),
]
BrowserOp = Annotated[
    NavigateOp
    | SnapshotOp
    | ClickOp
    | FillOp
    | SelectOp
    | PressOp
    | ScrollOp
    | WaitOp
    | BackOp
    | ReloadOp
    | DialogOp
    | ScreenshotOp,
    Field(discriminator="action"),
]


def ref_name(ref: str) -> str:
    return ref.removeprefix("@")


def commands(op: BrowserOp) -> list[list[str]]:
    """The ``agent-browser`` argument lists that carry out ``op``, run in order."""
    if isinstance(op, ClickOp) and op.x is not None and op.y is not None:
        return [["mouse", "move", str(op.x), str(op.y)], ["mouse", "down"], ["mouse", "up"]]
    return [_argv(op)]


def _argv(op: BrowserOp) -> list[str]:
    match op:
        case NavigateOp(url=url):
            return ["open", url]
        case SnapshotOp(interactive_only=interactive_only):
            return ["snapshot", "-i"] if interactive_only else ["snapshot"]
        case ClickOp(ref=str() as ref):
            return ["click", f"@{ref_name(ref)}"]
        case FillOp(ref=ref, text=text):
            return ["fill", f"@{ref_name(ref)}", text]
        case SelectOp(ref=ref, values=values):
            return ["select", f"@{ref_name(ref)}", *values]
        case PressOp(key=key):
            return ["press", key]
        case ScrollOp(direction=direction, pixels=pixels):
            return ["scroll", direction, str(pixels)]
        case WaitOp(milliseconds=milliseconds):
            return ["wait", str(milliseconds)]
        case BackOp():
            return ["back"]
        case ReloadOp():
            return ["reload"]
        case DialogOp(accept=True, text=str() as text):
            return ["dialog", "accept", text]
        case DialogOp(accept=True):
            return ["dialog", "accept"]
        case DialogOp():
            return ["dialog", "dismiss"]
        case ScreenshotOp():
            raise ValueError("screenshots are captured by the engine, not a single command")
        case _:
            raise ValueError("click needs a ref or coordinates")
