# Hermes performance and reliability audit

Date: 2026-11-02. Scope: current local code, isolated tests, and deterministic inspection. No production services, credentials, real provider calls, dependency installations, or application edits.

## What already works

- HTTP keepalive and per-host connection pools already exist (`router.py:59`): 20 cached pools, at least 32 connections per pool, and no automatic transport retries. Adding a Session is not a new optimization.
- The credential pool, cache, usage accounting and request log already synchronize mutations. Cache size and log size are bounded.
- Successful returned streams use `_with_cleanup()` to release the upstream response (`router.py:2777`, `router.py:5770`, `router.py:5798`).
- Model capabilities persist between restarts; fresh complete state avoids re-probing (`router.py:1932`).
- Both chat protocols share `_route_completion()`, so most routing fixes belong there once.

## Prioritized findings

### 1. Slow instance checks can freeze the whole dashboard

**Confirmed structure; high-confidence performance issue.** `router.py:4484` schedules an asynchronous refresh every five seconds without an in-flight guard, request timeout, cancellation, or hidden-tab pause. `router.py:4495` waits for all five endpoint responses before updating any dashboard data. Those include `/v1/instances` even when the user is viewing another tab.

`list_instances()` at `router.py:6443` serially calls `_instance_public()` for every instance. That calls `_probe_instance()` (`router.py:1257`) with health and authenticated model checks, each using a 1.5-second timeout. Docker instances additionally call `docker inspect` with a five-second subprocess timeout (`router.py:1303`). Thus failed/slow instances accumulate request latency; multiple dashboard viewers multiply the work. A refresh can still be running when the next interval starts. These requests share the Waitress worker pool with inference.

Smallest complete improvement:

1. Fetch/render overview data independently of instance checks and key configuration.
2. Request instances only when that view is visible; use a refresh loop scheduled after completion, with cancellation and hidden-tab pause.
3. Keep instance health snapshots with an age indicator and a manual refresh; bound concurrent probes instead of serial unbounded work on each read.
4. Invalidate snapshots after create/update/start/stop/restart/delete, and never include raw instance secrets.

Must reach: embedded dashboard JS, `list_instances`/public serialization, all instance mutation handlers, existing instance tests, browser tests, and monitoring docs. Preserve the current response schema where possible.

Measure: dashboard paint-to-first-status, endpoint p95, overlapping refresh count, provider traffic latency with 0/10/50 simulated instances and 1/10 dashboard tabs. Use slow and unreachable local fake endpoints. Acceptance: at most one refresh per view; a slow instance must not delay overview rendering.

### 2. Failover lacks a total time budget and explicit cleanup on rejected streams

**Confirmed structure; impact depends on upstream behavior.** Every chat attempt gets a `(10, 120)` connect/read timeout; Codex gets `(10, 180)` (`router.py:3528`, `router.py:3545`, `router.py:3609`). `_route_completion()` walks candidate models and keys (`router.py:5667` onward), with no overall request deadline. Multiple slow attempts can consume minutes. Read timeouts are not an application-wide failover deadline.

When `stream=True`, error paths such as 429, 413, 5xx and unexpected status return to the retry loop without explicitly closing `resp` (`router.py:5702` onward). Some branches consume `.text`, others do not. The success generator has cleanup; rejected responses need equally deterministic cleanup. Codex non-streaming aggregation consumes a streamed response without a surrounding `finally` (`router.py:5772`), so abnormal iteration also needs cleanup.

Smallest complete improvement: establish clear response ownership; close every response that will not be handed to a caller generator, using a `try/finally`. Then add an internal total routing deadline and pass remaining time to attempts, with a conservative default and an actionable timeout response. Do not retry after response bytes have reached the caller, and do not hedge duplicate paid calls by default.

Must reach: chat and Messages shared router, embeddings error paths, Codex aggregation, fake response fixtures (`close()` tracking), cancellation and stalled-upstream tests, config and timeout docs.

Measure: fake upstream 429/503 storms, stalled headers, stalled body, disconnected clients; record open sockets, retry count and time to terminal response. Acceptance: all rejected responses are closed exactly once and latency stays within the documented deadline plus scheduling tolerance.

### 3. Streaming telemetry cannot currently prove better performance or enforce accurate spend

**Confirmed accounting gap.** `stats.record_success()` runs immediately after `forward()` returns (`router.py:5757`). For streaming HTTP that is approximately response-header latency; for non-streaming it includes downloaded response time. The displayed average mixes unlike measurements and can count success before the stream fails.

`_record_request_tokens()` executes before the returned generator is consumed (`router.py:5905`, `router.py:5941`). Its stream path adds estimated input tokens only and no output cost (`router.py:3721`). `_streaming_with_usage()` records provider usage, not the authenticated caller's usage (`router.py:2788`). Consequently completion tokens and cost can be absent from per-key budgets for streamed chat. The Anthropic translation generator (`router.py:2960`) does not emit the upstream usage as an OpenAI usage block. Codex streams bypass `_streaming_with_usage()`.

Smallest complete improvement: collect a request-scoped stream outcome with first meaningful output time, total duration, final usage and provider/model; finalize provider and caller accounting once on completion or failure. Keep reported versus estimated usage distinct, handle cancellation, and avoid double-counting the current pre-flight estimate. A limit based only on completed requests can still overshoot under concurrent requests; strict hard budgets require explicit reservations, not a promise in UI copy.

Must reach: all three upstream stream translators, `_route_completion`, both inbound handlers, request logging, key/provider accounting, metrics, dashboard labels, tests, and budget docs.

Measure: first-token latency and end-to-end p50/p95/p99 separately, completion/error/cancel rates, exact reported token and cost totals across OpenAI/Anthropic/Codex fixtures. Acceptance: one accounting finalization and one outcome per request, with no fabricated accuracy when the upstream omits usage.

### 4. Semantic caching is a correctness boundary as well as a latency feature

**Confirmed matching limitations.** Exact caching hashes the full request and caller namespace (`router.py:2446`). Semantic caching compares only embedding similarity, namespace, dimensions and expiry (`router.py:2485`). It does not enforce model, system instructions, tools, response format, generation parameters or embedding-provider/model identity. Equal vector dimensions do not establish a shared embedding space. `_embed_text()` can fall through to another embedding provider (`router.py:3636`), and runs before routing, adding upstream work to every eligible exact-cache miss.

Keep semantic caching opt-in. Partition it by a compatible request signature and embedding identity; do not reuse across tool/format/system constraints. Apply a short, separate embedding timeout and gracefully skip semantic lookup when it is slow. Cache exact responses without needing embeddings first. A coalescing mechanism for identical concurrent exact misses is worth measuring later, scoped by the full caller/request key and excluding streams/tool loops.

Must reach: `ResponseCache`, persistent row schema/migration or cache invalidation, `_embed_text`, routing/cache callers, regression tests with same-dimension different embedding models, and configuration docs. A cache migration must never silently reclassify old embeddings as compatible.

Measure: warm/cold exact hits, semantic hit/miss, disabled mode, 100/1000/10000 entries, vector dimensions and concurrency. Track both added latency and answer equivalence; never market a cache speedup obtained by returning an incompatible response.

### 5. Persistent cache commits block other cache users

**Confirmed lock placement; benchmark before changing storage.** `ResponseCache.set()` holds the cache lock while eviction commits and insert commits run (`router.py:2472`). Expired `get()` entries commit deletion under that same lock (`router.py:2456`). JSON serialization and a linear cosine scan also occur in locked paths. On a slow disk, one write can delay other exact/semantic cache operations.

Smallest next step: benchmark default in-memory mode versus opt-in SQLite mode on actual supported hardware. For persistence, combine eviction/upsert into one transaction first. If measurements justify more work, serialize immutable data outside the cache lock and separate database synchronization without violating eviction ordering or restart durability. Do not add a queue/database dependency merely to hide latency; failure and shutdown semantics must remain explicit.

Must reach: cache get/set/delete/upsert/init, persistent storage tests and documented durability. Default max size is 100, so vector indexing is not justified without workload evidence.

### 6. Probes are sequential and share mutable routing data

**Confirmed structure; startup does not block serving.** One background thread serially validates providers and their models (`router.py:1932`, `router.py:2150`). Individual probes take up to 12 seconds and may include multiple calls. It builds new state then swaps global state, while also changing provider model lists and credential buckets during discovery. A slow early provider delays capability confirmation for later ones; strict tool loops depend on confirmed capabilities.

Smallest complete improvement: bounded per-provider probe concurrency, retaining the existing cached-state fast path and applying complete provider snapshots safely. Expose “checking”, “ready” and “unavailable” rather than presenting absent probe results as definitively unsupported. Keep probes budget-aware; they consume real upstream completions.

Must reach: startup/discovery, shared provider/model state, credential-pool updates, status API and discovery tests. Measure time to usable first provider and time to complete capability readiness, not merely process start.

### 7. Latent deadlock in an unused stats convenience method

`ProviderStats.all_summaries()` holds `self.lock` and calls `summary()`, which acquires that same non-reentrant lock (`router.py:2243`, `router.py:2230`). An isolated AST-only self-check reproduced the blocked call after adding one sample. Repository search found no caller, so this is a confirmed latent defect, not evidence of a current dashboard stall. Remove the unused helper or avoid nested acquisition before a new metrics endpoint starts using it. A populated-map regression test with a bounded watchdog is sufficient.

### 8. Connection pooling is present; verify isolation before increasing it

Installed `HTTPAdapter` defaults to `pool_block=False`; the explicit construction at `router.py:60` does not override it. `pool_maxsize` therefore is not itself a concurrency admission limit. Raising it blindly will not fix overloaded inference workers. The Session is shared by provider traffic, token refresh, and operator-configured instance probes; it also carries mutable session state. A code comment calling the entire Session “thread-safe” is not a proof of credential/cookie isolation.

First test concurrent credential headers, cookie behavior and connection reuse against local fake hosts. Keep headers request-local and avoid cross-account cookie persistence. Only change client ownership/pool limits when the test reveals a requirement. Bound incoming/queued work separately and preserve responsive health/status under streaming load.

## Baseline verification performed

- No repository `AGENTS.md` found at the project root by the audit read. Parent has the applicable session instructions.
- `python -B -m pytest -q -p no:cacheprovider --ignore=D:/Hermes-router/tests/test_dashboard_playwright.py D:/Hermes-router/tests`: **21 passed in 4.55 seconds**, Python 3.13 on this Windows environment.
- An initial relative `--ignore=tests/test_dashboard_playwright.py` failed collection because the test bootstrap changes the working directory. The absolute ignore path fixed collection. Playwright is not installed in this interpreter; browser tests were not run or installed by this audit.
- `tests/conftest.py` switches imports to a temporary directory, clears known provider credentials and redirects auth/state/cache. Tests did not use the operator's real `.env` or auth file.
- AST-only `ProviderStats` watchdog confirmed the nested-lock deadlock; no router import or provider request was needed for that check.
- CI declares Python 3.10/3.12, shell syntax checks, pytest and Chromium/Playwright (`.github/workflows/router-smoke-tests.yml:20`). Local Python 3.13 results do not replace that matrix.

## Fair performance comparison plan

### Measured synthetic instance-list baseline

A fresh `python -B` process ran `tests/conftest.py` with `runpy`, imported the router, and asserted `router.PROVIDERS` was empty. `_read_instances_doc` returned 1/5/10 in-memory Docker-mode entries. `_probe_instance` and `_docker_state` were replaced with functions that each sleep 10 ms and return fixed success metadata. The Flask test client called authenticated `GET /v1/instances` three times per size; every response was 200 with the expected entry count. Assertions confirmed exactly one health and one Docker probe per entry per request. No network, Docker, real credentials or production registry was used.

| Mock instances | Samples (ms) | Median (ms) |
|---|---|---|
| 1 | 21.52, 20.86, 21.11 | 21.11 |
| 5 | 104.35, 104.04, 103.42 | 104.04 |
| 10 | 207.81, 209.99, 207.70 | 207.81 |

This confirms additive serial work: the constructed minimum is `instances × 2 × 10 ms`. It is deliberately a synthetic delay experiment, not a production benchmark or a comparison with OmniRoute; it bypasses HTTP-server scheduling and actual socket/subprocess overhead. Real configured timeouts and slower instances can make the effect much larger. The ad hoc experiment changed only process-local objects and did not add product code.

### Comparative load benchmark to implement

Use a pinned Hermes revision and pinned OmniRoute revision, same machine, same upstream fixture, same model/keys, same concurrency and cache settings. Do not compare network-varying live models to claim router speed. Start with an OpenAI-compatible fake upstream that produces fixed JSON or paced SSE, then failure scenarios.

Workload matrix:

| Scenario | Inputs | Observe |
|---|---|---|
| Baseline proxy | 1/8/32 concurrent callers, short/long payloads, streaming and JSON | overhead versus direct fixture, first-token and total p50/p95/p99, CPU/RSS |
| Failover | first target 429/503/timeout, second succeeds; all fail | time to fallback, socket cleanup, retries, deadline |
| Cache | cold/warm exact, opt-in semantic, SQLite on/off | correctness, provider calls avoided, lock contention |
| Operational load | multiple dashboard tabs, slow instances, long streams | inference latency, health/status latency, bounded poll count |
| Routing scale | 1/10/50 providers, multiple models/keys | candidate ranking overhead, fairness, quota correctness |
| Cancellation | client leaves before/after first token | upstream connection release, accounting, worker recovery |
| Compatibility | fragmented SSE, tool arguments, usage blocks, empty/error output | complete and correctly shaped responses, no lost events |

Run warmup plus repeated timed samples and publish machine/version/settings with raw measurements. Report throughput only alongside error rates. Keep CI deterministic with local fakes and a modest smoke threshold; host-specific latency gates should run on controlled hardware. Do not claim “faster than OmniRoute” until those comparable measurements exist.

## Scope recommendation

Start with dashboard polling isolation, response cleanup/deadlines, and stream accounting; they improve perceived responsiveness and make future performance claims measurable. Preserve the existing lightweight deployment. An async rewrite, vector database, extra service, and large configuration surface are not prerequisites for these fixes.

Skipped live provider benchmarking and browser execution. The findings establish code paths and risks, not measured production speedups or full security assurance.
