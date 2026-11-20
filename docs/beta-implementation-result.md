# Orchestrator beta: implementation result

> Provenance: written by coding agents during the audit under the author's direction; translated from Russian for publication.
>
> Note (2026-11-19): the budget-simplification patch described below was never applied and has been dropped, so the code keeps the `--billing-policy` behaviour; the installer's check for a legacy per-user service label was removed before publication.

2026-09-05. **Technical candidate built; public publication not performed.**

Sources: [README.md](../README.md).

## Done

- R1: an active or undetermined session does not allow resume; the absence of a messaging route is not treated as proof that the session has stopped. Re-check immediately before launch.
- R2: the full package is checked before the service is stopped; managed install/upgrade/uninstall preserve state/logs and verify ownership. Failures while publishing files or starting the service leave a recoverable state. An interrupted uninstall resumes from a private journal without deleting files the user has modified.
- R3: the reply, the status of the original delivery and the ACK are written atomically. Rejected replies do not reach the inbox; conflicting long replies do not create an artifact.
- R5: anonymized skill and documentation, MIT, version, macOS CI, pinned dev tools, file checks and a reproducible archive. The globally installed version was left unchanged.
- Independent runtime review of R1/R3: PASS. Scoped re-review of two additional R2 findings: PASS.

## Fresh checks

| Check | Result |
|---|---|
| Python 3.14.6, full suite | 115/115 PASS, 15.741 s |
| Python 3.11.8, full suite | 115/115 PASS, 16.364 s |
| Ruff lint / format | PASS, 27 Python files formatted |
| Mypy, including bodies of untyped functions | PASS, 9 source files |
| JSON / YAML / frontmatter / release links | PASS |
| Skill validator | PASS |
| Archive reproducibility / non-overwrite / exclusions | PASS, regression tests |
| Archive checked against the source manifest | PASS, 35 files |

All service/provider interactions in the tests are fixtures. Real temporary Unix sockets and a real broker process are used in the transport/crash tests. The working launchd service, accounts and live models were not touched. CI is configured, but no hosted run has taken place yet.

## The only unfinished implementation item: spending simplification

The user asked not to complicate the budget for v1. A concrete patch was prepared (not included in this repository): remove the new `billing_policy`/`--billing-policy` and the default subscription-only block, keep ordinary Claude metering, the restrictions on account/credential operations and the honest receipt fields. The patch also removes the descriptions of the temporary flags from README/transport.

**The patch was NOT applied.** An automated permission check rejected the removal of the subscription-only restriction, judging the request to simplify to be insufficiently explicit permission to lift a financial block. Exact confirmation was requested; until it is given, the code keeps the strict default and the explicit native-metering alternative. The documentation and the archive reflect the behaviour that actually remains. The prepared diff was syntax-checked, but its runtime checks run only after an authorized application.

After confirmation: apply the diff, run the affected tests/quality checks, rebuild the candidate in a new output directory. This is not permission for a paid model call or an account change.

## Artifacts

These were kept with the working files and are not included in this repository:

- Candidate with the current restrictions (`orchestrator-0.1.0-beta.1.tar.gz`)
- SHA-256 (`orchestrator-0.1.0-beta.1.tar.gz.sha256`)
- Manifest (`source-manifest.json`)
- Python 3.14 log (`tests-python314.log`)
- Python 3.11 log (`tests-python311.log`)

## External release gates

A clean installation on another machine, a real task by an independent user, sleep/reboot and live quota transitions have not yet been verified. These results cannot be replaced by fixture tests. There is no production-ready claim, no GitHub publication, no tweet and no update of the global skill. The next public release remains a limited macOS beta after the remaining decision and the corresponding checks are completed.
