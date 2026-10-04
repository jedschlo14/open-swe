---

### Before/After Screenshots

Every PR that changes something a reviewer could see in a browser (a UI bug fix, a layout or styling change, a new or changed page or component) ships with before/after screenshots, without being asked. `open_pull_request` refuses a PR that changes UI files when its body has neither the screenshots nor a stated reason for leaving them out.

1. **Before editing any code**, decide whether the change is visible. If it is, load the Browser tools, start the app, open the page that shows the current behavior, and publish it with `browser_publish_screenshot` and `label: "before"`. If the full app needs services you can't run here (sign-in, a backend, real data), render the affected component on a temporary page or route with representative props, matching the user's description or screenshot.
2. **After the change**, reload the same page at the same viewport and with the same data, and publish the `after` capture. Delete any temporary page or route before committing.
3. **In the PR body**, put both in a two-column Before/After table using the Markdown the tool returns, and mention the screenshots in your final reply.

If you already changed the code, commit it, take the `before` capture from the base branch's version of the changed files (`git checkout origin/<base> -- <files>`), then restore yours (`git checkout HEAD -- <files>`).

Skip this only when the change has no visible effect (backend, tests, docs, config, refactors), the page cannot be rendered here even in isolation, or it would show secrets, other people's data, or anything private. When you skip a visible change or the tool fails, add a line starting `Before/after screenshots omitted:` with the specific reason to the PR body.
