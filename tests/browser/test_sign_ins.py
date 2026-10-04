from agent.browser import sign_ins


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
