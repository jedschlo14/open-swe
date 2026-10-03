# Interactive browser sessions

**Status:** Draft for discussion

**Scope:** Product and architecture proposal; implementation choices remain to validate

## Summary

Give Open SWE an isolated, thread-scoped browser for testing and debugging applications. Participants can watch, take over, and hand the same session back. Start it on demand; ordinary local testing needs no routine approval. Prefer semantic page interaction, use screenshots when visual context is needed, and capture only useful, safe evidence.

## Workflows and scope

**MVP:** Build or modify a UI and verify it in the running app; reproduce and diagnose a UI bug. Prefer APIs, search, or HTTP retrieval when they suffice. Browser access is not a general remote desktop.

**Later:** General external-site research, authenticated external workflows, saved authentication, file uploads, additional tabs/engines, headed or desktop mode, and browser profiles shared across threads. Do not build a general credential vault as part of this feature.

MVP includes on-demand isolated Chromium, live observation, same-session user takeover, thread-scoped disable, semantic interaction, screenshots, opt-in diagnostics, and bounded execution. Do not record continuously. Short recordings are a later option for cases where screenshots cannot show the result.

## Product behavior

The Browser tab lives in the dashboard's right panel alongside Terminal, Changes, and Files. It shows session status, live view, control ownership, and available actions.

### Startup and controls

- Managed browser support is available by default, subject to administrator policy. The first browser action starts the thread's session; opening a thread or panel does not start it.
- Distinguish **hide panel**, **stop session**, and **disable browser for this thread**. Hiding only hides the view. Stopping ends the process and may lose page/login state; a later action may start a new session. Thread disable prevents managed browser use and restart for that thread. Administrator disable takes precedence.
- Report startup/streaming failures accurately and continue with other methods where possible.
- Bound each browser run by action/step and elapsed-time limits; support cancellation. After meaningful actions, inspect the current page state before claiming success.

### Observe, take over, hand back

Watching is passive. **Take over** grants one authorized participant an exclusive input lease after in-flight agent actions settle or are safely cancelled. While held, browser actions from the agent—including CLI paths—must wait or return a recoverable conflict; unrelated coding can continue.

**Hand back** releases the lease. The agent re-reads the current page before resuming; stale queued actions must not replay. Other authorized participants may watch, but stale/disconnected viewers cannot send input. Define a visible lease-expiration and recovery policy: disconnect must not silently return control or block indefinitely.

### Minimal state model

| Concern | Representation |
|---|---|
| Session | `starting`, `ready`, `stopping`, `failed`; absence means no active session. Disable is policy; ended is history. |
| View | Per-viewer connection/freshness, not global session state. |
| Control | Exclusive agent or participant lease, valid only for a usable session. |
| Agent progress | Reuse run/tool activity; derive waiting from blocked action and represent help requests separately. |

Communicate availability, view freshness, control ownership, help requests, and available actions. Ownership does not imply activity; runtime failure differs from viewer disconnection. Preserve page state across agent turns and handoff within the thread; define idle cleanup and restart behavior. Never carry cookies between threads.

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
5. **Authorization:** enforce participant capabilities, thread disable, action grants, and publication permissions server-side.

## Security and failure requirements

- Authorize every session, view, and control request; thread visibility alone need not grant browser control.
- Isolate profiles and temporary data per thread. Enforce network egress policy outside the browser; allow the intended local dev app while blocking loopback/private/link-local destinations reached through external navigation, redirects, or DNS changes. Validate this in the spike.
- Treat all page-provided text, titles, URLs, screenshots, diagnostics, downloads, and tool results as untrusted. Prompt-injection detection may be evaluated as defense in depth, never as a substitute for isolation or authorization.
- A thread or administrator disable blocks managed provisioning, tool exposure, streaming, and control endpoints. Do not claim it blocks standalone browser binaries through shell access unless separately enforced.
- Mark stale views and disable their input; distinguish stream loss from runtime failure. Log operational metadata only.

## Rollout and open decisions

1. **Spike:** prove same-session observation/takeover/handback, CLI arbitration, stale-action handling, reconnects, local-app access, egress enforcement, semantic interaction, opt-in diagnostics, execution limits/cancellation, and cleanup.
2. **Before MVP:** settle participant permissions, low-privilege test credential handling, egress rules, lease recovery, event-history retention, and safe artifact hosting. Do not enable PR attachments until publication permissions and retention work for public/private repos.
3. **MVP:** on-demand local-app testing, live view, thread disable, takeover/handback, semantic interactions with visual fallback, bounded runs, and opt-in diagnostics. Capture minimal screenshots when they explain a UI change.
4. **Expand:** evaluate external browsing, saved authentication, file handling, recordings, and broader browser support only from demonstrated need.

Open decisions: runtime/transport; lease timeout and recovery; idle/restart policy; exact egress and action-confirmation rules; diagnostic redaction and retention; artifact destination and access; and whether broader shell-browser restrictions are required.

## Success criteria

- Local-app browser sessions start on demand, are reusable within the thread, and are disabled when the thread or administrator policy requires it.
- Semantic actions are preferred where available; screenshots cover visual checks and inaccessible interfaces.
- User takeover is exclusive across all supported control paths; handback refreshes agent context without replaying stale actions.
- Runs are bounded/cancellable and the agent verifies actual outcomes before reporting success.
- Diagnostics are opt-in and sanitized; safe, minimal evidence accompanies UI-change PRs only when authorized.
- Credentials, page data, and artifacts follow the defined isolation, access, and retention policies.
