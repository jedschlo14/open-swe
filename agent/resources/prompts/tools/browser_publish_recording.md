Publish the recording you stopped with `browser_record_stop` as pull request evidence, returning what to put in the PR description.

When the thread's owner has a GitHub token that GitHub accepts for uploads, the recording is uploaded as a GitHub attachment and you get a URL: put it alone on its own line in the PR description and GitHub shows a video player. Attachments cannot be deleted, so anyone who can read the repository can watch it for as long as the repository exists, and it is public on a public repository.

Otherwise it is published as an animated image on the repository's evidence branch: embed the returned Markdown. It loops without controls, and is deleted after 30 days.

Only publish after checking the frames from `browser_record_stop`. If this tool returns `status: "error"`, leave the recording out and add a line starting `Recording omitted:` with the reason to the PR description.
