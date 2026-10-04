Capture the browser's viewport and publish it as pull request evidence, returning Markdown to embed.

Use it by default, without being asked, for any PR that changes something visible in a browser: UI bug fixes and UI changes. Decide before you edit any code: take the `before` capture first, then fix, reload, and take the `after` capture of the same page, viewport, and data. Put both in the PR description as a two-column Before/After table. For work with no visible effect, publish only when asked or when it clearly helps a reviewer.

The image is stored on the repository's own evidence branch, so only people who can read the repository can see it, and it is deleted after 30 days. Password and autofilled fields are hidden in the capture, but nothing else is redacted: make sure the page shows no secrets, other people's data, or private content you would not put in the PR. If the page does, do not publish it.

Never use a download link for PR evidence. If this tool returns `status: "error"`, leave the screenshot out and say in the PR description that before/after evidence was omitted and why.
