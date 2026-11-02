# Foundation routing and instance responsiveness results

Date: 2026-11-02. Scope: approved foundation Task 2, using isolated fake providers and Docker probes. No real upstream requests, Docker operations, credentials, operator configuration, or dependency installations were used.

## Implemented behavior

- Instance serialization reads a snapshot and schedules background work; it performs no HTTP/Docker check in the request thread. Four daemon checks at most run concurrently, with no unbounded job queue and no duplicate in-flight check for a configuration identity. Health/model HTTP checks retain 1.5-second inactivity timeouts; Docker inspect has a five-second subprocess deadline. Pending entries are scheduled on subsequent list reads when slots are available.
- `instances[].live` and `docker` remain present. `freshness` adds `state` (`pending`, `checking`, `fresh`, `stale`), `age_seconds`, and `checked_at`. A snapshot is fresh for 15 seconds. Create/update/action responses may show `unknown`/`checking` until the first check completes. Existing secrets stay masked.
- Probe identity includes registry path, instance ID, full configuration digest, and mutation generation. Writes invalidate changed/deleted entries; Docker actions invalidate before and after the command. A completing worker verifies the current identity under the registry lock before publishing. A deleted/recreated ID cannot accept the old worker's snapshot.
- Every rejected upstream completion response, parsed JSON response, failed Codex aggregation, and completed/failed/cancelled stream closes its response. Explicit stream ownership also covers closing before the first iteration. Both inbound endpoint response-close callbacks close the managed iterator (coordinated with the access implementation).
- OpenAI, Anthropic, and Codex streams carry normalized final usage into one idempotent caller settlement and provider accounting. Cancellation before output releases an unused reservation; partial output without reported usage retains the conservative reservation. Successful empty/usage-only streams do not silently release potentially billed work. Streaming success metrics are recorded on completed output rather than headers.
- `ROUTER_REQUEST_TIMEOUT_SECONDS` (default 180) supplies one monotonic budget across completion failover attempts; subsequent attempts receive remaining transport time. Late responses are rejected with HTTP 504 and closed. Stream iteration checks the same deadline. `SEMANTIC_EMBED_TIMEOUT_SECONDS` (default 3) supplies a shorter embedding budget within that request budget.
- Exact caching remains the first lookup. Semantic entries additionally require compatible full request parameters/history/system constraints, caller namespace, vector dimensions, and embedding provider/base URL/model/header identity. Only the final user text may vary. Persisted legacy unlabelled vectors remain exact-only. Internal embedding responses close deterministically, provider/model restrictions apply, and token/cost-limited callers skip internal semantic embedding work.
- Removed the nested-lock deadlock in `ProviderStats.all_summaries()` without changing its return contract.
- Capability initialization remains one daemon worker, preserving the existing single writer for provider/model snapshots. `/health` remains public HTTP 200 for process liveness and now reports `initializing`, `ready`, and `readiness`; `/v1/status` also reports `readiness`. Readiness distinguishes initializing, ready, unavailable, and initialization error. Configured ordinary inference can run during a blocked initialization; strict tool loops still require confirmed capability metadata.

## Failure regressions and verification

The initial focused performance run reproduced 13 failing cases before fixes: rejected 429/401/400/413/500/302 response leaks, unstarted stream cleanup, cancellation settlement, cumulative failover budget, Codex aggregation cleanup, semantic request/embedding compatibility, legacy vector behavior, and stats deadlock. Two additional startup tests failed before adding readiness tracking. A deleted/recreated instance regression also failed before adding mutation generations.

Focused tests cover blocked probes, four-worker deduplication, changed/deleted/recreated/action invalidation, stale snapshot presentation, secret omission, all three streaming protocol usage paths, errors/cancellation, embedding identity/failover/scope, and continued serving during initialization.

Commands (absolute paths are necessary because the test bootstrap changes directory):

```powershell
python -B -m pytest -q -p no:cacheprovider D:/Hermes-router/tests/test_instances.py D:/Hermes-router/tests/test_foundation_performance.py
python -B -m pytest -q -p no:cacheprovider --ignore=D:/Hermes-router/tests/test_dashboard_playwright.py D:/Hermes-router/tests
python -B D:/Hermes-router/tests/benchmark_instance_snapshots.py
```

Latest focused result: **32 passed in 1.18 seconds**. Full-suite results are recorded by the integrating controller because access/UI tests are being added concurrently.

## Repeated synthetic benchmark

Reproducer: `tests/benchmark_instance_snapshots.py`. Raw samples: `docs/research/foundation-instance-benchmark.json`. Python 3.13.14, Windows 11 build 26300. Seven samples per size/path, with one mocked 10 ms health check and one mocked 10 ms Docker check per instance. The reference serial path emulates the previous live serializer inside the current authenticated Flask endpoint; it is not a historical revision checkout. The snapshot path includes serialization and background scheduling; warm samples run after all checks finish. Assertions verified exactly one background health/Docker check per instance and at most four simultaneous health checks.

| Instances | Serial reference median (ms) | Cold snapshot median (ms) | Warm snapshot median (ms) | Background checks | Peak checks |
|---:|---:|---:|---:|---:|---:|
| 1 | 21.616 | 0.561 | 0.461 | 1 | 1 |
| 5 | 105.033 | 0.753 | 0.932 | 5 | 4 |
| 10 | 208.911 | 0.999 | 0.873 | 10 | 4 |
| 50 | 1042.023 | 2.417 | 2.129 | 50 | 4 |

This measures removal of serial probe waits from list requests in a controlled mock, not production request throughput, actual sockets, browser paint, Docker performance, or a competitor comparison. Raw samples intentionally retain timing variation.

## Limits and retained roadmap

Requests connect/read timeouts bound inactivity, and the monotonic budget bounds additional retries and checks received chunks. They are not a hard wall-clock interrupt of DNS resolution, an upstream trickling data inside one blocking read, or arbitrary mocked/blocking code. Do not promise strict total cancellation within 180 seconds against those cases; moving transport ownership to a cancellable I/O implementation requires separate measurement/design. The same limitation applies to HTTP instance probes; four stuck workers remain bounded and instance listing still responds, but pending checks cannot progress until a worker returns.

Startup capability discovery remains sequential inside its single background worker, avoiding concurrent writes to existing shared provider/model state. Faster bounded per-provider discovery, cache persistence contention benchmarks, first-token percentile telemetry, real transport leak/stall tests, live provider validation, and fair competitor benchmarking remain future work. No async rewrite, transport dependency, vector database, or paid-call hedging was added.
