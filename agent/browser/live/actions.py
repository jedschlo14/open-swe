"""What a person does from the panel's toolbar and clipboard, run as the lease holder."""

import logging
from collections.abc import Callable, Mapping

from langsmith.sandbox import AsyncSandbox

from agent.browser import engine, policy, store
from agent.browser.live.messages import CopyMessage, NavigateMessage, PasteMessage
from agent.browser.models import BrowserSession
from agent.browser.ops import NavigateOp, commands

logger = logging.getLogger(__name__)

type Notify = Callable[[Mapping[str, str]], None]


def with_scheme(address: str) -> str:
    """What a person typed in the address bar, as a URL: bare local hosts get ``http``."""
    text = address.strip()
    if "://" in text:
        return text
    host = text.split("/", 1)[0].rpartition("@")[2].rsplit(":", 1)[0].lower()
    return f"http://{text}" if host in policy.LOOPBACK_HOSTS else f"https://{text}"


class PersonActions:
    """Runs one viewer's toolbar and clipboard requests against the session's browser."""

    def __init__(self, sandbox: AsyncSandbox, session: BrowserSession, notify: Notify) -> None:
        self._sandbox = sandbox
        self._session = session
        self._notify = notify

    async def copy(self, _message: CopyMessage) -> None:
        text = await engine.selection_text(self._sandbox, self._session)
        self._notify({"type": "clipboard", "text": text})

    async def paste(self, message: PasteMessage) -> None:
        await self.paste_text(message.text)

    async def paste_text(self, text: str) -> None:
        await engine.insert_text(self._sandbox, self._session, text)

    async def navigate(self, message: NavigateMessage) -> None:
        if message.action != "go":
            await engine.run_command(self._sandbox, self._session, [message.action])
            return
        op = NavigateOp(url=with_scheme(message.url))
        decision = policy.navigation(op, self._session.approved_endpoints)
        if isinstance(decision, policy.Refuse):
            self._notify({"type": "notice", "message": decision.reason})
            return
        if decision.endpoints:
            widened = await store.allow_endpoints(self._session.session_id, decision.endpoints)
            if widened is None:
                return
            await engine.write_allowlist(self._sandbox, widened, widened.allowed_endpoints)
        for args in commands(op):
            await engine.run_command(self._sandbox, self._session, args)
