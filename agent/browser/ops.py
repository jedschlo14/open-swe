"""The typed operations the broker executes, and nothing else.

There is deliberately no ``eval``, upload, download, or tab operation: each
operation maps to a fixed ``agent-browser`` argv built here, never to text the
caller wrote.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

_REF_PATTERN = r"^@?e\d{1,6}$"
Ref = Annotated[str, Field(pattern=_REF_PATTERN, description="An element ref from the snapshot.")]
Coordinate = Annotated[int, Field(ge=0, le=10_000)]


def _require_action(schema: dict[str, JsonValue]) -> None:
    required = schema.setdefault("required", [])
    if isinstance(required, list) and "action" not in required:
        required.insert(0, "action")


class _Op(BaseModel):
    # The action tag has a default for callers in code, but models must always send it.
    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_extra=_require_action)


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


def _no_leading_dash(value: str | None) -> str | None:
    if value is not None and value.startswith("-"):
        raise ValueError("a locator cannot start with a dash")
    return value


class FindOp(_Op):
    """Click or fill an element found by what it shows, so no snapshot or ref is needed."""

    action: Literal["find"] = "find"
    by: Literal["role", "text", "label", "placeholder"]
    value: str = Field(
        min_length=1,
        max_length=200,
        description="The role (e.g. button, textbox), or the visible text, label or placeholder.",
    )
    name: str | None = Field(
        default=None, max_length=200, description="With `by: role`, the accessible name to match."
    )
    do: Literal["click", "fill"] = "click"
    text: str | None = Field(default=None, max_length=10_000, description="What `fill` types.")

    @model_validator(mode="after")
    def _consistent(self) -> FindOp:
        _no_leading_dash(self.value)
        _no_leading_dash(self.name)
        if self.name is not None and self.by != "role":
            raise ValueError("name only applies with by=role")
        if (self.do == "fill") != (self.text is not None):
            raise ValueError("fill needs text, and click takes none")
        return self


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
    format: Literal["jpeg", "png"] = "jpeg"


class RecordStartOp(_Op):
    action: Literal["record_start"] = "record_start"


class RecordStopOp(_Op):
    action: Literal["record_stop"] = "record_stop"


ActOp = Annotated[
    ClickOp
    | FillOp
    | FindOp
    | SelectOp
    | PressOp
    | ScrollOp
    | WaitOp
    | BackOp
    | ReloadOp
    | DialogOp,
    Field(discriminator="action"),
]
FlowStep = Annotated[
    NavigateOp
    | ClickOp
    | FillOp
    | FindOp
    | SelectOp
    | PressOp
    | ScrollOp
    | WaitOp
    | BackOp
    | ReloadOp,
    Field(discriminator="action"),
]
BrowserOp = Annotated[
    NavigateOp
    | SnapshotOp
    | ClickOp
    | FillOp
    | FindOp
    | SelectOp
    | PressOp
    | ScrollOp
    | WaitOp
    | BackOp
    | ReloadOp
    | DialogOp
    | ScreenshotOp
    | RecordStartOp
    | RecordStopOp,
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
        case FindOp(by=by, value=value, name=name, do=do, text=text):
            argv = ["find", by, value, do]
            if text is not None:
                argv.append(text)
            if name is not None:
                argv += ["--name", name]
            return argv
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
        case ScreenshotOp() | RecordStartOp() | RecordStopOp():
            raise ValueError(f"{op.action} is run by the engine, not a single command")
        case _:
            raise ValueError("click needs a ref or coordinates")
