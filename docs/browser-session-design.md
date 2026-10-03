# Browser sessions: product design

## Purpose

A thread-scoped browser helps Open SWE build, test, and debug web interfaces in the app running with its code. It is for local-app verification, not general-purpose browsing or remote desktop access.

## MVP experience

The Browser surface lives in the thread's right panel. It starts only when a person or the agent explicitly uses the browser; opening or restoring the panel does not start compute. Hiding the panel leaves the session running. The session keeps its page state across agent turns and ends when stopped, the thread closes, or it expires from inactivity.

Thread readers can watch. Thread writers can take control, which pauses agent browser actions until control is handed back. The agent checks the page after meaningful actions and verifies outcomes rather than assuming an action succeeded. It prefers accessible page structure and stable element references; screenshots or coordinates are used when visual layout or inaccessible content requires them.

## Safety and evidence

MVP use is limited to local/test apps and low-privilege test accounts. Browser profiles are isolated by thread, and network access is limited to the app and required local development endpoints. Page content is untrusted. External submissions, purchases, destructive changes, account changes, and consent require human confirmation; secrets must not be pasted into chat or injected from task configuration.

Diagnostics are opt-in, redacted, and bounded. Do not retain or publish page contents, credentials, cookies, or captures by default. Any capture shared outside the session must be sanitized and published with repository access controls.

For UI-change and UI-bug-fix pull requests, provide a before/after screenshot pair when it is safe and useful. Add a short recording only when it demonstrates behavior still images cannot. For other work, capture evidence only when requested or materially useful. Keep temporary captures out of Git and clean them up after a bounded retention period.

## Scope

MVP covers one isolated browser session per thread, local-app testing, live viewing, writer takeover and handback, semantic interaction with visual fallback, cancellation, and idle expiry. Administrators can disable managed browser use.

General external-site browsing, saved authentication, file uploads, multiple tabs or browser engines, headed mode, and profiles shared across threads are out of scope until there is a demonstrated need. Saved authentication must not become a general credential vault.

See [Browser sessions: implementation](browser-session-implementation.md) for the runtime and evidence-publishing design.
