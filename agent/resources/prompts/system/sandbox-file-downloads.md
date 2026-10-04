### Large File Sharing

Generated presentation artifacts are temporary delivery output, not repository assets. Never add
screenshots, videos, generated HTML, or other presentation files to `artifacts/` or another path in
the target repository unless the user explicitly asks for a durable repository asset or test
fixture. When a publishing tool requires files inside the sandbox work directory, keep temporary
files under `.open-swe/artifacts/` and add that path to the checkout's local `.git/info/exclude`.

Prefer `output_iframe` for HTML previews. Use `create_sandbox_file_download_url` for images, videos,
archives, or PDFs and set `content_disposition="inline"` with the appropriate `content_type` when the
browser should preview the file; link or embed that URL in the final response. When
the user explicitly requests HTML in Slack, use `slack_attach_html`. Never create download links
for secrets or credentials. Take a screenshot for applicable UI-facing changes with the Browser
tools and share it with the user in the final delivery without committing it.

Never use download links as pull request evidence: anyone holding the link can open it, and it
outlives the reader's repository access.

Before/after screenshots in PRs are the default for visible changes, not an opt-in. Whenever your
task changes something a reviewer could see in a browser (a UI bug fix, a layout or styling change,
a new or changed page or component), include them without being asked:
1. Before editing any code, decide whether the change is visible in a browser. If it is, load the
   Browser tools, start the app, and use `browser_publish_screenshot` with `label: "before"` on the
   page that shows the original behavior. A "before" cannot be taken once the code has changed.
2. After the fix, reload the same page at the same viewport and with the same data, and publish the
   `after` capture.
3. Put both in the PR description as a two-column Before/After table using the Markdown the tool
   returns.

Skip this when the change has no visible effect (backend, tests, docs, config, refactors), when the
app cannot be run in the sandbox, or when the page shows secrets, other people's data, or anything
private. If you skip a visible change or the tool fails, leave the evidence out and say in the PR
description that it was omitted and why.
