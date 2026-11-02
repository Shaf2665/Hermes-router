# Foundation owner access and durable quotas

Implemented 2026-11-02, without real credentials, upstream requests or new dependencies.

## Interfaces

- `ADMIN_API_KEY` is independent from inference-only `PROXY_API_KEYS`. Both bootstrap values are persisted to `HR_ENV_FILE` (default `.env`) when absent. Logs name the setting/file without printing either secret. Explicit process environment wins when initially loading the configured file. Legacy application keys remain inference keys and gain no owner access.
- Browser `POST /v1/admin/session {api_key}` returns `{authenticated:true,csrf_token}` and an HttpOnly, SameSite=Strict cookie. `GET` returns the current status/token; `DELETE` logs out. Cookie mutations require `X-CSRF-Token` and same-origin checks. Sessions expire after `HR_ADMIN_SESSION_TTL` (default 3600 seconds). A dedicated owner Bearer credential also authorizes admin API requests.
- Cookies become Secure for HTTPS. `HR_TRUST_PROXY=1` opts into Werkzeug's single trusted `X-Forwarded-Proto` handling only; default ignores spoofed forwarded scheme headers. Restrict the backend to the trusted reverse proxy before enabling it. `HR_ADMIN_COOKIE_SECURE=1` can also force Secure cookies.
- `/v1/config/*`, `/v1/instances*`, `/v1/status`, `/v1/logs` and `/metrics` require owner authorization. `/v1/usage` preserves the owner's global response, while application callers receive only their own durable key ledger and no provider/global activity.
- Key creation, scope/limit updates and revocation apply under one process lock immediately. The effective application list is persisted as `auth.json.access_keys` so an immutable container's original `PROXY_API_KEYS` cannot resurrect a revoked key at restart. Initial installations without that field retain the environment list. Keep auth.json with backups. Provider and exact upstream model allow-lists are `allowed_providers` and `allowed_models`; empty lists mean unrestricted.
- Cache identity includes the application key and admitted provider/model policy. In-flight requests keep their admitted scope; new admissions observe revocation and edits. Automatic restart returns HTTP 409 with `supported:false` on Windows or when the Linux restart helper is absent.

## Durable accounting contract

`usage_store.py` uses standard-library SQLite at `HR_USAGE_DB` (default `./usage.db`). It stores SHA-256 key identities, admission timestamps/day, reserved tokens/cost and settlement provenance. `BEGIN IMMEDIATE` atomically checks concurrent allowances and creates a reservation. Pending reservations remain charged across restart, and settlement updates only an unsettled row. Ledger connections close explicitly.

`_admit_request(token,payload=None,embedding=False)` creates `flask.g.usage_reservation`. `_record_request_tokens(token,payload,result)` settles JSON/error responses and skips streams. `_settle_request_usage(reservation_id,usage,model,cached=False,failed=False)` is idempotent. The performance worker wraps protocol streams and invokes it at finalization; both HTTP endpoints also register the managed iterator's close callback for cancellation before normal iteration completes.

Money/token capped requests require plain-text input, one completion and an explicit positive output bound. Money budgets reject unknown-price candidates and unsupported billing protocols/units; multimedia, tools and embedding calls are rejected under these capped policies. The reservation uses serialized UTF-8 input size plus maximum output (including configured reasoning headroom), and maximum known price among eligible candidates. Provider usage reconciles the reservation when available. Missing/malformed usage retains the conservative amount. Cache hits settle with zero upstream cost; failures before output release reserved spend/tokens. Request counts remain admission counts. Internal semantic embeddings are disabled for capped requests and respect access scope otherwise.

Snapshots distinguish pending reservations, estimated requests, unknown-price requests and cache hits. Unknown-price usage is marked `cost_status:unknown`; zero known spend must not be interpreted as guaranteed free service.

## Verification

- `python -m pytest tests/test_access_usage.py -q`: **14 passed**. Covers app/owner boundaries, own-only telemetry, CSRF/origin/expiry/logout, HTTPS cookies, custom env bootstrap and process precedence, no secret logging, immediate create/scope/revoke, concurrent CRUD, last-app revocation, durable concurrent reservations, restart recovery, idempotent settlement, missing/malformed usage, cache zero spend, explicit bounds/unknown prices, corrupt-file preservation and real HTTP stream settlement/disconnect.
- `python -m pytest tests/test_access_usage.py tests/test_router_smoke.py tests/test_instances.py tests/test_routing_hints.py tests/test_foundation_performance.py -q`: **57 passed** on the integrated tree. Controller must run the final full suite after remaining work lands.
- No external provider or operator credential was used.

## Limits

- Budgets are estimated-spend admission controls, not upstream invoice guarantees. Pricing, tokenization, unsupported billing units and upstream discrepancies remain limitations; an owner must explicitly change a capped policy to permit unsupported requests.
- One router process owns live access policy and process-local browser sessions. SQLite admission is transaction-safe, but multiple independent router processes do not synchronize live auth edits. Restart signs browser sessions out safely.
- Reservations left by a killed process remain conservatively charged to their admission UTC day. They are not automatically released; this prevents restart-based budget resets. There is no new ledger retention/compaction policy. Back up the SQLite database consistently with its WAL, or while stopped.
- Durable per-key totals govern quotas. Existing provider/global operational counters remain process-local and retain the original admin response semantics.
