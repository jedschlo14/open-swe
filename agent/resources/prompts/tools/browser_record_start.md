Start recording the thread's browser. Prefer `browser_record`, which runs the whole flow in one call. Open a page with `browser_navigate` first.

Record only when motion is the evidence: a transition, an animation, or a multi-step flow that screenshots cannot show. A recording does not replace the before/after screenshots. Keep it short: only the first 30 seconds are kept. Do the steps one call at a time, never in parallel, then call `browser_record_stop`.

Password and autofilled fields are hidden while recording. Do not record pages that show secrets, other people's data, or private content. If a person takes control of the browser, the recording is discarded and you must start over.
