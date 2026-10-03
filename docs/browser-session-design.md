# Interactive browser sessions

**Status:** Draft for discussion

**Scope:** Product and architecture proposal; implementation choices remain to validate

## Summary

Give Open SWE browser access for tasks that benefit from interacting with websites and applications. Thread participants can watch live, take control when needed, and hand the same session back without losing page or authentication state. The browser starts on demand; ordinary agent work needs no routine approval gates. When visual evidence helps explain the result, capture relevant screenshots or short recordings for the PR or thread.

## Representative workflows

- Build or modify a UI and verify it in the running application.
- Reproduce and diagnose a bug.
- Inspect websites or JavaScript-rendered documentation.
- Exercise authenticated application flows.
- Capture visual evidence for a PR or explanation.

For example, the agent starts a local app, investigates a UI bug while the user watches, accepts a user-completed login, then verifies the fix and attaches evidence to the PR.

Choose tools by task: prefer APIs, search, or HTTP retrieval when sufficient; use the browser when rendering, interaction, authentication, or visual context makes it more suitable. This is browser access, not a general remote desktop.

## Initial scope

Include on-demand browsing, live observation, same-session takeover, scoped authorization, screenshots, and short task-focused recordings.

Defer desktop streaming, browser-chrome controls, headed mode, seamless mode switching, and comprehensive support for engines, extensions, CAPTCHA, hardware keys, or OS authentication. Continuous recording and indefinite retention of all activity are outside the initial scope; selected intermediate screenshots and before/after captures are supported.

Saved authentication across threads is worth exploring separately. It does not require building a general credential vault.

## Product behavior

### Startup and controls

- Managed browser support is available by default, subject to administrator policy. The first browser action starts or connects to the thread's session; later actions reuse it. Opening a thread or panel does not launch a browser.
- Closing the panel only hides the view; agent browsing continues.
- Stopping the session ends that browser process. A later browser action may start a fresh session; the UI must explain possible loss of page or login state. Stopping is not disabling future browsing.
- Do not introduce a per-thread switch that disables all browser tooling. Administrator controls apply to the managed capability; broader sandbox restrictions are a separate enforcement decision.
- Report startup or streaming failures accurately and continue with other methods where possible.

### Observe, take over, and hand back

Watching is passive. **Take over** grants an exclusive input lease to one authorized participant after in-flight agent actions have settled or been safely cancelled. While the user holds it, agent actions targeting that session wait or return a recoverable control-conflict result. Unrelated coding work can continue.

**Hand back** releases the user lease. The agent inspects the current page before resuming; stale queued actions must not replay blindly. All access paths to the shared session, including CLI-issued actions, must respect the lease.

Other authorized participants may watch. A stale or disconnected viewer cannot send input. A controlling user's disconnect must not silently return control immediately or block the agent indefinitely: define a visible lease-expiration and recovery policy before release.

### Minimal state model

Keep independent facts without introducing four new state machines:

| Concern | Proposed representation |
|---|---|
| Session lifecycle | `starting`, `ready`, `stopping`, `failed`; absence means no active session. Disabled is policy; ended is history. |
| View connection | Per-viewer transport status and freshness, not a global session state. |
| Control | Exclusive lease owned by the agent or a specific participant, valid only for a usable session. |
| Agent progress | Reuse existing run/tool activity. Derive waiting for control from a blocked action; represent an explicit help request separately. |

The UI must communicate availability, view freshness, control ownership, requested help, and available actions. Labels such as **Agent has control**, **You have control**, and **View disconnected** are illustrative, not a fixed vocabulary. Ownership does not imply activity; browser failure must remain distinguishable from viewer disconnection.

## Persistence

| Data | Proposed lifetime |
|---|---|
| Browser preferences | Persist at the appropriate user/workspace scope. |
| Page state and authentication | Preserve across agent turns and handoff within the thread; define idle cleanup and restart behavior. |
| Existing managed credentials | Keep their existing storage lifecycle; scope and expire browser access grants separately. |
| Final screenshots and recordings | Retain under artifact policy, independently of browser lifetime. |
| Temporary captures and downloads | Thread-isolated, with bounded cleanup windows. |

Do not automatically carry cookies between threads. An opt-in saved-authentication feature needs explicit ownership, permitted destinations, participant access, expiration, and revocation. In shared threads, a personal login can expose account data to other viewers and controllers; disclose and authorize that access before reuse.

## Credentials and authorization

Candidate credential sources:

- **Application environment:** task-local configuration such as `.env`; inject into the app without exposing values in model context, logs, or artifacts where possible. A file's presence does not authorize every credential in it.
- **Managed connections:** existing integrations, resolved at runtime for the intended service and participant.
- **Browser login:** the user authenticates during takeover and hands back the same session.
- **Task-scoped secrets:** a secure input mechanism, never requests to paste secrets into chat.

Authentication supplies access; authorization defines allowed actions. Users may preauthorize all otherwise-confirmable actions within a stated task, application/account, and duration. Grants must be visible, revocable, and bounded by administrator restrictions. Do not repeatedly prompt within a grant; request additional authorization only outside it. Routine local testing needs no extra confirmation when already authorized.

Handback after login lets the agent act through that account; make this clear. Distinguish local/test access from consequential external access. Permission to browse private content is not permission to publish it in a PR. Exact grant boundaries and credential classification remain implementation decisions.

## Visual evidence

- Capture useful screenshots automatically, including before/after pairs when a meaningful baseline exists. Allow on-demand and selected intermediate captures.
- Use short recordings when stills cannot convey behavior, with configurable duration/size limits. Record the demonstration, not the entire session.
- Attach relevant evidence to the resulting PR, or provide a thread artifact when there is no PR. This applies to features, investigations, and explanations as well as UI fixes.
- Exclude credentials and unrelated private content; avoid capturing login entry and authentication handoff. Mask sensitive regions where feasible. If safe capture is uncertain, omit the evidence and explain why.
- Keep generated evidence out of source control by default. Validate artifact access and retention for public/private repositories before release; clean up temporary capture data separately.

## Runtime and architecture

Start with one isolated headless Chromium session per thread and a primary page. Headless does not mean invisible: the dashboard supplies the live view and input relay. Headed/desktop support would require a demonstrated need and separate design.

Evaluate `agent-browser` first rather than assume a parallel stack is necessary. Its availability alone does not establish integration with thread authorization, shared-session arbitration, streaming, or evidence delivery.

Logical boundaries:

1. **Session manager:** owns thread association, lifecycle, cleanup, and managed-capability policy.
2. **Runtime and control arbiter:** execute browser actions and user input against the same session; enforce the lease across tools and CLI access.
3. **Authenticated dashboard channel:** streams the view and relays authorized input. Never expose raw CDP, unauthenticated browser ports, or sandbox credentials to clients.
4. **Evidence pipeline:** produces scoped captures and publishes them under artifact policy.
5. **Authorization:** checks participant capabilities, credential grants, and publication permissions server-side.

A technical spike must validate runtime and transport choices, latency, reconnects, in-flight action handling, authentication continuity, local app access, resource cost, and cleanup.

## Security and failure requirements

- Authorize every session, view, and control request; thread visibility alone need not grant control or credential access.
- Isolate browser profiles and temporary data to the thread; apply the lifetimes above.
- Enforce sandbox egress/SSRF policy. Treat page content as untrusted agent input.
- Log operational metadata, not credentials, cookies, page contents, or captures by default.
- Mark stale views and disable their input; distinguish stream loss from runtime failure.
- Administrator hard-disable blocks managed provisioning, tool exposure, streaming, and control endpoints. Do not claim it blocks standalone browser binaries through shell access unless separately enforced. Hiding a UI control is not enforcement.

## Rollout and open decisions

1. **Spike:** validate the shared-session integration, CLI arbitration, takeover, reconnects, local/external navigation, credential handling, and captures.
2. **Before MVP release:** settle participant authorization, credential isolation and grants, egress enforcement, safe artifact publication, retention, lease recovery, and administrator-disable scope.
3. **MVP:** on-demand browser, live view, takeover/handback, scoped preauthorization, and automatic relevant evidence for PRs or threads.
4. **Expand:** consider saved authentication, additional tabs/engines, and headed/desktop workflows from demonstrated need.

Open decisions: runtime/transport; supported MVP credential sources; grant and step-up boundaries; idle/restart and disconnected-controller timeouts; artifact storage and redaction limits; deployment/workspace policy scope; and whether broader shell-browser restrictions are required.

## Success criteria

- Browser tasks start on demand and reuse the session without requiring a viewer or routine approval prompts.
- Users can observe and take over without agent input races; handback preserves page/authentication state and refreshes agent context.
- Closing the panel does not disable browsing; stale viewers cannot send input.
- Relevant, safe evidence accompanies PRs or thread results without continuous recording.
- Preauthorization avoids repeat prompts within scope; administrator controls and data-lifetime policies are enforced.
