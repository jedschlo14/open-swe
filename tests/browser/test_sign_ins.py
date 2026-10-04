import pytest
from fastapi import HTTPException
from pydantic import JsonValue

from agent.browser import lease, routes, sign_ins


def test_only_cookies_for_the_origin_host_are_saved() -> None:
    data = {
        "cookies": [
            {"name": "sid", "value": "a", "domain": "app.example.com", "httpOnly": True},
            {"name": "sso", "value": "b", "domain": ".example.com", "expires": 1999999999.5},
            {"name": "ads", "value": "c", "domain": "ads.tracker.net"},
            {"name": "lookalike", "value": "d", "domain": "badexample.com"},
        ]
    }

    cookies = sign_ins.parse_cookies(data, "app.example.com")

    assert [(c.name, c.http_only, c.expires) for c in cookies] == [
        ("sid", True, None),
        ("sso", False, 1999999999),
    ]


def test_loopback_origins_cannot_be_saved() -> None:
    assert sign_ins.canonical_origin("http://localhost:3000/app") is None
    assert sign_ins.canonical_origin("https://Staging.Example.com:443/x?y=1") == (
        "https://staging.example.com"
    )


async def test_handing_back_with_save_is_refused_outside_your_own_private_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata: dict[str, JsonValue] = {"visibility": "private", "owner_login": "alice"}

    async def writable(thread_id: str, session: dict[str, str]) -> dict[str, JsonValue]:
        return metadata

    async def hand_back(thread_id: str, login: str) -> None:
        raise AssertionError("control must stay with the person when the save is refused")

    monkeypatch.setattr(routes, "_writable", writable)
    monkeypatch.setattr(lease, "hand_back", hand_back)

    with pytest.raises(HTTPException) as refused:
        await routes.api_browser_handback(
            "thread", routes.HandbackRequest(save_sign_in=True), {"sub": "bob"}
        )

    assert refused.value.status_code == 403
