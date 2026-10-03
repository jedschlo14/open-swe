# Interactive browser sessions

**Status:** Draft for discussion

**Scope:** Product and architecture proposal; some implementation choices remain open

## Summary

Give Open SWE an isolated, thread-scoped browser for testing and debugging applications. For the initial cloud runtime, run headless Chromium in the thread's coding sandbox so it can reach the agent's local app. Start it only on explicit browser use—not when a thread or panel opens—and share the session between the agent and dashboard through an authenticated broker. Prefer semantic interaction, with screenshots for visual checks and safe evidence.

## Workflows and scope

**MVP:** Build or modify a UI and verify it in the running app; reproduce and diagnose UI bugs. Prefer APIs, search, or HTTP retrieval when they suffice. Browser access is not a general remote desktop.

**Not in the MVP:** General external-site research, saved authentication, file uploads, additional tabs or engines, headed/desktop mode, and profiles shared across threads. These are candidates to revisit only against a concrete use case, not a committed roadmap. Do not build a general credential vault.

For local-app testing, these capabilities are not needed to validate the core workflow. Reconsider saved authentication only if repeatable tests of authenticated local apps need it, and uploads only when a target app's test flow depends on them. External browsing brings a separate network and safety scope; additional tabs, engines, and headed/desktop support should remain out of scope absent demonstrated demand. Cross-thread profile sharing conflicts with thread isolation and is not a planned follow-up.

## Product behavior

The MVP targets cloud thread sandboxes only. Local desktop support requires a separate host adapter and remains out of scope until there is a demonstrated need.

The Browser surface belongs in the thread's right panel alongside Terminal, Changes, and Files. It shows session status, the live view, control ownership, and available actions. The current panel store can persist a Browser tab entry, but the right panel does not yet render or connect a browser surface; restoring a saved entry must not start compute.

### Startup and controls

- The initial cloud runtime targets LangSmith-backed thread sandboxes. Reuse the thread's sandbox and workspace snapshot; local desktop sessions need a separate host adapter and are out of scope for this MVP.
- The first explicit browser action starts Chromium. Agent execution already creates or reconnects the sandbox through `ensure_sandbox_for_thread`; dashboard terminal connection only accepts an existing sandbox. Dashboard-first browser use therefore needs an authorized, idempotent start path that binds the sandbox to the thread. Opening or restoring a panel must not provision it.
- Managed browser support should be gated by an administrator policy across provisioning, agent tools, viewing, and control. There is currently no browser-specific administrator flag.
- Hiding the panel does not stop the session. Stopping ends the process and may lose page state. Support cancellation and normal infrastructure timeouts; inspect the page after meaningful actions before claiming success.

### Observe and take control

Watching is passive: thread readers may view, while only thread writers may control. Use separate read and write authorization checks; the terminal route requires a promptable user and is not sufficient for read-only admin viewers.

An authorized writer can take over a ready session. Stop dispatching agent browser actions, settle or cancel in-flight work, then transfer an exclusive lease. While the participant controls the browser, the agent must not send browser actions. On handback, revoke participant input, discard stale actions, and have the agent reread the page. Show the current controller and pending handoffs. Disconnect alone must not transfer control; define visible lease expiry and recovery before MVP. The lease applies to supported browser-control paths; shell access must be gated during takeover or the product must clearly disclose any remaining bypass.

Keep lifecycle, control, viewer connection, and agent progress as separate state. A non-ready session cannot grant control; stop or failure revokes the lease; stale viewers cannot send input. Preserve page state across prompts and handoffs, but never share profiles between threads. Stop on request, thread deletion/resolution, or inactivity; one hour is a provisional idle timeout, with a visible warning before expiry.

## Browser interaction and diagnostics

Prefer visible accessibility-tree/page-text reads and stable element references. Use screenshots or coordinates for layout, rendering, canvas, or inaccessible content. Refresh references after navigation or material page changes; do not return hidden DOM or raw source by default.

Console and network diagnostics are deferred beyond the MVP. Keep bounded operational history for session health only; do not log page contents, credentials, cookies, screenshots, or recordings by default. Add opt-in, redacted, truncated console/network diagnostics only when needed to support real user workflows.

## Credentials and authorization

MVP credentials are limited to local/test apps and low-privilege test accounts. Do not inject arbitrary `.env` values, managed connections, or task secrets. Never ask users to paste secrets into chat.

Authentication is not permission to act. Require human confirmation for actions such as external submissions, purchases, destructive changes, account changes, or consent; prohibit actions disallowed by policy. Enforce boundaries in the executor, not only in prompts. Treat page content as untrusted, and do not publish private content merely because the agent can view it. Any future saved-auth feature needs explicit ownership, destination restrictions, visibility, expiry, and revocation.

## Evidence

For UI-change and UI-bug-fix PRs, capture a concise before/after screenshot pair when the browser can reproduce the original and verify the result. Add a short recording only when it materially demonstrates behavior that still images cannot; recordings may follow screenshots as a fast follow-up if needed to keep the first release focused. Omit evidence if it is unsafe or cannot be produced; explain why. For other tasks, capture evidence only when requested or materially useful. Keep temporary artifacts out of Git and clean them up on a bounded schedule.

Sanitize captures, exclude unrelated private content, and mask sensitive regions where reliable. Publishing is separate from viewing and must preserve repository access boundaries. The existing `create_sandbox_file_download_url` returns bearer links that can be non-expiring; they are not scoped to PR readers. The screenshot guidance uses such links, and `gh pr edit` can add a reference but cannot upload media. The PR image proxy serves already-published GitHub raster images; it does not upload them or support video. A repository-authorized screenshot publisher and retention policy are MVP requirements; a safe video path is needed only for the recording follow-up.

## Runtime and architecture

Use one isolated headless Chromium profile and primary page per cloud thread. An existing right-panel browser prototype is the closest transport example: it installs Chromium in an existing sandbox, binds DevTools to loopback, tunnels CDP to the app server, and uses a short-lived ticket and dashboard screencast. It requires a pre-existing sandbox, reuses terminal authorization, and relays raw CDP; it does not provide agent browser tools or shared agent/user lease arbitration. Keep CDP private in production and expose narrower browser operations through a broker.

An existing Stagehand browser-tools prototype adds semantic interaction, but its session map is process-local; Browserbase runs outside the thread sandbox and cannot reach its localhost app. The `agent-browser` CLI supports CDP but is not part of the current agent runtime contract; Playwright is used by the UI/E2E workspace, not the agent runtime. Choose and pin an in-sandbox controller during implementation.

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

1. **Implementation:** choose the pinned in-sandbox controller; define durable session/lease state, visible lease expiry and recovery, dashboard-first startup and concurrency, allowed local endpoints, test credentials, and a repository-authorized screenshot publisher. Ensure shell access cannot silently bypass takeover, or disclose any limitation clearly.
2. **MVP:** deliver local-app testing, live view, read/write authorization, takeover/handback, semantic interaction with visual fallback, cancellation, same-thread sessions with idle expiry, enforced local-only egress, and safe before/after screenshots on applicable UI PRs.
3. **Fast follow:** add short recordings if they materially improve evidence but do not fit the first release. Keep diagnostics and recovery after browser-process/sandbox loss deferred; preserve state across normal prompts and turns, and report runtime loss clearly.
4. **Revisit only on demonstrated need:** external browsing, saved authentication, file uploads, additional tabs or engines, headed/desktop support, and quotas. These are not roadmap commitments; shared cross-thread profiles remain out of scope because they undermine isolation.

Revisit scope if implementation reveals a concrete safety or reliability blocker; do not assume infeasibility in advance.

## Success criteria

- Cloud sessions start on explicit use, reuse the thread sandbox across prompts and agent turns, and can be disabled by admin policy. Local desktop support is not part of the MVP.
- Semantic actions are preferred, with screenshots for visual checks and inaccessible interfaces.
- Thread readers can watch and writers can take over; handoff is exclusive across supported paths and handback refreshes agent context.
- Sessions stop on request, thread deletion/resolution, or idle expiry; expired sessions restart cleanly. Recovery after unexpected browser-process or sandbox loss is out of scope initially.
- Runs are cancellable and outcomes are verified. Applicable UI PRs include safe before/after screenshots; short recordings may follow when materially better. Diagnostics are limited to operational session health in the MVP.
- Credentials, page data, and artifacts follow defined isolation, access, and retention policies.
