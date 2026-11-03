# Live settings and dashboard refinements

## Delivered

- Reused the official cyan circuit logo from the VS Code package in the sidebar
  and sign-in screen. The compact existing PNG keeps the dashboard self-contained.
- Repaired the Connect application-name/button row with consistent spacing and
  a stacked phone layout. The generated-key controls wrap independently.
- Used Google Stitch project `17207046280167494997`, screen
  `20ec30688d5a416b9ea9dca2ae8cc61a`, as the Settings design reference.
  Adapted its grouped rows, switches and feedback to the existing HTML, theme
  tokens and actual functionality; omitted generated placeholder statistics,
  version labels and unsupported security claims.
- Settings switches save and apply live. Cache/log state changes use their
  existing locks; configuration writes are serialized. Invalid boolean values,
  failed configuration writes and unavailable cache storage return errors.
- Model discovery refreshes in a background thread when enabled. Disabling it
  does not undo models already discovered; provider-specific overrides remain.
- Request-history disable clears the in-memory history. Disk-cache disable closes
  the mirror but does not delete stored responses. Application key access remains
  isolated from administrator controls.

## Verification

- Initial full Windows suite: 145 passed, 13 platform-specific skips.
- Final targeted Windows checks: 17 live-settings tests and 12 browser tests passed.
- Linux backend suite: 141 passed, 8 platform-specific skips.
- Standard and CLI Docker images tested across AMD64 and emulated ARM64:
  health, authentication, application access, live settings, preserved settings
  after recreation and durable usage passed. Final compact-logo images were
  checked on standard ARM64 and CLI AMD64; runtime code is identical across builds.
- Browser layout checks at 1012, 918 and 390px: no horizontal overflow, accessible
  switches, successful saves, failed-save state preservation and separated Connect
  controls. The existing browser suite covers provider setup and application keys.
- Hosted InstaBox: administrator login, official logo, all five pages, switch
  on/off and restoration, session continuity, non-resetting uptime and a live AI
  request passed. No browser JavaScript errors.
- Documentation site built 17 pages; its existing custom 404-entry warning remains.

## Deployment

Published standard `settings-20261104` / `latest`:
`sha256:f2b8c9bf2a8684a69feb326f8505826fb2b4215f52d1e223cafa6ab3b4da376f`.

Published CLI `settings-20261104-cli` / `cli`:
`sha256:ae17349418a7f8a6c66ebdd4ba592d5b80021f0d9220745b8c9d18f18270e43a`.

The InstaBox deployment is pinned to the standard digest. Configuration and Compose
were privately backed up before replacement; persistent data and credentials were
retained. The update required one container replacement. Subsequent Settings
switches do not restart the process.

Provider credentials, configured models and rotation still require a restart.
Direct environment-file edits and the file-based feature CLI do too. Live changes
target the supported single-process threaded server; independent worker processes
are not synchronized. Every upstream provider and VM power-cycle recovery were
not retested. The VS Code extension did not need another package update.
