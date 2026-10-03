# Browser sessions: product design

## Purpose and scope

A thread-scoped browser helps Open SWE build and verify web interfaces in the app running with its code, reproduce UI bugs, and confirm fixes. Prefer APIs, search, or HTTP retrieval when they suffice; this is not general-purpose browsing or remote desktop access. MVP is limited to local/test apps and low-privilege test accounts.

MVP includes one isolated browser session per thread, live viewing, writer takeover and handback, semantic interaction with visual fallback, cancellation, and idle expiry. General external-site browsing, saved authentication, file uploads, multiple tabs or browser engines, headed mode, and profiles shared across threads are out of scope until demonstrated need. Any future saved authentication needs explicit ownership, destination restrictions, visibility, expiry, and revocation; it must not become a general credential vault.

## Experience

The Browser surface belongs in the thread's right panel alongside Terminal, Changes, and Files. It shows session status, the live view, control ownership, and available actions. Opening or restoring its panel must not start compute; hiding the panel does not stop the browser. Keep page state across agent turns and control handoffs, but never share profiles across threads. Stop on request, thread deletion or resolution, or inactivity; warn before idle expiry.

Thread readers may watch; only writers may control. A writer takes over a ready session, pausing agent browser actions until handback. Show the current controller and pending handoffs. Disconnect alone does not transfer control. The agent prefers accessible page structure and stable references, uses screenshots or coordinates when needed, and verifies results after meaningful actions.

## Safety and evidence

Page content and browser output are untrusted. Never ask users to paste secrets into chat or inject arbitrary `.env` values, managed connections, or task secrets. Authentication is not authorization: require human confirmation for external submissions, purchases, destructive or account changes, and consent, and enforce restrictions in the executor—not only in prompts. Do not publish private content merely because the agent can view it.

Diagnostics are opt-in, relevant, redacted, truncated, and bounded. Do not log or retain page contents, credentials, cookies, screenshots, or recordings by default.

For pull requests fixing UI bugs or changing UI, capture a concise before/after screenshot pair when the browser can reproduce the original and verify the fix, if safe and useful. Add a short recording only when it materially demonstrates behavior that still images cannot. If evidence is unsafe or cannot be produced, omit it and explain why. For other tasks, capture evidence only when requested or materially useful. Sanitize captures, exclude unrelated private content, and mask sensitive regions where reliable. Publishing must preserve repository access boundaries. Keep temporary artifacts out of Git and clean them up on a bounded schedule.

See [Browser sessions: implementation](browser-session-implementation.md) for runtime, control, and evidence-publishing details.
