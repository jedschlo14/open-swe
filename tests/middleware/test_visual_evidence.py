import json
from typing import Any, cast

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from agent.middleware import visual_evidence
from agent.middleware.visual_evidence import VisualEvidenceMiddleware

_EVIDENCE = "![after](https://github.com/o/r/blob/open-swe-evidence/evidence/x.png?raw=true)"


class _Request:
    def __init__(self, body: str) -> None:
        self.tool_call = {
            "name": "open_pull_request",
            "args": {"owner": "o", "repo": "r", "base": "main", "head": "fix", "body": body},
            "id": "call-1",
        }


async def _handler(_request: Any) -> ToolMessage:
    return ToolMessage(content="created", tool_call_id="call-1")


@pytest.mark.parametrize(
    ("files", "body", "blocked"),
    [
        (["ui/src/Message.tsx"], "Fixes the bubble width.", True),
        (["ui/src/Message.tsx"], f"| Before | After |\n|---|---|\n| {_EVIDENCE} | x |", False),
        (["ui/src/Message.tsx"], "Before/after screenshots omitted: needs prod data.", False),
        (["agent/server.py", "ui/src/Message.test.tsx"], "Backend fix.", False),
    ],
)
async def test_open_pull_request_requires_evidence_for_ui_changes(
    monkeypatch: pytest.MonkeyPatch, files: list[str], body: str, blocked: bool
) -> None:
    async def changed_files(*_args: str) -> list[str]:
        return files

    monkeypatch.setattr(visual_evidence, "_changed_files", changed_files)

    result = await VisualEvidenceMiddleware().awrap_tool_call(
        cast(ToolCallRequest, _Request(body)), _handler
    )

    assert isinstance(result, ToolMessage)
    if blocked:
        assert result.status == "error"
        payload = json.loads(str(result.content))
        assert payload["code"] == "visual_evidence_missing"
        assert "`ui/src/Message.tsx`" in payload["error"]
    else:
        assert result.content == "created"
