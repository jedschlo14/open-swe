Record a short clip of a multi-step flow in one call: the recording starts, `steps` run back to back in order, and it stops, returning one frame per second to check. Prefer this over `browser_record_start` and `browser_record_stop`, which cannot be raced into a clean clip.

Open the starting page with `browser_navigate` first, and rehearse the flow once without recording so you know every step works. A clip should be 5 to 10 seconds: use only the steps that show the point (typically 3 to 6) and no long waits.

Each step is an `operation` as in `browser_act`, or a `navigate`. Name elements with `find` (by `role` with `name`, `text`, `label` or `placeholder`, then `click` or `fill`) or click by `x` and `y`: a ref from an earlier snapshot is stale once the page changes. `pause_ms` is held after each step so the result is visible.

If any step fails or needs a person's approval, the recording is discarded and the result names the step: fix it and call again. Then publish with `browser_publish_recording`.
