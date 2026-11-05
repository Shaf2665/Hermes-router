# Hermes Router coding-client bridge — 0.1.0 preview

Continue a **local Codex CLI or Claude Code CLI** session using your enabled Hermes API providers when you choose to switch. The plugin provides setup guidance; the bundled Python launcher does the actual handoff. It does not install either official CLI or host a router.

**This preview does not automatically switch when a subscription expires.** Exit the original agent, select its saved session through the launcher, review the history and current files, then send your next instruction. The launcher never replays your last prompt. Native subscription credentials stay in the official client. Hermes requests use your provider quotas and may incur their normal API charges.

## Install the repo plugin

Requires Python 3.10+, the official local coding CLI, and a router containing the Responses bridge update. No additional Python packages are needed by the launcher.

Claude Code, from a terminal:

```sh
claude plugin marketplace add Shaf2665/Hermes-router
claude plugin install hermes-router@hermes-router
```

Use `/hermes-router:connect` for setup guidance. For a local checkout, `claude --plugin-dir ./plugins/hermes-router` also loads the plugin.

Codex CLI:

```sh
codex plugin marketplace add https://github.com/Shaf2665/Hermes-router.git
```

Open the plugin picker and install **Hermes Router** from the **Hermes Router** marketplace. The repository includes both portable and compatible Codex/Claude manifests. This is a repository marketplace, **not a listing approved or published in OpenAI's or Anthropic's public directories**.

Once subscription usage is exhausted, a skill may not be able to run an assistant turn. Use the launcher directly from your terminal; it does not need a model to start.

## Configure once

From the Hermes checkout (or use the absolute path to the script in the installed plugin):

```sh
python plugins/hermes-router/scripts/hermes_bridge.py configure --url https://YOUR-ROUTER
python plugins/hermes-router/scripts/hermes_bridge.py status --client codex
```

The status command asks for a **Hermes application key** at a hidden terminal prompt. You can alternatively supply `HERMES_ROUTER_API_KEY` through your shell's secret-management facility. Do not paste it into an agent conversation. The configuration stores only the URL and model alias, never the key. HTTP is accepted only for loopback hosts; remote servers require HTTPS. Redirects are refused so credentials cannot follow an unexpected destination.

`status` checks authentication, bridge support, enabled models, confirmed tool support and context limits without making an inference request. If it reports no eligible model, enable a suitable model and confirm its context/output limits in the dashboard. Claude Code additionally needs an enabled **Claude** model. Scopes on your application key still apply.

## Continue a session

Stop the original running agent first. In the same project directory, run:

```sh
python /path/to/Hermes-router/plugins/hermes-router/scripts/hermes_bridge.py codex --pick-session
# Or, for Claude Code:
python /path/to/Hermes-router/plugins/hermes-router/scripts/hermes_bridge.py claude --pick-session
```

Alternatively, use `--resume SESSION_UUID` to select an exact session. Omit both flags to open a new session. `--dry-run` performs readiness checks and shows a key-free command preview. The launcher does not guess the latest session or supply a prompt to the resumed client. Review completed tool work before asking it to proceed.

Keys are passed to child processes, not command-line arguments. Claude Code receives an additional private temporary settings file because settings-file environment values override shell exports. It is removed when the child exits normally or the launcher unwinds an error. Abrupt OS termination can leave that temporary file behind; it contains your Hermes application key and should be removed. Neither client's native login, global configuration, sandbox settings nor approval policy is edited. Close the Hermes session and run ordinary `codex` or `claude` to use your normal configuration again.

The launcher reads current context limits each time it starts. Dashboard model switches still apply to new requests in a running CLI session; a reduced context limit may reject a long existing conversation. Reopen through the launcher to refresh the CLI's displayed budget.

## Protocol coverage

| Client | Hermes endpoint | Preview behavior |
| --- | --- | --- |
| Codex CLI | `POST /v1/responses` | Stateless full history, text/images, function calls/results, custom calls/results, JSON and streaming output |
| Claude Code CLI | `POST /claude/v1/messages` | Existing Messages translation, restricted to enabled Claude candidates, strict confirmed tool support |
| Readiness | `GET /v1/bridge-info`, `/codex/v1/client-config`, `/claude/v1/client-config` | Authenticated, scoped metadata; no inference |

The Responses bridge translates through the existing router pipeline. It keeps application-key authorization, scope/budget controls, dashboard model enablement and context policy. It buffers tool arguments until the upstream turn finishes and validates all tool calls before marking any executable call complete. XML-looking text is never converted into a tool call. A truncated/failed stream emits a failure instead of a successful completion. Codex transport retries are set to zero by the launcher; Hermes can still try eligible providers before handing a stream to the client.

Custom free-text tools are represented upstream as a function taking an `input` string. Grammar descriptions are passed to the provider; grammar-constrained decoding is not guaranteed. The official client remains responsible for tool validation and execution.

Unsupported: stored Responses state, `previous_response_id`, background mode, hosted built-in tools (web search is disabled by the launcher), file-ID inputs, realtime/WebSockets, and `/responses/compact`. Encrypted reasoning is provider-specific and is not forwarded. Unknown request item/tool types are rejected instead of silently removed. Automatic compaction support depends on the client's local custom-provider behavior; very long sessions may need a new chat.

Desktop apps, VS Code windows, web/cloud sessions, and running native sessions are **not switched** by installing this plugin. Claude Desktop has separate third-party inference configuration. Claude Code routing to non-Claude models is not officially supported by Anthropic; this launcher's `/claude` route deliberately excludes them. Codex can use the broader enabled Hermes pool.

## Validation and next phase

Run `python -m pytest -q tests/test_responses_api.py tests/test_cli_bridge.py` from the repository with its test dependencies installed. An opt-in native Codex smoke test uses a loopback stub and an empty client state directory, with no provider billing:

```sh
HERMES_RUN_CODEX_SMOKE=1 python -m pytest -q tests/test_codex_cli_bridge.py
```

The test requires a host capable of running Codex's sandbox. It skips inside restricted nested containers. Protocol regression tests still run there. Validate the exact client version and your provider's tool behavior before relying on session handoffs for work that changes files.

Automatic switching is a later phase: detect documented quota events, capture an explicit session checkpoint, prevent overlapping agents, and demonstrate that interrupted edits/commands are not duplicated. No subscription-token import or unofficial quota bypass is part of this plugin.

## Official references

Verified November 5, 2026:

- [Codex custom providers](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers)
- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [OpenAI plugin packaging](https://developers.openai.com/plugins/build/plugins)
- [Responses function calling and stream events](https://developers.openai.com/api/docs/guides/function-calling)
- [Claude Code gateway connection and supported surfaces](https://code.claude.com/docs/en/llm-gateway-connect)
- [Claude Code gateway compatibility](https://code.claude.com/docs/en/llm-gateway)
- [Claude Code CLI resume options](https://code.claude.com/docs/en/cli-reference)
- [Claude Code plugin manifests](https://code.claude.com/docs/en/plugins-reference)
- [Claude Code credential restrictions](https://code.claude.com/docs/en/legal-and-compliance)
