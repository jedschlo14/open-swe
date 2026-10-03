# Browser sessions for interactive UI development

**Status:** Draft for discussion

**Scope:** Product and architecture proposal; implementation details remain to validate

## Summary

Give each Open SWE thread an optional, isolated browser session that the agent can use for UI work and the user can observe live from the thread. The agent normally drives the browser without interruption. A user can take control when needed—for example, to complete a login—and hand control back to the agent in the same session. For UI-fix work, Open SWE should produce concise, task-focused visual evidence and include it with the pull request automatically.

The initial implementation should use headless Chromium. “Headless” describes how the browser runs, not whether people can see or control it: the dashboard presents a live view and relays user input. Headed browser and desktop streaming are not needed for the target workflow and are out of scope for the first release.

Browser support is enabled by default, but the browser should start lazily when a task needs it rather than opening on every thread. Users can disable it for an individual thread. Workspace/deployment administrators need a hard disable that prevents the managed browser capability from being provisioned or used.

## Problem and target workflow

The primary use case is fixing a UI bug:

1. The user asks Open SWE to fix a UI bug.
2. Open SWE starts the app and opens it in the browser.
3. The user can watch the live page from the thread while the agent investigates and verifies the fix.
4. If the agent is blocked—for example, by a login—the user can take control, complete the step, and hand the same browser back.
5. Open SWE captures the fixed behavior and automatically includes concise evidence with the PR.

The browser is part of the coding workflow, not a general-purpose remote desktop. It should accelerate development rather than introduce routine approval gates.

## Goals

- Let the agent exercise real, JavaScript-rendered applications during development and verification.
- Let thread participants observe the current browser view in near real time.
- Support explicit, race-free user takeover and handback without losing page or authentication state.
- Keep the agent autonomous for ordinary development work; observation alone must not pause the agent.
- Support task-authorized credentials through more than manual login, while minimizing repeated permission prompts.
- Automatically produce useful, appropriately scoped visual evidence for UI-fix PRs.
- Make browser use optional per thread and completely disableable at workspace/deployment level.
- Isolate browser processes and state to the thread and clean them up promptly.

## Non-goals for the first release

- A general remote desktop, operating-system desktop, or browser-chrome control surface.
- Headed mode, virtual desktop streaming, or seamless headless-to-headed switching.
- Cross-thread browser profiles, persistent personal browsing profiles, or a general credential vault.
- Browser use as a substitute for API tools, web search, or HTTP fetch.
- Mandatory user approval for ordinary clicks, typing, navigation, screenshots, or other routine test actions.
- Continuous session recording, long-term video archives, or retaining every intermediate screenshot.
- Supporting every browser engine, mobile browser, extension, CAPTCHA, hardware-key, or OS-authentication flow.

## Product behavior

### Enablement and startup

- The feature is available by default, subject to an administrator's hard-disable policy.
- A thread-level control can turn browser access off. Turning it off ends or disables that thread's managed browser session and removes browser controls from the thread UI.
- The browser starts lazily when the agent needs it for a UI task, such as starting a local app and opening its URL. Merely opening a thread does not start Chromium.
- If browser startup or streaming is unavailable, the agent reports the limitation and continues with other available methods where possible; it should not silently imply that the user can see a live session.
- Browser settings, credentials, and session state are scoped to the thread and are ephemeral by default.

### Observe, take over, and hand back

- A live view is available while the agent is driving. Watching is passive and does not pause the agent.
- **Take over** atomically pauses browser actions by the agent and grants the user the input lease. The rest of the coding run need not be paused unless it reaches a browser action that must wait for control.
- While the user owns the input lease, the agent cannot concurrently click, type, navigate, or otherwise mutate the page. Browser actions wait or return a recoverable “user has control” result; they must not race ahead against a stale page.
- **Hand back** releases the user's lease, lets the agent inspect the current page, and resumes browser work from the resulting state. The agent must not assume the page is unchanged from before takeover.
- Control is single-writer. Other authorized participants can watch but cannot send input until control is released or transferred.
- If the dashboard disconnects, input is disabled until the view is current again. The browser session may continue under its existing controller; the UI must show that the view is stale and who still owns control.

### State model

Do not represent the session with one overloaded status enum. Lifecycle, transport, controller, and agent activity are independent dimensions. The UI can combine them into concise labels.

| Dimension | Representative states | Meaning |
|---|---|---|
| Session lifecycle | off, starting, ready, stopping, ended, failed | Whether a managed browser session exists and can be used. |
| Dashboard connection | live, reconnecting, disconnected | Whether the dashboard has a current view and can safely relay input. |
| Input controller | agent, user, none | Which actor holds the exclusive input lease. |
| Agent activity | acting, idle, waiting for user, waiting for control, finished, failed | What the agent is doing with respect to browser work. |

“Paused” is not a controller: it means the agent is not currently taking browser actions. “User is controlling” means the user owns the input lease, which implies the agent is paused for browser actions. “Waiting for user” means the agent has explicitly reached a point where user help is needed, but the user has not necessarily taken control. “Reconnecting” is a transport condition, not a lifecycle or control state.

Example user-facing labels include **Agent is driving**, **You’re driving**, **Waiting for you**, **Reconnecting — view may be stale**, and **Browser unavailable**. A failed session and a disconnected dashboard must remain distinguishable.

## Credentials and authorization

Browser tasks may need access to application credentials. User takeover for a web login is useful, but it is not the only possible source. The design should support a small set of explicit, scoped sources rather than treating arbitrary credentials as ordinary prompt text.

Candidate sources to support and validate:

1. **Application runtime environment:** project `.env` or other task-local configuration used to start the app. Where possible, inject values into the app process without echoing secret values into model context, tool output, logs, screenshots, or PR artifacts.
2. **Open SWE-managed connections:** existing workspace or user integration credentials, resolved at runtime and limited to the intended service and participant.
3. **User authentication in the live browser:** the user signs in during takeover; the same browser retains the resulting session when control returns to the agent.
4. **Task-scoped user-provided secrets:** a secure UI or secret mechanism may grant a specific credential for a stated task. Do not ask users to paste secrets into chat.

Credential access should be explicit, least-privilege, and short-lived where possible. A `.env` file can contain production credentials as well as local test values; its presence in a repository is not itself proof that every value is safe to use. The system should distinguish local/test credentials from credentials that grant access to real external systems.

Avoid per-click confirmation. Prefer one task- or credential-scope authorization that states what the agent can access and for how long. For routine local UI testing, no extra confirmation should be required when the task's authorized environment already provides the needed access. If credentials grant consequential access, use a narrow scope and avoid prompting repeatedly; request a step-up only when the agent needs authority materially beyond the task's granted scope, or decline an action prohibited by policy. Exact credential classification and step-up behavior remain open design decisions.

After a user signs in and hands the browser back, the agent can act with the authority of that authenticated session. Make this consequence clear in the UI. Do not persist browser cookies or authentication state beyond the thread unless a separate, explicit feature is designed for it.

## Evidence for pull requests

Evidence is part of the UI-fix workflow and should be automatic, not another routine approval gate.

- Capture task-relevant screenshots of the fixed state; capture a before/after pair when a meaningful “before” state is available.
- For behavior that is hard to convey in still images, create a short, focused recording of the relevant interaction. Do not record the whole browser session. A configurable hard duration/size cap should be chosen during implementation; a short clip on the order of tens of seconds is a reasonable starting point to validate.
- Start capture only for the demonstration, not for the entire development session. Delete temporary raw capture data after the final evidence is produced or after a short cleanup window.
- Automatically attach evidence to the PR when the task results in a UI-fix PR. If no PR is opened, make it available as a thread artifact.
- Evidence must not include passwords, tokens, secret-bearing fields, or unrelated private content. Mask password and secret inputs where technically possible; avoid recording login entry and authentication handoff. If evidence cannot be safely scoped, omit it and tell the user rather than silently publishing sensitive content.
- Do not commit generated screenshots or videos into the source tree by default. Use a PR-compatible artifact mechanism with access controls appropriate to the repository; its storage, retention, and private/public PR behavior must be validated before implementation.

## Browser mode and live view

The first release uses one isolated headless Chromium session per thread, with a primary page for the target app. Headless Chromium can render the app, execute JavaScript, accept browser-level input through automation, and produce screenshots/video. The dashboard's live view and input relay provide visibility and takeover; a visible OS window is not required.

Headed mode is out of scope. It would be reconsidered only for a demonstrated requirement such as testing browser chrome/extensions, an OS-level flow, or a display/GPU-specific defect. Those cases may need desktop streaming, not merely launching Chromium in headed mode. Do not promise that switching a running session between modes is seamless: it may require a new process and could lose page state or authentication.

The runtime choice is open. `agent-browser` is available in some task sandboxes and exposes browser control/streaming capabilities, but its presence in a sandbox does not establish that it is integrated with Open SWE's thread authorization, dashboard transport, lifecycle, or evidence flow. Evaluate it alongside other viable runtimes against the requirements below.

## Proposed architecture boundaries

This is a logical design, not a prescribed transport implementation.

1. **Thread-scoped browser session manager** owns lifecycle and associates exactly one managed session with the authorized thread. It starts/stops the browser in that thread's isolated sandbox and enforces the feature-disable policy.
2. **Browser runtime** owns Chromium and performs navigation, agent actions, user input, page inspection, and bounded capture. All agent browser actions and user input pass through one control arbiter; neither actor can bypass the input lease through the managed interface.
3. **Authenticated dashboard channel** carries the live view to authorized thread participants and relays user input back through the session manager. Do not expose raw Chrome DevTools Protocol (CDP), an unauthenticated browser port, or sandbox credentials to the browser client.
4. **Agent browser tools** operate on the same session shown in the dashboard. While the user has control, calls are serialized or suspended and resume only after handback; on resume, refresh page context.
5. **Evidence pipeline** creates bounded task-focused captures, applies secret/private-content protections, and attaches the result to the PR or thread artifact without adding generated files to the repository.
6. **Policy layer** combines deployment/workspace hard-disable, per-thread user disable, participant authorization, credential scope, and artifact policy. A UI toggle alone is not an enforcement boundary.

The streaming transport (for example, WebRTC versus image/frame updates over a WebSocket) and browser automation runtime must be selected by a technical spike. Validate latency, bandwidth, reconnect behavior, same-session input handoff, authenticated access, capture quality, resource cost, and cleanup. Do not expose a direct browser/CDP endpoint to solve streaming before reviewing its security boundary.

## Privacy, security, and failure behavior

- Browser process, profile, cookies, downloads, and temporary captures are isolated to the thread and removed when no longer needed.
- Enforce authorization on every session/view/control request; verify thread participation and appropriate capability on the server, not only in the dashboard.
- Validate outbound browser navigation and network access against sandbox egress/SSRF policy. The browser must not become an unrestricted path to private infrastructure or instance credentials.
- Treat webpage text, screenshots, and accessible content as untrusted input to the agent.
- Do not log credential values, cookies, page contents, screenshots, or video frames by default. Operational telemetry should record lifecycle and error metadata only.
- If the stream drops, visibly mark stale output and disable user input. If browser control or the runtime fails, report the failure and allow the agent to continue with non-browser work where safe.
- A workspace/deployment hard disable must prevent session provisioning, tool exposure, streaming, and control endpoint use. If the sandbox image includes a standalone browser CLI that an agent can run through shell access, determine whether that CLI must also be excluded or restricted to make “disabled” a stronger guarantee; hiding the product tool alone is insufficient.

## Rollout proposal

1. **Feasibility spike:** Confirm one runtime can keep a browser session in the thread sandbox while delivering a live dashboard view and accepting user input on the same page. Test takeover/handback, disconnection, local app access, `.env`-backed app startup, and capture cleanup.
2. **MVP:** Ship opt-out per thread and administrator hard-disable; lazy headless session; live view; single-controller takeover/handback; local application testing; automatic bounded screenshot evidence and focused short recording where useful; PR attachment.
3. **Harden:** Validate credential sources/scopes, repository privacy and artifact retention, authorization and egress enforcement, resource quotas, and observability before broad enablement.
4. **Expand only from demonstrated need:** Additional tabs, browser engines, extensions, persistent profiles, headed/desktop mode, and broader credential sources are separate decisions.

## Open questions

These questions do not block this product draft, but need answers before or during implementation:

1. **Default and admin policy:** Confirm whether “on by default” means enabled capability with lazy startup (proposed here), and whether the hard disable is deployment-wide, workspace-specific, or both.
2. **Credential contract:** Which of the candidate credential sources are MVP requirements? How can Open SWE distinguish local/test secrets from production or consequential credentials? What task scope and lifetime should a grant have?
3. **Step-up boundary:** Which actions materially exceed an existing task-scoped authorization and should trigger one consolidated confirmation or be disallowed? How can the policy avoid prompts for routine test actions?
4. **Evidence destination:** What PR-compatible storage supports private and public repositories with appropriate viewer access and retention? Should stills always attach, with recordings only when useful, or should every UI-fix PR include a short clip?
5. **Redaction and safe capture:** Which fields and page regions can be reliably excluded or masked? What should happen when safe capture cannot be guaranteed?
6. **Runtime and stream:** Can `agent-browser` or another runtime meet same-session viewing/control, thread authorization, reconnect, and lifecycle requirements without exposing CDP? Which transport provides acceptable latency and cost?
7. **Hard-disable guarantee:** Should disabling the managed browser capability also prevent browser binaries/CLI use through the general-purpose sandbox shell, and what enforcement mechanism is available?

## Success criteria

- A user asks for a UI fix; Open SWE starts the app, opens it in the thread's browser, and the dashboard shows a live, current view without user intervention.
- The user can take over at a login or other genuine blocker, interact without agent/browser races, and hand back the same session with authentication and page state intact.
- The agent continues verification and produces concise evidence automatically attached to the resulting PR, with no unnecessary session-long recording or routine approval prompts.
- Per-thread disable and administrator hard-disable prevent the managed browser capability and its view/control paths from being used.
- Browser cleanup removes ephemeral session state and temporary captures according to the defined retention policy.
