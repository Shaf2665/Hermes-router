# Custom and local provider setup

## Delivered

- Added Custom provider and Local model (Ollama / LM Studio) to the setup list.
  Multiple named custom entries can be saved and selected again for editing.
- Custom/local entries have editable base URLs, optional API keys, model catalog
  lookup and selected/manual model IDs. Saves update routing immediately and
  persist atomically in the protected auth.json alongside existing credentials.
- Blank keys preserve an existing key only at the same address. A new address
  requires an explicit key or No API key. Keyless servers receive no Authorization
  header. Saved secrets are excluded from configuration responses.
- Provider snapshots restrict credential selection to their own keys, preventing
  new credentials from being paired with an older in-flight endpoint. Retired keys
  can finish existing requests but are excluded from new requests and status lists.
- Model discovery and probes do not auto-select other models for custom servers.
  Custom requests and catalog calls do not follow redirects. Administrator-only
  writes, URL validation, application provider scopes and save errors are covered.
- The provider guide now correctly says changes apply immediately. It explains
  that localhost refers to Hermes's machine/container, and local model servers
  must be started separately. Existing preset addresses remain fixed.

## Verification

- Windows full suite: 174 passed, 13 platform-specific skips.
- Browser integration exercised custom catalog lookup, save, key replacement,
  scoped inference with the replacement key, and keyless local inference against
  a controlled mock server. Existing streaming/model-change tests still passed.
- Standard AMD64 and CLI emulated ARM64 Docker smoke checks passed, including
  custom/local inference, live rotation, process continuity and recreation with
  persisted custom URLs, keys and models.
- Documentation site: 17 pages built; existing missing custom 404-entry warning.
- Hosted InstaBox: login, editable custom/local controls, desktop/mobile layouts
  at 915/390px, rotation change and restoration without uptime reset, and a real
  request through the existing provider passed. No browser JavaScript errors.
  Production provider configuration was preserved; onboarding tests used isolated
  mock providers rather than adding temporary entries to the user's router.

## Release

Standard `custom-20261104` / `latest`, AMD64 and ARM64:
`sha256:dd6094498edd21ea76fbe73f68d6ffdd5324440436885df570b33a7f6a0877e2`.

CLI `custom-20261104-cli` / `cli`, AMD64 and ARM64:
`sha256:bfd5499f4d1d433a5115e818fecbb42218520f45b6e71e00d37f1caf95d5c32c`.

InstaBox is pinned to the standard digest. Persistent configuration and Compose
were privately backed up before container replacement. This deployment also ships
the previously pending live rotation change; the original rotation mode was
restored after verification.

## Limits

Custom endpoints must support OpenAI-compatible chat. This form does not install
model engines, download models, configure private networking, or support arbitrary
authentication headers. Actual Ollama/LM Studio installations and every third-party
service were not exercised. Their protocol was tested using a mock HTTP server.
Retired credentials/model buckets stay in memory until restart for active requests.
Direct file/CLI edits still need restart; independent worker processes are not
synchronized. Background capability probes may consume provider quota.
