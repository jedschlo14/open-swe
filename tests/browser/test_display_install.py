from dataclasses import dataclass, field

import pytest

from agent.browser import engine


@dataclass
class _Result:
    exit_code: int
    stderr: str = ""


@dataclass
class _Sandbox:
    results: list[int]
    commands: list[str] = field(default_factory=list)

    async def run(self, command: str, timeout: int) -> _Result:
        self.commands.append(command)
        return _Result(self.results.pop(0))


@pytest.mark.asyncio
async def test_display_present_skips_install() -> None:
    sandbox = _Sandbox([0])
    assert await engine._ensure_display(sandbox) is True  # type: ignore[arg-type]
    assert sandbox.commands == [engine._DISPLAY_CHECK]


@pytest.mark.asyncio
async def test_display_installed_when_missing() -> None:
    sandbox = _Sandbox([1, 0, 0])
    assert await engine._ensure_display(sandbox) is True  # type: ignore[arg-type]
    assert sandbox.commands[1] == engine._DISPLAY_INSTALL


@pytest.mark.asyncio
async def test_failed_install_falls_back_to_headless() -> None:
    sandbox = _Sandbox([1, 100])
    assert await engine._ensure_display(sandbox) is False  # type: ignore[arg-type]
