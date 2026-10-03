# Browser sessions: implementation

## Where it runs and when it starts

Run one isolated headless Chromium profile and page per cloud thread in the thread's existing LangSmith-backed coding sandbox, where it can reach the app under test. Start lazily on the first explicit browser action by the agent or an authorized user—not when the thread or panel opens. A session manager ensures and binds the sandbox, including for dashboard-first use, and makes concurrent starts idempotent. Expose starting, ready, stopping, stopped, and failed states; on sandbox loss, report failure rather than silently replacing the profile. The panel entry is presentation state, not session state or authorization. Hiding it does not stop the browser. Stop on request, thread closure, or an admin-configured idle timeout (default one hour); warn before expiry. Local desktop requires a separate adapter.

## How the agent drives it

Agent browser tools and the dashboard use the same page through an authenticated broker with typed operations and a thread-scoped live view; never expose provider URLs or CDP directly. Keep Chrome DevTools Protocol (CDP) loopback-only and expose narrow browser operations. Prefer accessible page structure and stable element references; use screenshots or coordinates for visual checks or inaccessible interfaces. Refresh references after navigation or material changes, and verify meaningful actions.

## How egress is contained

A sandbox-wide allowlist would also cut off git, package installs, and the app's own backend, so containment is per browser instead. Each session's browser runs in its own network namespace that has only loopback. Its only way out is an egress proxy that listens on that loopback, connects from the sandbox's own namespace, and admits only the session's allowlist of `host:port` pairs for every request, redirect, and subresource. The browser's own domain allowlist is a second fence that also disables WebRTC. If the proxy is not running, the browser has no network: it fails closed.

The allowlist starts with the external endpoints an admin approved (`browser_approved_dev_endpoints`). A loopback origin is added when an authorized controller explicitly navigates to it, together with any other loopback origins named in that navigation (`allow_origins`). Navigating to an approved external endpoint, or acting on one of its pages, needs a person's confirmation. Everything else is refused. Browsers launched from the shell are not contained, because the agent's shell has root in the sandbox.

## How a user watches and takes control

Authorize live view with thread-read access and control with thread-write access, rechecking on connect and each action. Do not rely on terminal authorization for read-only viewers. A single lease arbitrates browser actions. Before takeover, stop dispatching agent actions and settle or cancel in-flight work. While the user controls the page, the agent sends no browser actions. On handback, revoke user input, discard stale actions, and refresh the agent's page context. Disconnect alone does not transfer control; stop or failure revokes the lease. Show the current controller and handoff state. Browser-level arbitration cannot constrain shell-launched browsers unless shell access is also gated.

## How screenshots and recordings get into the PR

Capture and sanitize evidence in the sandbox. For applicable UI-change and UI-bug-fix PRs, upload a before/after screenshot pair through a repository-authorized publisher, then add the resulting image references to the PR; include a recording only when it adds meaningful evidence and link it through an approved, access-controlled publisher. Never use bearer download links as PR evidence: they may outlive access and are not scoped to PR readers. Keep captures out of Git, exclude unrelated private content, and apply bounded retention. If safe publishing is unavailable, omit the evidence and explain why.

PR publishing may follow the session MVP; until a publisher verifies repository access and enforces retention, omit evidence and explain why. The existing PR image proxy displays published raster images but cannot upload media or support video. Treat page content as untrusted and enforce authorization in the executor.
