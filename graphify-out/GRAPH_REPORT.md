# Graph Report - D:\Hermes-router  (2026-11-02)

This is the initial exploration snapshot of commit `9d87915`, before the foundation implementation. Its source locations and architecture descriptions reflect that baseline. See [foundation validation](../docs/research/foundation-validation.md) for the current implementation. Open `graph.html` locally for the interactive graph; `graph.json` contains the machine-readable graph. Extraction caches and machine-specific interpreter/mtime metadata are excluded from version control.

## Exploration summary

Hermes Router is a Flask/Waitress inference gateway centered on `router.py` (6,808 lines), supported by a Bash control CLI, VS Code extension, tests and an Astro/Starlight documentation site.

- OpenAI chat and Anthropic Messages share `_route_completion()`: cache lookup, model ordering, tool/vision checks, provider scope, breaker decisions, key rotation and forwarding.
- `CredentialPool`, `ProviderStats`, `ResponseCache`, `KeyUsage` and `RequestRingBuffer` hold the main operational state.
- VS Code registers `HermesChatModelProvider`; `RouterClient.streamChat()` crosses HTTP into `/v1/chat/completions`. Local/Docker controls use `hr`; remote restart uses the configuration API.
- The browser dashboard and administration endpoints live in `router.py`. The separate website is documentation, not the router dashboard.
- `hermes-router` dispatches to 11 scripts. Docker has minimal and CLI images; the CLI image persists mutable configuration in `/app/data`.
- Tests cover smoke behavior, routing hints, provider defaults, discovery, instances and browser dashboard actions. CI declares Python 3.10/3.12 and Playwright checks; this graph run did not execute them.

## Audit and limitations

- Scope: 82 files, 84,944 text words: automatic detection plus 20 additional shell, build and CI files. Binary image bytes are excluded from the word count; images were visually inspected. Shell internals are represented by scripts and configuration contracts.
- No application source was changed. Real credential files, runtime state, dependencies, build output, lockfiles and repository metadata were not analyzed as code.
- Static extraction is not proof of runtime behavior or provider availability. Documentation, marketing images and demo metrics are claims, not measurements.
- The initial AST contained 137 rationale fragments; these became node attributes. 141 method-name matches lacked a confirmed receiver and were removed; 38 calls with explicit receivers were recovered. Four `pool_ref` matches remain INFERRED. Other parser-resolved calls may still be incomplete.
- 51 unresolved external import/reference edges were excluded. Five VS Code relative-import targets and the test imports of `router` were resolved to local files. This is not a complete external dependency graph.
- The JSON preserves directed edges and source evidence. Repeated relations for a node pair are preserved in `evidence`/`relations`; the visualizer displays one representative relation. Communities use undirected connectivity; clustering and low cohesion do not by themselves diagnose bad design.
- Documentation drift: `vscode-extension/README.md:13` advertises removed configuration commands; `vscode-extension/CHANGELOG.md:35`, `documentation/vscode-extension.md:99` and `website/src/content/docs/vscode-extension.md:110` describe configuration in the browser. `documentation/deployment.md:226` has similar stale button instructions.
- `website/README.md` is a starter README. Dashboard figures in `website/src/content/docs/index.mdx:109` are explicitly demo data.
- **Token usage and monetary cost are unavailable.** Zeros in Graphify's standard fields are placeholders, not a claim of zero semantic-processing cost. The benchmark below is an estimated retrieval-size comparison, not measured model accuracy or actual billing savings.

## Corpus Check
- 82 files · ~84,944 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 475 nodes · 1044 edges · 22 communities detected
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 21 edges (avg confidence: 0.87)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Architecture and Integrations|Architecture and Integrations]]
- [[_COMMUNITY_Routing Cache and Failover|Routing Cache and Failover]]
- [[_COMMUNITY_Authentication and Usage Accounting|Authentication and Usage Accounting]]
- [[_COMMUNITY_Response Conversion and Cost|Response Conversion and Cost]]
- [[_COMMUNITY_Copilot Chat Transport|Copilot Chat Transport]]
- [[_COMMUNITY_VS Code Local Controls|VS Code Local Controls]]
- [[_COMMUNITY_CLI Deployment and CI|CLI Deployment and CI]]
- [[_COMMUNITY_Instance Lifecycle Management|Instance Lifecycle Management]]
- [[_COMMUNITY_Model Discovery and Probing|Model Discovery and Probing]]
- [[_COMMUNITY_Documentation Site and Branding|Documentation Site and Branding]]
- [[_COMMUNITY_Proxy Key Administration|Proxy Key Administration]]
- [[_COMMUNITY_Routing Hint Tests|Routing Hint Tests]]
- [[_COMMUNITY_Provider Credential Configuration|Provider Credential Configuration]]
- [[_COMMUNITY_Codex and Provider Initialization|Codex and Provider Initialization]]
- [[_COMMUNITY_Feature and Model Settings|Feature and Model Settings]]
- [[_COMMUNITY_Model Discovery Tests|Model Discovery Tests]]
- [[_COMMUNITY_Upstream Protocol Adapters|Upstream Protocol Adapters]]
- [[_COMMUNITY_Provider Capability Status|Provider Capability Status]]
- [[_COMMUNITY_Dashboard Browser Tests|Dashboard Browser Tests]]
- [[_COMMUNITY_Compose Configuration Documentation|Compose Configuration Documentation]]
- [[_COMMUNITY_Alternate Favicon Asset|Alternate Favicon Asset]]
- [[_COMMUNITY_Decorative Mascot Asset|Decorative Mascot Asset]]

## God Nodes (most connected - your core abstractions)
1. `_route_completion()` - 39 edges
2. `Hermes Router` - 31 edges
3. `_auth_check()` - 25 edges
4. `embeddings()` - 22 edges
5. `Architecture — How it works` - 22 edges

## Surprising Connections (you probably didn't know these)
- `Smart routing` --implemented_by--> `_route_completion()`  [INFERRED]
  documentation/routing.md → router.py
- `Credential pool` --implemented_by--> `CredentialPool`  [INFERRED]
  README.md → router.py
- `Response cache` --implemented_by--> `ResponseCache`  [INFERRED]
  documentation/configuration.md → router.py

## Hyperedges (group relationships)
- **Caller-managed iterative inference** — concept_hall_of_wisdom, concept_strict_tool_loop, concept_session_affinity, concept_workload_hint, concept_hermes_router [EXTRACTED 1.00]
- **Shared inference and configuration authority** — concept_proxy_keys, concept_key_limits, concept_dashboard [EXTRACTED 1.00]
- **Caller-owned agent execution** — concept_hermes_router, concept_hall_adapter, concept_copilot_agent_mode, concept_client_owned_tools [INFERRED 0.90]

## Communities

### Community 0 - "Architecture and Integrations"
Cohesion: 0.05
Nodes (98): Hermes Router marketing banner, Caller-owned agent loop, Anthropic protocol translation, OpenAI and Anthropic API compatibility, auth.json credential store, Per-model capability probing, Circuit breaker, hr CLI (+90 more)

### Community 1 - "Routing Cache and Failover"
Cohesion: 0.07
Nodes (26): POST /v1/embeddings, _add_provider_tokens(), _anthropic_streaming_generator(), _codex_streaming_generator(), _codex_text_and_tools(), _cosine(), CredentialPool, _embed_ordered() (+18 more)

### Community 2 - "Authentication and Usage Accounting"
Cohesion: 0.06
Nodes (34): POST /v1/messages, POST /v1/config/keys/<provider>, GET /v1/config/proxy-keys, GET /v1/logs, GET /metrics, GET /v1/models, GET /v1/usage, _admit_request() (+26 more)

### Community 3 - "Response Conversion and Cost"
Cohesion: 0.09
Nodes (18): GET /favicon.ico, GET /, _choice_has_output(), _completion_has_output(), _cost(), favicon(), _instance_health_url(), _instance_models_url() (+10 more)

### Community 4 - "Copilot Chat Transport"
Cohesion: 0.1
Nodes (15): POST /v1/chat/completions, POST /v1/config/restart, GET /dashboard, GET /health, config_restart(), dashboard(), _trigger_restart(), get() (+7 more)

### Community 5 - "VS Code Local Controls"
Cohesion: 0.13
Nodes (12): cfg(), controlArgv(), dockerContainer(), hrPath(), isDocker(), isLocal(), notifyMissing(), runHrTerminal() (+4 more)

### Community 6 - "CLI Deployment and CI"
Cohesion: 0.17
Nodes (21): Router smoke test pipeline, .env configuration, Provider model overrides, Free model rankings, systemd service, Pytest and Playwright tests, Fast-forward update and rollback, free-model-rankings.md documentation (+13 more)

### Community 7 - "Instance Lifecycle Management"
Cohesion: 0.18
Nodes (19): POST /v1/instances, DELETE /v1/instances/<instance_id>, POST /v1/instances/<instance_id>/<action>, GET /v1/instances, POST /v1/instances/<instance_id>, create_instance(), delete_instance(), _docker_action() (+11 more)

### Community 8 - "Model Discovery and Probing"
Cohesion: 0.15
Nodes (17): _cached_model_caps_compatible(), _discover_best_model(), _discover_models(), _env_flag(), _initialize_ratings(), _is_free_model_id(), _model_env_suffix(), _probe_anthropic() (+9 more)

### Community 9 - "Documentation Site and Branding"
Cohesion: 0.14
Nodes (13): VS Code activity icon, Circuit H mark, Website raster favicon, Hermes Router social preview, Website raster logo, VS Code extension raster icon, Astro, Astro Starlight documentation (+5 more)

### Community 10 - "Proxy Key Administration"
Cohesion: 0.19
Nodes (14): POST /v1/config/proxy-keys, DELETE /v1/config/proxy-keys/<tail>, POST /v1/config/proxy-keys/<tail>, config_create_proxy_key(), config_delete_proxy_key(), config_update_proxy_key(), _delete_proxy_key_meta(), _env_read_line() (+6 more)

### Community 11 - "Routing Hint Tests"
Cohesion: 0.22
Nodes (6): _Pool, _Response, test_non_streaming_request_records_session_affinity(), test_normal_routing_fails_over_to_the_next_provider(), test_tool_loop_bypasses_cache_and_records_session_affinity(), test_vision_payload_skips_text_only_candidates()

### Community 12 - "Provider Credential Configuration"
Cohesion: 0.17
Nodes (13): GET /v1/config/providers, _build_instance_from_body(), config_providers(), _copy_provider_keys_env(), _keys_for_instance_copy(), _load_auth_json(), _normalize_instance_base_url(), _parse_copy_provider_keys() (+5 more)

### Community 13 - "Codex and Provider Initialization"
Cohesion: 0.2
Nodes (7): _build_providers(), CodexCredentials, _excluded_models(), _filter_excluded(), _jwt_exp(), _keys(), _keys_for()

### Community 14 - "Feature and Model Settings"
Cohesion: 0.2
Nodes (10): POST /v1/config/features/<name>, POST,DELETE /v1/config/model/<provider>, POST /v1/config/rotation, config_feature(), config_model(), config_rotation(), _env_write_line(), _features_snapshot() (+2 more)

### Community 15 - "Model Discovery Tests"
Cohesion: 0.28
Nodes (4): _FakeModelsResp, _FakePool, test_refresh_bounds_extras_when_kept_meets_limit(), test_refresh_keeps_configured_above_limit_and_caps_extras()

### Community 16 - "Upstream Protocol Adapters"
Cohesion: 0.25
Nodes (8): forward(), _merge_anthropic_content(), _openai_content_to_anthropic(), _resolve_model(), _strip_message(), _strip_response(), _to_anthropic_body(), _to_codex_body()

### Community 17 - "Provider Capability Status"
Cohesion: 0.38
Nodes (6): GET /v1/status, _model_caps(), _model_supports_tools(), _model_supports_vision(), status(), _workload_fit()

### Community 18 - "Dashboard Browser Tests"
Cohesion: 0.47
Nodes (3): _free_port(), router_server(), _wait_for_health()

### Community 23 - "Compose Configuration Documentation"
Cohesion: 1.0
Nodes (1): docker-compose.yml documentation

### Community 24 - "Alternate Favicon Asset"
Cohesion: 1.0
Nodes (1): Geometric star SVG favicon

### Community 25 - "Decorative Mascot Asset"
Cohesion: 1.0
Nodes (1): Smiling hard-hat mascot illustration

## Ambiguous Edges - Review These
- `README.md` → `Legacy extension configuration commands`  [AMBIGUOUS]
  vscode-extension/README.md · relation: still_advertises_removed_commands

## Knowledge Gaps
- **40 isolated node(s):** `docker-compose.yml documentation`, `concepts.md documentation`, `free-model-rankings.md documentation`, `getting-started.md documentation`, `hall-of-wisdom.md documentation` (+35 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Compose Configuration Documentation`** (1 nodes): `docker-compose.yml documentation`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Alternate Favicon Asset`** (1 nodes): `Geometric star SVG favicon`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Decorative Mascot Asset`** (1 nodes): `Smiling hard-hat mascot illustration`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **How does a Copilot Chat tool call reach a provider and fail over?**
  _Connects the VS Code language model provider, HTTP client, chat endpoint, shared router and credential pool._
- **How do proxy keys connect authentication, budgets, caching and administration?**
  _Connects request admission, cache namespaces, usage accounting and configuration endpoints._
- **How does semantic caching reuse embeddings, and what can change during embedding failover?**
  _Connects cache behavior with provider selection and the documented embedding-dimension constraint._

## Retrieval-size benchmark

Graphify estimates 113,258 tokens for the 84,944-word text corpus versus an average 3,390 tokens per built-in graph query: 33.4x fewer tokens. These are heuristic size estimates. The generic benchmark questions do not establish answer completeness; small results can reflect weak question matching. See `benchmark.json` for all five queries.

## Artifact verification

Graph IDs, edge endpoints and direction metadata, confidence scores, source paths, community assignments, hyperedges and the Copilot-to-chat-to-routing links passed assert-based checks. The generated HTML rendered its graph canvas and 26 community controls in Chromium; searching `_route_completion` returned the expected node. The only browser console error was an incidental missing favicon. Application tests and provider calls were not run; tracked project files are unchanged.

Open `graph.html` locally in a browser. Its vis-network library loads from unpkg.com, so the interactive viewer needs network access unless that resource is cached. `graph.json` and this report are usable offline.
