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

Use one isolated headless Chromium session per thread with a primary page. The dashboard provides the live view and input relay; headless is not invisible. Evaluate `agent-browser` first, but compare it with a Playwright-based local runtime and managed-browser option against shared-session arbitration, local-app routing, streaming, security, cost, and cleanup—not availability alone.

1. **Session manager:** thread association, lifecycle, cleanup, and managed-capability policy.
2. **Runtime/control arbiter:** execute agent and user actions against the same session; enforce the lease across tools and CLI.
3. **Authenticated dashboard channel:** stream view and relay authorized input; never expose raw CDP or unauthenticated browser ports.
4. **Evidence pipeline:** scoped capture and publication under artifact policy.
5. **Authorization:** enforce participant capabilities, action grants, and publication permissions server-side.

## Security and failure requirements

- Authorize every session, view, and control request using the thread's existing read/write policy: thread readers may watch; thread writers may control. Do not grant access to anyone who lacks the corresponding thread permission.
- Isolate profiles and temporary data per thread. MVP browser networking is limited to the explicitly authorized local test app and required local development endpoints; general internet and other local/private/link-local destinations are blocked. Enforce this outside the browser, including redirects, DNS changes, and alternate access paths.
- Treat all page-provided text, titles, URLs, screenshots, diagnostics, downloads, and tool results as untrusted. Prompt-injection detection may be evaluated as defense in depth, never as a substitute for isolation or authorization.
- Administrator disable blocks managed provisioning, tool exposure, streaming, and control endpoints. Do not claim it blocks standalone browser binaries through shell access unless separately enforced.
- Mark stale views and disable their input; distinguish stream loss from runtime failure. Log operational metadata only.

## Rollout and remaining decisions

1. **Implementation planning:** select a runtime and transport, define the lease recovery behavior, enumerate allowed local development endpoints, specify test credential setup, set diagnostic redaction and retention, choose screenshot/recording artifact hosting and cleanup, and determine whether broader shell-browser restrictions are necessary. Resolve these through design and implementation work; do not make a separate feasibility spike a prerequisite.
2. **MVP:** implement on-demand local-app testing, live view, reader/writer authorization from the existing thread policy, takeover/handback, semantic interaction with visual fallback, cancellation, persistent same-thread sessions with idle expiry, opt-in diagnostics, enforced local-only egress, and automatic safe before/after screenshots—and useful short recordings where warranted—on applicable UI-change PRs. Define lease recovery and bounded artifact access/retention before enabling those paths.
3. **Expand:** consider general external browsing, saved authentication, file handling, additional recording use cases, configurable run quotas, and broader browser support only from demonstrated need.

If a concrete implementation blocker shows that an agreed requirement cannot be delivered safely or reliably, revisit its design and scope then; do not plan around presumed infeasibility.

## Success criteria

- Local-app browser sessions start on demand and are reusable within the thread; administrator policy can disable managed browser access.
- Semantic actions are preferred where available; screenshots cover visual checks and inaccessible interfaces.
- Thread readers can watch browser sessions and thread writers can take over; control is exclusive across supported paths, and handback refreshes agent context without replaying stale actions.
- Sessions persist across prompts and agent turns, then stop on explicit request, thread deletion/resolution, or idle expiry (initially one hour) with a visible warning; expired sessions restart cleanly.
- Runs can be cancelled, and the agent verifies actual outcomes before reporting success; no fixed per-run quota is required.
- Diagnostics are opt-in and sanitized; UI bug-fix and UI-change PRs include a safe, minimal before/after screenshot pair when reproducible and authorized, plus a short recording when it materially demonstrates the fix better than screenshots.
- Credentials, page data, and artifacts follow the defined isolation, access, and retention policies.
