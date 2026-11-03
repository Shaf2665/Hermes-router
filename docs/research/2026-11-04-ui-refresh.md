# Dashboard refresh and Docker restart — 2026-11-04

The dashboard now uses a navy navigation rail, restrained violet accents, clearer
typography, and consistent form and panel spacing. Provider setup is divided into
provider, credential, and model sections with guidance beside the form. Key rotation
remains available in a collapsed section. Home, Connect, Activity, Settings and sign-in
share the same styling. Light/dark/system appearance is retained; a chosen appearance
now survives a reload. Credentials are still excluded from browser storage.

The visual system uses #121a2d navigation, #f7f8fc canvas, #ffffff surfaces,
#4f46e5 actions, #202738 text and #616b80 secondary text, with separate dark tokens.
The existing vanilla HTML/JavaScript and Flask endpoints are retained; no frontend
framework, remote font or runtime dependency was added. Responsive checks cover
360, 768 and 1440 pixels. Layout properties no longer animate during viewport changes.

## Stitch status

The requested Google Stitch MCP connection authenticated using an existing local
configuration. A private design project and generation request were created.
Stitch returned a session identifier, but repeated screen-list checks returned no
completed screen. This release is a locally implemented design, not generated Stitch
output. No router credentials or private provider configuration were sent to Stitch.

## Restart fix

The previous restart handler deliberately refused container runtimes, causing the
dashboard to show an unsupported-restart error after saving a provider. The supplied
Docker Compose deployments now opt in with `HR_CONTAINER_RESTART=1` and retain
`restart: unless-stopped`. The authenticated handler only schedules a process exit
when explicitly enabled, running as PID 1, and inside Docker. Docker then starts a
fresh process which loads the persisted credentials and model choices. The response
is sent before the delayed exit. No Docker socket mount or host process control is
needed. Other runtimes retain the previous guarded behavior.

Restarts interrupt in-flight requests and end administrator browser sessions; sign in
again afterward. For a custom container launcher, the opt-in flag must be paired with
an appropriate restart policy. Do not enable it on a container without that policy.

## Verification

- Windows: 130 passed, 13 platform-specific skips, including all 11 browser tests.
- Linux backend suite: 124 passed, 8 platform-specific skips. A read-only test-cache
  warning occurred because the source directory was mounted read-only.
- Both standard and CLI images on AMD64 and emulated ARM64: health, dashboard,
  sessions, application-key creation/access control and persistent usage passed
  before and after container recreation.
- Isolated Docker restart smoke: unauthorized and application-key restart requests
  denied; administrator restart accepted; Docker restarted the container; saved
  provider/model configuration loaded; login remained functional. This test disables
  external networking and uses no real provider credentials.
- Hosted InstaBox browser: sign-in, all five pages, actual Restart action, session
  invalidation and subsequent sign-in passed without JavaScript errors.
- One live request through the hosted application key returned HTTP 200 and `OK.`
  from the configured `claude-sonnet-5-5` model after restart.
- Documentation site: 17 pages built. The existing missing custom 404-entry warning
  remains unrelated to this change.

Live calls to every catalog provider, VM power-cycle recovery, and native Windows
service restart were not tested. Other providers require their own valid credentials.

## Images and deployment

- `shafiq735/hermes-router:ui-20261104` (`latest`):
  `sha256:a0e97b74a9aea3252034ef8c8d0239617a46fbfde9d389ae03ce156fcd032596`
- `shafiq735/hermes-router:ui-20261104-cli` (`cli`):
  `sha256:3ae201fbbb2e48b155cf681bedb09692da374dd92a23df05d3f0356d8b170f6e`

The InstaBox deployment uses the standard image digest and the existing persistent
data directory. A private configuration/data backup was made before deployment.
The VS Code extension code did not change; it can use the updated hosted router or
the updated CLI image.
