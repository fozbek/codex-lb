## Overview

The query-caching capability is broader than cache TTLs. It also owns the database query shapes that sit on hot request and dashboard paths, especially when SQLite is the default backend.

## Decisions

- Keep the public request-log and usage APIs unchanged; optimize query shape and indexing underneath them.
- Preserve legacy `usage_history.window IS NULL` semantics as `"primary"` instead of forcing a data backfill in this change.
- Avoid related-table joins on request-log listing unless search actually needs `accounts.email` or `api_keys.name`.
- Avoid full window-ranking scans on hot selector and dashboard aggregate reads; prefer grouped latest-id or PostgreSQL `DISTINCT ON` shapes backed by matching indexes.
- Keep hot-path index migrations idempotent so manual production hotfix indexes do not break later schema upgrades.

## Cross-Replica Cache Invalidation Bus

- The bus is the `cache_invalidation` table (`namespace` TEXT PK, `version` INTEGER) plus one
  `CacheInvalidationPoller` per process (`app/core/cache/invalidation.py`, default poll 0.5s).
  Mutations bump a namespace's version with a dialect-atomic upsert; every process compares
  versions each poll and runs registered callbacks on change.
- Registered namespaces and their callbacks (wired in `app/main.py`):
  - `api_key` -> `ApiKeyCache.clear` (fallback TTL 2s)
  - `firewall` -> `FirewallIPCache.invalidate_all` (fallback TTL `firewall_ip_cache_ttl_seconds`, default 30s)
  - `account_routing` -> `RoutingAvailabilityCache.refresh_from_db` (snapshot of `accounts.id -> status`; no TTL — the snapshot is authoritative once seeded, degraded local-set semantics when unseeded)
  - `account_selection` -> `AccountSelectionCache.invalidate(propagate=False)` (fallback TTL 5s)
  - `settings` -> `SettingsCache.invalidate(propagate=False)` (fallback TTL 5s)
- Two bump flavors: `await bump(namespace)` (durable before the mutation response; used by
  security-bearing endpoints: settings/dashboard-auth mutations, account pause/reactivate/delete,
  OAuth re-auth) and sync `request_bump(namespace)` (coalesced into a pending set flushed at the
  start of each poll cycle; used on hot/scheduler paths). Coalescing bounds writes to <=1 per
  namespace per poll interval; worst-case cross-replica convergence is flush (<=0.5s) + peer poll
  (<=0.5s) ~= 1s for coalesced bumps and one poll interval for awaited bumps.
- Failure semantics: `bump()` retries transient lock errors (3 attempts, 0.05s base backoff); a
  final failure logs ERROR and increments
  `codex_lb_cache_invalidation_bump_failures_total{namespace}` but never fails the mutation —
  peers then converge via the cache's fallback TTL. Failed coalesced flushes stay pending and
  retry next cycle. Poll failures escalate to WARNING after 3 and ERROR after 10 consecutive
  failures and increment `codex_lb_cache_invalidation_poll_failures_total`.
- Poller callbacks must be registered with non-propagating variants — a propagating callback
  would re-bump on every observed bump and loop.
- Routing-unavailable derivation: an account is routing-unavailable when the snapshot says
  PAUSED / REAUTH_REQUIRED / DEACTIVATED, or the id is absent (deleted), or a local mark overlay
  entry exists (covers the window before the accompanying status write commits). RATE_LIMITED and
  QUOTA_EXCEEDED deliberately do not map to unavailable, preserving cooldown-state bridge-session
  reuse. Bridge-session reuse checks stay pure in-memory: zero per-request DB reads.

## Operational Notes

- Primary-window usage reads should normalize on `coalesce(window, 'primary')`.
- Latest usage selection should be backed by a composite latest-row index, not by Python-side deduplication.
- Default request-log listing should sort by latest-first timestamp and tie-breaker ID.
- Do not hold the load-balancer runtime lock across network-bound usage refresh calls; only protect the in-memory selection and runtime-state mutation step.
- Stale usage refreshes should collapse into one in-flight refresh per account, with followers re-checking persisted primary-window data before calling the upstream usage API again.
- On 2026-06-29, production `10.0.0.113` saw Postgres backend OOM kills while dashboard/account-selection requests ran large `request_logs` and `additional_usage_history` window-ranking queries. The durable mitigation is to keep additional-quota latest lookups and account request usage summaries off `row_number()` hot paths, then restore any temporary production registry/timeout workarounds after deployment verification.

## Example

These rows must both participate in a primary-window lookup:

```text
usage_history(window=NULL, account_id='acc_1', recorded_at='2026-03-08T10:00:00Z')
usage_history(window='primary', account_id='acc_1', recorded_at='2026-03-08T11:00:00Z')
```

`latest_by_account("primary")` should return only the later row while still treating both rows as part of the same logical primary window.
