# Browser sessions: implementation

Cloud MVP runs one isolated headless Chromium profile and primary page per thread in its existing LangSmith-backed coding sandbox and workspace snapshot, so it can reach the app under test. Local desktop needs a separate host adapter.

## Where it runs and when it starts

Start on the first explicit browser action by the agent or an authorized dashboard user—not when a thread or panel opens. A session manager associates the browser with the thread, ensures and binds the sandbox for dashboard-first use, and makes startup idempotent under concurrent requests. The panel's saved Browser tab is presentation state, not session state or authorization; restoring it does not start compute. Hiding it does not stop the browser.

Stop on request, thread deletion or resolution, or inactivity. One hour is a provisional idle timeout; warn before expiry. Stopping may lose page state. Support cancellation and infrastructure timeouts. An administrator policy must gate provisioning, agent tools, viewing, and control; no browser-specific flag exists yet.

## How the agent drives it

The agent and dashboard operate on the same page through an authenticated broker. Prefer visible accessibility-tree and page-text reads with stable references; use screenshots or coordinates for visual checks or inaccessible interfaces. Refresh references after navigation or material changes, verify meaningful actions, and do not return hidden DOM or raw source by default. Keep CDP loopback-only and expose narrow operations, not raw CDP.

## How users watch and take control

The dashboard uses a thread-scoped channel with separate read and write authorization; terminal authorization alone is insufficient for read-only viewers. Readers may watch; writers may control. A single lease arbitrates browser actions: stop dispatching agent actions and settle or cancel in-flight work before takeover; while a participant controls the page, the agent sends no actions. On handback, revoke participant input, discard stale actions, and have the agent reread the page. Disconnect alone does not transfer control. Keep lifecycle, viewers, agent progress, and control ownership distinct; stop or failure revokes the lease, and stale or non-ready sessions cannot accept input. Show the current controller and pending handoffs, with visible lease expiry and recovery.

Browser-level arbitration cannot constrain shell-launched browsers unless shell access is separately gated; admin disable cannot block them otherwise. Do not claim exclusivity beyond supported paths. Restrict network egress outside Chromium to the authorized local app and required development endpoints; block other internet and local/private/link-local destinations, including redirects and DNS changes.

## Screenshots, recordings, and PRs

For UI-change and UI-bug-fix PRs, include a sanitized before/after screenshot pair when the browser can reproduce the original and verify the result, if safe and useful. Add a short recording only when it materially demonstrates behavior screenshots cannot. For other tasks, capture evidence only when requested or materially useful. Exclude unrelated private content, mask sensitive regions when reliable, keep captures out of Git, and apply bounded retention. If safe publishing is unavailable, omit the evidence and explain why.

Do not use bearer download URLs as PR evidence: they may outlive access and are not scoped to PR readers. The PR image proxy only serves published raster images; it cannot upload media or support video. A repository-authorized publisher with access checks and retention, and a safe video path, remain to be implemented.

## Current baseline and remaining decisions

The right-panel prototype installs Chromium in an existing sandbox, binds CDP to loopback, tunnels it to the app server with a short-lived ticket, and streams a dashboard screencast. It reuses terminal authorization and relays raw CDP; it lacks agent tools and shared lease arbitration, and terminal auth is inadequate for read-only viewers. The Stagehand prototype has process-local session state; Browserbase runs outside the thread sandbox and cannot reach its local app. `agent-browser` supports CDP but is not in the agent runtime contract; Playwright is used by UI/E2E, not the agent runtime.

Choose and pin an in-sandbox controller. Define durable session/lease state, expiry and recovery; allowed local endpoints and test-account policy; diagnostic redaction and retention; shell behavior during takeover; and the authorized screenshot/video publisher. Treat page content, URLs, captures, diagnostics, downloads, and tool results as untrusted. Prompt-injection defenses do not replace isolation or authorization. Keep console/network diagnostics opt-in, relevant, redacted, truncated, and bounded; do not log page content, credentials, or cookies. Mark stale views and distinguish stream loss from browser failure.
