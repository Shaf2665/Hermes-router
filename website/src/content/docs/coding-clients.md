---
title: "Codex and Claude Code"
description: "Preview a local coding-client handoff to Hermes with a repository plugin and a credential-safe launcher."
---

The **Hermes Router coding-client bridge 0.1.0** is a preview for local Codex CLI and Claude Code CLI sessions. It includes a repository marketplace plugin, a Python launcher, and a stateless Responses API adapter.

When your native subscription is unavailable, exit the original agent and use the launcher to select its saved session. Review the conversation and files, then submit your next instruction. Requests then use the enabled Hermes provider pool and its quotas. **Automatic subscription-limit switching is not implemented in this preview.**

```sh
python plugins/hermes-router/scripts/hermes_bridge.py configure --url https://YOUR-ROUTER
python plugins/hermes-router/scripts/hermes_bridge.py status --client codex
python plugins/hermes-router/scripts/hermes_bridge.py codex --pick-session
```

Run from the checkout or use the launcher's absolute path. Python 3.10+ and the official local CLI are required. Enter your Hermes application key at the hidden terminal prompt, or supply `HERMES_ROUTER_API_KEY` through your local secret manager. The saved configuration contains no key. The launcher does not change your native login or global client settings.

For Claude Code, substitute `claude` for `codex`. Its dedicated `/claude/v1/messages` route uses only enabled Claude models with confirmed tool/context support; your application's provider/model restrictions still apply. Codex uses the wider Hermes pool through `/v1/responses`.

The Responses endpoint accepts complete message/tool history with `store=false`, and returns JSON or SSE. Hosted tools, saved response references, background mode, WebSockets, and `/responses/compact` are unsupported. Custom text tools are translated to functions; provider grammar enforcement is not guaranteed. Failed streams never claim a successful turn.

Installing the plugin does not switch a running editor, desktop or cloud session. It is distributed through this repository's marketplace, not published to an official public plugin directory.

See the [complete installation, handoff and compatibility guide](https://github.com/Shaf2665/Hermes-router/blob/main/plugins/hermes-router/README.md). The guide links the official client documentation and describes the security and session-resume tests.
