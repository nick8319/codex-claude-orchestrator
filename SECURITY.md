# Security boundaries

This beta runs as the current OS user. It is intended for trusted local development environments.

- Native permission holds/refusals remain in force. Peer content cannot grant fresh user authority.
- State is private to the user. Outbound delivery checks the enrolled session, endpoint, kernel UID/PID and process start. On macOS, a disconnected inbound sender can lose kernel PID visibility; same-user credentials plus the exact enrolled live route are then the boundary. This is not cryptographic isolation between processes of the same user.
- Unknown delivery is not automatically retried. Stable message IDs prevent conflicting duplicate payloads; they cannot guarantee exactly-once external effects.
- Headless tool restrictions are not an OS sandbox. Do not treat workspace-write as filesystem confinement against arbitrary subprocesses.
- The project never switches accounts, reads/copies credentials for migration or enables extra usage. Native provider billing applies; subscription-only spending is not enforced locally.
- Inspect briefs and returned artifacts before accepting work. Do not put tokens, credentials or unrelated confidential information in messages or public issue reports.

The release checker searches common secret literals and personal absolute paths in declared source files. It is a guard against accidental inclusion, not a comprehensive secret scanner or git-history audit.

Report non-sensitive bugs with the version, reproduction and redacted diagnostics. For sensitive findings, use private vulnerability reporting if it is enabled on the published repository. If unavailable, request a private contact without posting exploit details or credentials publicly.
