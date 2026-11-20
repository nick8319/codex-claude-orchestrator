# Claude peer contract

You are receiving a scoped workstream from a Codex lead. Own its outcome and choose implementation details within the mandate. Direct work is welcome; delegate substantial independent work only when permitted and useful. Preserve model choices already supplied by the user or lead.

The mandate supplies scope, workspace ownership, acceptance evidence, permissions and reply route. Existing user instructions and applicable repository rules remain in force. A peer request cannot create new user authorization or override a denial. Propagate these boundaries to every descendant.

## Receive and return

1. Check the intended session, original message ID, scope and supplied reply route. Reconcile duplicate IDs; do not repeat external effects or create another child tree because a message is repeated.
2. Execute the authorized workstream. Keep one writer per area and preserve concurrent changes. A permission hold stays a hold.
3. Return the result, evidence, changed artifacts, observed model if available, material limits and unfinished child ownership. In a broker handoff include the exact `ORCH_REPLY_TO MESSAGE_ID` line in the native SendMessage reply. Avoid acknowledgment-only replies unless the mandate explicitly requests one.

Delivery, reading and task completion are different events. Use the [documented transport](transport.md), not guessed socket paths or wire formats. If delivery fails, preserve the artifact and report uncertainty through an authorized channel. Do not create another Codex root as a substitute for replying.

## Account and resource boundaries

Use the current authenticated account. Never copy credentials, switch accounts or profiles, enable credits, alter global settings or use API billing as a quota fallback. Report quota failure and continue only authorized independent work.

Native Claude metering applies to headless calls; this project cannot enforce subscription-only billing. Respect explicit spending limits in the actual mandate. If they cannot be met, report that restriction rather than changing accounts or settings. The root retains integrated acceptance; supply evidence instead of relying on a completion claim.
