# Promotion lag: promote-time vs effect-time

When you run `python scripts/analysis/promote.py <lever>`, the file
changes on disk immediately — but **the live engine doesn't pick up
the new file until it boots its next session**. This doc explains the
gap per lever and tells you how to confirm whether your promote is in
effect.

If you're skimming: look for `promotion_lag_health` in the daily
human-review JSON or any `Promotion-lag:` line in the Notes block at
the top of the markdown report. That block answers "is my promote in
effect yet?" for every lever without you needing to check anything
manually.

## The 5 promote.py levers

| Lever | What promote does | What the engine loads | Effect time |
|---|---|---|---|
| `stage1` | atomically swaps `cache/mlb_ou_cache.json` | the Stage-1 Poisson cache (loaded at boot by `signal_engine.SignalEngine.__init__`) | next engine boot |
| `stage2` | atomically swaps `cache/mlb_stage2_run_env.json` | the Stage-2 park/weather residual model (loaded at boot) | next engine boot |
| `stage3-v2` | atomically swaps `cache/team_offense_v2_weights.json` | the Stage-3 v2 weights (loaded at boot) | next engine boot |
| `stake-scaling` | mutates `cache/live_engine_overrides.json` | the runtime-overrides config (read at boot) | next engine boot |
| `gate-threshold` | mutates `cache/live_engine_overrides.json` | the runtime-overrides config (read at boot) | next engine boot |

**All 5 take effect on next session boot.** None hot-reload during a
running session.

## How the tracker decides "is it in effect?"

The block in the daily review (`promotion_lag_health`) compares each
lever's cache mtime against the most recent engine boot, proxied by
the first bet's `placed_at` timestamp from the latest session file
(across both `data/live_trading/sessions/` and
`data/paper_trading/sessions/`).

For each lever it emits one of these statuses:

- **`effective_in_runtime`** — cache mtime is at-or-before the last
  engine boot, so the engine already loaded this version. Promote is
  live. `lag_hours` reports how long the new cache existed before the
  engine picked it up (informational only).
- **`pending_next_session_boot`** — cache mtime is after the last
  engine boot. The promote will take effect on the next restart.
  `lag_hours` reports how many hours have elapsed since the promote.
  When `lag_hours > 24` an alert fires under the `Promotion-lag:`
  Notes prefix.
- **`cache_missing`** — the lever's cache file does not exist. This
  is the state for stage1/stage2/stage3-v2 levers that have never
  been promoted from staging.
- **`no_session_history`** — no session files exist under either
  trading root. Fresh install / first-day operator. Cannot evaluate
  effect-time; no alert.
- **`check_error`** — a filesystem error reading the cache mtime.
  Surfaced for diagnostic purposes; never blocks the daily review.

## Two levers sharing one file: stake-scaling + gate-threshold

`promote.py stake-scaling` and `promote.py gate-threshold` both mutate
`cache/live_engine_overrides.json`. Any promote of either lever bumps
the same file mtime, so both levers report the same lag status. We
surface them separately so an alert filed under the lever name you
actually promoted is easy to grep for. If you promoted `stake-scaling`
and see an alert under `gate-threshold` too, that's expected — same
underlying file.

## How to clear a `pending_next_session_boot` status

1. Stop the live engine (`Ctrl-C` or whatever your session-management
   wrapper uses).
2. Restart the engine. It'll re-read every cache + overrides file at
   boot, picking up your promote.
3. The next daily review will flip the lever to `effective_in_runtime`
   and the `Promotion-lag:` alert will disappear.

## What this doesn't catch

- **Bugs in the engine's cache-loading path**: if the engine boots but
  reads the wrong path (e.g., loads the prior backup instead of the
  promoted production), the tracker can't tell — it only sees file
  mtimes. Use `cache_lineage_freshness_health` (loads the actual
  artifact + reads its embedded lineage block) to cross-check that
  the engine is loading the version you promoted.
- **Mid-session hot-reload attempts**: none of today's 5 levers
  hot-reload during a running session. If you need a hot-reload
  semantic, that's a roadmap item, not a bug.
- **Engines that boot but place zero bets**: the proxy uses the
  first-bet `placed_at` from the latest session. An engine that
  booted but placed no bets that day falls back to the session's
  `generated_at` (end-of-session write time) — less precise but
  still a valid signal that the engine was running.

## Related blocks in the daily review

- **`cache_lineage_freshness_health`** — surfaces each cache's
  embedded `built_at_utc` + `git_sha` + `builder_path` (the V3
  lineage block). Use to confirm WHICH version a cache is, not just
  WHEN it was built.
- **`cross_artifact_consistency_health`** — surfaces stale
  upstream-input relationships between artifacts. Use to spot
  "calibrator was built against a Stage-1 sha that the production
  cache no longer carries."
- **`stage1_alt_a_staging_health`** — surfaces existence + age +
  override-stats of the Stage-1 Alt-A staging cache (the prepared
  candidate that `promote.py stage1` would swap into production).
