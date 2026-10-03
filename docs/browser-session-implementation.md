# Browser sessions: implementation

Cloud MVP: one browser session per thread. Local desktop support needs a separate host adapter.

## Where it runs and when it starts

Run headless Chromium with an isolated profile in the thread's existing coding sandbox, so it can reach the app under test. Start lazily on the first explicit browser action from the agent or an authorized dashboard user—not when the thread or Browser panel opens. A session manager binds the browser to the thread and makes concurrent startup idempotent. Stop on request, thread deletion or resolution, or idle expiry; hiding the panel does not stop it. An administrator policy gates provisioning and access.

## How the agent drives it

Agent browser tools operate on the same page shown in the dashboard. Prefer accessibility-tree and page-text reads with stable element references; use screenshots or coordinates for visual checks or inaccessible interfaces. Refresh references after navigation or major page changes and verify meaningful actions. Keep CDP loopback-only; expose narrow browser operations through the broker.

## How users watch and take control

The dashboard uses an authenticated, thread-scoped channel. Readers may watch; writers may send input. A single control lease arbitrates agent and participant actions: settle or cancel agent work before takeover; on handback, revoke participant input, discard stale actions, and refresh agent context. Disconnect alone does not transfer control. Keep session lifecycle, viewer connections, agent progress, and control ownership distinct; stale or non-ready sessions cannot accept input.

Browser-level arbitration cannot constrain shell-launched browsers unless shell access is separately gated. Define this boundary before claiming exclusive control. Isolate profiles by thread, restrict egress to the authorized local app and required development endpoints, and enforce authorization in the executor.

## Screenshots, recordings, and PRs

For applicable UI-change and UI-bug-fix PRs, capture a sanitized before/after screenshot pair when safe and useful; add a short recording only when it demonstrates behavior screenshots cannot. Keep captures out of Git, exclude unrelated private content, and apply bounded retention.

Do not use bearer download URLs as PR evidence: they may outlive access and are not scoped to PR readers. The PR image proxy serves published raster images; it cannot upload media or support video. Implement a repository-authorized publisher that checks access and retention before attaching screenshots. Define a safe video-publishing path before adding recordings. If safe publishing is unavailable, omit the evidence and explain why.

## Remaining decisions

Choose and pin an in-sandbox browser controller. Define durable session/lease state and recovery, allowed local endpoints, test-account policy, diagnostic redaction/retention, shell behavior during takeover, and the authorized publisher. Do not log page content or credentials; mark stale views and distinguish stream loss from browser failure.
