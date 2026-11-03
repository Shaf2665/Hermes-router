# VS Code dashboard loading fix — 2026-11-04

## Cause and correction

The dashboard generated invalid JavaScript by nesting quoted button handlers
inside an HTML string. The script failed before registering its status listener,
leaving the panel on `Loading...` even with a working router and application key.

Version 0.9.7 uses button data attributes and a shared click handler. Extension
commands received from the panel are restricted to the four supported actions.
Owner access errors now point to Set Administrator Credential; chat still uses
the application key in `hermesRouter.apiKey`.

## Verification

- Reproduced the original generated-script syntax error before changing code.
- Four extension tests pass, including local and remote panel rendering,
  escaped error/provider text, button messages and command restrictions.
- TypeScript checking and the extension bundle build pass.
- Chromium rendered the generated panel, exercised its buttons and reported no
  JavaScript errors.
- The actual client, with a hosted URL ending in `/v1`, loaded owner status with
  the administrator key and streamed a successful live reply with an application
  key. An application key was denied owner access as intended.
- Installed 0.9.7 locally and reloaded VS Code. The real panel displayed the
  administrator setup guidance instead of remaining on `Loading...`.

The administrator credential has not been entered into the user's VS Code
SecretStorage. Complete that one-time setup using the separate dashboard login
key. Copilot's full chat UI and every upstream provider were not retested.
No router image change is required for this extension-only fix.
