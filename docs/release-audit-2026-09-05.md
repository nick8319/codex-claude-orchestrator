# Orchestrator: public release readiness audit

> Provenance: written by coding agents during the audit under the author's direction; translated from Russian for publication.
>
> Note (2026-11-19): the budget-simplification patch described in [beta-implementation-result.md](beta-implementation-result.md) was never applied and has been dropped, so the code keeps the `--billing-policy` behaviour; the installer's check for a legacy per-user service label was removed before publication.

Date: 2026-09-05. **VERDICT: FAIL — the current package is not ready to be released with a production-ready claim.**

There is a basis for a limited macOS beta release. The full existing test suite passed **71/71**, but additional scenarios revealed defects outside that suite. R1–R5 below must be closed before a beta; a production-ready claim additionally requires portability and operational evidence from the release gates.

## Scope and method

- Target: the installed package at `~/.codex/skills/orchestrator` (the pre-fix installed copy), not the author's working repository and not the broker's runtime state.
- A SHA-256 manifest of the package files was recorded. At the end of the audit the contents matched; `__pycache__` was excluded. Snapshot identifier: `e73b8730241010ffbb23e9923335ac54a0d24d72406f841ef3255b3b22b2ddcf`.
- Independent read-only reviews: broker/store/transport; headless/CLI/policies. The lead auditor checked the installer and the package, reproduced the key scenarios and reconciled the findings.
- Environment: macOS 15.7.9 arm64, Python 3.14.6. Test processes, sockets and databases were confined to temporary directories. No new Claude calls, no changes to accounts, working sessions, settings or the installed service.
- This is an audit and a release plan. No source fixes, GitHub repository creation or post publication were performed.
- The historical log `references/verification.md` was read, but its live provider smoke tests and heartbeat were not repeated today.

## Verified findings

All file and line references below point to the pre-fix installed copy, not to the current files in this repository.

### R1 · P1 · The active-session check allows resume when the state is undetermined

**Evidence (pre-fix installed copy):** `scripts/headless.py:129`, `scripts/claude_transport.py:199`, the re-check before launch at `scripts/headless.py:210`.

`_refuse_live_resume()` treats the absence of the UUID from the results of `discover()` as sufficient to launch. But `discover()` suppresses validation errors for individual entries. A denied `ps`, an unreachable messaging socket or unsupported route metadata removes the session from the list, although none of these proves that it has stopped. The re-check uses the same logic.

**Reproduction:** a temporary record for the requested UUID + an injected `TransportError` during route validation. The test executable was actually launched with the exact `--resume UUID`. The final `session_mismatch` in the fixture occurs only after the launch and does not prevent it. No real parallel Claude launch was performed; what is proven is a defect in the local guard condition.

**Consequence:** a second process may continue the transcript of a still-active session, contrary to the ownership contract.

**Closing condition:** a dedicated check of the specific session with states active / stopped / unknown; unknown forbids resume. The existence of a usable messaging route must not substitute for the lifecycle check. Regressions: unavailable `ps`, missing socket with a live PID, incompatible metadata, stopped session.

### R2 · P2 · Installation does not recover after a failure and conflicts with retained state

**Evidence (pre-fix installed copy):** existing-path check `scripts/install.py:31`, file creation `scripts/install.py:49`, exclusive creation of logs `scripts/install.py:58`, late `launchctl bootstrap` `scripts/install.py:72`.

Two cases reproduced in a substitute home directory:

1. A `launchctl bootstrap` failure leaves the skill, the launcher and the plist. A second run stops at `Existing path preserved`.
2. Installing with retained state that contains the previous `daemon.log` ends in `FileExistsError`, having already created the skill and the launcher but not yet the plist. This conflicts with the recommendation to keep state on uninstall.

`launchctl` was substituted in the reproduction; the real service was not started or stopped.

**Closing condition:** check for conflicts up front; ensure a safe completion or rollback of a partial installation; reuse verified private logs without data loss. Provide a verified upgrade and uninstall path that preserves state. Regressions for both scenarios are mandatory. Automatically overwriting someone else's installation is not required.

### R3 · P2 · A rejected reply is still persisted and delivered to the inbox

**Evidence (pre-fix installed copy):** persistence before the state check `scripts/service.py:85`, the subsequent ACK `scripts/service.py:89`, permitted ACK states `scripts/store.py:302`.

**Reproduction:** cancelled original message → correlated reply arriving on the inbound path of an already verified peer → the handler raises `Cannot acknowledge an undelivered message`, but one record remains in the inbox. The next dispatch moves this reply to `delivered`. The original message remains `failed`. The same branch applies to `queued`/`refused`.

This is a consistency violation in handling a rejected frame, **not a proven authorization bypass**. In the normal scenario a message cancelled before sending should not receive a reply; what matters is that the handler for an invalid/late frame reports the rejection after an irreversible change to the inbox.

**Closing condition:** define reply semantics for each original state and check them before writing; perform the related inbox/status/ACK changes atomically, or explicitly support a documented recovery. Regressions: queued, cancelled, refused, duplicate, permitted late reply.

### R4 · P2 · The spending contract contradicts headless behaviour

**Evidence (pre-fix installed copy):** prohibition on launching without an established subscription-only mode `references/prompting.md:41`; `subscription_only_enforced=False` and an unknown extra-usage setting `scripts/headless.py:122`; the subsequent launch `scripts/headless.py:193`; a test explicitly expects that launch `tests/test_headless.py:47`.

The CLI honestly reports that there is no spending guarantee. However, the instructions require not launching precisely under this uncertainty, and the standard headless path launches. The current extra-usage settings and actual charges were not checked; the finding does not claim that the tool enables credits or that any charges occurred.

**Closing condition:** a single contract. For a task with a mandatory subscription-only restriction, the unknown must stop the path. If the user explicitly permits native metering, that is a separate, explicit launch policy. A Max login must not be presented as verification that there are no additional charges. The work must not be extended to manipulating credentials or account settings.

### R5 · Release blocker · The installed directory is not yet a public distribution

**Evidence (pre-fix installed copy):** personal absolute paths `INSTALLATION.json:2`, working sessions/identifiers and context in `references/verification.md:9`, a personal two-account policy `SKILL.md:17`. The inventory found `.DS_Store`, `INSTALLATION.json` and `__pycache__`; README, LICENSE, CI, CHANGELOG and Python check configuration are missing.

The installer copies the source directory as a whole, excluding only `__pycache__`/`*.pyc` (`scripts/install.py:49`). When installing from a git clone this also includes `.git` and other non-release files. This is a property of copytree and its ignore list; no leak of git secrets was found or is claimed.

A limited search for common provider-token literals and private-key headers produced no matches. This is not a full secret scan and not a check of the future repository's history.

**Closing condition:** build the package from an allowlist; separate the machine manifest from the sources; anonymize examples and the log; turn personal model preferences into a configurable preset while keeping the safety restrictions. Add a README, a chosen license, CI, a version, troubleshooting and a description of the security/permission boundary. Check the final release archive and git history before publication. For a beta it is acceptable to explicitly limit support to the current Max/macOS scenario instead of adding all providers and operating systems.

## Release gates and the limits of claims

| Gate | Current result | Required for release |
|---|---|---|
| Existing test suite | PASS: 71/71, 12.331 s | Add R1–R4 regressions and installer tests |
| Python / JSON / YAML | AST parse, JSON parse, YAML/frontmatter parse PASS | Designate a supported Python and reproducible checks in CI |
| Lint / typecheck | UNVERIFIED: no configuration; `ruff` and `mypy` not found | Set up appropriate checks; do not count AST parsing as lint/typecheck |
| Installation on another machine | UNVERIFIED | Clean user/another machine: install → smoke → restart → upgrade → uninstall → reinstall with the previous state |
| CLI compatibility | Dated local snapshot | Publish a tested matrix; for an unknown version, return an explicit status/refusal for sensitive operations |
| Real working scenario | The log contains a live smoke test; not repeated today | An external user completes a limited real task following the README; human interventions and the verified result are recorded |
| Mac sleep / reboot | UNVERIFIED according to the log | Verify before production-ready claims of automatic recovery |
| Quota / billing | Fixtures partially check auth; real transitions not checked | A single R4 policy, verified refusal/limit scenarios without switching accounts and without automatic retries |
| Public package | FAIL: R5 | Clean archive, license, instructions, CI, version |

The adapter checks that the version equals the one of the registered session, but initial registration accepts any non-empty version string (`scripts/claude_transport.py:122`, pre-fix installed copy). Headless checks a lower bound on the version (`scripts/headless.py:99`, pre-fix installed copy). This is a gap in the compatibility policy, not evidence that newer versions are broken. Current model availability and the guarantees of third-party CLIs are not established by this audit.

The following properties are already described honestly and do not by themselves require extending the product:

- The broker does not guarantee exactly-once external effects and does not automatically retry an uncertain send.
- Inbox delivery, reading a message and completing a task are different events.
- The inbound trust boundary is a single OS user; there is no cryptographic isolation between its processes.
- Headless tool restrictions are not an OS sandbox; descendant limits in a brief do not become a mechanical global scheduler budget.
- The post-turn heartbeat gives scheduled latency and depends on the Mac/Codex being available; instantaneous external push is not promised.
- Linux/Windows are not needed for an honest macOS beta release.

## What was verified positively

Transactional claims, SQLite WAL/full sync, immutable peer identity, protection against conflicting duplicate IDs, private-file checks, outbound UID/PID/process-start, frame limits, reply correlation, recovery after SIGKILL in a separate test process. Headless passes argv/stdin without a shell, drops conflicting provider variables, does not store the email from auth status, separates root-model observations from child events and does not invent observed effort. Result errors give a non-zero CLI exit.

## Final order of work

1. Fix R1 and define the R4 contract; add regressions without live paid calls.
2. Fix R2/R3; run failure/recovery scenarios and the full test suite.
3. Prepare a separate public package for R5 and CI for the declared macOS configuration.
4. Verify installation and a real task with several external users. This is a test of the portability hypothesis, not a promise of statistical representativeness.
5. Release a limited beta with a short demo and precise known limitations. A production-ready claim comes after the operational gates above.

Rewriting the broker or adding a universal multi-provider platform is not required. The work is limited to the specific defects, packaging and portability evidence. No effort estimate is established until the spending contract and the supported matrix are chosen.

## Fresh verification artifacts

These were kept with the audit working files and are not included in this repository:

- Manifest snapshot (`source-manifest.json`)
- Full successful test run (`tests.log`)
- Results of the additional reproductions (`reproductions.json`)

All audit phases are complete: snapshot → independent reading → tests/reproductions → reconciled verdict → re-check that the sources were unchanged. The fixes remain the next stage.
