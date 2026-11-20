# Orchestrator

A Codex skill and local broker for handing scoped work to Claude Code and collecting durable, correlated replies.

Use it when you already work with both tools and want to stop copying assignments and results between sessions. The lead assigns ownership, the broker records delivery, and the lead verifies the result.

**Status: macOS beta candidate.** Local fixture tests cover transport, crash recovery and installation lifecycle. A second-machine install, physical sleep/reboot and independent-user workflow remain unverified. This is an independent project, not an OpenAI or Anthropic product.

## Status and provenance

- The code and documentation were written by coding agents (Claude Code and Codex) under the author's direction. The author set the requirements, the review gates and the release criteria, and decided what shipped; he did not write the code by hand.
- First public release. Before this it ran on one machine, for one user.
- macOS only. CI runs the same checks on hosted macOS runners with Python 3.11 and 3.14.
- Headless launch does not work out of the box today: it accepts only Claude Code 2.1.261, the one version it was tested against, and the default billing policy blocks every launch (see [Optional headless worker](#optional-headless-worker)). Peer messaging between live sessions is the part that was exercised.

## Documents

- [Failure modes of coding agents under a single operator](docs/agent-failure-catalogue.md): a catalogue of 15 failure classes from about ten weeks of session transcripts, with the countermeasure adopted for each. The rules this broker enforces come from it.
- [Release audit, 2026-09-05](docs/release-audit-2026-09-05.md): the pre-release audit that failed the build while all tests passed.
- [Beta implementation plan](docs/beta-implementation-plan.md) and [result](docs/beta-implementation-result.md): what was changed in response to the audit.

## What happens

```text
Codex task → scoped brief → local SQLite queue → exact Claude session
Codex task ← durable inbox ← correlated reply ← result and artifacts
```

The broker is a passive per-user process. It does not call models. A delivered message is not a completed task; the lead checks the returned artifacts. Uncertain sends are retained for reconciliation, never blindly replayed.

## Requirements

- macOS with a logged-in user launchd domain; Python 3.11 or newer.
- Codex for the lead and Claude Code for the peer. Initial local compatibility snapshot: Claude Code **2.1.261**, peer protocol **1**. Headless launch rejects untested CLI versions.
- Headless execution currently supports a first-party Claude Max login. Models in [the catalog](references/models.json) are explicit optional presets, not an account-entitlement guarantee.
- Local Unix sockets and process inspection (`ps`). An agent sandbox may require scoped permission to use them.

The Python runtime uses the standard library. Install with a durable Python executable; the launcher pins that interpreter path.

## Install

From an extracted release or clean checkout:

```sh
python3 scripts/install.py --dry-run
python3 scripts/install.py
~/.local/bin/orchestrator --help
```

This installs the skill at `~/.codex/skills/orchestrator`, the command at `~/.local/bin/orchestrator`, and the passive launchd service `local.codex-claude-orchestrator`. State and logs live in `~/.local/state/codex-orchestrator`. Use the full command path if `~/.local/bin` is not on your PATH.

Installation does not enroll sessions, send messages, launch models or create a heartbeat. Existing unmanaged installations are preserved; the installer refuses to overwrite them. A dry-run reads filesystem state and does not query or change launchd.

## First handoff

In an existing Codex task, invoke `$orchestrator` and describe the outcome and permitted work. The skill uses this sequence:

1. `orchestrator enroll-root` records the exact current task and checkout. Outside Codex, supply the exact `--thread UUID --cwd /absolute/path`.
2. `orchestrator discover` lists validated live Claude routes. Choose the intended session and `orchestrator enroll --session UUID`.
3. Write a brief with the outcome, owned files, constraints, acceptance evidence and reply route. Send it with `orchestrator send --to claude:UUID --body-file /absolute/brief.md`.
4. `orchestrator wait --timeout 50` reads replies without making model calls. After processing a reply, use `orchestrator ack MESSAGE_ID`.

UUIDs and paths above are parameters to replace with observed values. Start an existing peer with a context/availability exchange before assigning work. Read the [transport contract](references/transport.md) for correlation, native permission holds and recovery.

The inbox survives a broker restart. Continuing after the Codex turn ends requires an available native heartbeat tool and appropriate user authorization; this package does not create a schedule. Heartbeats are scheduled wakeups, not instantaneous push.

## Optional headless worker

Inspect a launch without sending a prompt:

```sh
orchestrator headless --cwd /absolute/worktree --brief /absolute/brief.md \
  --model claude-fable-5-1 --effort high --mode read-only --preflight
```

See [headless execution](references/transport.md#headless-execution) for launch controls and receipts. Read-only workers receive Read/Glob/Grep tools. Workspace-write additionally permits native Bash/Edit/Write/Agent tools; this is not an OS sandbox. Always assign an isolated checkout or clear ownership.

In this candidate, headless defaults to `--billing-policy subscription-only` and blocks because no-overage cannot be established. `--billing-policy native-metering` is available only for explicitly authorized native-metering launches. Inspect the preflight's `launch_allowed` and `block_reason`; preflight itself does not call a model.

V1 uses native Claude metering; it does not enable extra usage, change accounts or enforce a local spending budget. A Max login does not prove subscription-only billing. Respect any stricter constraint in the actual user mandate.

## Upgrade, recovery and uninstall

Use the new release's installer to upgrade a managed public installation:

```sh
python3 scripts/install.py --upgrade --dry-run
python3 scripts/install.py --upgrade
python3 scripts/install.py --uninstall --dry-run
python3 scripts/install.py --uninstall
```

Upgrade and uninstall verify ownership and preserve private state/logs. Modified or extra installed files stop replacement for manual reconciliation. A failed or uncertain service start retains owned code and a recovery status; inspect the named service and follow the error's recovery instruction. An interrupted uninstall keeps an ownership journal in state; rerun `--uninstall` from the original release source if the installed script is already gone. Do not delete state to make an error disappear.

Common issues:

| Symptom | Next step |
|---|---|
| Empty discovery inside a sandbox | Check permission for local sockets and `ps`; emptiness does not prove all sessions stopped. |
| Resume blocked as unknown | Preserve the session; use its existing interactive task. No usable messaging route is not proof of inactivity. |
| Untested CLI version | Validate the adapter/flags for that version before changing the compatibility policy. |
| Native delivery held/refused | Resolve the normal Claude-side permission decision; do not use another route to bypass it. |
| Outgoing state `unknown` | Reconcile the original message ID with receipts/artifacts before sending anything again. |
| Installation reports ownership mismatch | Compare the changed files and preserve them; do not force-remove the installation. |

## Verify and build

Development dependencies are pinned separately from the dependency-free runtime:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy
.venv/bin/python scripts/check_release.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/build_release.py --output-dir dist
```

Tests use temporary fixture homes, sockets, processes and databases, including a real broker SIGKILL/restart test. They do not call Claude models or operate an installed service. The installer tests simulate launchctl. The archive builder selects declared files and writes a deterministic tarball plus SHA-256 checksum; it excludes `.git`, local manifests, state and caches. Use a new output directory for each build.

For a transport-only demonstration, run the daemon test with `python3 -m unittest discover -s tests -p test_daemon.py -v`. This is a fixture demonstration, not evidence of a live multi-agent task.

Read [verification and limits](references/verification.md), [security boundaries](SECURITY.md) and [contribution guidance](CONTRIBUTING.md). Licensed under [MIT](LICENSE).
