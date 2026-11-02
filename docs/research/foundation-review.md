# Independent foundation review

Reviewed the uncommitted foundation against `cbfe18e` on 2026-11-02. This is a separate reviewer pass over authentication, admission/accounting, routing callers and migration surfaces. No provider credentials or external model calls were used. Findings below describe the tree at discovery; the controller is applying fixes and owns final verification.

## Findings requiring fixes

1. **High: embeddings inherit a previous request deadline.** `router.py`'s `embeddings()` ingress did not initialize `_req_ctx.deadline`, although `forward_embeddings()` now calls `_upstream_timeout()`. A reused Waitress worker whose preceding chat deadline has expired rejects all subsequent embedding attempts before sending them. Isolated reproduction: load `tests/conftest.py`, configure a fake embedding provider/pool, set `router._req_ctx.deadline = time.monotonic() - 1`, mock `_HTTP.post`, and POST `/v1/embeddings` with the test app key. Observed HTTP 503 and **zero outbound calls**. Initialize a fresh request deadline at embeddings ingress and stop further attempts when its cumulative budget expires.

2. **High: token-only quotas admit protocols that do not enforce the reserved output bound.** `_budget_candidate_allowed()` returned early when there was no money cap, allowing Codex and Anthropic under `tokens_per_day`. With `max_completion_tokens: 1`, `_output_bound()` reserves one output token, but `_to_codex_body()` sends no output limit and `_to_anthropic_body()` sends `max_tokens: 1024`. Isolated reproduction under `{tokens_per_day: 100000}` returned `allowed: true` for both candidates, with the mismatching outbound limits. Gate capped candidates on the actual protocol's enforceable bound; either reject unsupported paths or correctly translate the bound. Cover both token-only and money policies with regression tests.

3. **Medium: the feature CLI retained the old administrator credential.** `scripts/features.sh` read `PROXY_API_KEYS` for `/v1/status`, which now requires `ADMIN_API_KEY`. Consequently `hr features list/enable/disable` failed with 401 after migration. The controller has already changed the script while this review was running; retain a regression check for the admin credential and custom `HR_ENV_FILE` path.

4. **Medium: metrics authentication was still presented as an optional toggle.** `/metrics` correctly requires `_admin_check()` unconditionally, but `_features_snapshot()` and the endpoint docstring still described `METRICS_REQUIRE_AUTH` as an optional flag. Remove the nonfunctional toggle or present the protection as always enabled, and align examples/docs.

## Checks and storage observations

- Application keys are rejected by privileged endpoints; owner credentials are not inference keys. Session mutations check CSRF and origin, sessions expire, and forwarded HTTPS handling is explicitly opt-in.
- `usage_store.KeyUsage._connect()` explicitly closes every SQLite connection. The earlier connection-leak suspicion is not present in this tree.
- SQLite `BEGIN IMMEDIATE` serializes admission checks and reservations; settlement updates only an unsettled row. Pending reservations survive restart. Scope snapshots and cache namespace policy isolate restrictive policy changes for subsequent requests.
- Authorization mutations use a process lock and atomic replacement of each file. `.env` and `auth.json` are not one filesystem transaction. With existing `access_keys` state, `auth.json` remains authoritative at restart, so a failure after the `.env` write does not silently revoke/reactivate keys. A failed first key creation can leave a generated key in the environment file before its policy metadata/access list is committed; this is not an acknowledged successful operation. A follow-up could consolidate the duplicated app-key persistence, but no exposed-secret or false-success authorization bug was established in this failure window.
- `snapshot()` loads a key's lifetime rows into Python, and the rolling-minute admission query has no `(key_hash, created)` index. These are growth risks for a long-lived busy installation; the instance-list benchmark does not measure them. Prefer SQL aggregates and the appropriate index before claiming sustained routing-throughput improvement.

## Verification boundaries

Reproductions used the isolated test bootstrap and mocked HTTP, without implementation edits. The full suite, browser/build checks and final regression tests belong to the controller after all concurrent edits land. Docker runtime, Linux service behavior, live upstream billing and competitor throughput were not exercised by this review.

## Fix re-review

The follow-up tree initializes a fresh embeddings deadline, rejects Codex under either token or money caps, maps Anthropic `max_completion_tokens`, migrates the feature CLI to the administrator credential/custom environment path, and makes metrics protection a built-in feature. SQL snapshots now aggregate in the database, and the rolling request query uses a `(key_hash, created)` index. Independent targeted execution of `test_foundation_review.py`, `test_usage_store.py`, and `test_installation.py`: **23 passed in 7.54 seconds**.

One remaining quota edge was reported immediately: the Anthropic forwarding branch returns before the generic output clamp. An owner-configured `ANTHROPIC_MAX_OUTPUT_TOKENS=1` reduces `_output_bound()` to 1, while a client requesting `max_completion_tokens:1000` is still forwarded with `max_tokens:1000`. The actual Anthropic wire body must honor the same configured cap.

Native installer/service checks confirm explicit command-failure propagation, existing-config preservation, quoted systemd paths, custom environment/port health checks, truthful lingering warnings, and retention of a unit when stop/disable fails. PowerShell success and failure harnesses exercise dependency validation and configuration preservation. Actual Windows Task Scheduler and Linux systemd operation remain untested on those hosts.

Two pre-existing restart/setup interactions matter to the newly documented native-server flow and were sent to the controller: `restart.sh` handles system units but not user units, and its broad fallback `pgrep -f router.py` can target another installation; `setup.sh` starts a standalone process before offering service installation, so a service startup can collide on the same port while `/health` succeeds against the old process. Correct the caller behavior or avoid the unsafe helper in the documented path. These observations are not covered by the 23-test result above.

The Anthropic cap follow-up is now verified: `forward()` clamps the Anthropic wire body's `max_tokens` before the HTTP call. The five review regressions pass independently (**5 passed in 0.95 seconds**), including the mocked wire-body assertion. All four original review findings and the remaining protocol-bound edge are resolved. Native restart/setup follow-up verification is recorded separately below when available.

### Final disposition

The native follow-up handles both system and user systemd units. Standalone restart now checks a numeric tracked PID, verifies that its Python command names this installation, and refuses an occupied untracked router port; the broad process-name kill and forced kill fallback are gone. Setup stops only the child it started before handing over to a service and propagates service-install failures. Both helpers now read the selected environment file's port. Independent rerun of the final installation suite: **29 passed in 9.34 seconds**, including the seven restart/setup regressions.

**No unresolved material finding remains from this review.** A router started directly in the foreground has no managed PID file; automatic restart can therefore refuse it safely, requiring Ctrl+C and a manual start or the service manager. This behavior is being documented by the controller. Test harnesses mock process/service operations, so actual systemd, Task Scheduler and Docker runtime validation remains a release limitation rather than a tested claim. The controller owns the final full-suite run and deployment verification.
