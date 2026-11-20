# Verification scope

The public beta candidate is prepared from a locally used Codex/Claude workflow. Its original private smoke-test session IDs, nonces, paths and account details are intentionally not distributed.

## Automated evidence

The suite uses temporary fixture processes, private state and fake launchctl runners. It covers SQLite claims and recovery, native socket identity/framing, correlated replies, headless output/auth fixtures, resume lifecycle uncertainty, installer ownership/failure recovery and deterministic distribution packaging. A separate-process broker test verifies duplicate frames and reply retention after SIGKILL/restart. It does not operate an installed broker or invoke a provider model.

Ruff lint/format, mypy with untyped function body checks, Python/JSON/YAML parsing and the declared-file release checker accompany the suite. The CI configuration targets macOS with Python 3.11 and 3.14; configured CI is not evidence that a hosted run has occurred.

## Initial compatibility

- Local verification host: macOS 15.7.9 arm64, Python 3.14.6.
- Initial CLI snapshot: Claude Code 2.1.261, native peer protocol 1; Codex CLI/catalog observation 0.153.2.
- Model identifiers are optional initial presets, not a guarantee of availability, entitlement or subscription metering for a future call.
- Effective effort is unknown when the runtime does not emit it; requested effort is not substituted as evidence.

## Remaining acceptance gates

A clean installation on another machine, an independent user's real task, physical sleep/reboot recovery, hosted CI results and real provider quota transitions are not established by the fixture suite. Keep the beta label until the relevant evidence exists.

Native Claude metering may include extra usage according to the user's account. This project does not configure billing or enforce subscription-only spending. Exactly-once external execution, instantaneous Codex wakeup and an OS sandbox are not promised.
