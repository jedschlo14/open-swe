"""Hold back a new pull request that changes UI files but carries no before/after evidence."""

import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import quote

from langchain.agents.middleware.types import AgentState
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from agent.browser.evidence import EVIDENCE_BRANCH
from agent.github.http import GITHUB_API_BASE, github_client, github_request
from agent.middleware.trace import OpenSWEMiddleware
from agent.prompts import prompt

logger = logging.getLogger(__name__)

_GUARDED_TOOL = "open_pull_request"
_BROWSER_ATTEMPT_TOOL = "browser_navigate"
_UI_SUFFIXES = (
    ".tsx",
    ".jsx",
    ".vue",
    ".svelte",
    ".astro",
    ".html",
    ".css",
    ".scss",
    ".sass",
    ".less",
)
_NON_VISUAL = re.compile(r"(\.|/)(test|spec|stories)\.[a-z]+$|(^|/)(__tests__|tests?|e2e)/")
_OMITTED = re.compile(r"screenshots?\s+omitted", re.IGNORECASE)
_MAX_LISTED_FILES = 5


def is_visual_file(path: str) -> bool:
    return path.lower().endswith(_UI_SUFFIXES) and _NON_VISUAL.search(path) is None


def has_screenshots(body: str) -> bool:
    return f"/blob/{EVIDENCE_BRANCH}/" in body


def states_omission(body: str) -> bool:
    return _OMITTED.search(body) is not None


def tried_browser(state: object) -> bool:
    messages = state.get("messages") if isinstance(state, Mapping) else None
    return any(
        isinstance(message, AIMessage)
        and any(call.get("name") == _BROWSER_ATTEMPT_TOOL for call in message.tool_calls)
        for message in messages or ()
    )


async def _changed_files(owner: str, repo: str, base: str, head: str) -> list[str] | None:
    from agent.github.sandbox_access import repository_token

    access = await repository_token([f"{owner}/{repo}"], permissions={"contents": "read"})
    url = (
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/compare/"
        f"{quote(base, safe='/')}...{quote(head, safe='/:')}"
    )
    async with github_client(token=access.token) as client:
        response = await github_request(client, "GET", url, params={"per_page": "100"})
    if response.status_code != 200:
        logger.info(
            "Could not list pull request files for the visual evidence check",
            extra={"repo_full_name": f"{owner}/{repo}", "status_code": response.status_code},
        )
        return None
    files = response.json().get("files")
    if not isinstance(files, list):
        return None
    return [
        item["filename"]
        for item in files
        if isinstance(item, dict) and isinstance(item.get("filename"), str)
    ]


class VisualEvidenceMiddleware(OpenSWEMiddleware):
    """Require before/after screenshots on UI PRs, or a stated reason after a browser attempt."""

    state_schema = AgentState

    async def _refusal(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_call = request.tool_call
        if tool_call.get("name") != _GUARDED_TOOL:
            return None
        args: Mapping[str, Any] = tool_call.get("args") or {}
        body, owner, repo = args.get("body"), args.get("owner"), args.get("repo")
        base, head = args.get("base"), args.get("head")
        if not isinstance(body, str) or has_screenshots(body):
            return None
        omitted = states_omission(body)
        if omitted and tried_browser(request.state):
            return None
        if not all(isinstance(value, str) and value for value in (owner, repo, base, head)):
            return None
        try:
            files = await _changed_files(str(owner), str(repo), str(base), str(head))
        except Exception:
            logger.warning(
                "Visual evidence check failed; letting the pull request through",
                extra={"repo_full_name": f"{owner}/{repo}"},
                exc_info=True,
            )
            return None
        visual = [path for path in files or () if is_visual_file(path)]
        if not visual:
            return None
        listed = ", ".join(f"`{path}`" for path in visual[:_MAX_LISTED_FILES])
        if len(visual) > _MAX_LISTED_FILES:
            listed += f" and {len(visual) - _MAX_LISTED_FILES} more"
        return ToolMessage(
            content=json.dumps(
                {
                    "success": False,
                    "code": "visual_evidence_missing",
                    "error": prompt("runs/missing-visual-evidence", files=listed, omitted=omitted),
                }
            ),
            tool_call_id=tool_call.get("id"),
            name=_GUARDED_TOOL,
            status="error",
        )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        refusal = await self._refusal(request)
        if refusal is not None:
            return refusal
        return await handler(request)
