# Live key rotation mode

Deployment follow-up: the [custom-provider release](2026-11-04-custom-providers.md)
subsequently deployed this change to InstaBox and verified live rotation there.

Changing rotation mode in Providers now saves the setting and updates the running
credential pool under its existing lock. The next key selection uses the new mode.
Pool position, cooldowns, usage counters and active requests are preserved. Settings
survive a later restart. Dashboard feedback says "Applied immediately" and does not
show a restart banner. Direct configuration-file and CLI edits still require restart.

The endpoint retains administrator-only access and existing mode aliases. Invalid
types/modes return 400; failed writes return a safe 500 without changing runtime
mode. The dashboard retains compatibility with older servers that request restart.

## Verification

- Reproduced the old restart requirement and invalid numeric input returning 500.
- 26 targeted backend and browser tests passed. These cover rotation behavior in
  both directions, preserved cooldowns/counters/pool identity, persisted mode,
  invalid input, failed saves, application-key denial and live dashboard feedback.
- Docker standard AMD64 and CLI emulated ARM64 smoke checks passed, including
  mode changes without a process restart and mode persistence after recreation.
  Existing provider/model/settings and authentication smoke checks also passed.
- Documentation build: 17 pages; the existing custom 404-entry warning remains.
- The full unrelated test suite and every upstream provider were not rerun.

## Published release and deployment status

Standard `rotation-20261104` / `latest` (AMD64 and ARM64):
`sha256:5b0f6f37648c5443f1a52c5439d08e73890e5a4c1a65e6c559a01b4e1c34c063`.

CLI `rotation-20261104-cli` / `cli` (AMD64 and ARM64):
`sha256:b9ebe9fe1d5f80ba172c6641ccb2d98872678ed6eeb36a955b714c0b80fa1572`.

Repository Compose is pinned to the new standard digest. Hosted deployment remains
pending: InstaBox rejected SSH access because the machine is cooling down until
2026-11-04 02:59:50 UTC (08:29:50 IST). No host files or running containers were
changed in this release attempt. Resume with a private configuration backup, copy
the updated Compose file, pull/recreate Hermes, restart the gateway, and verify
rotation in both directions while restoring the user's original mode.

Live configuration targets the supported single-process threaded server;
independent worker processes are not synchronized. Installing the release requires
one container replacement; subsequent dashboard rotation changes do not.
