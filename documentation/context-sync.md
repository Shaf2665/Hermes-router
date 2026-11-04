# Dashboard and extension context synchronization

Extension 0.9.8 replaces the fixed 32,000-input/8,192-output declaration with
metadata from `GET /v1/client-config`. Upgrade the router and extension together.
Older routers return an explicit update-needed error; they are not silently
treated as supporting 256K. Preserve any local server changes when deploying it.

The published 0.9.7 VSIX is a separate dashboard-loading fix; it does not contain
this feature. Version 0.9.8 in this source checkout includes that fix and context
synchronization. A GitHub merge alone does not deploy a router, rebuild an image,
or install/publish an extension.

| Router | Extension | Result |
| --- | --- | --- |
| Current source with `/v1/client-config` | 0.9.8 from this source | Context limits synchronize when eligible models have confirmed capacities. |
| Current source | Published 0.9.7 or earlier | The old fixed client limit remains; upgrade the extension. |
| Older router without `/v1/client-config` | 0.9.8 | Explicit router-update error; no guessed fallback capacity. |

## Operator setup

1. Update the router from this source, preserving `.env`, provider credentials and
   mounted data; follow [Setup → Backup, update and recover](setup.md#4-backup-update-and-recover).
   Rebuild/recreate a source-based Docker installation; restarting an old container
   does not update its code. Install/build extension 0.9.8 from `vscode-extension`
   (`npm ci`, `npm run check`, `npm test`, `npm run compile`, `npm run package`).
   Install the generated VSIX and run **Developer: Reload Window** once.
2. In **Settings → Context length**, inspect **Confirm model capacities**.
   Use actual provider/account documentation for total context and maximum output.
   Model names alone are not evidence of capacity. Metadata already returned by
   optional model discovery is used when it includes both limits (including
   OpenRouter's `context_length` and `top_provider.max_completion_tokens`).
   No new upstream discovery calls are introduced by client polling.
3. Save confirmed limits for models without discoverable metadata. **Use discovered
   limits** removes an override. Unknown capacities are excluded from extension
   model discovery and from routing when a minimum is configured. The existing
   Auto-mode inference behavior for other clients is retained.
4. Select Auto or a minimum. Saves are applied to new requests immediately and
   persisted atomically in `ROUTER_CONTEXT_CONFIG`; no restart is needed.
5. The extension polls at `hermesRouter.refreshSeconds` (default 10 seconds),
   checks on connection/key changes, and rechecks before every request. VS Code's
   model-change event refreshes the picker without an editor restart.

## Verify after upgrading

1. Check `/health` and sign into the same router URL that VS Code uses.
2. Confirm that the application key can read `GET /v1/client-config`. It should
   return a model with positive input/output limits, not `model: null`.
3. Select **128K**, then **256K**, then **Auto**. Use a model whose confirmed
   capacity supports those choices. Wait for the configured refresh interval
   after each save and inspect Copilot's model details. Provider ceilings may
   reduce the displayed total; see the example below.
4. Send a short message after each change to verify the next request. These are
   real inference requests and may consume provider quota or incur a charge.
5. Reduce the limit with a conversation open. A request that fits uses the new
   budget; an oversized one is rejected with guidance to shorten it. Do not
   expect Hermes to truncate conversation history or recover text already
   compacted by the editor.

This synchronizes **context metadata and request budgets**. Other router settings,
such as caching and key rotation, remain server-side behavior; the extension does
not mirror every dashboard control as a VS Code setting.

## Meaning of the limits

The minimum filters the provider pool by **actual total model context**. It also
sets the extension's target context budget. Auto uses actual confirmed capacity.
For each eligible model the router reserves at most 8,192 output tokens, bounded
by the model's confirmed output limit and provider output ceiling. Input is
bounded by the remaining context and any provider input ceiling. The logical
pool advertises the lowest input and output budgets across eligible models, so
failover cannot silently use a smaller model than the client was promised.

For a single eligible 512,000-context model with at least 8,192 output capacity,
selecting 256K advertises 247,808 input + 8,192 output = 256,000 total. Selecting
Auto advertises 503,808 input + 8,192 output. Mixed provider ceilings can result
in a lower displayed total than the selected minimum; the setting does not
increase a provider's real capacity.

Token counting for the extension uses UTF-8 bytes plus framing, a deliberately
conservative estimate rather than the previous characters/4 guess. Preflight
includes serialized messages, tool results, tool arguments and tool schemas.
The router checks the same serialized input. This can admit less text than a
provider-specific tokenizer would; it avoids treating a large tool schema as
zero tokens. No conversation is silently truncated.

## API and access boundaries

`GET /v1/client-config` accepts an application key using the existing inference
authentication. It respects the key's provider/model scope and returns
`Cache-Control: no-store`. The response contains only:

- `schema_version: 1`, `revision`, `context_window` (selected minimum).
- `model`: router model ID, effective `context_window`, `max_input_tokens`,
  `max_output_tokens`, and capabilities; or `null` with a setup explanation.

No provider credentials, key tails, administrator settings or hidden model list
are returned. Administrator keys cannot be substituted for application keys.

`POST /v1/config/context` retains the administrator/CSRF boundary. It accepts
`context_window` and/or a `model_limits` patch keyed by `provider::model`.
Each entry has integer `context_window` and `max_output_tokens` fields; `null`
removes an override. The maximum accepted number is 16,000,000 tokens, output
must be smaller than context, and unknown model IDs are rejected. Concurrent
saves are serialized, and failed persistence does not change live settings.

The extension sends `max_tokens` and `X-Hermes-Config-Revision` with completion
requests. A configuration/scope change between preflight and send returns 409
before admission or upstream inference. Oversized input/output is rejected.
Requests already admitted keep their policy snapshot during failover. A smaller
limit may require shortening an existing conversation and retrying. Offline,
revoked-key, malformed-metadata and unavailable-model states fail closed.

## Validation

- Python tests: minimum changes, Auto, mixed capacities, input/output ceilings,
  unknown capacities, scopes, secret redaction, admin/CSRF checks, malformed
  values, atomic failure, restart persistence, stale requests and failover.
- Extension tests: model-change events, increase/decrease/Auto, reconnect,
  credentials/router switching races, cancellation/disposal, tool/Unicode
  accounting, strict metadata parsing and actual local HTTP wire behavior.
- A real installed VSIX/Copilot session must still be checked when rolling out:
  change 128K → 256K → Auto and verify the model picker and next request update.
