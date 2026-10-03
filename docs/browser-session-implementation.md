# Browser sessions: implementation

## Runtime and startup

Run one isolated headless Chromium profile and page per cloud thread in its existing LangSmith-backed coding sandbox/workspace, so it can reach the app under test. Start on explicit agent or authorized dashboard use—not panel open. Session management binds the browser to the thread and ensures the sandbox for dashboard-first starts. Startup must be idempotent. The Browser panel descriptor is presentation state, not session state or authorization. Hiding it does not stop the browser. Stop on request, thread closure, or a provisional one-hour idle timeout; warn before expiry. Stopping may lose page state. Support cancellation and infrastructure timeouts. Local desktop needs a separate adapter. Admin policy must gate provisioning, agent tools, viewing, and control.

## Agent and user control

Agent tools and dashboard view share the same page through an authenticated, thread-scoped broker. Keep CDP loopback-only; expose narrow operations. Prefer accessible page structure and stable references; screenshots/coordinates are visual fallback. Readers may watch; writers may control, with separate read/write authorization. A single lease pauses agent actions during user takeover; settle or cancel in-flight actions first. On handback revoke user input, discard stale actions, and refresh agent context. Disconnect does not transfer control. Track lifecycle, viewers, agent progress, and lease separately; failure/stop revokes control. Define lease expiry and recovery. Browser-level leases cannot constrain shell-launched browsers unless shell access is also gated.

## Network and evidence

Restrict browser egress to the authorized local app and required development endpoints; block other internet and local/private/link-local destinations, including redirects and DNS changes. Treat page text, URLs, captures, diagnostics, downloads, and tool results as untrusted. Require authorization in the executor; prompt-injection defenses do not replace isolation. Diagnostics are opt-in, redacted, truncated, and bounded. Do not log page content, credentials, or cookies; distinguish stream loss from browser failure and mark stale views.

For applicable UI PRs, attach sanitized before/after screenshots when safe and useful; include a recording only when it adds meaningful evidence. Keep captures out of Git and use bounded retention. Do not use bearer download links as PR evidence; they are not scoped to PR readers. The image proxy serves published raster images but cannot upload or support video. Implementation needs a repository-authorized publisher with access checks/retention and a safe video path; otherwise omit evidence and explain why.

Existing prototypes inform this choice: the right-panel prototype tunnels loopback CDP and streams a screencast but lacks agent tools and lease arbitration; Stagehand's session map is process-local; Browserbase cannot reach the thread sandbox's local app. `agent-browser` is not in the agent runtime contract; Playwright is currently used by UI/E2E only.