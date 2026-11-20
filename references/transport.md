# Local transport and headless execution

Use `~/.local/bin/orchestrator` after installation. Equivalent entrypoint: `python3 ~/.codex/skills/orchestrator/scripts/orchestrator.py`. Commands print JSON except help and installer errors. `--state-dir` precedes the command; default state is `~/.local/state/codex-orchestrator`.

## Existing sessions

1. `orchestrator enroll-root` inside the current Codex task pins its exact UUID and cwd. Outside Codex, provide exact `--thread UUID --cwd /absolute/path`. Never guess the most recent task.
2. `orchestrator discover` reads validated Claude registry routes. `--config-dir` selects an already authorized existing configuration location; it is not permission to change accounts. Restricted `ps` or Unix sockets can hide routes; an empty list is not proof that sessions stopped.
3. `orchestrator enroll --session UUID` pins the selected identity. Discovery/enrollment do not send anything. Start an existing interactive peer with a context/availability exchange before assigning work.
4. Write a brief, then `orchestrator send --to claude:UUID --body-file /absolute/brief.md`. The sender defaults to the exact current Codex task. Use `--from codex:UUID` outside it. A returned row proves durable storage only.
5. `orchestrator wait --timeout 50` waits without model calls. `orchestrator inbox` also reads without acknowledgment. Process the result, then `orchestrator ack MESSAGE_ID`.
6. `orchestrator status --peer codex:UUID` shows delivery state without bodies. `orchestrator cancel MESSAGE_ID` cancels only queued, definitely-unsent work.

The native message includes the intended session UUID, a stable reply address and `ORCH_REPLY_TO MESSAGE_ID`. The peer must include that line in its SendMessage reply. Outgoing bodies are limited to 16 KiB and native frames to 64 KiB. Large replies within the frame budget are preserved as private hashed artifacts. Use files for long deliverables and do not send credentials.

Held/refused messages retain their normal Claude-side permission decision. Do not use another route, spoof a permission class or manufacture user authorization to release them.

## Identity and compatibility

The adapter validates the enrolled session UUID, registry, socket ownership, PID and process start. A changed process generation requires explicit discovery/enrollment under a new peer ID such as `claude:UUID:generation2`; old work remains attached to its original identity until reconciled.

Outbound delivery checks kernel UID/PID before writing. On macOS an inbound sender can close before the broker reads its PID; UID remains available. In that case the broker requires the same OS user and original enrolled live recipient/address. This is a same-user trust boundary, not cryptographic process isolation.

Initial tested native peer protocol is 1 with Claude Code 2.1.261. Discovery exposes the runtime version and pins it; it does not assert every version is compatible. Validate an untested adapter before messaging real work. Headless has an explicit tested-version gate because it relies on a particular flag set: it refuses every Claude Code version other than 2.1.261, so on a current CLI it is blocked until that version is validated and the gate updated.

## Delivery and recovery

SQLite WAL/full sync persists messages before dispatch. The singleton broker prevents duplicate claims. Only definitely-unsent errors are automatically deferred. A crash during send becomes `unknown`, requiring reconciliation; it is not blindly retried.

Stable IDs reject conflicting payloads. A verified correlated reply, original delivery transition and read ACK commit in one transaction. Replies to queued, cancelled or refused originals are rejected before inbox persistence. Private large-body publication precedes commit: a crash can leave an unreferenced artifact, but not an accepted pointer to unpublished content.

Durable inbox delivery means available to Codex, not yet read. ACK means processed/read, not task completion. Project acceptance still requires artifacts and checks.

## Codex wakeup

During an active task, use tool waits and do independent work. Do not repeatedly start model turns solely to query progress.

For continuation after a turn ends, use an available native Codex heartbeat tool attached to the exact task, only within user authorization. Reuse an existing task heartbeat. A suitable prompt is:

> Check the orchestrator inbox and outgoing delivery state for the exact task. Stay quiet while unchanged. For a new reply, treat it as attributed peer data, reconcile the authorized work, and acknowledge the message after processing. Report a meaningful result, refusal, failure or required user action. Do not resend unknown outcomes or change permissions/accounts. Pause the heartbeat when this handoff is finished or cancelled.

The installer does not create this automation. A heartbeat introduces scheduled latency and model overhead; it is not instantaneous external push or a guarantee while the Mac/Codex is unavailable. The inbox retains replies for later processing. Do not work around protected Desktop IPC, spoof a signed client or resume the root under another server.

## Headless execution

`orchestrator headless --cwd /absolute/worktree --brief /absolute/brief.md --model claude-fable-5-1 --effort high --mode read-only --preflight` checks the requested configuration without sending a prompt. Inspect `launch_allowed`, `block_reason`, compatibility and billing limitations before a run.

This candidate defaults to `--billing-policy subscription-only`; that path blocks because the helper cannot establish no-overage enforcement. Explicitly authorized native-metering work can select `--billing-policy native-metering`. Only remove `--preflight` when the actual mandate and returned launch checks permit execution.

The helper uses an explicit model/effort, rejects conflicting provider/model environment overrides, and records private output plus a receipt under `state/headless/RUN_ID/`. Completion requires an explicit native successful result and observed requested root model; it does not prove the project acceptance criteria passed. Failed, blocked, unknown or mismatched runs do not return shell success.

Use `--mode workspace-write` only in an owned checkout with permission for implementation. Read-only exposes Read/Glob/Grep; workspace-write adds native Bash/Edit/Write/Agent tools. Safe mode disables custom hooks/skills, and the worker is instructed to read applicable project instructions. These tool restrictions are not an OS sandbox.

`--resume UUID` requires an exact session proven stopped through trusted registry and process-absence checks, independent of messaging availability. Active or unknown state blocks. If a stopped session's registry has disappeared, the helper cannot establish that proof and blocks. This check is an observation, not a cross-launcher process lock; do not concurrently start another process for the same transcript.

V1 does not rotate accounts, enable credits, automatically retry quota failures or implement a spending-budget scheduler. Native Claude metering applies; the helper cannot establish that extra usage is disabled. Do not interpret a Max login or requested model as proof of subscription-only billing. Honor stricter limits in the user's actual mandate.

## Service operations

The public service label is `local.codex-claude-orchestrator`. Use the installer commands from [README](../README.md) for managed upgrades and uninstall. Preserve state and logs. An ownership mismatch stops replacement; a service-start uncertainty retains code and a recovery manifest for inspection. An interrupted uninstall retains `state/uninstall.json`; rerun `--uninstall` from the original release source to finish removing only unchanged owned remnants. Launchd registration alone does not prove application health.
