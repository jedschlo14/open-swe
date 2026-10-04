Capture the browser's viewport and publish it as pull request evidence, returning Markdown to embed.

Use it for UI-bug-fix and UI-change PRs when the browser can show the original behavior and the result. Take the `before` capture before you change the code, then fix, reload, and take the `after` capture of the same page, viewport, and data. Put both in the PR description as a two-column Before/After table. For other work, publish only when asked or when it clearly helps a reviewer.

The image is stored on the repository's own evidence branch, so only people who can read the repository can see it, and it is deleted after 30 days. Password and autofilled fields are hidden in the capture, but nothing else is redacted: make sure the page shows no secrets, other people's data, or private content you would not put in the PR. If the page does, do not publish it.

Never use a download link for PR evidence. If this tool returns `status: "error"`, leave the screenshot out and say in the PR description that before/after evidence was omitted and why.
