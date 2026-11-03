# Live provider credentials and model selections

## Delivered

- Dashboard provider saves persist the API key and selected models together before
  updating the running router. Model overrides and resets also apply immediately.
- Replace only the changed provider's configuration. Existing requests retain their
  provider object, while subsequent requests see the updated selection.
- Preserve existing credential cooldowns and usage counters. Initialize model/key
  buckets before publishing new configuration, and deduplicate repeated key saves.
- Clear response caches and include the routing configuration in cache namespaces,
  so an old in-flight response cannot become a cached answer for the new models.
- Refresh capabilities in the background. Results from an older provider object
  cannot overwrite a newer selection; live checks do not auto-select other models.
- Keep administrator-only writes, validate model IDs, report persistence failures,
  and roll back the saved model override if saving its accompanying key fails.
- Dashboard feedback now says changes apply immediately. Documentation and the
  pinned InstaBox Compose image reflect the new behavior.

## Verification

- Windows full suite: 155 passed, 13 platform-specific skips.
- Linux backend suite: 147 passed, 8 platform-specific skips.
- Browser integration includes a held-open streaming response while the model is
  changed: the original response finishes, the next identical request uses the new
  model, and server uptime does not reset.
- Standard and CLI Docker images, on AMD64 and emulated ARM64: health, dashboard,
  administrator/application access separation, live provider and model changes,
  live Settings, unchanged process identity, and persistence after container
  recreation passed. Provider inference uses a controlled local mock upstream.
- Documentation site build: 17 pages; existing missing custom 404-entry warning.
- Hosted InstaBox: browser administrator login, saving the existing model without
  a restart banner or uptime reset, session continuity, five pages without browser
  errors, application-key rejection for owner writes, and real inference passed.
  Re-saving its existing provider key also kept the process running and did not
  duplicate credentials. Production's model choice was preserved; actual model
  switching and fresh-key onboarding were exercised against isolated mock providers.

## Published images

Standard `providers-20261104` / `latest`:
`sha256:8fdccd4ddfc527bcb4d9805cff658bc04ed36c91315401aa38d38562b9b31375`.

CLI `providers-20261104-cli` / `cli`:
`sha256:a95bd39c762b0bc9412b4d2ae831853cc95c11880903995094884acc9bdc7e04`.

InstaBox uses the standard digest. Its Compose configuration and persistent
credentials were privately backed up before replacement. Existing credentials and
volumes were retained. This upgrade required one container replacement.

## Limits

Direct file edits and the file-based CLI still need a restart, as do key-rotation
mode changes. Live updates target the supported single-process threaded server;
independent worker processes are not synchronized. Retired model pool buckets stay
until restart so active requests can finish; very frequent model changes can grow
that small in-memory pool. Background capability checks can consume provider quota.
Every upstream provider and VM power-cycle recovery were not retested. No VS Code
extension package change is needed for this backend update.
