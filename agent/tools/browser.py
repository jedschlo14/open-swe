"""The agent's browser tools, loaded on request as the ``Browser`` tool group.

They drive the thread's one browser session through the broker, which enforces
the lease, the egress allowlist, and the confirmation gate. Page text reaches the
model inside untrusted-content markers.
"""

import json

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from agent.browser import broker, manager
from agent.browser.broker import BrowserOutcome
from agent.browser.ops import ActOp, NavigateOp, ScreenshotOp, SnapshotOp
from agent.prompts import prompt
from agent.run_config import RunConfig

type ContentBlock = dict[str, str]

BROWSER_TOOL_NAMES = (
    "browser_navigate",
    "browser_snapshot",
    "browser_act",
    "browser_screenshot",
    "browser_stop",
)


class _NavigateArgs(BaseModel):
    url: str = Field(
        description="An http(s) URL served from this sandbox, e.g. http://localhost:3000."
    )
    allow_origins: list[str] = Field(
        default_factory=list,
        description="Other loopback origins the page needs, e.g. an API on http://localhost:8000.",
    )


class _SnapshotArgs(BaseModel):
    interactive_only: bool = Field(
        default=True, description="Only list interactive elements, which keeps the result short."
    )


class _ActArgs(BaseModel):
    operation: ActOp
    confirmation_id: str | None = Field(
        default=None,
        description="Only after a person approved this exact operation in the Browser panel.",
    )


class _NoArgs(BaseModel):
    pass


def _context() -> tuple[str, str | None]:
    cfg = RunConfig.from_runtime()
    if not isinstance(cfg.thread_id, str) or not cfg.thread_id:
        raise ValueError("no thread_id in run config")
    return cfg.thread_id, cfg.workspace_slug


def _report(outcome: BrowserOutcome) -> str:
    fields = outcome.model_dump(exclude={"snapshot", "image_base64"}, exclude_none=True)
    report = json.dumps(fields)
    if outcome.snapshot is None:
        return report
    return f"{report}\n<untrusted-page-content>\n{outcome.snapshot}\n</untrusted-page-content>"


async def browser_navigate(url: str, allow_origins: list[str] | None = None) -> str:
    thread_id, workspace = _context()
    op = NavigateOp(url=url, allow_origins=tuple(allow_origins or ()))
    return _report(await broker.execute(thread_id, op, workspace_slug=workspace))


async def browser_snapshot(interactive_only: bool = True) -> str:
    thread_id, workspace = _context()
    op = SnapshotOp(interactive_only=interactive_only)
    return _report(await broker.execute(thread_id, op, workspace_slug=workspace))


async def browser_act(operation: ActOp, confirmation_id: str | None = None) -> str:
    thread_id, workspace = _context()
    return _report(
        await broker.execute(
            thread_id, operation, workspace_slug=workspace, confirmation_id=confirmation_id
        )
    )


async def browser_screenshot() -> list[ContentBlock] | str:
    thread_id, workspace = _context()
    outcome = await broker.execute(thread_id, ScreenshotOp(), workspace_slug=workspace)
    if outcome.image_base64 is None:
        return _report(outcome)
    return [
        {"type": "text", "text": "Viewport screenshot of the thread's browser."},
        {"type": "image", "base64": outcome.image_base64, "mime_type": "image/jpeg"},
    ]


async def browser_stop() -> str:
    thread_id, _ = _context()
    stopped = await manager.stop_session(thread_id, "requested")
    return json.dumps({"status": "stopped" if stopped is not None else "no_session"})


def browser_tools() -> list[BaseTool]:
    return [
        StructuredTool.from_function(
            coroutine=browser_navigate,
            name="browser_navigate",
            description=prompt("tools/browser_navigate"),
            args_schema=_NavigateArgs,
        ),
        StructuredTool.from_function(
            coroutine=browser_snapshot,
            name="browser_snapshot",
            description=prompt("tools/browser_snapshot"),
            args_schema=_SnapshotArgs,
        ),
        StructuredTool.from_function(
            coroutine=browser_act,
            name="browser_act",
            description=prompt("tools/browser_act"),
            args_schema=_ActArgs,
        ),
        StructuredTool.from_function(
            coroutine=browser_screenshot,
            name="browser_screenshot",
            description=prompt("tools/browser_screenshot"),
            args_schema=_NoArgs,
        ),
        StructuredTool.from_function(
            coroutine=browser_stop,
            name="browser_stop",
            description=prompt("tools/browser_stop"),
            args_schema=_NoArgs,
        ),
    ]
