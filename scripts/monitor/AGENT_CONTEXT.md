# scripts/monitor Agent Context

This package owns the live MLB Polymarket O/U monitor: schedule refresh, event /
market discovery, CLOB book polling, and append-only JSONL recording. It is the
base layer the trading runtime subclasses — `LiveTradingEngine` /
`SignalEngine` extend `MLBPolymarketMonitor` and override `_on_tick_batch`.
Touch lightly: a regression here (missed tick, dropped recorder handle, schedule
glitch handling) ripples directly into trade decisions and audit trails.

Last checked against the active `scripts/monitor/*.py` files: 2026-05-09
(post-monitor-refactor — single 1,861-line file split into 10 focused modules,
all under 1,000 lines).

## Runtime Dataflow

`MLBPolymarketMonitor.run()` is a single loop with three independent timers:

1. **Schedule refresh** (default every 30s): `MLBStatsClient.fetch_schedule_payload`
   pulls `statsapi.mlb.com/api/v1/schedule` with `hydrate=team,linescore,venue`.
   `parse_games` produces one `ScheduledGame` per `gamePk`, stamps current pitcher
   ERA from the local pitcher cache, carries linescore inning-run arrays for
   downstream scoring-path shadow/modeling features, and the recorder writes the raw JSON under
   `data/polymarket/mlb_ou/schedules/`. Weather temp/wind are intentionally not
   taken from StatsAPI; live FV uses `cache/weather/game_weather_<date>.json`
   Weather v2 rows instead.
2. **Polymarket discovery** (default every 120s): `PolymarketDiscoveryClient`
   resolves each `ScheduledGame` to a Gamma event slug. Strategy 1 = deterministic
   `mlb-{away}-{home}-{date}` slug guesses; Strategy 2 = `/events?query=` keyword
   fallback filtered by `±2 day` proximity. Each match yields one
   `GameMarketMatch` containing `OUMarket` rows for every line on the event.
   Markets with `deploying=True` (Polygon contract not yet live) are kept but
   re-checked each cycle; once any market goes live the per-game backoff resets.
3. **Active-game sync + poll** (default every 2.5s): `_sync_active_games` flips
   the per-game polling bit when a game enters Live (or Preview, with
   `--start-on-preview`); STOP closes recorder handles and clears stale-book
   failure state. `_poll_once` dispatches one `PolymarketBookClient.fetch_book`
   per `(game, market, side)` over a persistent `ThreadPoolExecutor`, applies
   stale-book retirement bookkeeping, builds a tick payload, and calls
   `_on_tick_batch(tick_batch)`. The hook is a no-op in the base class; trading
   subclasses run gates, FV inference, and order placement here.

The loop exits when `--once`, `--run-seconds`, or `_all_games_done()` is true.
Final shutdown drains the executor (`wait=False` — futures already collected),
emits a compact `MONITOR ROLLUP` summary, and closes recorder handles.

## Subclass Contract

Trading engines (`scripts/trading/live_engine.py`, `signal_engine.py`) subclass
`MLBPolymarketMonitor` and rely on these guarantees. Do not break them without
coordinating updates in `scripts/trading`:

- `_on_tick_batch(tick_batch)` is called every poll cycle, even when the cycle
  produced **zero** records. Subclasses use the empty-batch tick for settlement,
  fill polling, ask-reversal checks, and FV-decay cancellation. There is also an
  explicit final-call before `run()` exits when all games are Final.
- `tick_batch` is a list of `(ScheduledGame, OUMarket, side, payload)` tuples.
  `payload` is the same dict that was just appended to the JSONL recorder, so
  subclasses can re-derive everything without touching disk.
- `self.games`, `self.matches`, `self.active_games` are kept current before each
  tick batch fires. Subclasses read these directly.
- `ScheduledGame.is_final()` requires inning >= 8 (or unknown) AND a final
  status — guards against transient MLB API "Final" glitches in early innings.
  This invariant has fired in production; do not relax it.
- `MLB_AVG_ERA = 4.20` is the fallback when the pitcher cache is missing or a
  pitcher has no qualifying stats. Gate 8i only fires when ERA is **below** its
  threshold, so the default safely disables the gate during data outages.
- `parse_args` lives in `monitor_cli.py` and is re-imported by
  `signal_config.py` so live/paper engines inherit every monitor flag.

## File Ownership

- `monitor_mlb_polymarket_ou.py`: orchestration core + CLI entry point. Owns
  `MLBPolymarketMonitor` (`__init__`, `_refresh_schedule`, `_discover_markets`,
  `_sync_active_games`, `_build_poll_jobs`, `_poll_once`, `_on_tick_batch`,
  `_log_monitor_rollup`, `run`), `main`, and a backward-compat re-export block
  (`__all__`) that keeps every legacy import path working for trading scripts
  and the test suite.
- `monitor_constants.py`: URLs (`MLB_SCHEDULE_URL`, `GAMMA_BASE`, `CLOB_BASE`),
  default paths and timeouts, `TEAM_SLUGS`, `OU_LINE_RE`, status-set constants
  (`LIVE_STATES` / `PREVIEW_STATES` / `FINAL_STATES`), debug-cadence ints
  (`POLL_PROGRESS_DEBUG_EVERY_N_CYCLES`, `SCHEDULE_*_DEBUG_EVERY_N_REFRESHES`,
  `DISCOVERY_NO_NEW_INFO_EVERY_N_CYCLES`), pitcher-cache thresholds
  (`PITCHER_CACHE_MAX_AGE_HOURS` / `PITCHER_CACHE_STALE_FALLBACK_MAX_AGE_HOURS`
  / `PITCHER_CACHE_MIN_PITCHER_COUNT`), `MLB_AVG_ERA = 4.20`, the shared
  `LOGGER`, `NOISY_LIBRARY_LOGGERS`, and `suppress_noisy_library_loggers`. No IO.
- `monitor_utils.py`: `_safe_float`, `_safe_int`, `_normalize_slug_piece`,
  `_game_dir_name`, `_now_iso`. Pure helpers; do not add IO or logging here.
- `monitor_system.py`: `_setup_performance_mode` (P-core pinning + HIGH process
  priority via psutil; auto-detects i7-12700K, falls back to a warning if psutil
  is missing or affinity fails) and `_prevent_sleep` (Windows
  `SetThreadExecutionState` so the monitor does not sleep mid-game; no-op on
  non-Windows; cleared via `atexit`). Both fail open with a warning.
- `monitor_models.py`: dataclasses `ScheduleScore`, `ScheduledGame` (with
  `is_live` / `is_preview` / `is_final` predicates and the inning>=8 final
  guard), `OUMarket` (with `deploying` flag), `GameMarketMatch`. These are the
  canonical types crossed by trading code and tests; field shapes are public
  contract — bump tests in lockstep.
- `monitor_recorder.py`: `LocalRecorder` writes `mlb_schedule_*.json`,
  per-game `meta.json`, `market_map.json`, and append-only
  `ou_<line>_<side>.jsonl` snapshot files under
  `data/polymarket/mlb_ou/<date>/<away>_at_<home>_<game_pk>/`. One open handle
  per `(game_pk, line, side)` keyed in `_file_handles`; per-game `threading.Lock`
  serializes appends across the polling thread pool. `flush()` after every line
  guards against tick loss on crash. `close_game(game_pk)` releases handles when
  a game goes inactive; `close_all()` runs in the `run()` finally block.
- `monitor_stats_client.py`: `MLBStatsClient` owns three concerns:
  - HTTP session config (User-Agent, `pool_maxsize=20` to match `max_workers`).
  - `fetch_schedule_payload` with a 2-attempt short retry budget (fast +
    bumped timeout) so schedule glitches never freeze the polling loop. Final
    failure re-raises so `run()`'s WARN path runs and `_schedule_refresh_error_count`
    increments.
  - `_load_or_rebuild_pitcher_cache` / `_build_pitcher_cache` /
    `_validate_stale_pitcher_cache` — TR14 hardened: rebuild when the cache's
    `built_at` date does not match the local run date OR age > 24h; if rebuild
    fails but a same-season cache <= 72h old with >= 50 pitchers exists, fall
    back to it so Gate 8i stays active during brief StatsAPI outages. Build
    retries 3x with 15s/30s/60s timeouts.
  - `parse_games` enriches each `ScheduledGame` with venue, weather, and the
    current defensive pitcher's ERA from the cache.
- `monitor_discovery.py`: `PolymarketDiscoveryClient` queries
  `gamma-api.polymarket.com/events`. `_extract_ou_markets_from_event` parses
  `clobTokenIds` (a JSON-encoded string), extracts O/U line via `OU_LINE_RE`,
  and tracks `deploying = True` when the market is `deploying=true` OR
  `active=false`. Slug collisions are blocked by the orchestrator BEFORE
  `self.matches` is updated.
- `monitor_book_client.py`: `PolymarketBookClient` fetches CLOB
  `/book?token_id=...`. On 404 with a known `market_id`, falls back to Gamma
  `/markets/{market_id}` `outcomePrices` and synthesizes a 2c-spread
  bid/ask around the outcome mid. `source` field on the returned dict is
  `"clob"` or `"gamma"`; Gamma boundary prices (0/1) are returned with
  `error="gamma_invalid_price:..."` and treated as retirable failures by the
  orchestrator. Thread-local `requests.Session` so the persistent ThreadPool
  keeps connections warm across poll cycles.
- `monitor_cli.py`: `parse_args`. Lifted out so `signal_config.py` can import
  the parser without dragging in the orchestrator. Validates
  book-failure-cooldown invariants at parse time.

## Stale-Book Retirement (per-`(game_pk, line, side)` cooldown)

Late in games, CLOB returns 404 and Gamma returns invalid boundary prices for
markets that are effectively settled. Polling these is pure noise and burns
HTTP budget. The orchestrator tracks consecutive **retirable** failures —
`gamma_invalid_price:*` always, and `http_404` only when inning >= 9 AND
`inning_state == "end"` AND outs >= 3 (avoids retiring pre-game "not deployed
yet" 404s) — and after `--book-failure-retire-streak` (default 6) consecutive
failures, retires the key for `cooldown * 2^(retire_count - 1)` seconds, capped
at `--book-failure-max-cooldown-secs` (default 1800s). A successful or
non-retirable response resets the streak. The first retirement per key logs at
INFO; subsequent retirements log at DEBUG and accumulate into the
`_book_fail_retire_rollup` printed by `_log_monitor_rollup` at shutdown.

## Observability

- `LOGGER` name = `"mlb_poly_monitor"`. Tests in
  `tests/test_observability_noise_rollups.py` pin the cadence constants and
  the noisy-library suppression list.
- Schedule churn is rolled up: first refresh logs at INFO, subsequent state
  changes increment a Counter that emits one DEBUG line per
  `SCHEDULE_CHANGED_DEBUG_EVERY_N_REFRESHES`. Unchanged refreshes are silent
  except every `SCHEDULE_UNCHANGED_DEBUG_EVERY_N_REFRESHES`.
- Discovery cycles that attempt games but find no new mappings increment a
  separate counter that emits one INFO every `DISCOVERY_NO_NEW_INFO_EVERY_N_CYCLES`.
- Poll progress logs at DEBUG every `POLL_PROGRESS_DEBUG_EVERY_N_CYCLES`.
- Run shutdown emits `=== MONITOR ROLLUP ===` with refresh / change / failure /
  poll-cycle / tick-snapshot / discovery / retirement counters, plus the top
  schedule-state-change buckets and the worst retirement rows.

## Refactor Priorities

- Done (2026-05-09): `monitor_mlb_polymarket_ou.py` split from one 1,861-line
  file into 10 modules, all under 1,000 lines (largest is the orchestrator at
  831 lines). Backward-compat re-exports at the bottom of the main file keep
  every legacy import path working for `signal_engine`, `signal_pipeline`,
  `candidate_logging`, `capture_helpers`, `signal_config`, and the two monitor
  test modules.
- Next (low priority): `MLBPolymarketMonitor` is still the single largest file
  in the package. The book-failure-retirement state (~5 dicts + 5 helper
  methods) could move into a `monitor_book_failure_tracker.py` if the
  orchestrator grows further. Hold off until there is a real reason — splitting
  shared mutable state across modules is harder than it looks.
- Next: schedule-refresh and discovery state-change rollup counters are nearly
  identical patterns. A small `monitor_rollup_counter` helper could
  deduplicate, but the current code is only ~60 lines total — not worth the
  abstraction yet.

## Safe-Edit Checklist

- Do not remove or rename anything in the `__all__` block of
  `monitor_mlb_polymarket_ou.py` without also updating
  `scripts/trading/{signal_engine,signal_pipeline,signal_config,candidate_logging,capture_helpers}.py`
  and the two monitor tests. Trading code imports a mix of dataclasses,
  constants, helpers, and clients directly from this module name.
- Do not change `_on_tick_batch`'s call cadence (every cycle, including empty
  batches and the final-before-exit call). Trading subclasses depend on it for
  settlement and order lifecycle.
- Do not weaken `ScheduledGame.is_final()`'s inning >= 8 guard — it exists to
  catch transient MLB API "Final" glitches in early innings that have caused
  bad shutdowns in production.
- Do not cache schedule payloads across the run-date boundary. The pitcher
  cache rebuild key is the local run date; schedule reads are always live.
- Do not log noisy DEBUG per tick. Add to the existing rollup counters
  (schedule changes, discovery no-new, book-failure retirement) instead.
- New modules in this package should follow the existing import convention:
  use `from monitor_<x> import ...` (the monitor folder is on `sys.path` via
  test/trading bootstrap); do not switch to package-relative imports without
  also updating callers and the `sys.path` bootstrap in tests.
- Run `python -m compileall -q scripts/monitor scripts/trading` and the full
  pytest suite (`tests/`) before handing off. Monitor changes most often break
  `tests/test_monitor_pitcher_cache.py`, `tests/test_observability_noise_rollups.py`,
  and the trading characterization tests that import `ScheduledGame` /
  `OUMarket`.
