# Scoped mandates

Give a lead enough context to own an outcome, not a command-by-command script. Omit fields already present in its context.

```text
Outcome and why it matters:
Relevant context and prior decisions:
Owned checkout/files and concurrent owners:
Acceptance evidence:
Authority and constraints:
Resources: explicit limits, shared descendant allocation, stop conditions:
Coordination: original message ID, exact recipient, reply route:
Return: result, evidence, changed artifacts, limits, unfinished child ownership:
```

Local repository instructions and the user's restrictions travel with the mandate. Fresh peers do not know the previous conversation. Ask for progress at meaningful milestones or blockers; use tool waits instead of repeated model turns that only ask for status.

## Models and effort

Preserve the user's root model and effort. Use the runtime's available model list and task difficulty when choosing children. The [catalog](models.json) records the initial locally observed model preset. It is not a current entitlement, benchmark or cross-provider effort equivalence table.

Prefer direct work for small tasks. A lead need not spawn children to demonstrate orchestration. Keep requested model/effort separate from observed values and do not invent telemetry that the runtime does not emit.

## Resources and acceptance

Respect explicitly assigned concurrency, time, quota and spending constraints across the entire child tree. Unknown quota is unknown; do not turn cached tokens into subscription-limit percentages. The first version has no local budget manager, account rotation or automatic provider fallback.

Headless calls use native Claude metering. Checking a Max login does not prove extra usage is disabled, and natural-language instructions cannot control provider billing. If the user's actual mandate forbids potential charges, do not launch unless that restriction can be met. The CLI reports this limitation; it does not offer a subscription-only guarantee.

Accept results against the actual mandate: inspect changes and run relevant checks. Resolve material uncertainty with independent review where useful. Return evidence and ownership of unfinished descendants; do not silently abandon them.
