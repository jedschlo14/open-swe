Sign the thread's browser in to an external site with a sign-in its owner saved earlier, so nobody has to sign in again.

Pass the site's origin, such as `https://staging.example.com`. The browser opens that origin with the saved session restored, and the result reports `ok` once the page no longer shows a sign-in form. Saved sign-ins exist only for external origins an admin approved, and only in threads their owner keeps private; you cannot see or change what a sign-in contains.

If there is no saved sign-in for the origin, the result lists the origins that have one. If it reports the sign-in expired, ask the person to take control in the Browser panel, sign in, and tick "Remember my sign-in" when they hand control back; do not try to sign in yourself with credentials from elsewhere. Pages you open afterwards still follow the usual confirmation rules for external sites.
