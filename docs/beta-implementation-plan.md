# Orchestrator beta implementation plan

> Provenance: written by coding agents during the audit under the author's direction; this document was written in English and is published with paths sanitized.
>
> Note (2026-11-19): the R4 simplification patch below was never applied and has been dropped, so the code keeps the `--billing-policy` behaviour; the installer's check for a legacy per-user service label was removed before publication.

Approved scope: release audit R1–R5, 2026-09-05. Source: [release-audit-2026-09-05.md](release-audit-2026-09-05.md).

Goal: a tested, sanitized macOS beta candidate in `orchestrator/`, preserving the installed global skill and broker.

Architecture: retain the SQLite broker, native peer transport and foreground headless launcher. Fail closed on uncertain resume identity. Install a declared file set with ownership-aware recovery; retain private state across uninstall. Public documentation states tested versions and limits.

Constraints: Python standard library at runtime, macOS support only, no credentials/account/settings changes, no live model launches, no existing-service operations, no commits or external publication in this implementation stage. Tests first for behavioral defects. MIT license for the new public candidate. Model IDs remain explicit; local preferences are an optional documented preset.

- [x] R1: targeted resume lifecycle check, unknown blocks; regressions verified.
- [ ] R4 simplification: review-only patch prepared; explicit approval pending after automatic rejection of guard removal. Current guarded behavior remains documented and tested.
- [x] R3: validate and atomically persist correlated reply/status/ACK; rejected replies leave no inbox record. Owner: broker worker; files store.py, service.py and corresponding tests.
- [x] R2: release allowlist, install recovery, upgrade/uninstall preserving state, fixture lifecycle tests. Owner: installer worker; files install.py, packaging.py, test_install.py and test_packaging.py. No real launchctl operations.
- [x] R5: public skill/docs, MIT license, version, compatibility policy, build/check commands, pinned CI tooling and release archive. Owner: root.
- [x] Integrated verification: regression/full suite, lint/typecheck, package/source privacy scan, install lifecycle in temporary fixture homes, independent code review and fixes, global source manifest unchanged. Owner: root plus independent reviewers after implementation.
- [x] Deliver candidate, change report, reproducible commands and explicit unverified external/machine gates. Owner: root.

Dependency ordering: workers operate on disjoint owned files after source copy; root docs integrate actual CLI contracts. Full suite and artifact creation follow worker completion; review follows integration; any substantive review fixes receive targeted checks before final full verification.

User steering: do not overcomplicate spending budgets for v1. Ruling: no billing-policy API or local budget manager. Resolve R4 through a consistent native-metering contract; never enable extra usage or change accounts. A user mandate that forbids potential spending still constrains the agent; the CLI does not claim to enforce provider billing.
