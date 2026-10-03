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
outlives the reader's repository access. Until a publisher that checks repository access exists,
leave screenshots and recordings out of pull requests and say in the description that before/after
evidence was omitted and why. Share captures with the user in this conversation instead.
