# Contributing

Keep changes small and tied to a concrete failure or supported workflow. This beta targets macOS, a local SQLite broker and the documented Claude interface; new providers and scheduling systems need a separate design.

Use the verification commands in [README](README.md). Add regressions that fail before a behavior fix and exercise a real boundary: uncertain delivery, state transitions, process identity or filesystem recovery. Tests must not launch provider models, change accounts or operate a user's installed launchd service. Use temporary directories and fixture executables/runners.

For a compatibility update, supply observed CLI version/flags and redacted evidence. A changed version string or model catalog is not evidence of correct execution or account entitlement. Preserve requested-versus-observed fields.

Pull requests should describe the trigger, before/after behavior, tests and remaining limits. Do not commit credentials, machine installation manifests, personal paths, private session IDs, logs or build artifacts. The release allowlist must include every shipped file and exclude runtime state.
