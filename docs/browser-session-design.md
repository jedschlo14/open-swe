# Browser sessions: product design

A thread-scoped browser lets Open SWE build and verify the local web app, reproduce UI bugs, and confirm fixes. Prefer APIs or HTTP retrieval when sufficient; this is not general browsing or remote desktop access. MVP supports one isolated session per thread in the cloud. Local desktop requires a separate adapter.

## Experience

The Browser surface sits beside Terminal, Changes, and Files. It shows session status, live view, controller, and actions. Start only on explicit agent or user browser use; opening/restoring the panel never starts compute. Hiding it does not stop the session. Keep page state across agent turns and handoffs; stopping may lose it. Support cancellation. Stop on request, thread closure, or a provisional one-hour idle timeout, with a warning before expiry.

Thread readers may watch; writers may take control. Pause agent browser actions during takeover; on handback, revoke user input and refresh the agent's page context. Prefer accessible page structure and stable references; use screenshots or coordinates when needed, and verify meaningful actions.

## Safety and evidence

Limit MVP to local/test apps and low-privilege test accounts. Isolate profiles by thread and restrict browser traffic to the authorized app and required development endpoints. Treat page content as untrusted. Never inject task secrets or ask users to paste them into chat. Require human confirmation for external submissions, purchases, destructive/account changes, and consent; enforce this in the executor. Diagnostics are opt-in, redacted, and bounded; do not log or retain page content, credentials, cookies, or captures by default.

For UI-bug-fix and UI-change PRs, include a sanitized before/after screenshot pair when the browser can reproduce the original and verify the result, if safe and useful. Add a short recording only when it materially adds evidence. For other work, capture only when requested or useful. Preserve repository access controls; omit unsafe or unavailable evidence with an explanation. Keep temporary captures out of Git and delete them on a bounded schedule.

External-site browsing, saved authentication, uploads, multiple tabs or engines, headed mode, and cross-thread profiles are out of scope. Any future saved authentication needs explicit ownership, destination restrictions, expiry, and revocation; it is not a general credential vault.

See [implementation](browser-session-implementation.md) for architecture and acceptance requirements.
