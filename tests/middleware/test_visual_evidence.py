import json
from typing import Any, cast

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage

from agent.middleware import visual_evidence
from agent.middleware.visual_evidence import VisualEvidenceMiddleware

_EVIDENCE = "![after](https://github.com/o/r/blob/open-swe-evidence/evidence/x.png?raw=true)"
_OMITTED = "Before/after screenshots omitted: the chat view needs an authenticated backend."
_NAVIGATED = AIMessage(
    content="",
    tool_calls=[{"name": "browser_navigate", "args": {"url": "http://localhost:3000"}, "id": "b1"}],
)


class _Request:
    def __init__(self, body: str, browsed: bool) -> None:
        self.state = {"messages": [_NAVIGATED] if browsed else []}
        self.tool_call = {
            "name": "open_pull_request",
            "args": {"owner": "o", "repo": "r", "base": "main", "head": "fix", "body": body},
            "id": "call-1",
        }


async def _handler(_request: Any) -> ToolMessage:
    return ToolMessage(content="created", tool_call_id="call-1")


@pytest.mark.parametrize(
    ("files", "body", "browsed", "blocked"),
    [
        (["ui/src/Message.tsx"], "Fixes the bubble width.", True, True),
        (
            ["ui/src/Message.tsx"],
            f"| Before | After |\n|---|---|\n| {_EVIDENCE} | x |",
            False,
            False,
        ),
        (["ui/src/Message.tsx"], _OMITTED, False, True),
        (["ui/src/Message.tsx"], _OMITTED, True, False),
        (["agent/server.py", "ui/src/Message.test.tsx"], "Backend fix.", False, False),
    ],
)
async def test_open_pull_request_requires_evidence_for_ui_changes(
    monkeypatch: pytest.MonkeyPatch, files: list[str], body: str, browsed: bool, blocked: bool
) -> None:
    async def changed_files(*_args: str) -> list[str]:
        return files

    monkeypatch.setattr(visual_evidence, "_changed_files", changed_files)

    result = await VisualEvidenceMiddleware().awrap_tool_call(
        cast(ToolCallRequest, _Request(body, browsed)), _handler
    )

    assert isinstance(result, ToolMessage)
    if blocked:
        assert result.status == "error"
        payload = json.loads(str(result.content))
        assert payload["code"] == "visual_evidence_missing"
        assert "`ui/src/Message.tsx`" in payload["error"]
    else:
        assert result.content == "created"
