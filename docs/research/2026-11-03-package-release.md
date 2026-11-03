# Package release — 2026-11-03

The foundation branch incorporates upstream `main` through `f77b822`, including Codex tool-history/output-item handling and choosing one process owner during Linux setup. Foundation stream usage accounting, deadline handling and safe service errors were retained. Shell scripts now use LF consistently so Docker images built on Windows can execute the CLI.

## Packages

- Docker: `shafiq735/hermes-router:foundation-20261103` and `foundation-20261103-cli`; aliases `latest` and `cli`. Both include Linux AMD64 and ARM64.
- VS Code: version **0.9.6**, with administrator SecretStorage and separate application credentials. Install the VSIX attached to GitHub release `vscode-v0.9.6`.
- Marketplace upload was attempted twice and rejected for missing extension-update permissions on publisher `MohammedShafiq`. No credential is stored in the repository or release artifacts. Marketplace availability must not be claimed until publication is verified.

## Verification

- Windows suite: **115 passed, 13 skipped** (POSIX-only harnesses).
- Linux container suite, excluding browser tests: **113 passed, 8 skipped** (Windows PowerShell harnesses).
- Extension compile, TypeScript check, credential test and VSIX packaging passed.
- Each Docker variant on each architecture passed HTTP health, dashboard, administrator session, live application-key creation, administrator API denial for application keys, and durable usage after container removal/recreation with the same volume. The CLI variant additionally passed `hr status --json` and `hr features list`.
- Reproducer: `python tests/smoke_docker_images.py IMAGE linux/amd64` (or `linux/arm64`). It creates isolated disposable containers/volumes and never touches an existing installation. ARM64 runs were emulated on the Windows AMD64 host.

Live provider billing, actual systemd boot and Windows Server Task Scheduler startup were not tested. Per-key money limits remain estimates; full OmniRoute parity remains staged.

Prior image digests for rollback (restore matching backed-up data if required): standard `sha256:8e69db906b91cc1a9d0edb29441d7d5062bd8790fd78c9a2cad872150fc6371b`; CLI `sha256:5620cd186085de1ec94ac59f1cf4105f25a1eabf734f2d4df6a07d076004c7d8`.

Published image index digests: standard `sha256:15aa43ad837a822816e737dc38c62e8bc815f7934644e5ce51a57edadf8c5378`; CLI `sha256:fa1c1501e7d62da5f0ca8a61ce8bf9b5a245841ce6809cb13b01fdddf94cb7df`.
