"""Where the browser may go and which actions wait for a person.

Egress is decided per ``host:port``: the session's own loopback origins, which
an authorized caller opens by navigating to them, plus the endpoints an admin
approved. Everything else is refused here and again by the sandbox proxy.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel

from agent.browser.models import PageRef
from agent.browser.ops import (
    BrowserOp,
    ClickOp,
    FillOp,
    FindOp,
    NavigateOp,
    SelectOp,
    ref_name,
)

LOOPBACK_HOSTS = ("localhost", "127.0.0.1")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_SENSITIVE_WORDS = re.compile(
    r"\b("
    r"delete|remove|destroy|erase|wipe|deactivate|close account|"
    r"pay|buy|purchase|checkout|check out|place order|order now|donate|transfer|"
    r"subscribe|accept|agree|consent|authori[sz]e|grant|allow|"
    r"sign up|register|invite"
    r")\b",
    re.IGNORECASE,
)


class Endpoint(BaseModel):
    scheme: Literal["http", "https"]
    host: str
    port: int

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def loopback(self) -> bool:
        return self.host in LOOPBACK_HOSTS


def endpoint_of(url: str) -> Endpoint | None:
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host or parts.username or parts.password:
        return None
    if scheme == "http":
        return Endpoint(scheme="http", host=host, port=port or _DEFAULT_PORTS[scheme])
    return Endpoint(scheme="https", host=host, port=port or _DEFAULT_PORTS[scheme])


def normalize_approved_endpoint(value: str) -> str:
    """An admin-approved endpoint as ``host:port``; raises on anything that is not an origin."""
    text = value.strip()
    endpoint = endpoint_of(text if "://" in text else f"https://{text}")
    if endpoint is None or endpoint.loopback:
        raise ValueError(f"not an external http(s) origin: {value!r}")
    parts = urlsplit(text if "://" in text else f"https://{text}")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"approved endpoints are origins, without a path: {value!r}")
    return endpoint.key


def loopback_endpoints(port: int) -> list[str]:
    """Every spelling of one loopback origin, so a page on ``localhost`` can load ``127.0.0.1``."""
    return [f"{host}:{port}" for host in LOOPBACK_HOSTS]


def allowed_domains(approved_endpoints: Sequence[str]) -> str:
    """The browser's own domain allowlist, a second fence inside the proxy's."""
    hosts = dict.fromkeys(
        [*LOOPBACK_HOSTS, *(key.rpartition(":")[0] for key in approved_endpoints)]
    )
    return ",".join(hosts)


class Allow(BaseModel):
    verdict: Literal["allow"] = "allow"
    endpoints: list[str] = []


class Confirm(BaseModel):
    verdict: Literal["confirm"] = "confirm"
    reason: str
    endpoints: list[str] = []


class Refuse(BaseModel):
    verdict: Literal["refuse"] = "refuse"
    reason: str


Decision = Allow | Confirm | Refuse


def navigation(op: NavigateOp, approved_endpoints: Sequence[str]) -> Decision:
    target = endpoint_of(op.url)
    if target is None:
        return Refuse(reason="Only http and https URLs can be opened.")
    extra: list[str] = []
    for origin in op.allow_origins:
        endpoint = endpoint_of(origin)
        if endpoint is None or not endpoint.loopback:
            return Refuse(
                reason=f"allow_origins only accepts loopback origins; {origin!r} is not one."
            )
        extra.extend(loopback_endpoints(endpoint.port))
    if target.loopback:
        return Allow(endpoints=[*loopback_endpoints(target.port), *extra])
    if target.key in approved_endpoints:
        return Allow(endpoints=extra)
    return Refuse(
        reason=(
            f"{target.host} is not allowed. The browser reaches only this sandbox's own "
            "servers and the endpoints an admin approved."
        )
    )


def action(
    op: BrowserOp, *, page_url: str, refs: Mapping[str, PageRef], target_label: str | None
) -> Decision:
    """Whether a page action may run now, needs a person's confirmation, or cannot run.

    Only a click on a sensitive-looking element of an external page waits for a person.
    ``target_label`` names the element under a coordinate click, read by the executor.
    """
    page = endpoint_of(page_url)
    external = page is not None and not page.loopback
    if isinstance(op, FindOp):
        found = f"{op.name or ''} {op.value}"
        if external and op.do == "click" and _SENSITIVE_WORDS.search(found):
            return Confirm(reason=f'clicking "{found.strip()[:80]}"')
        return Allow()
    if not isinstance(op, ClickOp):
        if isinstance(op, FillOp | SelectOp) and ref_name(op.ref) not in refs:
            return Refuse(reason=f"{op.ref} is not in the latest snapshot; take a new snapshot.")
        return Allow()
    label = target_label
    if op.ref is not None:
        ref = refs.get(ref_name(op.ref))
        if ref is None:
            return Refuse(reason=f"{op.ref} is not in the latest snapshot; take a new snapshot.")
        label = ref.name
    if external and label is not None and _SENSITIVE_WORDS.search(label):
        return Confirm(reason=f'clicking "{label[:80]}"')
    return Allow()
