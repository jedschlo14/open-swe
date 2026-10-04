"""The agent's browser tools, loaded on request as the ``Browser`` tool group.

They drive the thread's one browser session through the broker, which enforces
the lease, the egress allowlist, and the confirmation gate. Page text reaches the
model inside untrusted-content markers.
"""

import base64
import json
import logging

import httpx2
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, JsonValue

from agent.audit_logs.tools import audit_tool
from agent.browser import attachments, broker, evidence, manager
from agent.browser.broker import BrowserOutcome
from agent.browser.evidence import Label
from agent.browser.ops import (
    ActOp,
    NavigateOp,
    RecordStartOp,
    RecordStopOp,
    ScreenshotOp,
    SnapshotOp,
)
from agent.credential_scope import private_credential_login
from agent.prompts import prompt
from agent.run_config import RunConfig

logger = logging.getLogger(__name__)

type ContentBlock = dict[str, str]

BROWSER_TOOL_NAMES = (
    "browser_navigate",
    "browser_snapshot",
    "browser_act",
    "browser_screenshot",
    "browser_publish_screenshot",
    "browser_record_start",
    "browser_record_stop",
    "browser_publish_recording",
    "browser_use_saved_sign_in",
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


class _SignInArgs(BaseModel):
    origin: str = Field(
        description="The external origin to sign in to, e.g. https://staging.example.com."
    )


class _NoArgs(BaseModel):
    pass


class _PublishArgs(BaseModel):
    label: Label = Field(description="`before` or `after` for a comparison pair, else `other`.")
    caption: str = Field(max_length=120, description="Short alt text describing what it shows.")


class _PublishRecordingArgs(BaseModel):
    caption: str = Field(
        max_length=120, description="Short alt text, used when it is published as an animation."
    )


def _context() -> tuple[str, str | None]:
    cfg = RunConfig.from_runtime()
    if not isinstance(cfg.thread_id, str) or not cfg.thread_id:
        raise ValueError("no thread_id in run config")
    return cfg.thread_id, cfg.workspace_slug


def _report(outcome: BrowserOutcome) -> str:
    fields = outcome.model_dump(
        exclude={"snapshot", "image_base64", "image_mime_type"}, exclude_none=True
    )
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
        {
            "type": "image",
            "base64": outcome.image_base64,
            "mime_type": outcome.image_mime_type or "image/jpeg",
        },
    ]


async def browser_publish_screenshot(label: Label, caption: str) -> str:
    thread_id, workspace = _context()
    cfg = RunConfig.from_runtime()
    if not cfg.repo_full_name:
        return json.dumps({"status": "error", "message": "This thread has no repository."})
    outcome = await broker.execute(thread_id, ScreenshotOp(format="png"), workspace_slug=workspace)
    if outcome.image_base64 is None:
        return _report(outcome)
    from agent.github.http import github_client
    from agent.github.sandbox_access import repository_token

    try:
        access = await repository_token([cfg.repo_full_name], permissions={"contents": "write"})
        if not access.token:
            raise RuntimeError("The GitHub App has no write access to this repository.")
        async with github_client(token=access.token) as client:
            published = await evidence.publish_image(
                client,
                cfg.repo_full_name,
                thread_id=thread_id,
                label=label,
                caption=caption,
                data=base64.b64decode(outcome.image_base64),
                extension="png",
            )
    except (evidence.EvidenceError, RuntimeError) as exc:
        return json.dumps(
            {
                "status": "error",
                "message": f"{exc} Leave the screenshot out of the PR and say why.",
            }
        )
    return json.dumps({"status": "ok", "markdown": published.markdown})


async def browser_record_start() -> str:
    thread_id, workspace = _context()
    return _report(await broker.execute(thread_id, RecordStartOp(), workspace_slug=workspace))


async def browser_record_stop() -> list[ContentBlock] | str:
    thread_id, workspace = _context()
    outcome = await broker.execute(thread_id, RecordStopOp(), workspace_slug=workspace)
    if outcome.image_base64 is None:
        return _report(outcome)
    return [
        {"type": "text", "text": outcome.message or "Recording preview."},
        {
            "type": "image",
            "base64": outcome.image_base64,
            "mime_type": outcome.image_mime_type or "image/jpeg",
        },
    ]


async def _personal_token() -> str | None:
    """The private thread owner's GitHub token, the only one a recording may be uploaded with."""
    try:
        login = await private_credential_login()
    except RuntimeError:
        logger.info("No personal GitHub token for the recording upload", exc_info=True)
        return None
    if login is None:
        return None
    from agent.dashboard.profiles import get_valid_access_token

    return await get_valid_access_token(login)


async def _upload_video(thread_id: str, repo_full_name: str) -> tuple[str | None, str | None]:
    """A user-attachments URL for the recording and, when there is none, why not."""
    token = await _personal_token()
    if token is None:
        return None, "no personal GitHub token is available in this thread"
    outcome, video = await broker.export_recording(thread_id, "mp4")
    if video is None:
        return None, outcome.message or "the recording could not be encoded"
    from agent.github.http import github_client

    try:
        async with github_client(timeout=120.0) as client:
            url = await attachments.upload_video(
                client, token, repo_full_name, name="recording.mp4", data=video.data
            )
    except (attachments.AttachmentError, httpx2.HTTPError) as exc:
        logger.info(
            "Recording upload as a GitHub attachment failed; falling back to an animation",
            exc_info=True,
            extra={"repo_full_name": repo_full_name},
        )
        return None, str(exc)
    return url, None


async def _publish_animation(
    thread_id: str, repo_full_name: str, caption: str
) -> evidence.PublishedEvidence | str:
    from agent.github.http import github_client
    from agent.github.sandbox_access import repository_token

    access = await repository_token([repo_full_name], permissions={"contents": "write"})
    if not access.token:
        return "The GitHub App has no write access to this repository."
    for recording_format in ("gif", "webp"):
        outcome, animation = await broker.export_recording(thread_id, recording_format)
        if animation is None:
            return outcome.message or "The recording could not be encoded."
        if len(animation.data) > evidence.MAX_IMAGE_BYTES:
            continue
        async with github_client(token=access.token) as client:
            return await evidence.publish_image(
                client,
                repo_full_name,
                thread_id=thread_id,
                label="recording",
                caption=caption,
                data=animation.data,
                extension=recording_format,
            )
    return "The recording is too large to publish as an animation. Record something shorter."


async def browser_publish_recording(caption: str) -> str:
    thread_id, _ = _context()
    cfg = RunConfig.from_runtime()
    if not cfg.repo_full_name:
        return json.dumps({"status": "error", "message": "This thread has no repository."})
    url, reason = await _upload_video(thread_id, cfg.repo_full_name)
    if url is not None:
        return json.dumps(
            {
                "status": "ok",
                "kind": "video",
                "markdown": url,
                "note": "Put this URL alone on its own line in the PR description; GitHub "
                "then shows a video player.",
            }
        )
    try:
        published = await _publish_animation(thread_id, cfg.repo_full_name, caption)
    except (evidence.EvidenceError, RuntimeError) as exc:
        published = str(exc)
    if isinstance(published, str):
        return json.dumps(
            {
                "status": "error",
                "message": f"{published} Leave the recording out of the PR and say why.",
            }
        )
    return json.dumps(
        {
            "status": "ok",
            "kind": "animation",
            "markdown": published.markdown,
            "note": f"Published as an animated image because a video upload was not possible "
            f"({reason}). It plays on a loop, without controls.",
        }
    )


@audit_tool()
async def browser_use_saved_sign_in(origin: str) -> dict[str, JsonValue]:
    thread_id, workspace = _context()
    try:
        owner = await private_credential_login()
    except RuntimeError:
        owner = None
    if owner is None:
        return {
            "ok": False,
            "status": "refused",
            "message": "Saved sign-ins can only be used in a private thread its owner started.",
        }
    outcome = await broker.restore_sign_in(thread_id, origin, owner=owner, workspace_slug=workspace)
    return {
        "ok": outcome.status == "ok",
        **outcome.model_dump(exclude={"snapshot", "image_base64"}, exclude_none=True),
    }


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
            coroutine=browser_publish_screenshot,
            name="browser_publish_screenshot",
            description=prompt("tools/browser_publish_screenshot"),
            args_schema=_PublishArgs,
        ),
        StructuredTool.from_function(
            coroutine=browser_record_start,
            name="browser_record_start",
            description=prompt("tools/browser_record_start"),
            args_schema=_NoArgs,
        ),
        StructuredTool.from_function(
            coroutine=browser_record_stop,
            name="browser_record_stop",
            description=prompt("tools/browser_record_stop"),
            args_schema=_NoArgs,
        ),
        StructuredTool.from_function(
            coroutine=browser_publish_recording,
            name="browser_publish_recording",
            description=prompt("tools/browser_publish_recording"),
            args_schema=_PublishRecordingArgs,
        ),
        StructuredTool.from_function(
            coroutine=browser_use_saved_sign_in,
            name="browser_use_saved_sign_in",
            description=prompt("tools/browser_use_saved_sign_in"),
            args_schema=_SignInArgs,
        ),
        StructuredTool.from_function(
            coroutine=browser_stop,
            name="browser_stop",
            description=prompt("tools/browser_stop"),
            args_schema=_NoArgs,
        ),
    ]
