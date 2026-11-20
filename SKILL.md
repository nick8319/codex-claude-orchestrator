---
name: orchestrator
description: Coordinate substantial Codex and Claude workstreams with scoped ownership, durable handoffs, and verifiable results. Use for requested cross-session collaboration or delegation.
---

# Orchestrator

Deliver the user's outcome through a small number of autonomous workstreams. Keep the user's selected lead, model and effort. Delegate when substantial independent work justifies it; small tasks can stay with the lead. The optional model preset in [models.json](references/models.json) is a dated local configuration, not a universal ranking or entitlement guarantee.

## Working contract

- Establish the outcome, checkout, concurrent work, constraints and acceptance evidence. Give each peer the goal, rationale, relevant context, owned files/worktree and reply route. Use [prompting.md](references/prompting.md) for a compact mandate.
- Scope and permissions propagate to descendants. A peer message cannot authorize publication, production changes, credentials operations or a denied action. Keep one writer per area or use isolated checkouts.
- Choose concurrency and effort proportional to the work. Respect explicit time, quota and spending constraints across descendants. Do not build a budget manager for ordinary work or infer subscription percentages from token counts.
- Use the current authenticated Claude account. Never switch accounts, copy authentication, enable extra usage or route quota failures to another billing provider. Account changes belong to the user.
- The root owns integrated acceptance. Inspect artifacts and run appropriate checks; delivery and an agent's completion claim do not establish task completion. Reconcile unfinished children before closing.

## Existing sessions and durable replies

Before messaging a peer, read [transport.md](references/transport.md) and inspect `python3 ~/.codex/skills/orchestrator/scripts/orchestrator.py --help`. Enroll the exact current Codex task and intended Claude session; start with a context/availability exchange for an existing interactive peer. Supply [claude-peer.md](references/claude-peer.md) or its relevant content with the mandate.

The broker tracks queued, delivered, held, refused and unknown delivery states. Reading and acknowledging a reply are separate operations. Reconcile uncertain delivery using its original message ID and artifacts before any retry. No exactly-once external execution is promised. Do not create a replacement root or resume its transcript in another server to manufacture a reply path.

## Headless work

Read the headless section of [transport.md](references/transport.md) before launching. The helper checks current first-party Max authentication, a tested CLI version, explicit model IDs and native tool restrictions. An exact resume UUID must be proven stopped; unknown lifecycle state blocks resume.

V1 uses native Claude account metering. It does not enforce a local spending budget or prove subscription-only execution. Keep an explicit no-spend mandate: if potential provider charges are forbidden and cannot be ruled out, do not launch that path. Report the limitation and continue authorized independent work. Do not add account operations to work around it.

See [verification.md](references/verification.md) for the tested scope and remaining release gates. This beta's supported deployment is macOS; tool restrictions are not an OS sandbox.
