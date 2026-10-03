Perform one action on the page in this thread's browser.

`operation.action` is one of:

- `click`: a `ref` from the latest snapshot, or `x` and `y` viewport coordinates for elements the snapshot cannot name (canvas, custom widgets).
- `fill`: replace the text in the `ref` field with `text`.
- `select`: choose `values` in the `ref` dropdown.
- `press`: a key such as `Enter`, `Tab`, or `Escape`.
- `scroll`: `direction` (`up`, `down`, `left`, `right`) by `pixels`.
- `wait`: pause for `milliseconds`, for example while the page loads.
- `back` or `reload`.
- `dialog`: `accept` (optionally with `text` for a prompt) or dismiss a JavaScript dialog.

Verify each meaningful action with a new snapshot or a screenshot rather than assuming it worked.

Some actions need a person's approval: deleting, paying or buying, subscribing, accepting or agreeing, granting access, signing up, accepting a dialog, and any submission on an external page. The result then has `status: "confirmation_required"` and a `confirmation_id`. Tell the user what you want to do and ask them to approve it in the Browser panel, then stop and wait for their reply. Once they approve, repeat the identical `operation` with that `confirmation_id`. An approval covers that one operation once. Never try to get around a required confirmation.

`status: "user_in_control"` means a person is controlling the browser: do not retry; wait until they hand it back. An action that was running when they took control is discarded rather than finished.

A result with `handback` means a person controlled the page since your last action and has returned it. It names who and the page they left. Earlier refs no longer work: take a new snapshot before acting.
