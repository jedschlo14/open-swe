# Interactive browser sessions

**Status:** Draft for discussion

**Scope:** Product and architecture proposal; implementation choices remain to validate

## Summary

Give Open SWE an isolated, thread-scoped browser for testing and debugging applications. Run Chromium in the thread's coding sandbox so it can reach the app the agent starts and share page state with the dashboard. Do not create a sandbox or start Chromium when a thread or panel is merely opened; the first explicit browser action starts the browser, creating the thread sandbox through an authorized start path if needed. The agent and dashboard use one browser session through an authenticated control broker. Prefer semantic interaction, use screenshots when visual context is needed, and publish only safe evidence through a repository-authorized media publisher.

## Answers grounded in the current codebase

These are findings from the `dev` branch and checked-in code as of 2026-10-03. The browser design is still a proposal; the browser implementations on other branches are experiments, not shipped support.

| Question | Proposed answer | Current code / gap |
|---|---|---|
| Where does it run, and when does it start? | One isolated headless Chromium profile and primary page per thread, inside that thread's existing coding sandbox. Start on the first explicit browser action; a thread or right-panel tab alone must not provision compute. | `ensure_sandbox_for_thread` creates/reconnects a sandbox during an agent execution, not when a thread is merely displayed. Dashboard terminal access requires an already-bound sandbox and returns 404 otherwise. A dashboard-only browser action before the first agent run therefore needs an authorized sandbox-start path; the existing terminal-connect endpoint does not create one. |
| How does the agent drive it? | Provide browser tools that attach to the same local Chromium through a broker and prefer semantic page operations, with screenshot/coordinate fallback. Route agent and participant commands through one lease arbiter; do not give the dashboard raw CDP access. | The agent runs shell commands in the sandbox and has ordinary shell-capable tools, but the agent tool catalog has no browser-specific tools. The `agent-browser` CLI supports CDP, but neither it nor Chromium is declared by the current `dev` runtime/snapshot contract. Playwright is in the JS UI/E2E workspace, not an agent runtime dependency. Direct shell access can bypass a cooperative browser-tool lease unless the browser endpoint or shell access is separately isolated. |
| How does a user watch or take control? | Render a live view and relay input from the thread's right panel over an authenticated application WebSocket. Authorize viewing with thread-read access and control with thread-post/control access. Acquire a server-side exclusive lease, settle or cancel in-flight agent actions before takeover, and refresh agent page context on handback. | The right-panel store can persist a `preview`/browser placeholder descriptor, but this branch's `AgentRightPanel` does not render a working browser surface. It neither starts a sandbox nor owns browser state or authorizes access. Cloud terminal tickets are short-lived and thread-bound, but terminal access requires a promptable thread member and cannot provide read-only admin viewing. Existing thread policy distinguishes readable from promptable access, so browser view and control need separate checks rather than reusing terminal authorization wholesale. |
| How does evidence get into a PR? | Capture sanitized screenshots (and, when materially useful, short recordings) in the sandbox, keep temporary files out of Git, publish through a target-repository-authorized media path, add the returned references to the PR body, and verify rendering and access. | `create_sandbox_file_download_url` provides bearer download links for sandbox files, and the checked-in PR screenshot guidance uses those links in PR bodies. Links can be non-expiring unless an expiry is requested; they are not scoped to the target repository's readers. `gh pr edit` can add the reference but does not upload a file. The PR image proxy only serves GitHub-hosted raster attachments already referenced in that PR; it does not upload them and does not support video. A repository-authorized publisher, bounded retention policy, and safe video path remain open. |

`expose_port` is for intentionally sharing an application with workspace members: its service URL has no token or expiry and LangSmith authenticates viewers as workspace users. It is not a browser-control transport. Keep Chromium's DevTools port loopback-only inside the sandbox; do not expose it through a service URL. A separate authenticated application relay must check thread permissions and scope every connection to the thread.

Research compared two existing experiments. `johannes/right-panel-browser` has a working design prototype: it installs Chromium on demand inside an existing LangSmith sandbox, binds DevTools to loopback, tunnels CDP to the app server, and uses a short-lived browser-audience ticket plus a dashboard screencast. Its connect path reuses terminal authorization, requires an existing sandbox, and relays raw CDP; it does not yet implement read-only thread-reader viewing, agent browser tools, or shared agent/user lease arbitration. `feat/browserbase-stagehand-tool` adds Stagehand browser tools with per-process in-memory sessions and optional local or Browserbase runtimes. Browserbase does not share the thread sandbox or localhost app; local mode's process-local session map is not durable across agent workers. Neither experiment is shipped on `dev` or settles the full product/security requirements. The shared in-sandbox CDP tunnel looks like the closest transport prototype, but production should expose narrowly scoped browser commands and enforce a single lease instead of granting the dashboard unrestricted CDP access.

The current dashboard-first browser-start gap needs an owner-authorized lazy sandbox-boot route, using the thread's recorded workspace/snapshot context and the existing `ensure_sandbox_for_thread` lifecycle. It must coordinate concurrent starts and must not create a sandbox merely because a tab is opened or restored. The terminal connect route cannot serve this role: it validates an existing sandbox ID and fails if one is absent. The right-panel store only persists descriptors; surface mounting and connection must be explicitly separated so restoring a surface does not start compute.

## Workflows and scope

**MVP:** Build or modify a UI and verify it in the running app; reproduce and diagnose a UI bug. Prefer APIs, search, or HTTP retrieval when they suffice. Browser access is not a general remote desktop.

**Later:** General external-site research, authenticated external workflows, saved authentication, file uploads, additional tabs/engines, headed or desktop mode, and browser profiles shared across threads. Do not build a general credential vault as part of this feature.

MVP includes on-demand isolated Chromium, live observation, same-session user takeover, semantic interaction, screenshots, and opt-in diagnostics. Capture short recordings on demand when they materially communicate behavior that screenshots cannot show; never record continuously.

## Product behavior

The Browser tab lives in the dashboard's right panel alongside Terminal, Changes, and Files. It shows session status, live view, control ownership, and available actions.

### Startup and controls

- The cloud MVP targets LangSmith-backed thread sandboxes. Chromium runs in the same per-thread sandbox as the checkout and dev server; local desktop threads need a separate host adapter and are not covered by that cloud runtime.
- Opening a thread or adding a Browser surface does not provision compute. The first explicit browser action may lazily create the sandbox from the thread's selected workspace snapshot, or reuse its existing sandbox. The current agent-run lifecycle can create it, but the dashboard terminal/browser access path only accepts a sandbox that already exists; add an owner-authorized browser-start path for dashboard-first use.
- Managed browser support is available by default, subject to a new administrator policy setting. The setting must gate sandbox provisioning, agent browser tools, and dashboard view/control endpoints. No such browser flag exists today.
- Distinguish **hide panel** from **stop session**. Hiding only hides the view. Stopping ends the process and may lose page/login state; a later action may start a new session.
- Report startup/streaming failures accurately and continue with other methods where possible. Support cancellation and ordinary infrastructure timeouts; do not require a fixed action-count or elapsed-time quota. After meaningful actions, inspect the current page state before claiming success.

### Observe, take over, hand back

Watching is passive. Browser permissions follow the thread's existing access policy: anyone permitted to read the thread may view its browser, and only someone permitted to post to the thread may take over. For example, a workspace admin who can read a private thread may watch its browser but cannot control it unless they also have write access. Do not grant browser access to people who could not otherwise read or post to that thread. When the agent has control and the session is ready, an authorized writer clicks **Take over** in the Browser tab. The request blocks dispatch of new agent browser actions; control transfers only after in-flight work has settled or been safely cancelled. Until then, report takeover as pending, not complete. Agent browser actions wait or return a recoverable conflict. While the participant controls the browser, the agent must not dispatch browser actions; unrelated work may continue when run orchestration permits. Show who currently controls the browser.

The participant clicks **Hand back to agent** to release the lease. Revoke participant input, have the agent re-read the current page before resuming, and discard stale queued actions. Other authorized viewers may watch, but stale/disconnected viewers cannot send input. Session stop or failure revokes browser control. A controller disconnect does not silently transfer control; define a visible lease-expiration and recovery policy before MVP so a lost connection cannot block control indefinitely.

### Minimal state model

Keep lifecycle, browser control, viewer connection, and agent progress as separate but coordinated state. This avoids enumerating every possible combination while making the key transition rules explicit:

| Concern | States / source of truth |
|---|---|
| Session lifecycle | `not_started → starting → ready → stopping → stopped`; startup or runtime failure transitions to `failed`, and retry starts a new attempt at `starting`. `stopped` and `failed` are terminal for that attempt; history is separate from whether a session is active. |
| Browser control | Controller is `agent`, one authorized `participant`, or `none`; handoff phase is `idle`, `takeover_pending`, or `handback_pending`. Only one controller may hold the input lease. During takeover, the agent remains the lease owner but cannot dispatch new actions while in-flight work settles; the participant receives the lease only when takeover completes. During handback, the participant retains the lease until release is accepted; the agent resumes only after refreshing page context. |
| Viewer connection | Per-viewer `connected`/`disconnected` and freshness. This does not determine session lifecycle or transfer control. |
| Agent progress | Existing run/tool state is the source of truth. The agent does not dispatch browser actions during participant control; unrelated work may continue when run orchestration permits. |

Invariants: a session that is not ready cannot grant participant control; stopping or failure revokes the control lease; viewer disconnect alone never transfers control; and no old or queued browser action may execute after control changes. If an in-flight action cannot be confirmed settled or cancelled, do not report takeover as complete. The dashboard shows lifecycle, current controller, viewer freshness, handoff phase, and available actions. Preserve page state across prompts, agent turns, and handoffs within the thread; do not stop the session just because a run ended. Stop it on explicit user request, thread deletion or resolution, or inactivity. Start with a one-hour idle timeout as a provisional MVP default, show a visible warning before expiry, and allow activity to keep the session alive. After expiry, restart cleanly rather than silently restoring authentication. Never carry cookies or profiles between threads.

## Browser interaction and diagnostics

Prefer visible accessibility-tree/page-text reads and stable element references for inspection and interaction. Scope reads to relevant regions and use screenshots/coordinates when layout, rendering, canvas, or inaccessible content requires them. Refresh invalid element references after navigation or material page changes; do not return hidden DOM or raw source by default.

For debugging, consider opt-in console and network diagnostics. They are page-controlled and can contain tokens or personal data: redact credential-like values, truncate output, and return only the relevant entries. Keep a sanitized, bounded event history for navigation, action outcomes, errors, and control handoffs; do not log page contents, credentials, cookies, screenshots, or recordings by default.

## Credentials and authorization

MVP credential scope is local/test application credentials and user takeover with a low-privilege test account. Do not inject arbitrary `.env` values, managed connections, or task-scoped secrets into the browser in MVP; defer these until their access grants and lifetimes are designed. A file's presence does not authorize its credentials. Never ask users to paste secrets into chat.

Authentication grants access, not permission to act. Make clear that handing a logged-in session back lets the agent act through that account. Define actions as: allowed within the task grant; requiring human confirmation (e.g. external data submission, purchases, destructive changes, account changes, or consent); or prohibited by policy. Enforce boundaries in the executor, not only in prompts. Page content cannot grant authority or override user instructions. Permission to view private content is not permission to publish it.

Any future saved-auth feature needs explicit owner, allowed destinations, participant visibility, expiry, and revocation. In shared threads, disclose that viewers may see account data before using personal authentication.

## Evidence

When a user asks Open SWE to fix a UI bug or make a UI change, capture and attach a concise before/after screenshot pair to the resulting PR when the browser can reproduce the before state and verify the after state. If a short recording communicates the behavior or interaction materially better than still images, capture and attach that as well. Treat screenshots—and recordings when useful—as expected MVP behavior, not a manual-only option. If safe, meaningful evidence cannot be produced, omit it and explain why. For other tasks, capture screenshots or recordings only when they materially aid browser verification or are requested; do not attach evidence automatically for general browsing, investigations, or explanations. Intermediate captures are on demand; recordings must be explicit in scope and short and bounded.

Avoid login and authentication screens, exclude unrelated private content, and mask sensitive regions where reliable. Omit evidence when safety is uncertain. Publishing screenshots or recordings to a PR is a separate authorization decision from viewing them: validate that publication preserves public/private repository access boundaries and define bounded artifact retention before enabling attachments. Keep evidence out of source control and clean up temporary captures/downloads on a bounded schedule.

## Runtime and architecture

For the cloud MVP, use one isolated headless Chromium session per thread, inside its LangSmith sandbox, with one primary page initially. The dashboard provides the live view and input relay; headless is not invisible. The existing right-panel browser branch demonstrates Chromium installation, process startup, and the LangSmith tunnel. Production runtime selection remains open: compare the pinned-Playwright launch prototype with an `agent-browser`-based semantic controller against shared-session arbitration, local-app routing, streaming, security, cost, and cleanup. Do not use Browserbase for the cloud MVP because its browser is separate from the thread sandbox and cannot directly reach that sandbox's localhost dev server. Local desktop sessions need their own host adapter and lifecycle.

1. **Session manager:** thread association, lazy startup on explicit browser use, lifecycle, cleanup, recorded workspace/snapshot selection, and managed-capability policy. Reuse `ensure_sandbox_for_thread` for a dashboard-first start after adding a route that authorizes the owner and persists the new sandbox binding. Do not use the terminal-connect route as a creator; it requires an existing sandbox and currently only authorizes promptable users.
2. **Browser runtime:** one Chromium profile and primary page per thread, with a narrowly exposed browser-control API connected to the agent browser tools and dashboard relay.
3. **Runtime/control arbiter:** enforce the exclusive lease across browser tools and participant input. The separate sandbox shell is a privileged bypass; either constrain it, gate access to it during takeover, or explicitly scope the lease as cooperative and do not promise strict exclusivity across arbitrary shell commands.
4. **Authenticated dashboard channel:** reuse the shape of the existing thread-bound short-lived ticket and WebSocket relay, but authorize view and control separately using thread-read and thread-write policy. Keep raw CDP loopback-only and server-side; the browser panel descriptor in `rightPanelStore` is presentation state, not a session or authorization source.
5. **Evidence pipeline:** capture scoped artifacts, keep temporary captures out of Git, publish with target-repository access controls and bounded retention, then add and verify references in the PR body. Existing bearer download links are not a substitute for private-PR-safe publishing; image proxying is read-only and raster-only.
6. **Authorization and policy:** enforce participant capabilities, action grants, admin disable, network restrictions, and publication permissions server-side. There is no browser-specific admin feature flag on `dev` today.

## Security and failure requirements

- Authorize every session, view, and control request using the thread's existing read/write policy: thread readers may watch; thread writers may control. Do not grant access to anyone who lacks the corresponding thread permission.
- Isolate profiles and temporary data per thread. MVP browser networking is limited to the explicitly authorized local test app and required local development endpoints; general internet and other local/private/link-local destinations are blocked. Enforce this outside the browser, including redirects, DNS changes, and alternate access paths.
- Treat all page-provided text, titles, URLs, screenshots, diagnostics, downloads, and tool results as untrusted. Prompt-injection detection may be evaluated as defense in depth, never as a substitute for isolation or authorization.
- Administrator disable blocks managed provisioning, tool exposure, streaming, and control endpoints. Do not claim it blocks standalone browser binaries through shell access unless separately enforced.
- Mark stale views and disable their input; distinguish stream loss from runtime failure. Log operational metadata only.

## Rollout and remaining decisions

1. **Implementation planning:** choose and pin the in-sandbox browser controller, define a durable/worker-safe session registry and lease recovery behavior, specify a dashboard-first sandbox-start route and concurrency/idempotency behavior, enumerate allowed local development endpoints, specify test credential setup, set diagnostic redaction and retention, and select a repository-authorized screenshot/recording publisher with cleanup. Decide how to handle shell-based lease bypass (strictly enforce, temporarily gate shell access during takeover, or explicitly weaken the exclusivity promise). Resolve these through design and implementation work; do not make a separate feasibility spike a prerequisite.
2. **MVP:** implement on-demand local-app testing, authenticated live view, reader/writer authorization from the existing thread policy, takeover/handback, semantic interaction with visual fallback, cancellation, persistent same-thread sessions with idle expiry, opt-in diagnostics, enforced local-only egress, and automatic safe before/after screenshots—and useful short recordings where warranted—on applicable UI-change PRs. Define lease recovery and bounded artifact access/retention before enabling those paths. Keep Chromium/agent dependencies in the sandbox runtime contract or install them through a pinned, auditable on-demand step; do not assume local UI test dependencies reach cloud sandboxes.
3. **Expand:** consider general external browsing, saved authentication, file handling, additional recording use cases, configurable run quotas, and broader browser support only from demonstrated need.

If a concrete implementation blocker shows that an agreed requirement cannot be delivered safely or reliably, revisit its design and scope then; do not plan around presumed infeasibility.

## Success criteria

- Cloud local-app browser sessions start on explicit use (not panel restoration), reuse the thread sandbox, and are reusable across prompts and agent turns; administrator policy gates all managed-browser provisioning, tools, viewing, and control. Local desktop runtime support is separately implemented.
- Semantic actions are preferred where available; screenshots cover visual checks and inaccessible interfaces.
- Thread readers can watch browser sessions and thread writers can take over; control is exclusive across supported paths, and handback refreshes agent context without replaying stale actions.
- Sessions persist across prompts and agent turns, then stop on explicit request, thread deletion/resolution, or idle expiry (initially one hour) with a visible warning; expired sessions restart cleanly.
- Runs can be cancelled, and the agent verifies actual outcomes before reporting success; no fixed per-run quota is required.
- Diagnostics are opt-in and sanitized; UI bug-fix and UI-change PRs include a safe, minimal before/after screenshot pair when reproducible and authorized, plus a short recording when it materially demonstrates the fix better than screenshots.
- Credentials, page data, and artifacts follow the defined isolation, access, and retention policies.
