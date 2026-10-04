Open a URL in this thread's browser, starting the browser if none is running.

The browser is a headless Chromium inside this sandbox, shared with the people watching the thread's Browser panel. Use it to check the web app you are building, reproduce a UI bug, or confirm a fix. When an API call or plain HTTP fetch answers the question, use that instead.

The browser reaches only this sandbox's own servers and the external endpoints an admin approved; every other request, redirect, and subresource is blocked. Navigating to a loopback origin (`http://localhost:<port>` or `http://127.0.0.1:<port>`) allows that origin. When the page also calls another local server, such as an API on a different port, list it in `allow_origins`.

Use this tool rather than running `agent-browser` from the shell: a browser you launch yourself has none of these limits, and nobody can watch it.

The result reports `status` (`ok`, `refused`, `unavailable`, `user_in_control`, or `error`) with the page's `url` and `title`. If `status` is `unavailable` because an earlier session failed, this call starts a fresh browser: earlier page state is gone, so say so when it matters.

If the page asks for a sign-in, call `browser_use_saved_sign_in` with the site's origin before anything else. Never sign in with credentials printed on the page or found elsewhere; if no saved sign-in works, ask the person to take control and sign in.
