# Changelog

## 0.1.0-beta.1

Initial public candidate for macOS.

- Durable correlated Codex/Claude handoffs with explicit delivery and acknowledgment states.
- Exact-session resume checks reject active or unverifiable sessions.
- Correlated reply, original delivery state and acknowledgment commit atomically; invalid replies do not enter the inbox.
- Managed install/upgrade/uninstall preserve private state, reject unowned files and retain recovery evidence when service state is uncertain.
- A declared file set, reproducible source archive, development checks and macOS CI configuration.

Native Claude metering applies. The project does not manage spending budgets or switch billing routes. Physical sleep/reboot, a second-machine installation and independent-user acceptance remain release gates; no production-readiness or latency SLA is claimed.
