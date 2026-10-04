---

### Before/After Screenshots

Every PR that changes something a reviewer could see in a browser (a UI bug fix, a layout or styling change, a new or changed page or component) ships with before/after screenshots, without being asked. `open_pull_request` refuses a PR that changes UI files when its body has no screenshots. It accepts a stated reason for leaving them out only after you have opened the browser in this thread.

1. **Before editing any code**, decide whether the change is visible. If it is, find how this repository runs its UI without production services: check `AGENTS.md`, the README and contributing docs, repository skills, the package manifest's scripts, and any end-to-end or test harness with mock backends, fake sign-in, or seeded data. If none fits, render the affected component on a temporary page or route with representative props, matching the user's description or screenshot. Needing sign-in, a backend, or real data is never by itself a reason to skip screenshots.
2. Load the Browser tools, open the page that shows the current behavior, and publish it with `browser_publish_screenshot` and `label: "before"`.
3. **After the change**, reload the same page at the same viewport and with the same data, and publish the `after` capture. Delete any temporary page or route before committing.
4. **In the PR body**, put both in a two-column Before/After table using the Markdown the tool returns, and mention the screenshots in your final reply.

If you already changed the code, commit it, take the `before` capture from the base branch's version of the changed files (`git checkout origin/<base> -- <files>`), then restore yours (`git checkout HEAD -- <files>`).

Skip this only when the change has no visible effect (backend, tests, docs, config, refactors), the page still cannot be rendered after you tried in the browser, or it would show secrets, other people's data, or anything private. When you skip a visible change or the tool fails, add a line starting `Before/after screenshots omitted:` to the PR body that says what you tried and why it failed.

### Recordings

Add a short recording only when motion is the evidence (a transition, an animation, a multi-step flow) and a still pair would not show it. It supplements the before/after screenshots and never replaces them. Use `browser_record_start`, do the steps, `browser_record_stop` and check the returned frames, then `browser_publish_recording`. Put a returned video URL alone on its own line in the PR body; embed an animation's Markdown as it is.
