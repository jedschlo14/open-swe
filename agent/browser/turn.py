"""Short-lived TURN credentials for the thread browser's live view.

The live view is WebRTC between the sandbox and the viewer's browser. The sandbox
accepts no inbound UDP, so both ends meet at a TURN relay we run or rent
(coturn ``use-auth-secret`` scheme). Credentials are minted per use from a shared
secret that never leaves the server, and each names the browser session so a
relay's logs can attribute traffic to it. The credential handed to the sandbox's
WebRTC endpoint is longer-lived than a viewer's, because it cannot be rotated
without restarting that endpoint; the relay's own quotas bound what it is worth.
"""

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass

from pydantic import JsonValue

from agent.config import ENV

VIEWER_TTL_SECONDS = 15 * 60
BACKEND_TTL_SECONDS = 12 * 60 * 60


@dataclass(frozen=True)
class IceServer:
    urls: tuple[str, ...]
    username: str
    credential: str

    def as_json(self) -> dict[str, JsonValue]:
        urls: list[JsonValue] = list(self.urls)
        return {"urls": urls, "username": self.username, "credential": self.credential}


def turn_urls() -> tuple[str, ...]:
    raw = ENV.BROWSER_TURN_URLS.get()
    return tuple(url for url in (part.strip() for part in raw.split(",")) if url)


def configured() -> bool:
    return bool(turn_urls()) and bool(ENV.BROWSER_TURN_SECRET.get())


def mint(session_id: str, *, ttl_seconds: int, now: float | None = None) -> IceServer:
    """A TURN credential for ``session_id`` valid for ``ttl_seconds``."""
    secret = ENV.BROWSER_TURN_SECRET.get()
    if not secret or not turn_urls():
        raise RuntimeError("TURN is not configured")
    expires = int((time.time() if now is None else now) + ttl_seconds)
    username = f"{expires}:{session_id}"
    digest = hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    return IceServer(turn_urls(), username, base64.b64encode(digest).decode())
