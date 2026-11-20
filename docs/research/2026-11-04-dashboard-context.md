# Dashboard context minimum and GroqCloud preset

## Behavior

- Settings > Context length saves the minimum model capacity to the existing
  environment file and applies it to new chat requests without restarting.
  Presets include Auto, 16K, 32K, 40K, 64K, 128K, 200K, 256K, 512K and 1M.
- A request header may raise the minimum but never lower the dashboard value.
  The effective gate also includes estimated input and output allowance.
  Unknown capacities cannot satisfy a positive minimum. Failover and cache
  isolation retain this requirement; ongoing responses finish normally.
- Administrator-only `POST /v1/config/context` accepts `context_window` as an
  integer from 0 to 2147483647. Failed persistence does not change the running
  setting. `/v1/status` and authenticated `/v1/models` expose the minimum.
- Extension 0.9.9 defaults `hermesRouter.contextWindow` to 0: follow the dashboard.
  It refreshes through `/v1/models` with the application key, without owner access.
  Auto/older routers fall back to 40192 tokens. A positive client value can raise
  the effective window. Stale responses from an earlier router are ignored.
- Existing Groq URLs were already correct. The preset is now named
  GroqCloud (Groq), suggests GPT-OSS 120B and 20B, links to the official docs and
  explains account limits. Verified Groq production capacities allow its GPT-OSS
  and Llama 3.1 8B / 3.3 70B models to qualify for 128K routing.

## Sources

- Base URL and API-key setup: https://console.groq.com/docs/openai
- Model capacities: https://console.groq.com/docs/models
- Key creation: https://console.groq.com/docs/quickstart

Checked 2026-11-04. Groq's base URL is `https://api.groq.com/openai/v1` and its
key page is `https://console.groq.com/keys`. Listed production model contexts are
131072 tokens; rate limits remain separate from model capacity.

## Verification and delivery

- Full Python suite: 208 passed, 13 platform-specific skips, including browser
  integration. A 128K dashboard minimum rejects a 32K mock model even with a 16K
  client header; switching to 16K permits inference. Failed saves preserve state.
- Mobile/desktop controls checked at 390px and 1366px without horizontal overflow.
- Extension: 12 tests passed, TypeScript check passed; VSIX build, manifest,
  Overview and bundled sync code verified. Packaging now compiles automatically.
- Website: 17 pages built; five changed production guides checked for new content.
  Production deployment: `dpl_9Nduv8UJ7sHmmmupA6TCfWr2RxuD`.
- Package: `vscode-extension/hermes-router-0.9.9.vsix`.
  SHA256: `32BB3EFE1DF633FA588FC54C22B8A6798D461C6228B2E0DC057E088D9ED60B49`.

The user will upload the extension. Router changes are local source changes;
neither a Docker image nor the hosted router was deployed in this task. The
published `custom-20261104` image predates context routing. Groq inference was
tested through mocks, not a live account. No production context setting was changed.
