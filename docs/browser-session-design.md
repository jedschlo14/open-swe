# Interactive browser sessions

**Status:** Draft for discussion

**Scope:** Product and architecture proposal; some implementation choices remain open

## Summary

Give Open SWE an isolated, thread-scoped browser for testing and debugging applications. For the cloud MVP, run headless Chromium in the thread's coding sandbox so it can reach the agent's local app. Start it only on explicit browser use—not when a thread or panel opens—and share the session between the agent and dashboard through an authenticated broker. Prefer semantic interaction, with screenshots for visual checks and safe evidence.

## Workflows and scope

**MVP:** Build or modify a UI and verify it in the running app; reproduce and diagnose UI bugs. Prefer APIs, search, or HTTP retrieval when they suffice. Browser access is not a general remote desktop.

**Later:** General external-site research, saved authentication, file uploads, additional tabs or engines, headed/desktop mode, and profiles shared across threads. Do not build a general credential vault.

## Product behavior

The Browser surface belongs in the thread's right panel alongside Terminal, Changes, and Files. It shows session status, the live view, control ownership, and available actions. The current panel store can persist a browser placeholder, but the `dev` panel does not render or connect a browser surface; restoring a descriptor must not start compute.

### Startup and controls

- The cloud MVP targets LangSmith-backed thread sandboxes. Reuse the thread's sandbox and workspace snapshot; local desktop sessions need a separate host adapter.
- The first explicit browser action starts Chromium. Agent execution already creates or reconnects the sandbox through `ensure_sandbox_for_thread`; dashboard terminal connection only accepts an existing sandbox. Dashboard-first browser use therefore needs an authorized, idempotent start path that binds the sandbox to the thread. Opening or restoring a panel must not provision it.
- Managed browser support should be gated by an administrator policy across provisioning, agent tools, viewing, and control. No browser-specific flag exists on `dev` today.
- Hiding the panel does not stop the session. Stopping ends the process and may lose page state. Support cancellation and normal infrastructure timeouts; inspect the page after meaningful actions before claiming success.

### Observe and take control

Watching is passive: thread readers may view, while only thread writers may control. Use separate read and write authorization checks; the terminal route requires a promptable user and is not sufficient for read-only admin viewers.

An authorized writer can take over a ready session. Stop dispatching agent browser actions, settle or cancel in-flight work, then transfer an exclusive lease. While the participant controls the browser, the agent must not send browser actions. On handback, revoke participant input, discard stale actions, and have the agent reread the page. Show the current controller and pending handoffs. Disconnect alone must not transfer control; define visible lease expiry and recovery before MVP.

Keep lifecycle, control, viewer connection, and agent progress as separate state. A non-ready session cannot grant control; stop or failure revokes the lease; stale viewers cannot send input. Preserve page state across prompts and handoffs, but never share profiles between threads. Stop on request, thread deletion/resolution, or inactivity; one hour is a provisional idle timeout, with a visible warning before expiry.

## Browser interaction and diagnostics

Prefer visible accessibility-tree/page-text reads and stable element references. Use screenshots or coordinates for layout, rendering, canvas, or inaccessible content. Refresh references after navigation or material page changes; do not return hidden DOM or raw source by default.

Console and network diagnostics should be opt-in, redacted, truncated, and limited to relevant entries. Keep only bounded operational history; do not log page contents, credentials, cookies, screenshots, or recordings by default.

## Credentials and authorization

MVP credentials are limited to local/test apps and low-privilege test accounts. Do not inject arbitrary `.env` values, managed connections, or task secrets. Never ask users to paste secrets into chat.

Authentication is not permission to act. Require human confirmation for actions such as external submissions, purchases, destructive changes, account changes, or consent; prohibit actions disallowed by policy. Enforce boundaries in the executor, not only in prompts. Treat page content as untrusted, and do not publish private content merely because the agent can view it. Any future saved-auth feature needs explicit ownership, destination restrictions, visibility, expiry, and revocation.

## Evidence

For UI-change and UI-bug-fix PRs, capture a concise before/after screenshot pair when the browser can reproduce the original and verify the result. Add a short recording only when it materially demonstrates behavior that still images cannot. Omit evidence if it is unsafe or cannot be produced; explain why. For other tasks, capture evidence only when requested or materially useful. Keep temporary artifacts out of Git and clean them up on a bounded schedule.

Sanitize captures, exclude unrelated private content, and mask sensitive regions where reliable. Publishing is separate from viewing and must preserve repository access boundaries. The existing `create_sandbox_file_download_url` returns bearer links that can be non-expiring; they are not scoped to PR readers. The screenshot guidance uses such links, and `gh pr edit` can add a reference but cannot upload media. The PR image proxy serves already-published GitHub raster images; it does not upload them or support video. A repository-authorized publisher, retention policy, and safe video path remain open.

## Runtime and architecture

Use one isolated headless Chromium profile and primary page per cloud thread. The `johannes/right-panel-browser` experiment is the closest transport prototype: it installs Chromium in an existing sandbox, binds DevTools to loopback, tunnels CDP to the app server, and uses a short-lived ticket and dashboard screencast. It requires a pre-existing sandbox, reuses terminal authorization, and relays raw CDP; it does not provide agent browser tools or shared agent/user lease arbitration. Keep CDP private in production and expose narrower browser operations through a broker.

The `feat/browserbase-stagehand-tool` experiment adds semantic Stagehand tools, but its session map is process-local; Browserbase runs outside the thread sandbox and cannot reach its localhost app. The `agent-browser` CLI supports CDP but is not declared in the `dev` runtime contract; Playwright is used by the UI/E2E workspace, not the agent runtime. Choose and pin an in-sandbox controller during implementation.

1. **Session manager:** thread association, explicit-use startup, workspace/snapshot selection, lifecycle, cleanup, and admin policy. Make dashboard-first startup idempotent and safe under concurrent requests.
2. **Browser runtime and arbiter:** connect agent tools and dashboard controls to the same page and enforce one lease. Sandbox shell access can bypass a cooperative browser lease; constrain it, gate it during takeover, or clearly limit the exclusivity guarantee.
3. **Dashboard channel:** authenticated, thread-scoped WebSocket; authorize viewing and control separately. Keep raw CDP loopback-only. The right-panel descriptor is presentation state, not session state or authorization.
4. **Evidence and policy:** capture scoped artifacts; publish with repository access checks and bounded retention. Enforce participant permissions, action grants, admin disable, network restrictions, and publication authorization server-side.

## Security and failure requirements

- Isolate browser profiles and temporary data by thread. Restrict MVP networking to the authorized local test app and required local development endpoints; block general internet and other local/private/link-local destinations outside the browser, including redirects and DNS changes.
- Treat page text, URLs, screenshots, diagnostics, downloads, and tool results as untrusted. Prompt-injection detection is defense in depth, not a substitute for isolation or authorization.
- Admin disable must block managed provisioning, tool exposure, streaming, and control. Do not claim it blocks standalone browser binaries through shell unless separately enforced.
- Mark stale views and disable input; distinguish stream loss from browser failure. Log operational metadata only.

## Rollout and remaining decisions

1. **Implementation:** choose the pinned in-sandbox controller; define durable session/lease state, lease recovery, dashboard-first startup and concurrency, allowed local endpoints, test credentials, diagnostic redaction/retention, and a repository-authorized evidence publisher. Decide how to handle shell-based lease bypass.
2. **MVP:** deliver local-app testing, live view, read/write authorization, takeover/handback, semantic interaction with visual fallback, cancellation, same-thread sessions with idle expiry, opt-in diagnostics, enforced local-only egress, and safe before/after screenshots—with short recordings when useful—on applicable UI PRs.
3. **Expand:** consider external browsing, saved authentication, file handling, more recording use cases, quotas, and additional browser support only when demonstrated need warrants it.

Revisit scope if implementation reveals a concrete safety or reliability blocker; do not assume infeasibility in advance.

## Success criteria

- Cloud sessions start on explicit use, reuse the thread sandbox across prompts and agent turns, and can be disabled by admin policy; local desktop support has a separate runtime.
- Semantic actions are preferred, with screenshots for visual checks and inaccessible interfaces.
- Thread readers can watch and writers can take over; handoff is exclusive across supported paths and handback refreshes agent context.
- Sessions stop on request, thread deletion/resolution, or idle expiry; expired sessions restart cleanly.
- Runs are cancellable and outcomes are verified. Diagnostics are opt-in and sanitized. Applicable UI PRs include safe before/after screenshots and, when materially better, a short recording.
- Credentials, page data, and artifacts follow defined isolation, access, and retention policies.
