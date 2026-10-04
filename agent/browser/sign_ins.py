"""Saved browser sign-ins: one encrypted session per person and exact external origin.

A sign-in is the cookies and localStorage a person's browser held for one origin
after they signed in during a takeover. Restoring it is the owner's own choice
of that origin, so it needs no per-use confirmation, but it only ever happens in
the owner's private threads and lasts ``SIGN_IN_LIFETIME``.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue
from pydantic.alias_generators import to_camel
from sqlalchemy import text

from agent.browser.policy import endpoint_of
from agent.database import postgres
from agent.encryption import decrypt_token, encrypt_token

SIGN_IN_LIFETIME = timedelta(days=30)
MAX_STATE_CHARS = 64_000
SignInStatus = Literal["active", "expired"]


class SignInCookie(BaseModel):
    name: str
    value: str
    domain: str
    path: str = "/"
    http_only: bool = False
    secure: bool = False
    same_site: Literal["Strict", "Lax", "None"] | None = None
    expires: int | None = None


class SignInState(BaseModel):
    cookies: list[SignInCookie]
    local_storage: dict[str, str]


class SavedSignIn(BaseModel):
    """What the dashboard and the agent may see of a saved sign-in: never its contents."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    sign_in_id: str
    origin: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    status: SignInStatus


def canonical_origin(url: str) -> str | None:
    """``scheme://host[:port]`` for an external http(s) URL, else ``None``."""
    endpoint = endpoint_of(url)
    if endpoint is None or endpoint.loopback:
        return None
    default_port = 443 if endpoint.scheme == "https" else 80
    port = "" if endpoint.port == default_port else f":{endpoint.port}"
    return f"{endpoint.scheme}://{endpoint.host}{port}"


def _cookie_applies(cookie_domain: str, host: str) -> bool:
    domain = cookie_domain.lstrip(".").lower()
    return host == domain or host.endswith(f".{domain}")


def _flag(value: JsonValue) -> bool:
    return value is True


def _same_site(value: JsonValue) -> Literal["Strict", "Lax", "None"] | None:
    match value:
        case "Strict":
            return "Strict"
        case "Lax":
            return "Lax"
        case "None":
            return "None"
        case _:
            return None


def parse_cookies(data: Mapping[str, JsonValue], host: str) -> list[SignInCookie]:
    """The cookies from ``agent-browser cookies get`` that belong to ``host``."""
    raw = data.get("cookies")
    if not isinstance(raw, list):
        return []
    cookies: list[SignInCookie] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name, value, domain = item.get("name"), item.get("value"), item.get("domain")
        if not (isinstance(name, str) and isinstance(value, str) and isinstance(domain, str)):
            continue
        if not _cookie_applies(domain, host):
            continue
        path, same_site, expires = item.get("path"), item.get("sameSite"), item.get("expires")
        cookies.append(
            SignInCookie(
                name=name,
                value=value,
                domain=domain,
                path=path if isinstance(path, str) and path else "/",
                http_only=_flag(item.get("httpOnly")),
                secure=_flag(item.get("secure")),
                same_site=_same_site(same_site),
                expires=(
                    int(expires)
                    if isinstance(expires, int | float)
                    and not isinstance(expires, bool)
                    and expires > 0
                    else None
                ),
            )
        )
    return cookies


def parse_local_storage(data: Mapping[str, JsonValue]) -> dict[str, str]:
    stored = data.get("data")
    if not isinstance(stored, dict):
        return {}
    return {key: value for key, value in stored.items() if isinstance(value, str)}


class _Row(BaseModel):
    sign_in_id: str
    origin: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    expired_at: datetime | None

    def view(self) -> SavedSignIn:
        expired = self.expired_at is not None or self.expires_at <= datetime.now(UTC)
        return SavedSignIn(
            sign_in_id=self.sign_in_id,
            origin=self.origin,
            created_at=self.created_at,
            expires_at=self.expires_at,
            last_used_at=self.last_used_at,
            status="expired" if expired else "active",
        )


def _view(row: Mapping[str, object]) -> SavedSignIn:
    return _Row.model_validate(dict(row)).view()


_COLUMNS = "sign_in_id, origin, created_at, expires_at, last_used_at, expired_at"


async def save(owner: str, origin: str, state: SignInState) -> SavedSignIn:
    """Store ``state`` for ``owner`` and ``origin``, replacing any earlier one."""
    async with postgres.transaction() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        f"""
                        INSERT INTO browser_saved_sign_in
                            (sign_in_id, owner_login, origin, encrypted_state, expires_at)
                        VALUES (:sign_in_id, :owner, :origin, :state, :expires_at)
                        ON CONFLICT (owner_login, origin) DO UPDATE SET
                            encrypted_state = EXCLUDED.encrypted_state,
                            created_at = clock_timestamp(),
                            expires_at = EXCLUDED.expires_at,
                            last_used_at = NULL,
                            expired_at = NULL
                        RETURNING {_COLUMNS}
                        """
                    ),
                    {
                        "sign_in_id": uuid.uuid4().hex[:16],
                        "owner": owner.lower(),
                        "origin": origin,
                        "state": encrypt_token(state.model_dump_json()),
                        "expires_at": datetime.now(UTC) + SIGN_IN_LIFETIME,
                    },
                )
            )
            .mappings()
            .one()
        )
    return _view(row)


async def list_for(owner: str) -> list[SavedSignIn]:
    async with postgres.transaction() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        f"SELECT {_COLUMNS} FROM browser_saved_sign_in"
                        " WHERE owner_login = :owner ORDER BY origin"
                    ),
                    {"owner": owner.lower()},
                )
            )
            .mappings()
            .all()
        )
    return [_view(row) for row in rows]


async def delete(owner: str, sign_in_id: str) -> bool:
    async with postgres.transaction() as conn:
        result = await conn.execute(
            text(
                "DELETE FROM browser_saved_sign_in"
                " WHERE sign_in_id = :sign_in_id AND owner_login = :owner"
            ),
            {"sign_in_id": sign_in_id, "owner": owner.lower()},
        )
    return result.rowcount > 0


async def load(owner: str, origin: str) -> tuple[SavedSignIn, SignInState] | None:
    """The owner's sign-in for ``origin`` with its contents; ``None`` when none is saved."""
    async with postgres.transaction() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        f"SELECT {_COLUMNS}, encrypted_state FROM browser_saved_sign_in"
                        " WHERE owner_login = :owner AND origin = :origin"
                    ),
                    {"owner": owner.lower(), "origin": origin},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        return None
    state = decrypt_token(str(row["encrypted_state"]))
    if not state:
        return None
    return _view(row), SignInState.model_validate_json(state)


async def mark_used(sign_in_id: str) -> None:
    async with postgres.transaction() as conn:
        await conn.execute(
            text(
                "UPDATE browser_saved_sign_in SET last_used_at = clock_timestamp()"
                " WHERE sign_in_id = :sign_in_id"
            ),
            {"sign_in_id": sign_in_id},
        )


async def mark_expired(sign_in_id: str) -> None:
    async with postgres.transaction() as conn:
        await conn.execute(
            text(
                "UPDATE browser_saved_sign_in SET expired_at = clock_timestamp()"
                " WHERE sign_in_id = :sign_in_id"
            ),
            {"sign_in_id": sign_in_id},
        )


def origins(sign_ins: Sequence[SavedSignIn]) -> list[str]:
    return [sign_in.origin for sign_in in sign_ins if sign_in.status == "active"]
