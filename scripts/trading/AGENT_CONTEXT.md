# scripts/trading Agent Context

This package is production-like MLB Polymarket Over trading research. Preserve gate
logic unless a task explicitly asks for strategy changes. Prefer observability,
paper/live comparability, and compact analysis-friendly data over raw complexity.

Last checked against the active `scripts/trading/*.py` files: 2026-05-14
(orphan-fill reconciliation, correlated-line exposure cap, calibrated stake
scaling in shadow, live_engine.py split into four impl modules to stay
under the 1,200-line LLM-friendly threshold; inferred and current-state
Stage-1 support/trust diagnostics now flow through candidate/session/live/EV
surfaces).

## Recent changes

_Append dated bullets here when you change anything in this folder.
Mirrors `MASTER_CONTEXT.md`'s "Recent major shifts" pattern; bump
"Last checked" above when you sweep the whole doc._

- **2026-05-29 (Tier-2 data capture)** — two pieces. (1) Schedule-derived
  players (no extra fetch): `signal_context_fields` expanded to carry both
  starting pitchers (+ERAs), current batter, and on-deck (sourced from the
  monitor's `probablePitcher` hydrate + `linescore.offense`); absent fields
  now resolve to **None** (not "") so the candidate writer's None-stripping
  keeps rows clean. Matching `BetRecord` fields added. (2) New
  `game_meta_client.py` (mirrors `weather_client.py`): per-game home-plate
  umpire / officials cache at `cache/game_meta/game_meta_<date>.json`, fetched
  once per game from the boxscore endpoint (too heavy to poll per tick). The
  engine loads it at startup (`_load_game_meta_cache` /
  `_game_meta_fields_for_game`, same pattern as weather) and
  `build_base_candidate_payload` joins `hp_umpire_id`/`hp_umpire_name` onto
  candidate rows by game_pk. Umpire timing caveat: only populated once
  lineups post / game is live (documented in the client). Closes Tier-2 of
  the 2026-05-28 data-capture audit.
- **2026-05-28 (Tier-1 data capture)** — pitcher identity/ERA, count
  (balls/strikes), and scheduling metadata (start_time_utc, day_night) are
  now logged on **both** the candidate row (`build_base_candidate_payload`)
  and the `BetRecord`/`LiveBetRecord`. These were already in memory on the
  `ScheduledGame` (gate 8i used ERA but never logged it) and were dropped
  before logging. New shared helper `models.signal_context_fields(game)` is
  the single source for both paths (splatted via `**`). New monitor field
  `ScheduledGame.day_night` (extracted from StatsAPI `dayNight` in
  `parse_games`). Immediately enables pitcher/count/day-night cohorts in the
  gate-EV audit + calibration. ~zero bloat (None fields stay omitted from
  candidate rows). Closes the Tier-1 gaps from the 2026-05-28 data-capture
  audit; Tier-2 (per-game enrichment cache: umpire, both starters, batter)
  is a follow-up.
- **2026-05-28 (live UNDER)** — real-money UNDER trading shipped
  (`--under-mode live`). Operator-directed accepted-loss data-gathering
  posture (pre-B4, UNDER calibrator still `unreliable_pre_refit`). Changes:
  `place_bet` (live_engine_placement) is now **side-parameterized**
  (`side="over"` default; `side="under"` routes book/limit/CLOB/record/log to
  `market.under_token_id`; EV-policy gate is OVER-only and skipped for
  UNDER). New `LiveTradingEngine._place_under_bet` (real CLOB via
  `place_bet(side="under")`) + `SignalEngine._place_under_bet` (paper);
  `signal_pipeline._maybe_emit_under_candidate` now places for mode in
  {paper, live} (decision tag `live_under`). `LiveBetRecord.under_token_id`
  added + `models.bet_traded_token_id(bet)` helper; **all lifecycle
  (`try_recover_fill`, orphan reconciliation) routes on the side-correct
  token**. `_is_bet_executable` is now provenance-aware: a `placement_mode
  == "live"` UNDER order is executable ONLY when `order_status=="filled"`
  (the old blanket `side==under -> True` would have fabricated P&L on
  unfilled live UNDER orders). FV-decay + ask-reversal early-cancel are
  **OVER-only** (live UNDER rests to fill / game-final / stale-timeout —
  intentional, maximizes fill data). Correlated-line cap now applies
  **per side** (paper + live). Budget / per-game / max-open-order caps are
  shared across both sides. New paper fleet preset `M_under_paper`
  (A_current + `--under-mode paper`) mirrors live UNDER for B4 evidence.
- **2026-05-28** — `L_enforce_min_raw_095` paper preset added to
  `launch_parallel_engines.py` (12th preset; added to the default
  `--config` fleet). It is `A_current` + `--prob-calibration-enforce-min-raw
  0.95` -- the only varying knob vs A. A/B-tests the
  `analyze_calibration_edge_shaving.py` finding that the band-gated
  calibrator wrongly flattens the realized +EV [0.90,0.95) raw-FV band
  while correctly killing the -EV [0.95,1.0) tail. Production default stays
  0.90 (`DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW` unchanged); the preset is
  the evidence-gathering harness before any live flip. `--prob-calibration-
  enforce-min-raw` is paper-safe (not in LIVE_ONLY_ENGINE_FLAGS).
- **2026-05-26 (later)** — Hygiene #1 line-5.5 high-FV slice guard
  shipped as K_line5p5_block paper preset. New CLI flags in
  `signal_config.py`: `--line-high-fv-block-mode {off,shadow,enforce}`
  (default `off` -- production unchanged), `--line-high-fv-block-min-raw-fv`
  (0.90), `--line-high-fv-block-lines` ("5.5"). New gate in
  `signal_pipeline_gates_post_fv.evaluate_post_fv_gates` (between
  `gate_extreme_edge` and `gate_fv_ask_gap`, reason
  `gate_line_high_fv_block`). K_line5p5_block = A_current with the
  guard in enforce mode -- pure A/B-test fleet expansion (11 total).
  5 unit tests in `LineHighFvBlockGateTests` + 1 preset shape test
  cover off / shadow / enforce / non-matching line / sub-threshold FV.
- **2026-05-26 (earlier)** — F-J aggregator normalization shipped.
  `aggregate_parallel_engines._bet_metrics` adds `profit_per_settled_bet`,
  `n_unique_game_lines`, `bets_per_unique_game_line`, plus a
  `volume_index_vs_baseline` post-pass (baseline = `A_current` when
  present, else first alpha). Lets F_no_dedup (5-10x A's volume) read
  against A on equal per-bet footing. Frontend surfaces in both
  CompareView and MultiEngineDayView; new "Best $/Bet" daily-read chip.
- **2026-05-26** — multi-engine fleet doubled from 5 → 10 presets.
  Added F_no_dedup (operator-requested permissive config: strip
  event-dedup, inning-dedup-gap, correlated-line-cap; keep edge/ask
  floors), G_loose_edge (-5pp edge floors, mirror of E), H_late_innings
  (`--min-inning 6` enforced — tests the +75pp inn_4-5 vs inn_6-7 ROI
  gap from the 2026-05-25 walk-forward cohort), I_extreme_018
  (`--extreme-edge-max 0.18`, Phase 6 prep), J_no_phantom_filter
  (`--extreme-edge-max 1.0`, pairs with I for a clean
  {off, current, tightened} sweep). Each maps to one open roadmap
  question so the daily aggregate becomes decision evidence. Default
  `--config` list expanded; 6 new pytest cases lock in the per-preset
  flag shapes + assert none use LIVE_ONLY flags.
- **2026-05-26 (F2 fix)** — Scoped Alt-A apply log dedup'd.
  `_apply_stage1_alt_a_scope` in `signal_pipeline_gates_post_fv.py`
  was emitting one INFO line per tick per cohort that stayed in the
  apply state; on the 2026-05-25 audit A_current's launch_log had
  2,270 such lines (85%+ of the entire log). Now: INFO is emitted
  ONCE per `(game_pk, line, inning, inning_state, matched_rule)`
  cohort, and subsequent ticks increment `_scoped_alt_a_rollup_counts`.
  New `flush_scoped_alt_a_rollup(engine, force=...)` emits a single
  summary line on the 30-min cadence (alongside
  `_log_runtime_debug_rollups`) and force-fires on shutdown from
  `live_engine._shutdown_gracefully` + `paper_engine_consumer.main`.
  **Measured reduction on yesterday's log: 2,270 INFO lines → ~80
  (96.5%).** 4 new unit tests in `ScopedAltAEnforceTests`.
- **2026-05-26** — post-session aggregator hook added to
  `launch_parallel_engines.py` (`--post-session-aggregate`, default on).
  When the last engine exits, the launcher shells out to
  `aggregate_parallel_engines.py` for the active date and echoes the
  daily read to stdout. Fixes the F1 finding from the 2026-05-25
  multi-engine audit: canonical `parallel_engine_comparison.md` was
  silently stuck on 2026-05-24 data overnight.
- **2026-05-25** — multi-engine parallel paper runs landed:
  `launch_parallel_engines.py`, `paper_engine_consumer.py`,
  `live_quote_engine.py`, `shared_market_data.py`,
  `shared_market_watcher.py`, `inventory_tracker.py`. Doc gained
  "Multi-engine parallel paper architecture" subsection.
- **2026-05-21** — Scoped Alt-A enforce (TR25):
  `_apply_stage1_alt_a_scope` in `signal_pipeline_gates_post_fv.py`;
  correlated-line cap lifted to shared `signal_config.py` so paper
  sessions also enforce.
- **2026-05-19** — Band-gated calibrator enforce (TR23):
  `DEFAULT_PROB_CALIBRATION_MODE=enforce`,
  `DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW=0.90` in `signal_config.py`.
  Added `below_min_raw_kept_raw` candidate-row diag.
- **2026-05-19** — UNDER candidate emission (TR26):
  `--under-emission-mode {off,shadow}`; new `decision=shadow_under` /
  `gate_no_under_liquidity` skip reasons.
- **2026-05-13** — Wallet-aware paper fallback (TR22c):
  `_record_paper_fallback_bet` + `--wallet-exhausted-cooldown-secs`.
- **Open**: `signal_engine.py` has grown back to 2,117 lines from
  the post-Tier-2 baseline of 1,016; worth re-evaluating a Tier 6
  split if growth continues.

Current strategic frame: state-value Over trading around market overreaction to
score and no-score transitions. Existing score-event gates remain stable while
new transition/no-score ideas should start as shadow diagnostics until enough
outcome and execution data supports enforcement.

Extreme-edge phantom-run protection is enforced as a hard gate (TR17, 2026-05-01):
`gate_extreme_edge` blocks any signal with `edge > extreme_edge_max` (default 0.22
since TR19, 2026-05-03) in any inning. **Probability calibration is in band-gated
enforce since 2026-05-19** (`DEFAULT_PROB_CALIBRATION_MODE=enforce`,
`DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW=0.90` in
[signal_config.py](signal_config.py#L177); overwrites raw FV only when raw>=0.90,
captures the +28pp dangerous-tail overconfidence, leaves the mid-band alone).
LTP/ask-gap, no-score drift, and EV policy remain shadow tags pending fresh
walk-forward audits. Score-event transition and no-score drift must remain
separate model families: do not pool calibration curves, EV policy artifacts,
promotion thresholds, or walk-forward conclusions across them.

## Runtime Dataflow

0. `live_engine.py` runs the canonical daily refresh by default before
   constructing `LiveTradingEngine`. As of 2026-05-25 it is a ~63-step base
   pipeline plus one `daily_human_review:<date>` step per stale session
   (full step list is generated by `build_refresh_steps()` in
   `scripts/analysis/run_daily_refresh.py`; canonical descriptions in
   `scripts/analysis/AGENT_CONTEXT.md` are slightly out of date —
   the 2026-05-16 list covered 45 steps; UNDER-family, drift,
   loss-attribution, settlement-verification, Alt-A staging, and
   auto-daemon steps have been added since). Order at a glance:
   inline preflight (`preflight_env_secrets`) ->
   scrape/cache rebuild (`scrape_recent_games`, `stage1_ou_cache`,
   `scrape_active_schedule`, `game_weather_cache`, `pitcher_cache`,
   `team_game_log`, `park_hr_factors`) ->
   inline `preflight_artifacts` (Stage-1/2/3 cache loads + Stage-3
   active-season coverage check) ->
   per-date `daily_human_review` ->
   research tables (`analysis_safe_trade_table`, `candidate_universe_table`,
   `calibration_opportunity_training`, `calibrate_signal_probabilities`,
   `model_maturity_report`, `fair_value_stage_ablation`,
   `fv_gap_decomposition`, `fv_trust_shrinkage`,
   `calibration_market_anchored_alpha`, `stage1_inferred_empirical_audit`,
   `unified_signals`,
   `signal_training_table`, `clv_report`, `fv_disagreement_quality`) ->
   decision-artifact retraining (`train_baseline_models`,
   `ev_policy_backtest`, `stage2_run_env_retrain_staging`, the three
   Stage-3 v2 retrain steps) ->
   inline `model_freshness_health` (Stage-2 staging-vs-production Brier
   diff, age-checks every model artifact) ->
   diagnostics + research replays (`execution_diagnostics`,
   `queue_aware_execution_replay`, `learn_execution_policy`,
   `state_value_transition_report`, `no_score_drift_policy`,
   `no_score_drift_paper_ledger`, walk-forwards) ->
   inline `refresh_health_rollup` (one operator-facing INFO block).
   It excludes the active run date from training/report refresh by
   default and fails open unless `--startup-refresh-strict` is set.
   Skip flags exist for each subprocess step
   (`--startup-refresh-skip-recent-games-scrape`,
   `--startup-refresh-skip-stage1-cache`,
   `--startup-refresh-skip-team-game-log`,
   `--startup-refresh-skip-preflight-artifacts`, etc.) and
   `--startup-refresh-require-poly-private-key` promotes the env-secrets
   check to a hard failure. As of 2026-05-12 the refresh DOES retrain
   probability-calibration and the EV-policy table in-band, plus the
   Stage-2 staging cache and Stage-3 v2 weight research fits. Only the
   Stage-3 v2 promotion to production (`promote_team_offense_v2.py`)
   stays a manual step. Heavy retrain steps carry `StalenessCheck` so
   re-running on an unchanged corpus is near-free; `--force-retrain`
   bypasses every check. The manifest under
   `data/analysis_output/startup_refresh/` carries `summary` (counts
   `ok` and `skipped-fresh`), `summary_status`, `failed_step_names`,
   `logs_dir_bytes`, and a `phase6_reminder` (fires after 2026-06-07 =
   30 days post-TR20). Plan-only refreshes write
   `<date>_startup_refresh_plan.json` so they cannot overwrite the
   completed-run `<date>_startup_refresh.json` manifest.
   `SignalEngine` loads the active-date weather cache into flattened
   weather-v2 fields. Weather v2 is the only live weather input for Stage-2
   FV adjustment; candidate rows, placed-bet diagnostics, and EV-policy
   runtime feature rows carry the same fields for model parity. If the cache
   misses a game, Stage-2 keeps park context and uses unknown temp/wind buckets.
   `build_calibration_opportunity_training_table.py` writes a combined audit
   table plus family-specific files under
   `data/analysis_output/calibration_opportunity_training/by_family/`; use the
   family files for model training/promotion.
1. `live_engine.py` / `signal_engine.py` receive monitor tick batches.
2. `signal_pipeline.py` performs per-tick gate evaluation and FV inference in
   explicit phases:
   - `TickContext`: immutable per-tick data shared by helpers.
   - `_evaluate_early_gates`: Gates 1-5, before model/FV work.
   - `_evaluate_pre_inference_gates`: Gates 6-8e, game-state gates before
     run-count inference.
  - `_run_inference_and_fv_phase`: run-count inference, Gate 8f, Stage-2,
     Gate 8h, Stage-3, calibration, edge/min-edge computation.
   - `_evaluate_post_fv_gates`: `gate_min_edge`, `gate_extreme_edge` (TR17,
     enforced), `gate_fv_ask_gap`, and same-event/cross-inning dedup gates,
     in that order.
  - Shadow state-value diagnostics attach current-score FV, inferred-score FV
     lift, score-event proxy, and phantom-risk fields to post-FV candidate rows.
     The score-event path still uses Poisson Stage-1 as live base FV, but it
     now logs inferred-state empirical sibling probability, support counts,
     line fallback metadata, support proxies/trust weights, and a compact
     +1/+2/+3 run-count inference panel so overconfidence and ask-distance run
     selection can be audited directly. Current-state Stage-1 support/trust is
     logged separately for the confirmed score state.
     Candidate rows also carry paired Under top-of-book, midpoint, and no-vig
     Over/Under midpoint fields when both books are available in the same tick
     batch; these are observability-only and do not affect placement.
     Scoring-path timing fields (`scoring_inning_rate`, `scoring_half_rate`,
     `burst_share`, `scoreless_streak`, `recent2_run_share`,
     `weighted_run_inning_norm`, `inning_run_slope`) are logged as shadow/model
     features from schedule linescore inning runs. They do not affect live FV,
     gates, sizing, or execution.
   - Shadow post-TR20 probes attach hypothetical pass/fail fields for a tighter
     edge cap, stricter ask-edge ramp, Gate 6 relax-as-enforced, and their
     combined result. These are observability-only and must not affect live
     placement.
   - Shadow no-score drift logging can write one `shadow_no_score_drift` row per
     same-score segment when ask has decayed but current-state FV still shows
     value. These rows never place orders.
   - `process_tick`: now mostly orchestration, placement, trade-row feature
     capture, state updates, and book-capture launch.
3. `signal_engine.py` records paper/session state, candidate rows, outcomes,
   book/tape/velocity sidecars, and settlement. The class body is now an
   orchestration core; the bulk of logic lives in helper modules listed below.
4. `live_engine.py` overrides placement for real CLOB orders, budget exposure,
   cancellation, fill recovery, and live lifecycle ledgers.
5. `polymarket_client.py` is the only authenticated CLOB SDK wrapper.

## Live Execution Invariants

- `BetRecord.entry_ask` / `LiveBetRecord.decision_ask` are decision-time prices.
  `LiveBetRecord.execution_bid` and `execution_ask` are fresh placement-time book
  prices. Do not mix stale decision asks with fresh bids for execution.
- Limit BUY order size passed to the CLOB is shares, not USDC. Runtime accepts a
  desired USDC stake, converts it with `shares = stake_usdc / limit_price`, and
  settles live P&L from realized shares and fill price.
- Live exposure accounting counts accepted unresolved states including
  `pending`, `live`, `delayed`, `matched`, `open`, and `unmatched`.
- Sub-minimum Kelly stakes skip by default. `--kelly-floor-to-min` is the explicit
  opt-in for forcing weak Kelly signals up to the exchange minimum.
- Probability calibration must route by `signal_model_family`. Missing-family
  calibration is identity/no-op in shadow and fail-closed in enforce; do not
  apply the score-event curve to no-score drift diagnostics.
- Live EV policy runtime only loads `score_event_transition` artifacts. No-score
  drift policy remains in its paper/walk-forward lane until separately promoted.
- Failed order placement must return `None` and must not update dedup state as if
  a trade was placed.
- Unfilled cancellations release the line lock through
  `_release_line_after_unfilled_order(...)`, with cooldown, so later valid states
  can be reconsidered.
- Live lifecycle rows are written to both `live_orders_ledger.jsonl` and
  `master_ledger.jsonl` for compatibility with older analysis scripts.
- **Orphan-fill reconciliation** (`live_reconciliation.reconcile_orphan_fills`)
  runs on both startup (after session resume) and shutdown (before settle).
  Queries the public Polymarket data-api by **wallet address**, which
  catches fills the CLOB SDK's `maker_address`-filtered `get_order` /
  `get_trades` paths miss. When a position is found for a token marked
  `cancelled`/`expired`/`missed`, the LiveBetRecord is patched to
  `filled`, the cancel cleared, and a `_event="reconciled_filled"` row
  appended. Caught the 2026-05-10 MIN@CLE 7.5 silent miss in regression.
- **Correlated-line exposure cap** (Active #6 part 1, 2026-05-12) blocks
  multiple over-side bets on the same game when they share a correlated
  trade idea. Two rules, both counting filled + open bets:
  count cap (default `--max-correlated-over-lines-per-game 2`) and
  spacing cap (default `--min-correlated-line-gap 1.5` runs -- blocks
  O7.5+O8.5 but allows O7.5+O9.5). Logic in
  `LiveTradingEngine._evaluate_correlated_line_cap`; gate fires in
  `_place_bet` before stake sizing. Skip reasons:
  `correlated_line_count_cap` and `correlated_line_gap_cap`.
- **Calibrated-edge stake scaling** (Active #6 part 2, 2026-05-12)
  multiplies the base stake by a [min, max] multiplier derived from
  the *calibrated* edge (`fair_value_calibrated - decision_ask`).
  Three modes via `--calibrated-stake-scale-mode`: off / shadow /
  enforce. Default `shadow` records the multiplier on the bet record
  but does NOT change stake. Five new audit fields on `LiveBetRecord`
  (`calibrated_stake_mode`, `calibrated_stake_base_stake`,
  `calibrated_stake_multiplier`, `calibrated_stake_edge_used`,
  `calibrated_stake_applied`). Multiplier helper is in `live_pricing.py`
  (`calibrated_stake_multiplier`, `resolve_calibrated_edge`).
- **Wallet-aware paper fallback** (2026-05-13). When the CLOB returns
  `not enough balance / allowance` (the user's wallet is below the
  order's USDC requirement), placement does NOT raise / drop the bet
  / spam the API with retries. It instead routes the bet through
  `_record_paper_fallback_bet` in `live_engine_placement.py`:
  `placement_mode="paper_fallback"`, `paper_fallback_reason` set to
  `"clob_balance_error"` or `"wallet_cooldown"`, `order_status="filled"`
  with synthesized fill at `limit_price`. The bet enters `_bets` (so
  settlement picks it up at game-final and computes won/profit normally
  off the synthesized fill) but does NOT enter `_open_orders` (so the
  lifecycle polling never fetches the fake order_id). A session-level
  cooldown (`engine._wallet_exhausted_until`, configured via
  `--wallet-exhausted-cooldown-secs`, default 300s) skips CLOB
  attempts for subsequent placements until it elapses; real money
  resumes automatically. Engine state added: `_wallet_exhausted_until`
  (monotonic deadline) + `_paper_fallback_stats` dict (placed,
  total_stake, wallet_exhausted_events, last_at, last_reason). Session
  summary surfaces `paper_fallback_*` fields. Two new fields on
  `LiveBetRecord`: `placement_mode` (default `"live"`,
  `"paper_fallback"` for fallbacks) and `paper_fallback_reason`
  (`Optional[str]`). Analysis code that wants real-money-only metrics
  should filter `placement_mode == "live"`; signal/outcome analysis
  (training tables, walk-forward) should keep both.
- **Order-status normalizer** collapses `canceled` -> `cancelled` so
  the American spelling returned by some CLOB responses doesn't leak
  past `_normalize_order_status` and leave orders stuck in
  `_open_orders` past stale-order timeout.
- CLOB signing path is selected at engine init: legacy proxy/Safe
  (`sig_type=2`, default) vs ERC-1271 deposit wallet (`sig_type=3`,
  opt-in via `--use-deposit-wallet` + `--deposit-wallet 0x...` or
  `POLY_USE_DEPOSIT_WALLET=1` + `POLY_DEPOSIT_WALLET=0x...` in `.env`).
  Deposit wallets address the 2026-05 ghost-fills issue. When opted in,
  `funder` and `maker_address` resolve to the deposit wallet and API
  creds are re-derived against the ERC-1271 endpoint -- legacy creds
  are not reused. Migration runbook:
  `model_improvements/deposit_wallet_migration_plan.txt`.

## File Ownership

- `book_features.py`: pure Family B book-depth features. No IO.
- `book_velocity.py`: pure Family C tick-buffer velocity features. No IO.
- `candidate_logging.py`: small public facade for raw candidate writes,
  early-gate raw sampling, skip-row dedup, and skip-debug dedup. Rollups count
  every early gate attempt, but non-model-bearing pre-FV skip rows only write
  the first raw row per coarse state/price bucket plus periodic samples.
  `SignalEngine` keeps thin method wrappers (`_record_candidate_decision`,
  `_log_skip_debug_once`, `_candidate_log_path`, etc.) so test stubs and
  pipeline call-sites work unchanged.
- `candidate_paths.py`: candidate IDs, candidate-universe paths, JSONL helpers,
  and final-score outcome rows.
- `candidate_rollups.py`: candidate rollup counter initialization, observation,
  snapshot assembly, and rollup JSON write.
- `candidate_schema_enrichment.py`: observability-only modeling fields for
  post-FV opportunities: buckets, logit residuals, execution-policy panel, and
  compact `*_calibration_opportunities.jsonl` rows.
- `candidate_score_confirmation.py`: score-event confirmation sidecar lifecycle
  for `*_score_confirmations.jsonl`; diagnostic-only phantom-run labels.
- `capture_helpers.py`: book/tape/Family-B/Family-C sidecar capture (free
  functions: `fetch_depth_snapshot`, `start_book_capture`, `start_tape_capture`,
  `start_family_b_capture`, `start_family_c_capture`). Engine keeps thin
  `_start_*_capture` wrappers; called by both placed-bet and skip-with-features
  paths. Sidecar paths are `data/<paper|live>_trading/<kind>/<date>/<bet_id>.*`.
- `clob_order_client.py`: compatibility re-export; do not add logic here.
- `ev_policy.py`: pure EV policy artifact scorer. Fail closed on schema
  mismatch; runtime artifacts should not require post-signal book horizons.
- `line_state.py`: per-(game,line) `LineState` dataclass plus pure helpers
  `_mid`, `_now_iso`, `_now_ts`, `_runs_pace_ok`, `_ask_edge_boost`. No engine
  coupling. `signal_engine` re-exports `LineState`, `_now_iso`, `_now_ts` so
  `from signal_engine import LineState, _now_iso, _now_ts` keeps working for
  `live_engine`, `paper_trader`, and tests.
- `live_diagnostics.py`: read-only end-of-run diagnostics builders + loggers
  (`build_shadow_order_diagnostics`,
  `build_current_state_edge_band_diagnostics`, `current_state_edge_band`,
  `log_shadow_order_diagnostics`, `log_current_state_edge_band_diagnostics`).
  All summarize `engine._bets`; the only state mutated is the one-shot
  `_*_summary_logged` flag.
- `live_engine.py`: orchestration core for live execution -- `__init__`,
  `_settle_finished_games`, `_on_tick_batch`, the small ledger method
  wrappers, startup refresh wiring, daily human-review report write on
  final shutdown/save, and the `main()` CLI entry point. As of
  2026-05-12 the file is **~1,100 lines** -- everything else lives in
  the four `live_engine_*.py` modules below, plus the existing
  `live_pricing.py`, `live_order_lifecycle.py`, `live_diagnostics.py`,
  `live_session_loading.py`, and `live_reconciliation.py`. Engine keeps
  thin method wrappers so test stubs and method dispatch keep working.
  Shutdown does a best-effort active-date final MLB scrape before daily
  human-review/model-maturity reports so side-neutral, UNDER, and
  no-score paper labels include the just-finished date when game feeds
  are available.
- `live_engine_cli.py`: argparse parser (`parse_live_args`) plus every
  `DEFAULT_*` constant the engine reads. Imports back into
  `live_engine.py` are wired so `from live_engine import DEFAULT_*` keeps
  working without churn.
- `live_engine_placement.py`: the full `_place_bet` execution path --
  fresh-book lookup, limit-price calculation, stake sizing (including
  the calibrated-edge multiplier when shadow/enforce), EV-policy gate,
  budget + per-game + correlated-line caps, max-open-orders, bet
  record construction, dry-run vs live CLOB post, ledger write. Engine
  exposes the thin `_place_bet` wrapper; this module owns the body.
- `live_engine_session_io.py`: session JSON writer
  (`save_session(engine, force)`) plus the tagged-event live-ledger
  helpers (`append_to_live_ledger`, `write_live_ledger_row`,
  `bootstrap_live_ledger_event_keys`). Dedup keys are hydrated from any
  existing ledger on first write so warm starts don't duplicate rows.
- `live_engine_setup.py`: logging configuration, log rotation (gzip
  closed days + delete past retention), library-log suppression, and
  the `run_startup_refresh` wrapper that builds the `RefreshConfig`
  from `live_args`/`monitor_args`. Imports lazy where possible to keep
  startup paths simple.
- `live_reconciliation.py`: orphan-fill reconciler. Walks unfilled
  LiveBetRecords on startup + shutdown, queries the wallet-keyed
  Polymarket data-api to recover fills the CLOB SDK's
  `maker_address`-filtered `get_order` / `get_trades` paths miss.
  Patches the bet record (`fill_price`, `fill_size`, etc.) and writes
  a `_event="reconciled_filled"` ledger row. Failures are best-effort
  and never block shutdown.
- `live_ev_policy_runtime.py`: EV-policy artifact loading and per-signal
  evaluation (`load_ev_policy_runtime`, `build_ev_feature_row`,
  `evaluate_ev_policy`). Fails closed in enforce mode; degrades to "off" in
  shadow mode on artifact load failure. Runtime must load the decision-time
  artifacts (`ev_signal_win_if_filled_model.json` and
  `ev_execution_fill_runtime_model.json`); the strict fill artifact is
  offline-only and is rejected because it uses post-signal fields like
  `ask_1s` / `bid_1s`.
- `live_order_lifecycle.py`: open-order maintenance loop -- fill polling
  (`check_open_orders`), trade-history fill recovery (`try_recover_fill`),
  stale-order timeout (`cancel_stale_orders`), 5-second ask-reversal cancel
  (`check_ask_reversal`), live-state FV recompute (`recompute_fv`), FV-decay
  cancel with score-over-line guard (`check_fv_decay`), end-of-game cleanup
  (`cancel_orders_for_game`), and per-line lock release after cancellation
  (`release_line_after_unfilled_order`). All cancellation paths funnel
  through the line-release helper so a future event on the same line can be
  reconsidered after cooldown.
- `live_pricing.py`: limit-price calculation, fill-cost accounting, and Kelly
  sizing (`compute_limit_price`, `filled_notional`, `kelly_components`,
  `compute_stake`). Pure pricing math; no IO.
- `live_session_loading.py`: resume `LiveTradingEngine` state from prior
  session JSON (`load_existing_session`). Rebuilds dedup state, re-registers
  open orders for crash recovery, and runs an immediate sync poll if any are
  found.
- `models.py`: shared dataclasses only.
- `model_families.py`: canonical strategy/model-family labels and inference
  (`score_event_transition`, `no_score_drift`). Use this instead of ad hoc
  string checks when routing calibration, training, or diagnostics by family.
- `paper_trader.py`: compatibility CLI shim; do not add logic here.
- `polymarket_client.py`: CLOB auth/order/cancel/status/trade-history wrapper.
  Supports both the legacy proxy (`sig_type=2`) and the ERC-1271 deposit-wallet
  (`sig_type=3`) signing paths via `use_deposit_wallet` + `deposit_wallet`
  kwargs (or `POLY_USE_DEPOSIT_WALLET` / `POLY_DEPOSIT_WALLET` env vars).
  Transient HTTP errors (5xx, timeouts, connection refused) auto-retry with
  exponential backoff via `_retry_on_transient`; 4xx client errors do not
  retry.
- `pricing_features.py`: pure Family D pricing features. No IO.
- `probability_calibration.py`: runtime probability calibration. Supports
  legacy single-curve artifacts and separate family artifacts; callers should
  pass `model_family` when available so score-event and no-score drift curves
  are never pooled accidentally.
- `real_trader.py`: compatibility CLI shim; do not add logic here.
- `runtime_log_rollups.py`: compact DEBUG rollups for repeated inference
  fallback, Stage-2, and Stage-3 adjustment logs keyed by game, line,
  inning/state, and source key. Runtime emits periodic/final summaries instead
  of one line per repeated tick.
- `session_serialization.py`: session JSON payload builders
  (`build_paper_session_payload`, `build_live_session_payload`). Pure builders;
  no IO, no logging, no throttling. Each engine owns its own `_save_session`
  for those side effects. The two builders intentionally maintain divergent
  field sets to preserve on-disk JSON exactly (paper has `shadow_relaxed_*`
  fields paper-only; live has `extreme_edge_max`, `ltp_ask_gap_max`, and
  `live_args.*` live-only).
- `signal_config.py`: defaults and CLI parsing. Gate thresholds live here.
- `signal_engine.py`: orchestration core only — `__init__`, `_on_tick_batch`,
  `_process_tick`, `_place_bet`, `_settle_finished_games`, `_save_session`,
  CLI `main`, plus thin delegators forwarding to `signal_pipeline`,
  `capture_helpers`, `candidate_logging`, and `session_serialization`. All
  candidate / skip-debug counter state lives on the engine instance for
  cross-module reads (e.g. session payload builders read
  `engine._candidate_rollup_*`).
- `signal_gates.py`: gate relax and shadow-evaluation helpers.
- `signal_interaction_features.py`: pure Family E features.
- `signal_pipeline.py`: per-tick orchestrator only -- builds `TickContext`,
  calls into the phased gate modules below, and dispatches placement +
  trade-feature attachment. The TickContext / PreInferenceGateResult /
  FvPhaseResult dataclasses live here. Keep gate order and reason strings
  stable unless explicitly doing a gate-logic change. Logic split across:
- `signal_pipeline_payload.py`: canonical candidate-row schema +
  shadow-risk-tag attacher (`build_base_candidate_payload`,
  `record_early_skip`, `attach_shadow_risk_tags`,
  `compact_state_value_bet_diagnostics`,
  `STATE_VALUE_BET_DIAGNOSTIC_KEYS`). All schema-touching code lives here
  to prevent drift between early-skip / late-skip / trade-row writers.
  Candidate rows and placed-bet diagnostics include flattened weather-v2
  fields from `weather_client.WEATHER_FEATURE_FIELD_KEYS` when the startup
  cache has a matching `game_pk`.
- `scoring_path_features.py`: pure scoring-timing feature helper. Consumes the
  monitor's inning-by-inning linescore arrays and emits shadow/modeling fields
  only. Keep these features out of gate/FV logic until walk-forward evidence
  justifies promotion.
- `signal_pipeline_state_value.py`: shadow current-score FV diagnostics
  (`lookup_state_value_components`, `apply_state_value_adjustments`,
  `compute_state_value_snapshot`, `attach_score_transition_shadow_fields`).
  Diagnostic-only; never blocks or places.
- `signal_pipeline_no_score_drift.py`: standalone observability writer
  (`maybe_record_no_score_drift_candidate`) that records one
  `shadow_no_score_drift` candidate per same-score segment. Never places.
  Stamps `signal_model_family=no_score_drift`.
- `signal_pipeline_capture.py`: skip-row recorder + Family A-E feature
  attachment (`record_skip`, `attach_skip_features`, `attach_trade_features`,
  `LATE_STAGE_SKIP_GATES`). Lifted from the three closures that used to
  live inside `process_tick`. Centralizes feature-attachment so the
  skip-with-features and trade-row paths stay in schema lock-step.
- `signal_pipeline_gates_pre_fv.py`: gate phases that fire BEFORE FV
  inference -- `evaluate_early_gates` (Gates 1-5) and
  `evaluate_pre_inference_gates` (Gates 6-8e). Conceptual cut: "do we
  even bother running the model?".
- `signal_pipeline_gates_post_fv.py`: gate phases that fire AFTER FV
  inference -- `run_inference_and_fv_phase` (run-count inference,
  Stage-2/3, calibration, edge computation; stops on Gates 8f and 8h)
  and `evaluate_post_fv_gates` (gate_min_edge -> gate_extreme_edge [TR17,
  enforced] -> gate_fv_ask_gap -> gate_sp_era -> dedup). Conceptual cut:
  "does the model output justify a trade?".
- `shadow_post_tr20.py`: shadow-only candidate-row probes for the fresh
  post-TR20 testing window: `shadow_post_tr20_extreme_020_pass`,
  `shadow_post_tr20_ask_ramp_v2_pass`,
  `shadow_post_tr20_gate6_relax_enforce_pass`, and
  `shadow_post_tr20_combined_pass`. These fields model possible future tuning
  choices without changing current gates.
- `tape_capture.py`: public trade-tape fetch plus Family A features.
- `weather_client.py`: startup-safe stadium weather enrichment. Loads
  `data/reference/mlb_stadium_weather_metadata.json`, fetches active-date
  local weather via Open-Meteo or writes metadata/MLB schedule fallback rows
  when provider is `none`, and derives diagnostic air-density / wind-component
  fields. Exports full audit fields via `WEATHER_FEATURE_FIELD_KEYS` plus a
  model-safe subset via `WEATHER_MODEL_FEATURE_FIELD_KEYS`; the model-safe
  subset excludes cache/provenance IDs and uses `weather_effective_*` fields
  that are populated only for open-air weather-usable games. Retractable-roof
  games remain raw audit data until roof-state is validated. Exports
  `flatten_weather_cache_game`, and `load_weather_features_by_game` as the
  canonical runtime/analysis bridge. Does not change live FV or gate decisions.
- `live_engine.py.disabled.bak2` and `polymarket_client.v1.bak`: historical
  backups only; do not treat these as active code.

### Multi-engine parallel paper architecture (shipped 2026-05-25)

- `launch_parallel_engines.py`: launches N paper `SignalEngine` processes
  against the same live market day, each with its own `paper_root` and
  `config_label`. Owns the daily refresh once; child engines all get
  `--no-startup-refresh` so N configs don't rebuild artifacts N times.
  5 built-in `PRESETS` (`A_current`, `B_cal_only`, `C_raw`,
  `D_scope_only`, `E_tight_edge`) with 2x2 factorial design across
  calibrator + scoped Alt-A modes plus an edge-threshold lever.
  `RESERVED_ENGINE_FLAGS` reject paper-only overrides;
  `LIVE_ONLY_ENGINE_FLAGS` reject Kelly/daily-budget/stake-mode so
  live-only flags don't leak into paper presets.
  **Post-session aggregator hook (2026-05-26)**: `main()` calls
  `_run_post_session_aggregator` after the run-mode returns, which
  shells out to `scripts/analysis/aggregate_parallel_engines.py
  --date-range <active_date>:<active_date>` and echoes the daily-read +
  per-config headline back to stdout. Runs whether engines exited
  normally or via SIGINT. Fail-open: aggregator errors are printed but
  do not change the launcher's exit code. Opt out with
  `--no-post-session-aggregate`. Motivation: 2026-05-25 audit found
  the canonical `parallel_engine_comparison.md` was stuck on 2026-05-24
  data because nobody manually re-ran the aggregator overnight; this
  hook closes the gap at the data source rather than waiting for
  next-morning refresh.
- `paper_engine_consumer.py`: paper engine that consumes shared market
  data instead of owning its own monitor poll loop. Used by the
  parallel launcher so N configs share book/tape polling.
- `shared_market_data.py` + `shared_market_watcher.py`: shared
  book-poller infrastructure. One watcher owns the CLOB polling; N
  consumers (one per engine config) read from a shared per-tick
  payload queue.
- `shared_capture.py`: shared sidecar capture path for multi-engine
  runs. Per-engine sidecars still write under each engine's
  `paper_root` so analysis stays clean.
- `live_quote_engine.py`: two-sided quote engine (shadow only;
  bidirectional pivot Phase A scaffolding). Logs hypothetical bid + ask
  quotes alongside each candidate row; outputs feed the
  `quote_engine_shadow` analysis report.
- `inventory_tracker.py`: per-game / per-line exposure tracker. Used
  by the correlated-line exposure cap (TR22a) and will be the input
  for inventory-aware quoting in Phase B of the bidirectional pivot.

### Other modules added since 2026-05-14 (not in main listing)

- `live_engine_overrides.py`: runtime config overrides layer. Allows
  the auto-promote/demote daemon (Active #15) to actuate
  stake-scaling + gate-threshold levers without restarting the engine.
- `stage1_cache_audit.py`: Stage-1 cache cell audit helpers
  (fallback metadata, support counts).
- `stage1_support.py`: Stage-1 support/trust proxy computation
  (effective_n, line-exact flags, fallback penalties).
- `order_status.py`: order-status normalizer (`canceled` ->
  `cancelled`, fail-closed on unknown statuses).
- `candidate_score_confirmation.py`: score-event confirmation sidecar
  lifecycle for `*_score_confirmations.jsonl`; diagnostic-only
  phantom-run labels.
- `signal_gates.py`: gate relax and shadow-evaluation helpers
  (re-exported for back-compat with older test stubs).

### New candidate decisions / skip reasons (since 2026-05-14)

- `shadow_under` — UNDER candidate row passed UNDER gates
  (`--under-mode shadow`); never places.
- `paper_under` / `live_under` — UNDER candidate passed gates under
  `--under-mode paper` / `live`; places a paper BetRecord / real CLOB
  order on the under_no token respectively (2026-05-28).
- `no_under_token` — live UNDER placement skipped: market has no
  `under_token_id` (place_bet side=under).
- `gate_no_under_liquidity` — UNDER candidate skipped because the
  UNDER book is too thin to evaluate.
- `correlated_line_count_cap` / `correlated_line_gap_cap` — same-game
  exposure cap (TR22a).
- `wallet_exhausted_cooldown` — CLOB skipped because the wallet
  balance error cooldown is active (TR22c).
- `placement_mode="paper_fallback"` (not a skip; a placement mode
  on the bet record itself when wallet was exhausted).
- `stage1_alt_a_scope_*` fields on candidate log: `mode`, `decision`,
  `action`, `rule_matched`, `reason` (TR25 Scoped Alt-A enforce).
- `below_min_raw_kept_raw` — calibrator was scored but did NOT
  overwrite raw FV because raw < `--prob-calibration-enforce-min-raw`
  (TR23 band-gated enforce).

## Refactor Priorities

- Done (2026-04): `signal_pipeline.process_tick` was split into early gates,
  pre-inference gates, FV/inference, and post-FV gates.
- Done (2026-05-01, Tier 1): sidecar capture extracted to `capture_helpers.py`;
  paper+live session payload assembly extracted to `session_serialization.py`.
  Both `_save_session` methods are now thin shells around the builders.
- Done (2026-05-01, Tier 2): `LineState` + pure helpers extracted to
  `line_state.py`; candidate JSONL / rollup / skip-debug methods extracted to
  `candidate_logging.py`. `signal_engine.py` shrank from 2,058 to ~1,016 lines.
  **As of 2026-05-25 `signal_engine.py` is back up to ~2,117 lines** (Family E
  + UNDER candidate emission + Scoped Alt-A + correlated-line cap + paper
  fallback orchestration all landed in the engine class). Worth re-evaluating
  whether the next ~1,000-line round of growth should trigger Tier 6.
- Done (2026-05-01, Tier 3): `live_engine.py` split into five focused modules
  (`live_pricing.py`, `live_ev_policy_runtime.py`, `live_order_lifecycle.py`,
  `live_diagnostics.py`, `live_session_loading.py`). `live_engine.py` shrank
  from 2,448 to ~1,317 lines (-46%). Engine still owns `__init__`,
  `_place_bet`, `_save_session`, `_settle_finished_games`, `_on_tick_batch`,
  `_shutdown_gracefully`, and CLI; everything else is delegated.
- Done (2026-05-12, Tier 5): `live_engine.py` split a second time into
  four focused modules (`live_engine_cli.py`, `live_engine_placement.py`,
  `live_engine_session_io.py`, `live_engine_setup.py`). `live_engine.py`
  shrank from ~2,021 to ~1,103 lines (-45%) after the placement,
  ledger-IO, and CLI bodies moved out. Pattern matches the existing
  impl-module split (`live_pricing.py`, `live_diagnostics.py`, etc.):
  engine class keeps thin method wrappers, free functions live in the
  impl modules with `engine` as first arg.
- Done (2026-05-02, Tier 4): `signal_pipeline.py` split into six focused
  modules (`signal_pipeline_payload.py`, `signal_pipeline_state_value.py`,
  `signal_pipeline_no_score_drift.py`, `signal_pipeline_capture.py`,
  `signal_pipeline_gates_pre_fv.py`, `signal_pipeline_gates_post_fv.py`).
  signal_pipeline.py shrank from 2,074 to ~356 lines (-83%). Three nested
  closures inside `process_tick` were lifted to free functions; the
  post-placement Family A-E + gate-proximity block was extracted to
  `attach_trade_features`. Optional cleanup also bundled: pipeline
  helpers `_now_iso`, `_now_ts`, `_runs_pace_ok`, `_ask_edge_boost` are
  now imported from `line_state.py` instead of duplicated.
  **As of 2026-05-25 `signal_pipeline.py` is back up to ~630 lines** after
  Scoped Alt-A enforce + UNDER emission + paper-fallback wiring; still well
  under the original 2,074 but worth monitoring.
- Next: consolidate the two session payload builders behind a shared
  `_common_session_params(trade_args)` helper for the ~50 fields paper+live
  share verbatim. Safe to do now that they sit next to each other; care needed
  to keep on-disk JSON field sets unchanged in each engine.
- Next observability patch before runs: startup file/artifact fingerprint plus
  periodic/end-of-run decision summaries by skip reason and data-volume counters.
- Keep compatibility shims small and boring.

## Daily Data Efficiency

- Candidate rollups are the complete audit stream for high-volume early gates;
  raw candidate JSONL is full fidelity for trade/model-bearing rows and sampled
  for repeated early pre-FV skips. Runtime omits `None` fields from candidate
  rows to reduce bloat while keeping downstream builders missing-safe.
- Early pre-FV skip rows are additionally compacted: the raw stream writes the
  first row per coarse state/price bucket plus periodic samples, and omits
  verbose Weather v2 payload fields plus legacy `weather_mlb_schedule_*`
  fields. Calibration/model-bearing rows still keep full Weather v2 context;
  the candidate rollup tracks compacted-field and raw-sample omissions.
- Normal tick-buffer health is DEBUG-only every 30 minutes; stale/empty buffers
  warn at the same cadence. Repeated inference fallback and Stage-2/Stage-3
  adjustment DEBUG lines are rolled up rather than emitted on every tick.
- Live shutdown/final run exit writes compact daily human-review JSON/Markdown
  under `data/analysis_output/daily_human_review/` after the final session and
  candidate rollup are saved. The report now includes a shadow Stage-2
  suppression dollar audit for blocked eventual winners/losers.
- Post-FV model-bearing opportunities now carry calibration diagnostics in the raw
  candidate row: ask/edge/runs-needed buckets, logit-scale model-vs-market
  residuals, outcome join key, and an execution-policy panel for current limit,
  +1c, +2c, and taker-like prices. They also carry shadow post-TR20 tuning
  probes when edge context is available. Early gate rows stay lean.
- `*_calibration_opportunities.jsonl` is the compact one-row-per-opportunity
  modeling stream for trades, post-FV skips, extreme-edge blocks, and no-score
  drift rows. Use it for calibration modeling instead of scraping raw logs.
- `*_score_confirmations.jsonl` is a sidecar keyed by `candidate_id` that labels
  score-event opportunities as official score changes within 10/30/60 seconds or
  no score change within 60 seconds. This is the main phantom-run separation
  label; it never changes live decisions.
- Gates 1-5 now write early skip candidate rows before model/FV work, so gate
  dollar audits can include early blockers. Late post-FV skips in
  `LATE_STAGE_SKIP_GATES` (in `signal_pipeline.py`, includes `gate_extreme_edge`
  since TR17 enforcement) can attach Family A-E features for execution modeling.
- No-score drift rows are sparse by design: one per same-score segment after age,
  tick-count, drawdown, ask, spread, and current-state edge filters. They are
  meant to measure a possible second strategy arm, not to replace gate skips.
  No-score diagnostics now include Poisson-edge x empirical-edge x ask x drawdown
  buckets for variant review.
- `scripts/analysis/no_score_drift_walk_forward.py` is the canonical no-score
  model-family validation path. It builds a deduped one-row-per-score-segment
  training table, attaches paper-ledger budget/cap/fill outcomes, and runs
  rolling train/validation/test windows. Use it before proposing promotion.
- Sidecars are useful but can grow quickly:
  `book_captures`, `tape_captures`, `book_decision_snapshots`,
  `velocity_snapshots`. Prefer feature summaries in candidate rows and reserve raw
  sidecars for placed bets or intentionally sampled skip contexts.
- Live lifecycle rows are intentionally written to both `live_orders_ledger.jsonl`
  and `master_ledger.jsonl` for compatibility with older analysis scripts.

## Safe-Edit Checklist

- Do not change thresholds, gate order, or skip reasons without a gate audit.
- Do not promote `gate_ltp_ask_gap`, no-score drift, EV policy, or probability
  calibration from shadow to enforce without a fresh walk-forward / dollar audit.
  (`gate_extreme_edge` was promoted from shadow to enforce on 2026-05-01 [TR17]
  based on cumulative live evidence: 1W/5L, -$117.56 realized on filled bets
  with edge > 0.25; Wilson 95% upper bound well below break-even.)
- If decision logic changes, update candidate logging and tests in the same patch.
- There are golden candidate-row tests in
  `tests/test_signal_engine_phase1_characterization.py` for representative
  early, pre-inference, and post-FV gate outputs. Update them only when an
  intentional schema/decision-output change is made.
- Tests stub helper methods directly on the engine instance
  (`engine._record_candidate_decision = ...`, `engine._candidate_log_path = lambda: path`,
  `engine._start_tape_capture = lambda **kwargs: ...`, `engine._save_session = lambda *a, **kw: None`,
  `engine._compute_stake(...)`, `engine._evaluate_ev_policy(...)`,
  `engine._build_shadow_order_diagnostics()`).
  When refactoring helper modules, keep the engine method wrappers in place so
  these stubs continue to work.
- If storage schema changes, verify the analysis builders still handle old and new
  rows.
- Run `python -m compileall -q scripts/trading` and the pytest suite before handing
  off.
