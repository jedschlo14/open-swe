# Interactive browser sessions

**Status:** Draft for discussion

**Scope:** Product and architecture proposal; implementation choices remain to validate

## Summary

Give Open SWE an isolated, thread-scoped browser for testing and debugging applications. Participants can watch, take over, and hand the same session back. Start it on demand; ordinary local testing needs no routine approval. Prefer semantic page interaction, use screenshots when visual context is needed, and capture only useful, safe evidence.

## Workflows and scope

**MVP:** Build or modify a UI and verify it in the running app; reproduce and diagnose a UI bug. Prefer APIs, search, or HTTP retrieval when they suffice. Browser access is not a general remote desktop.

**Later:** General external-site research, authenticated external workflows, saved authentication, file uploads, additional tabs/engines, headed or desktop mode, and browser profiles shared across threads. Do not build a general credential vault as part of this feature.

MVP includes on-demand isolated Chromium, live observation, same-session user takeover, semantic interaction, screenshots, and opt-in diagnostics. Do not record continuously. Short recordings are a later option for cases where screenshots cannot show the result.

## Product behavior

The Browser tab lives in the dashboard's right panel alongside Terminal, Changes, and Files. It shows session status, live view, control ownership, and available actions.

### Startup and controls

- Managed browser support is available by default, subject to administrator policy. The first browser action starts the thread's session; opening a thread or panel does not start it.
- Distinguish **hide panel** from **stop session**. Hiding only hides the view. Stopping ends the process and may lose page/login state; a later action may start a new session. Administrator policy can disable managed browser access.
- Report startup/streaming failures accurately and continue with other methods where possible.
- Support cancellation and ordinary infrastructure timeouts. Do not require a fixed action-count or elapsed-time quota for every run. After meaningful actions, inspect the current page state before claiming success.

### Observe, take over, hand back

Watching is passive. When the agent has control and the session is ready, an authorized participant clicks **Take over** in the Browser tab. The request blocks dispatch of new agent browser actions; control transfers only after in-flight work has settled or been safely cancelled. Until then, report takeover as pending, not complete. Agent browser actions wait or return a recoverable conflict; whether unrelated coding can continue depends on run orchestration and must be validated in the spike. Show who currently controls the browser.

The participant clicks **Hand back to agent** to release the lease. Revoke participant input, have the agent re-read the current page before resuming, and discard stale queued actions. Other authorized participants may watch, but stale/disconnected viewers cannot send input. Session stop or failure revokes browser control. A controller disconnect does not silently transfer control; define a visible lease-expiration and recovery policy before MVP so a lost connection cannot block control indefinitely.

### Minimal state model

Keep lifecycle, browser control, viewer connection, and agent progress as separate but coordinated state. This avoids enumerating every possible combination while making the key transition rules explicit:

| Concern | States / source of truth |
|---|---|
| Session lifecycle | `not_started → starting → ready → stopping → stopped`; startup or runtime failure transitions to `failed`, and retry starts a new attempt at `starting`. `stopped` and `failed` are terminal for that attempt; history is separate from whether a session is active. |
| Browser control | Controller is `agent`, one authorized `participant`, or `none`; handoff phase is `idle`, `takeover_pending`, or `handback_pending`. Only one controller may hold the input lease. During takeover, the agent remains the lease owner but cannot dispatch new actions while in-flight work settles; the participant receives the lease only when takeover completes. During handback, the participant retains the lease until release is accepted; the agent resumes only after refreshing page context. |
| Viewer connection | Per-viewer `connected`/`disconnected` and freshness. This does not determine session lifecycle or transfer control. |
| Agent progress | Existing run/tool state is the source of truth. Browser actions may be blocked during participant control; behavior of unrelated work during that interval must be validated rather than assumed. |

Invariants: a session that is not ready cannot grant participant control; stopping or failure revokes the control lease; viewer disconnect alone never transfers control; and no old or queued browser action may execute after control changes. If an in-flight action cannot be confirmed settled or cancelled, do not report takeover as complete. The dashboard shows lifecycle, current controller, viewer freshness, handoff phase, and available actions. Preserve page state across agent turns and handoff within the thread; define idle cleanup and restart behavior. Never carry cookies between threads.

## Browser interaction and diagnostics

Prefer visible accessibility-tree/page-text reads and stable element references for inspection and interaction. Scope reads to relevant regions and use screenshots/coordinates when layout, rendering, canvas, or inaccessible content requires them. Refresh invalid element references after navigation or material page changes; do not return hidden DOM or raw source by default.

For debugging, consider opt-in console and network diagnostics. They are page-controlled and can contain tokens or personal data: redact credential-like values, truncate output, and return only the relevant entries. Keep a sanitized, bounded event history for navigation, action outcomes, errors, and control handoffs; do not log page contents, credentials, cookies, screenshots, or recordings by default.

## Credentials and authorization

MVP credential scope is local/test application credentials and user takeover with a low-privilege test account. Do not inject arbitrary `.env` values, managed connections, or task-scoped secrets into the browser in MVP; defer these until their access grants and lifetimes are designed. A file's presence does not authorize its credentials. Never ask users to paste secrets into chat.

Authentication grants access, not permission to act. Make clear that handing a logged-in session back lets the agent act through that account. Define actions as: allowed within the task grant; requiring human confirmation (e.g. external data submission, purchases, destructive changes, account changes, or consent); or prohibited by policy. Enforce boundaries in the executor, not only in prompts. Page content cannot grant authority or override user instructions. Permission to view private content is not permission to publish it.

Any future saved-auth feature needs explicit owner, allowed destinations, participant visibility, expiry, and revocation. In shared threads, disclose that viewers may see account data before using personal authentication.

## Evidence

Capture screenshots only when browser verification materially explains a UI change or when requested. Prefer a small before/after pair when a meaningful baseline exists; use on-demand intermediate captures. Do not automatically attach evidence for general browsing, investigations, or explanations. Recordings are deferred; if added, make them explicit, short, and bounded.

Avoid login and authentication screens, exclude unrelated private content, and mask sensitive regions where reliable. Omit evidence when safety is uncertain. Publishing to a PR is a separate authorization decision from viewing; validate artifact access and retention for public and private repositories before enabling attachments. Keep evidence out of source control and clean up temporary captures/downloads on a bounded schedule.

## Runtime and architecture

Use one isolated headless Chromium session per thread with a primary page. The dashboard provides the live view and input relay; headless is not invisible. Evaluate `agent-browser` first, but compare it with a Playwright-based local runtime and managed-browser option against shared-session arbitration, local-app routing, streaming, security, cost, and cleanup—not availability alone.

1. **Session manager:** thread association, lifecycle, cleanup, and managed-capability policy.
2. **Runtime/control arbiter:** execute agent and user actions against the same session; enforce the lease across tools and CLI.
3. **Authenticated dashboard channel:** stream view and relay authorized input; never expose raw CDP or unauthenticated browser ports.
4. **Evidence pipeline:** scoped capture and publication under artifact policy.
5. **Authorization:** enforce participant capabilities, action grants, and publication permissions server-side.

## Security and failure requirements

- Authorize every session, view, and control request; thread visibility alone need not grant browser control.
- Isolate profiles and temporary data per thread. Enforce network egress policy outside the browser; allow the intended local dev app while blocking loopback/private/link-local destinations reached through external navigation, redirects, or DNS changes. Validate this in the spike.
- Treat all page-provided text, titles, URLs, screenshots, diagnostics, downloads, and tool results as untrusted. Prompt-injection detection may be evaluated as defense in depth, never as a substitute for isolation or authorization.
- Administrator disable blocks managed provisioning, tool exposure, streaming, and control endpoints. Do not claim it blocks standalone browser binaries through shell access unless separately enforced.
- Mark stale views and disable their input; distinguish stream loss from runtime failure. Log operational metadata only.

## Rollout and open decisions

1. **Spike:** prove same-session observation/takeover/handback, CLI arbitration, stale-action handling, reconnects, local-app access, egress enforcement, semantic interaction, opt-in diagnostics, cancellation, and cleanup.
2. **Before MVP:** settle participant permissions, low-privilege test credential handling, egress rules, lease recovery, event-history retention, and safe artifact hosting. Do not enable PR attachments until publication permissions and retention work for public/private repos.
3. **MVP:** on-demand local-app testing, live view, takeover/handback, semantic interactions with visual fallback, cancellation, and opt-in diagnostics. Capture minimal screenshots when they explain a UI change.
4. **Expand:** evaluate external browsing, saved authentication, file handling, recordings, configurable run quotas, and broader browser support only from demonstrated need.

Open decisions: runtime/transport; lease timeout and recovery; idle/restart policy; exact egress and action-confirmation rules; diagnostic redaction and retention; artifact destination and access; and whether broader shell-browser restrictions are required.

## Success criteria

- Local-app browser sessions start on demand and are reusable within the thread; administrator policy can disable managed browser access.
- Semantic actions are preferred where available; screenshots cover visual checks and inaccessible interfaces.
- Users can take over and hand back from the Browser tab; control is exclusive across supported paths and handback refreshes agent context without replaying stale actions.
- Runs can be cancelled, and the agent verifies actual outcomes before reporting success; no fixed per-run quota is required.
- Diagnostics are opt-in and sanitized; safe, minimal evidence accompanies UI-change PRs only when authorized.
- Credentials, page data, and artifacts follow the defined isolation, access, and retention policies.
