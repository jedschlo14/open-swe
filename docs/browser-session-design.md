# Interactive browser sessions

**Status:** Draft for discussion

## Purpose

Give each cloud thread an isolated browser for building, testing, and debugging local web apps—not a general-purpose remote desktop or external-site research tool.

## Runtime and agent interaction

Run headless Chromium with one isolated profile and page inside the thread's existing coding sandbox, where it can reach the local app. Start the session on the first explicit browser action by the agent or an authorized user, never just because a thread or Browser panel opens. A session manager starts or reconnects the sandbox idempotently and preserves the browser across agent turns; stop on request, thread deletion/resolution, or idle expiry.

The agent drives the same page through browser tools and an authenticated broker. Prefer semantic reads of visible content and stable element references; use screenshots or coordinates when visual rendering requires them. Keep raw DevTools/CDP private and inspect the page after meaningful actions.

## Watching and taking control

The thread's Browser panel shows session status, a live view, controller, and actions. Thread readers can watch; only thread writers can control, with separate server-side authorization for each. A writer takes over through an exclusive lease: settle agent actions before transfer, block agent input while the user controls the page, and revoke user input on handback before the agent rereads it. Hiding the panel does not stop the session, and disconnect alone does not transfer control. Constrain shell-based browser access during takeover or explicitly exclude it from the lease guarantee.

## Screenshots and recordings in PRs

For UI-change and UI-bug-fix PRs, capture sanitized before/after screenshots when the browser can reproduce and verify the change. Add a short recording only when motion materially clarifies behavior. Publish captures through a repository-authorized artifact publisher that checks PR access and provides PR-readable image URLs and recording links; embed those in the PR description, not sandbox bearer links. Keep temporary files out of Git and apply bounded retention. Omit unsafe or unavailable evidence and explain why.

## MVP safeguards and scope

Limit use to local/test apps and low-privilege test accounts. Restrict network access to the authorized local app and necessary development endpoints; block general internet and unrelated private/local destinations. Treat page content and browser output as untrusted, and require human confirmation for consequential actions. Enforce permissions and administrator disable across provisioning, agent tools, viewing, and control. Isolate profiles and artifacts by thread; do not inject task secrets or support saved authentication in MVP. Later work may consider external browsing, saved authentication, uploads, more tabs or engines, and headed mode.
