---
name: connect
description: Check a Hermes Router connection and prepare an explicit handoff from a local Codex CLI or Claude Code CLI session after native usage runs out. Use when the user asks to connect Hermes, check gateway readiness, or continue a local coding session through Hermes.
---

Use the bundled `../../scripts/hermes_bridge.py` with Python 3.10 or later. Resolve the path relative to this skill, including when installed in a plugin cache. Run `--help` first.

1. Identify whether the user is using the local Codex CLI or Claude Code CLI. Explain that the launcher does not switch a running desktop, editor, web, or cloud session. Do not promise an automatic usage-limit handoff.
2. If needed, run `configure --url <their-router-url>` to save the URL. Use the router alias unless the user has a different alias. Never ask for a key in chat, inspect native login files, or print environment variables. Tell the user to set `HERMES_ROUTER_API_KEY` in their terminal or enter the key at the launcher's hidden prompt.
3. Run `status --client codex` or `status --client claude` only when a Hermes key is available in the environment. This is a read-only check without inference charges. Explain errors plainly; do not disable model restrictions to make it pass. Claude requires an enabled Claude model with confirmed context and tool support.
4. For continuation, record completed work, pending work, current directory, and uncommitted changes in the conversation. Use an explicitly identified session UUID or `--pick-session`. Never guess the last session, read whole transcript stores, or resend the failed prompt automatically.
5. Show the user the terminal command `python <bundled-script> codex --pick-session` or `python <bundled-script> claude --pick-session`. Tell them to stop/exit the old agent before launching the resumed session, review its history and repository state, then submit the next instruction. Do not launch a nested interactive CLI from the agent's noninteractive tool runner.

The bridge uses Hermes application keys and provider quotas. Native subscription credentials stay in the official client. The launcher does not change the native login, global client config, sandbox or approval settings. A plugin cannot generate an assistant turn once the native subscription has stopped; use the terminal launcher directly at that point. See the plugin's `../../README.md` for installation and current limitations.
