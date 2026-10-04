Perform one action on the page in this thread's browser.

`operation.action` is one of:

- `click`: a `ref` from the latest snapshot, or `x` and `y` viewport coordinates for elements the snapshot cannot name (canvas, custom widgets). The viewport follows the size of the person's panel and can change between calls, so take a fresh screenshot before clicking by coordinates.
- `fill`: replace the text in the `ref` field with `text`.
- `find`: click or fill an element by what it shows, with no snapshot: `by` is `role` (with an optional `name`), `text`, `label` or `placeholder`, `value` is what to match, `do` is `click` or `fill` (with `text`).
- `select`: choose `values` in the `ref` dropdown.
- `press`: a key such as `Enter`, `Tab`, or `Escape`.
- `scroll`: `direction` (`up`, `down`, `left`, `right`) by `pixels`.
- `wait`: pause for `milliseconds`, for example while the page loads.
- `back` or `reload`.
- `dialog`: `accept` (optionally with `text` for a prompt) or dismiss a JavaScript dialog.

Verify each meaningful action with a new snapshot or a screenshot rather than assuming it worked.

Clicking something that looks sensitive on an external page (deleting, paying or buying, subscribing, accepting or agreeing, granting access, signing up) needs a person's approval. The result then has `status: "confirmation_required"` and a `confirmation_id`. Tell the user what you want to do and ask them to approve it in the Browser panel, then stop and wait for their reply. Once they approve, repeat the identical `operation` with that `confirmation_id`. An approval covers that one operation once. Never try to get around a required confirmation.

`status: "user_in_control"` means a person is controlling the browser: do not retry; wait until they hand it back. An action that was running when they took control is discarded rather than finished.

A result with `handback` means a person controlled the page since your last action and has returned it. It names who and the page they left. Earlier refs no longer work: take a new snapshot before acting.

Browser tools run one at a time per thread, in the order you call them. Call them one after another, each depending on the last, rather than batching a flow.
