"""Talks to a session's neko server through the sandbox tunnel.

The REST calls authenticate with the session's admin token. A viewer gets a member
of their own, created for the connection and deleted after, whose profile follows
their ticket's role: only a control ticket may host, which is what lets input
through neko itself as well as through the relay's lease check.
"""

import json
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

import aiohttp
from pydantic import JsonValue

from agent.browser.models import BrowserSession
from agent.browser.neko import NekoError, api_token
from agent.dashboard.oauth import BrowserTicketRole

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_MAX_MESSAGE_BYTES = 1024 * 1024

type NekoEvent = dict[str, JsonValue]


def _profile(role: BrowserTicketRole) -> dict[str, bool | str]:
    return {
        "name": "viewer",
        "is_admin": False,
        "can_login": True,
        "can_connect": True,
        "can_watch": True,
        "can_host": role == "control",
        "can_share_media": False,
        "can_access_clipboard": False,
        "sends_inactive_cursor": False,
        "can_see_inactive_cursors": False,
    }


@dataclass(frozen=True)
class Member:
    username: str
    password: str


class NekoApi:
    """One viewer's connection to neko: a member, its WebSocket, and the admin REST calls."""

    def __init__(self, session: BrowserSession, port: int) -> None:
        self._base = f"http://127.0.0.1:{port}"
        self._admin = {"Authorization": f"Bearer {api_token(session)}"}
        self._http = aiohttp.ClientSession(timeout=_TIMEOUT)
        self._member: Member | None = None
        self._socket: aiohttp.ClientWebSocketResponse | None = None

    async def __aenter__(self) -> NekoApi:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def _check(self, response: aiohttp.ClientResponse, what: str) -> None:
        if response.status >= 400:
            raise NekoError(f"neko refused {what}: {response.status}")

    async def join(self, role: BrowserTicketRole) -> None:
        """Create this viewer's member, log it in, and open its WebSocket."""
        member = Member(f"v-{uuid.uuid4().hex}", secrets.token_urlsafe(24))
        created = await self._http.post(
            f"{self._base}/api/members",
            headers=self._admin,
            json={
                "username": member.username,
                "password": member.password,
                "profile": _profile(role),
            },
        )
        await self._check(created, "the viewer")
        self._member = member
        login = await self._http.post(
            f"{self._base}/api/login",
            json={"username": member.username, "password": member.password},
        )
        await self._check(login, "the viewer's login")
        token = (await login.json()).get("token")
        if not isinstance(token, str):
            raise NekoError("neko returned no session token")
        self._socket = await self._http.ws_connect(
            f"{self._base}/api/ws", params={"token": token}, max_msg_size=_MAX_MESSAGE_BYTES
        )

    async def events(self) -> aiohttp.ClientWebSocketResponse:
        if self._socket is None:
            raise NekoError("the viewer has not joined")
        return self._socket

    async def send(self, event: str, payload: Mapping[str, JsonValue] | None = None) -> None:
        if self._socket is None:
            raise NekoError("the viewer has not joined")
        await self._socket.send_str(json.dumps({"event": event, "payload": payload or {}}))

    async def set_screen(self, width: int, height: int, rate: int) -> None:
        response = await self._http.post(
            f"{self._base}/api/room/screen",
            headers=self._admin,
            json={"width": width, "height": height, "rate": rate},
        )
        await self._check(response, "the screen size")

    async def close(self) -> None:
        """Drop the viewer's WebSocket and member; the member must not outlive the connection."""
        try:
            if self._socket is not None:
                await self._socket.close()
            if self._member is not None:
                await self._http.delete(
                    f"{self._base}/api/members/{self._member.username}", headers=self._admin
                )
        finally:
            await self._http.close()
