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
    DialogOp,
    FillOp,
    NavigateOp,
    PressOp,
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
        return Confirm(
            reason=f"opening {target.host}, an external endpoint an admin approved",
            endpoints=extra,
        )
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

    ``target_label`` names the element under a coordinate click, read by the executor.
    """
    page = endpoint_of(page_url)
    external = page is not None and not page.loopback
    if isinstance(op, DialogOp):
        return Confirm(reason="accepting a page dialog") if op.accept else Allow()
    if not isinstance(op, ClickOp | FillOp | SelectOp | PressOp):
        return Allow()
    label = target_label
    if isinstance(op, ClickOp | FillOp | SelectOp) and op.ref is not None:
        ref = refs.get(ref_name(op.ref))
        if ref is None:
            return Refuse(reason=f"{op.ref} is not in the latest snapshot; take a new snapshot.")
        label = ref.name
    if external and not isinstance(op, FillOp):
        return Confirm(reason=f"acting on {page.host if page else 'an external page'}")
    if isinstance(op, ClickOp) and label is not None and _SENSITIVE_WORDS.search(label):
        return Confirm(reason=f'clicking "{label[:80]}"')
    if isinstance(op, ClickOp) and label is None:
        return Confirm(reason="clicking an element the executor could not identify")
    return Allow()
