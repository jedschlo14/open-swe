"""A tiny self-contained login app for smoke-testing browser sign-ins without any external site.

Run it in the sandbox, approve its endpoint (default ``127.0.0.2:8765``, which the browser
treats as an external origin) in Settings, then sign in as ``demo`` / ``demo``.
"""

import argparse
import secrets
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

USERNAME = "demo"
PASSWORD = "demo"
COOKIE = "smoke_session"
DEFAULT_HOST = "127.0.0.2"
DEFAULT_PORT = 8765

_LOGIN_PAGE = """<!doctype html>
<meta charset="utf-8"><title>Smoke login</title>
<h1>Smoke login</h1>
{error}
<form method="post" action="/login">
  <label>Username <input name="username" autocomplete="username"></label><br>
  <label>Password <input name="password" type="password" autocomplete="current-password"></label><br>
  <button type="submit">Sign in</button>
</form>
<p>Hint: demo / demo</p>
"""

_HOME_PAGE = """<!doctype html>
<meta charset="utf-8"><title>Smoke home</title>
<h1>Signed in as {user}</h1>
<script>localStorage.setItem("smoke_user", "{user}");</script>
<form method="post" action="/logout"><button type="submit">Sign out</button></form>
"""


class SmokeLoginServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _Handler)
        self.sessions: dict[str, str] = {}


class _Handler(BaseHTTPRequestHandler):
    server: SmokeLoginServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _user(self) -> str | None:
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        morsel = jar.get(COOKIE)
        return self.server.sessions.get(morsel.value) if morsel else None

    def _send(self, status: int, body: str, headers: dict[str, str] | None = None) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str, headers: dict[str, str] | None = None) -> None:
        self._send(303, "", {"Location": location, **(headers or {})})

    def do_GET(self) -> None:
        user = self._user()
        if user:
            self._send(200, _HOME_PAGE.format(user=user))
        else:
            self._send(200, _LOGIN_PAGE.format(error=""))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode())
        if self.path == "/logout":
            jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            if COOKIE in jar:
                self.server.sessions.pop(jar[COOKIE].value, None)
            self._redirect("/", {"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly"})
            return
        if self.path != "/login":
            self._send(404, "Not found")
            return
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        if secrets.compare_digest(username, USERNAME) and secrets.compare_digest(
            password, PASSWORD
        ):
            token = secrets.token_urlsafe(24)
            self.server.sessions[token] = username
            self._redirect("/", {"Set-Cookie": f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"})
        else:
            self._send(401, _LOGIN_PAGE.format(error="<p>Wrong username or password.</p>"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    server = SmokeLoginServer((args.host, args.port))
    print(f"Smoke login app on http://{args.host}:{args.port} (demo / demo)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
