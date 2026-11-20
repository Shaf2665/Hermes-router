# Setup documentation and extension Overview refresh

Updated the website, repository guides and extension Overview with simple paths
for local Windows/macOS/Linux, Docker, Linux servers, Windows Server and macOS
login startup. Explained provider, administrator and application keys separately;
added a dashboard test before connecting VS Code. Corrected obsolete restart,
authentication and navigation guidance.

## Verification

- Astro build: 17 pages generated.
- Built-site audit: 903 internal links checked, including anchors; no broken links.
- Chromium: homepage, getting started, local setup, server setup and extension
  guide at 1440px and 390px; no JavaScript errors or horizontal overflow, one H1
  per page. Active navigation passes 4.5:1 contrast in light and dark themes.
- Installation/startup tests: 31 passed, 13 platform skips on Windows.
- Production checks: homepage and six main guide pages return HTTP 200 and
  contain the updated instructions.

## Published artifacts

- Website: https://hermes-router.vercel.app
- Vercel deployment: `dpl_4KRdWJEqRrvtyJepfzrLu4kXWU8S`.
- Extension: `vscode-extension/hermes-router-0.9.8.vsix`; packaged Overview matches
  the source README. The user will upload it to Marketplace.
- VSIX SHA256: `E0218A740C914AAE2F3735FB4F3EC5B064568AC05467235AC9109E98D0449BCF`.

## Limits

Fresh OS installations and server restarts were not executed. This was a website
deployment, not a router/container release. Context settings in extension 0.9.8
require the matching router changes; the published `custom-20261104` image does
not contain those changes. The guides state that version requirement.
