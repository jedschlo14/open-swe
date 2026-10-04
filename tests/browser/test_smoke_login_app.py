import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator

import pytest

from agent.browser import policy, sign_ins
from scripts.smoke_login_app import DEFAULT_HOST, DEFAULT_PORT, SmokeLoginServer


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


@pytest.fixture
def base_url() -> Iterator[str]:
    server = SmokeLoginServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _post(url: str, fields: dict[str, str], cookie: str = "") -> tuple[int, str, str]:
    request = urllib.request.Request(url, urllib.parse.urlencode(fields).encode())
    if cookie:
        request.add_header("Cookie", cookie)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        response = opener.open(request)
    except urllib.error.HTTPError as error:
        response = error
    return response.status, response.headers.get("Set-Cookie", ""), response.read().decode()


def _get(url: str, cookie: str = "") -> str:
    request = urllib.request.Request(url)
    if cookie:
        request.add_header("Cookie", cookie)
    with urllib.request.urlopen(request) as response:
        return response.read().decode()


def test_login_page_shows_a_password_field_when_signed_out(base_url: str) -> None:
    assert 'type="password"' in _get(base_url)


def test_wrong_password_is_rejected(base_url: str) -> None:
    status, set_cookie, body = _post(f"{base_url}/login", {"username": "demo", "password": "no"})

    assert status == 401
    assert set_cookie == ""
    assert "Wrong username or password" in body


def test_login_sets_a_cookie_that_signs_the_person_in_until_logout(base_url: str) -> None:
    status, set_cookie, _ = _post(f"{base_url}/login", {"username": "demo", "password": "demo"})
    cookie = set_cookie.split(";")[0]

    assert status == 303
    assert "Signed in as demo" in _get(base_url, cookie)

    _post(f"{base_url}/logout", {}, cookie)

    assert 'type="password"' in _get(base_url, cookie)


def test_default_endpoint_is_saveable_without_any_external_site() -> None:
    approved = policy.normalize_approved_endpoint(f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")

    assert approved == f"{DEFAULT_HOST}:{DEFAULT_PORT}"
    assert sign_ins.canonical_origin(f"http://{approved}/") == f"http://{approved}"
