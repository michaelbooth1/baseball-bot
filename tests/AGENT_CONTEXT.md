# tests Agent Context

This package is the pytest suite for the MLB Polymarket Over/Under trading
research codebase. Tests cover both `scripts/trading` (runtime gates, live
execution, signal pipeline) and `scripts/analysis` (table builders, walk-forward
validation, report writers). Treat these as the contract: when changing decision
logic, schemas, or report shapes, update tests in the same patch instead of
deleting failing assertions.

Last checked against the active `tests/test_*.py` files: 2026-05-14
(post calibration method-stability gate + walk-forward certification +
wallet-aware paper fallback + stake-scaling promotion analyzer +
weekly drift rollup HTML + Stage-3 weight externalization +
run_daily_refresh smoke test).

There are **102 test modules** (as of 2026-06-15; recent adds:
test_build_shadow_clv [T1 shadow-CLV collector],
test_fleet_signal_table_foldin [T2 fleet fold-in],
test_concept_drift_psi_watchpoint [T7], plus the 2026-06-14 audit ships). They split roughly between trading runtime
behavior, analysis-builder schemas, observability/rollup characterization,
UNDER-family side-aware tests, lineage/promotion-audit tests, drift
detection, and parallel-engine smoke. Many are golden / characterization
tests -- update field-by-field, not by loosening matchers.

## Recent changes

_Append dated bullets here when you add or restructure tests. Mirrors
`MASTER_CONTEXT.md`'s "Recent major shifts" pattern; bump "Last checked"
above when you sweep the whole doc. To verify the test count is current
run `pytest --collect-only -q 2>&1 | tail -5`._

- **2026-05-29 (Tier-3 feed enrichment)** — added `test_build_feed_enrichment.py`
  (parse_iso/half/defending-side/platoon helpers, GameTimeline build, enrich
  at mid-stint / after-reliever / pregame / no-ts, pitch-count + TTO +
  bullpen + handedness + velocity math, model-bearing filter, end-to-end
  main writing jsonl/csv/summary over a synthetic feed).
- **2026-05-29 (Tier-2 capture)** — added `test_game_meta_client.py` (umpire/
  officials extraction, fail-open build with injected schedule/boxscore
  fetchers, write/load round-trip, engine candidate-row join). Extended
  `test_signal_context_capture.py` for the new schedule-derived players (both
  starters + ERAs, current batter + on-deck) and the None-when-absent
  behavior (writer strips Nones → no empty-string bloat). Updated the
  `test_run_daily_refresh` weather-only step assertion to expect
  `[game_weather_cache, game_meta_cache]`.
- **2026-05-28 (Tier-1 capture)** — added `test_signal_context_capture.py`
  (pitcher id/name/era + balls/strikes + start_time_utc + day_night now flow
  onto candidate rows AND bet records via `models.signal_context_fields`).
  Updated the 4 golden candidate-row dicts in
  `test_signal_engine_phase1_characterization.py` for the new fields
  (current_pitcher_era=4.2, current_pitcher_name/start_time_utc/day_night="").
- **2026-05-28 (gate EV audit)** — added `test_audit_over_gate_ev.py` (10
  cases): Wilson interval, taker-ROI math, verdict classifier
  (+EV/-EV/marginal/insufficient thresholds), cohort aggregation with
  bettable-subset split, and end-to-end `main()` that flags a synthetic
  -EV gate (blocks winners) vs +EV gate (blocks losers).
- **2026-05-28 (live UNDER)** — extended `test_under_paper_placement.py`
  (live-UNDER path: `place_bet` is side-parameterized; live UNDER routes the
  CLOB order to `under_token_id`; `bet_traded_token_id` side routing;
  `_is_bet_executable` requires a real fill for `placement_mode="live"`
  UNDER — no fabricated P&L; `--under-mode live` accepted). Replaced the
  obsolete "place_bet is OVER-only" docstring test. Extended
  `test_parallel_engines_mvp.py` for the `M_under_paper` preset (13-preset
  fleet). No new test-module files (module count unchanged at 90).
- **2026-05-28** — added `test_analyze_calibration_edge_shaving.py` (28
  cases): band-gated FV transform, Wilson interval, gate admission, raw-FV
  band labelling, cohort stats, suppression-cohort split (over_shrinking /
  justified / insufficient), reliability table, edge-shaving summary,
  scenario sweep, recommendation objective, verdict synthesis (keyed off
  the recommendation grid, not the blended suppression cohort), and
  end-to-end `main()` writing JSON + Markdown over synthetic rows.
- **2026-05-25** — added 9 new test categories (UNDER family, Stage-1,
  promote.py + daemon, lineage, drift, settlement/attribution/counterfactual,
  quote-engine + parallel, weekly rollup + stake scaling, CLV/disagreement/alpha).
  Test count jumped 36 → 84 modules between 2026-05-14 and 2026-05-25.
- **Open**: when adding a test, also add its one-line description to the
  right category section below — the dashboard is most useful when complete.

## Test Dataflow

1. Most tests import scripts directly. For `scripts/trading` modules
   (`signal_engine`, `live_engine`, `ev_policy`, `models`, `polymarket_client`)
   imports work via the conftest-managed `sys.path`. For `scripts/analysis`
   modules, individual files do `sys.path.insert(0, str(ANALYSIS_DIR))`
   themselves -- preserve that pattern when adding new analysis tests.
2. Engine-touching tests build a stub via `_make_engine_stub()` (or local
   equivalents) and replace helper methods on the instance
   (`engine._record_candidate_decision = ...`, `engine._candidate_log_path =
   lambda: path`, `engine._save_session = lambda *a, **kw: None`). The trading
   AGENT_CONTEXT.md treats this as a public testing surface; keep those method
   wrappers in place when refactoring.
3. Builder/replay tests stage temp directories with synthetic JSONL inputs
   (`_write_signal_rows`, `_read_jsonl`) under `tempfile.TemporaryDirectory()`,
   run the script's `main` or top-level builder function, then assert on
   produced JSON / Markdown / JSONL artifacts.
4. Characterization tests assert exact field sets on candidate rows, ledger
   rows, and report payloads. Schema drift -- new key, renamed key, type
   change -- is what they exist to catch.

## File Ownership

### Trading runtime / signal engine

- `test_signal_engine_phase1_characterization.py`: Largest characterization
  surface. Covers `signal_engine.py` + `signal_pipeline*.py` decisions:
  ask-edge boost monotonicity, relax-mode resolution (strict / shadow /
  enforce / A/B), min-current-total and blowout relax, probability
  calibration with family fallback (fail-closed in enforce mode), candidate
  skip-row dedup, early gates (`min_inning`, `min_entry_ask`, `ask_jump`),
  late post-FV gates, `skip_with_features` Family A-E feature capture,
  no-score-drift shadow logging once per score segment, tick-buffer health
  logging, and `candidate_table` column/label exports. Stubs:
  `_make_engine_stub()`, `_ConstantCache`, `_IdentityOffenseModel`,
  `_FakeCalibrator`. Golden row assertions -- update intentionally.
- `test_trading_live_execution_fixes.py`: Live execution invariants from the
  trading AGENT_CONTEXT.md. Covers `live_engine.py`, `live_pricing.py`,
  `live_order_lifecycle.py`, `live_diagnostics.py`, `polymarket_client.py`,
  `ev_policy.py`, `probability_calibration.py`. Includes USDC->shares
  conversion, settlement payouts, line-release-after-unfilled, Kelly
  floor-to-min opt-in, EV policy enforce/shadow with schema validation,
  calibration mode routing (off / shadow / enforce by family), and shadow
  diagnostics (high-edge losses, phantom risk, current-state edge bands,
  bottom-9 risk). Uses `_live_bet(**overrides)`, `_FakeClob`,
  `_FakeFinalGame`. The deposit-wallet `sig_type=3` branch added in the
  2026-05-06 patch lives under "polymarket_client" tests here.
- `test_remaining_opportunity.py`: Pure-helper unit tests for
  `remaining_opportunity.py`: bottom-9 availability flags
  (`home_leading_late`, `home_skip_bottom9_risk`),
  `expected_remaining_half_innings`, `expected_remaining_pa_bucket`. No
  fixtures -- just function-level math.
- `test_candidate_logging_compaction.py`: Unit tests for
  `candidate_logging.record_candidate_decision`. Covers per-game JSONL
  rotation under `candidate_universe/`, hash-stability of dedup keys
  across reruns, and that gate-skip rows preserve enough feature
  payload for the calibration-opportunity table to pick them up
  downstream.
- `test_wallet_paper_fallback.py`: Tests for the wallet-aware paper
  fallback added 2026-05-13. Covers the balance-error string matcher
  (positive + allowance + case-insensitive + negative cases), cooldown
  state lifecycle (not-tripped -> 0s remaining; tripped -> deadline
  set; expired -> 0s; `--wallet-exhausted-cooldown-secs=0` -> trip
  counters update but no forward window), `_record_paper_fallback_bet`
  fill synthesis (placement_mode + reason set, order_status="filled"
  so settlement picks it up, fill at limit_price, shares = stake /
  limit, NOT added to `_open_orders` so polling doesn't fetch the
  fake order_id), counter increments across multiple fallbacks, and
  the cooldown lifecycle for two consecutive fallback bets (first one
  trips, second one inherits).
- `test_live_reconciliation.py`: Unit tests for
  `live_reconciliation.reconcile_orphan_fills`. Wallet-keyed
  Polymarket data-api joins, status normalization
  (`NormalizeOrderStatusTests`), and recovery of fills that the live
  loop missed. Uses `_FakeCLOB` + `_FakeEngine` doubles.

### Analysis: unified table / training table / candidate universe

- `test_build_unified_signal_table.py`: Unit/integration tests for
  `scripts/analysis/build_unified_signal_table.py`. Covers horizon CSV
  parsing and validation, dynamic master-column generation, phase2 capture
  feature computation (ask/bid at horizons, ask velocity, spread, fill
  simulation), state-value candidate field propagation
  (`edge`, phantom risk, ltp gap), weather-v2 field propagation, and
  missing-data guards. Uses
  `CaptureData` named tuple with `snapshots`, `session_rows`,
  `candidate_rows` dicts.
- `test_build_signal_training_table.py`: Unit tests for
  `build_signal_training_table.py`: train/val/test allocation and temporal
  ordering, settled vs. unsettled label generation
  (`target_filled`, `target_win`, `target_profit`), and the
  `PRE_SIGNAL_COLUMNS` definition (state-value fields, phantom risk,
  remaining half-innings, weather-v2 fields). Catches leakage if a
  post-signal column sneaks into pre-signal.
- `test_build_calibration_opportunity_training_table.py`: Unit tests for
  `build_calibration_opportunity_training_table.py`. Joins
  `score_confirmations.jsonl` and `calibration_opportunities.jsonl`
  sidecars to candidate rows, generates per-family training rows, and
  writes the combined audit + per-family files under
  `data/analysis_output/calibration_opportunity_training/`.
- `test_side_neutral_under_alpha.py`: Cross-module tests covering the
  side-neutral research stack -- `build_side_neutral_opportunity_table`
  pairing `over_yes`/`under_no` ticks, `build_under_paper_ledger` UNDER
  replay, and `train_market_anchored_alpha` market-anchored alpha fit.
  Analysis-only; live trading still ignores `under_no` ticks.

### Analysis: report builders

- `test_build_execution_diagnostics_report.py`: Unit tests for
  `build_execution_diagnostics_report.py`. First-limit-touch detection from
  snapshots (earliest tick where ask crosses limit price), cancel reason and
  counterfactual outcome recording, posted-limit fallback to `limit_price`
  field, counterfactual profit for settled vs. unsettled.
- `test_build_state_value_transition_report.py`: Integration test for
  `build_state_value_transition_report.py`. Imports via `sys.path` insert.
  Asserts the report cleanly separates `score_event_transition` regimes
  (by phantom risk x current edge) from `no_score_drift` regimes (by
  poisson/empirical/both support), with `win_rate` rollups and top-N
  ranking.
- `test_build_daily_human_review_report.py`: Integration test for
  `build_daily_human_review_report.py`. Stages temp sessions, candidates,
  and log files; asserts compact JSON + Markdown output, the nesting rules
  (`candidate_rollup` and `shadow_feature_diagnostics` removed from
  `session_summary` but retained at report root), bet-level field
  preservation (`filled_shares`, `fill_cost_usdc`, `payout_usdc`), and
  log-health regex parsing.
- `test_run_daily_refresh.py`: Unit tests for the startup/post-session refresh
  orchestrator. Covers completed-session date selection (active run date is
  excluded by default), stale daily-review detection, planned refresh step
  composition including active-date weather-cache refresh, skip flags, and
  plan-only manifest writing without spawning subprocesses.
- `test_calibration_stability_gate.py`: Tests for the
  method-stability gate added to `calibrate_signal_probabilities.py`
  on 2026-05-14. Covers modal selection math (unanimous, majority,
  tie returns None, empty), trailing-family-history extraction
  (chronological order, same-date dedup keeps the latest entry,
  `exclude_date` skips today, window clamps to N), the gate verdict
  itself (catches both real 2026-05-12 flips, no-op when today matches
  modal, no-op below min_history, no-op on modal tie, allows drift
  through after the new method wins enough days), JSONL history I/O
  (round-trip, append on multiple writes, missing file is empty,
  malformed lines are skipped), and `_history_row_date` fallback
  semantics (prefers data_max_date, falls back to generated_at_utc
  prefix, empty row returns empty string).
- `test_build_walk_forward_certification.py`: Tests for the Active #1
  certification report builder. Covers the readiness verdict transitions
  (READY/PRELIMINARY/INSUFFICIENT thresholds), `BetRow` projection
  (None-safe field extraction, drop unparseable rows, missing-bucket
  fallback for cohort fields), cohort band assignment (inclusive cuts +
  missing handling), `CohortStats` math (fill rate, ROI, max drawdown
  from a profit time series), and the per-gate sweep + verdict logic
  (KEEP when blocked cohort is worse, RETUNE when blocked cohort is
  materially better, KEEP-low-confidence when the blocked cohort is
  too thin to evaluate). End-to-end main() writes both JSON and Markdown
  artifacts; empty-input renders the safe "no data" page.
- `test_analyze_stake_scaling_promotion.py`: Tests for the Active #6
  part 2 stake-scaling promotion-gate analyzer. Covers bet extraction
  from session JSON (filter to filled+settled bets that carry
  `calibrated_stake_multiplier`), tercile cutpoints under bimodal data
  (clamps at 0.5 / 1.5 floor + ceiling -- inclusive cuts on both sides
  so the high cohort isn't lost to ties), bucket-aggregate metrics
  (WR / ROI / avg multiplier / avg edge), the four verdict states
  (`need_more_data` on session count, `need_more_data` on thin bucket,
  `hold` when margin is too small, `promote` on a clear win), and the
  end-to-end main() writing both JSON and Markdown artifacts.
- `test_build_weekly_drift_rollup.py`: Tests for the weekly drift HTML
  rollup. Covers per-day metric extraction (including the
  attempt-vs-CLOB-success fill-rate split that motivated the 2026-05-13
  fix -- the rollup uses `fill_rate_health.today.placed` as the
  denominator so wallet-balance error days surface clearly), trailing
  window selection, end-to-end main() writing dated + canonical HTML
  files, alerts-feed ordering (newest first), KPI math against the
  attempt denominator, and a safe "no data" page when the input dir
  is empty.
- `test_run_daily_refresh_smoke.py`: End-to-end smoke test that walks the
  full daily-refresh pipeline (~63 steps as of 2026-05-25; the test was
  originally written against a 32-step skeleton and asserts on the live
  count via `result_names` rather than a hardcoded number) with
  `subprocess.run` stubbed out. Asserts every planned step has a result
  entry, the manifest is written with the expected fields,
  `refresh_health_rollup` reads alert counts from the daily-review JSON,
  `StalenessCheck` skips Stage-2 staging when the artifact already exists
  newer than its inputs, `--force-retrain` bypasses the skip, and a
  non-zero subprocess exit is non-strict by default. Catches
  refresh-wiring regressions, not builder correctness.
- `test_fair_value_stage_ablation_report.py`: Unit tests for
  `fair_value_stage_ablation_report.py`. Covers the Brier and AUC
  decomposition by FV stage (`current_state_value_base_poisson`,
  `*_fv_raw`, `*_fv_calibrated`) and the score-confirmed sub-window
  rollups that surface the calibration anomaly fixed on 2026-05-08.
- `test_build_model_maturity_report.py`: Unit tests for
  `build_model_maturity_report.py`. Covers per-family rolling profit
  rollups, pricing-aware win-unit accounting, day-bucket regimes, and
  the maturity gate boolean used by the live engine to flag immature
  families.
- `test_weather_client.py`: Unit tests for `scripts/trading/weather_client.py`.
  Covers stadium metadata alias matching, Open-Meteo-shaped hourly payload
  selection, derived air-density / wind-out fields, JSON cache writing, and
  flattened weather-feature loading by `game_pk`.
- `test_build_mlb_ou_cache.py`: Unit tests for the `cache/build_mlb_ou_cache.py`
  Stage-1 cache builder. Covers regular-season filtering, run-totals
  aggregation per `(away_runs, home_runs)` cell, and JSON output shape.
- `test_stage2_run_env_model.py`: Stage-2 runtime tests focused on the
  `density_alt` / `hr_factor` families and their backward-compatible
  fallback when `park_hr_factors.json` is missing. Imports directly from
  `cache/stage2_run_env_model.py`.

### Analysis: modeling / calibration

- `test_train_baseline_models.py`: Unit tests for `train_baseline_models.py`.
  Covers `fit_preprocessor()` numeric/categorical detection and feature
  generation (`cat::col==val`), logistic regression fit/predict, and
  `metric_summary()` (`accuracy_0p5`, AUC, logloss).
- `test_probability_calibration_families.py`: Cross-folder test --
  `ProbabilityCalibrator` from `scripts/trading` plus the offline
  `calibrate_signal_probabilities.py` builder in `scripts/analysis`. Tests
  family inference from historical bet rows (default
  `score_event_transition`), runtime routing in `separate` mode with
  family-specific Platt calibrators, fail-closed enforce-mode behavior on
  missing families (p=1e-8), and tempfile-driven calibration script I/O
  with separate-families output.
- `test_team_offense_weights_externalization.py`: Tests the Stage-3 v2
  weights externalization (2026-05-12). Covers
  `team_offense_model._load_weights_overrides` (missing file falls back
  to compiled defaults, malformed JSON falls back, wrong
  `schema_version` falls back, partial overrides only override what's
  present), `TeamOffenseModel.from_payload` plumbs overrides through to
  instance attributes, and `promote_team_offense_v2.build_weights_payload`
  extracts betas + shrinkage from `phase4_models.json` -- including the
  end-to-end loop where the promoted file is consumed by the runtime
  loader.

### Analysis: walk-forward / policy backtests

- `test_walk_forward_runner.py`: Integration test for `walk_forward_runner.py`.
  Covers `plan_windows()` rolling train/val/test date splits with
  `min_train_dates`, `start_date` prior-history preservation, strict-mode
  failure on insufficient data, `aggregate()` metrics
  (`accuracy_0p5` mean, baseline vs. `model_policy` profit delta, max
  drawdown, incremental profit), and `_observed_order_model_policy_results()`
  EV filtering.
- `test_backtest_ev_policy.py`: Unit tests for `backtest_ev_policy.py`. EV
  formula `_ev_if_filled`, policy application with daily caps + per-day
  filtering, `policy_metrics()` (`realized_profit_sum`,
  `expected_profit_sum`, `fill_rate`, `win_rate_filled`), and
  `choose_best_policy()` grid search with `min_validation_trades` fallback
  to `"positive_expected_any_trades"`. Also protects the EV artifact split:
  runtime win/fill features exclude post-signal, token/provenance, and Kelly
  sizing columns while strict fill keeps post-signal analysis fields.
- `test_evaluate_no_score_drift_policy.py`: Integration test for
  `evaluate_no_score_drift_policy.py`. Imports via `sys.path` insert. Dedup
  to first candidate per `(game, line, score_segment)`, support-regime
  separation (`poisson_and_empirical_support`, `poisson_only`,
  `empirical_only`), report `by_support_regime` `win_rate` rollups.
- `test_no_score_drift_walk_forward.py`: Integration test for
  `no_score_drift_walk_forward.py` -- the canonical no-score-drift
  validation path called out in the trading AGENT_CONTEXT.md. Covers
  `build_training_rows()` merging `policy_rows` and `ledger_rows`
  (`dedup_candidate_rows`, `target_win`, `paper_profit_usdc`),
  `plan_windows()` rolling windows, `run_window()` model training with
  thresholds and `min_train_rows`, and `probability_metrics`.
- `test_build_no_score_drift_paper_ledger.py`: Integration test for
  `build_no_score_drift_paper_ledger.py`. Taker and `ask_minus_cents`
  price policies, `daily_budget` and `per_game_budget_fraction` caps,
  `max_orders_per_game_line` dedup, `touch_within_segment` fill assumption
  with `touch_window_seconds`, and `summarize_ledger()` (`roi`,
  submitted/filled/skipped counts).
- `test_build_queue_aware_execution_replay.py`: Unit tests for
  `build_queue_aware_execution_replay.py`. `queue_adjusted` fill model
  (ask must move through `posted_limit`, not just touch), `fill_seconds`
  interpolation, summary profit comparison across
  `current_limit` / `limit_p1c` / `taker_like` x touch / queue-adjusted.
- `test_learn_execution_policy.py`: Unit tests for the Active #7
  `learn_execution_policy.py` prototype. Covers the pivot+aggregate of
  per-bet realized rows, the baseline / oracle / cohort lookups, and
  the leave-one-out cross-validation guard that prevents overfitting at
  n~=71. Includes an end-to-end test that runs the script against a
  synthetic queue-aware-replay JSONL and asserts the output report
  shape under
  `data/analysis_output/execution_policy_prototype/`.

### Observability / diagnostics

- `test_monitor_pitcher_cache.py`: Unit test for the pitcher-cache helper in
  `monitor_mlb_polymarket_ou.py` (`scripts/monitor`). Imports via `sys.path`
  insert and uses tempfile. Validates rebuild-once-per-refresh-date logic
  with `_FakeStatsClient` stub.
- `test_observability_noise_rollups.py`: Integration / characterization
  tests for `mlb_poly_monitor.py` and `signal_engine.py` log behavior.
  Repeated schedule-refresh compaction (info logged once, debug
  suppressed), poll-progress debug periodicity, noisy-library logger
  suppression (`urllib3`, `httpcore`, `hpack`), book-retirement retry
  rollups, and `candidate_rollup` counter preservation
  (`attempted_rows`, `written_rows`, `dedup_suppressed_rows`,
  `by_decision_reason`). Uses `assertLogs()`.
- `test_monitor_cli_performance_mode.py`: Unit test for the monitor
  CLI's `--performance-mode` / `--no-performance-mode` toggle (psutil
  affinity + HIGH priority pinning).

### UNDER side-aware family (Phase A5 of bidirectional pivot)

- `test_under_calibrator.py`: UNDER-specific Platt calibration
  artifact (`signal_win_calibration_under.json`); separate from
  OVER curve.
- `test_under_book_ingestion.py`: UNDER-side book ingestion + ask
  derivation from `under_no` ticks.
- `test_under_candidate_emission.py`: `--under-emission-mode shadow`
  emits one UNDER candidate row per OVER candidate that reaches FV
  phase; gate evaluation; `decision=shadow_under` /
  `gate_no_under_liquidity` / `gate_min_edge` skip reasons.
- `test_build_under_candidate_universe.py`: UNDER candidate universe
  table writer (pairs to OVER table).
- `test_build_under_state_value_transition_report.py`: UNDER-side
  state-value transition report.
- `test_under_walk_forward.py`: UNDER walk-forward harness rolling
  windows + per-window metrics.
- `test_side_neutral_under_alpha.py`: cross-module tests covering the
  side-neutral research stack and the UNDER paper ledger.

### Stage-1 family (Alt-A staging + scope + audit)

- `test_stage1_cache_promote.py`: `stage1_cache_promote` inline
  refresh step — sanity guard (game-count floor + coverage window)
  before promoting `cache/mlb_ou_cache.staging.json` to
  `cache/mlb_ou_cache.json`.
- `test_stage1_shadow_empirical_runtime.py`: runtime Alt-A shadow
  logging path; both production + Alt-A FVs logged per candidate.
- `test_stage1_support.py`: Stage-1 support/trust proxy math
  (`effective_n_proxy`, `stage1_trust_weight`, support buckets,
  exact-cell flags).
- `test_stage1_inferred_empirical_audit.py`: daily overconfidence
  audit (Poisson vs empirical sibling reconstruction).
- `test_build_stage1_cell_loss_attribution.py`: cell-conditional
  loss attribution (Active #10).
- `test_build_stage1_shadow_override_report.py`: Alt-A + Alt-B
  counterfactual replay; cohort breakdown across 5 dimensions.

### Promote.py + auto-daemon

- `test_promote_cli.py`: `promote.py` 4 levers (stage2, stage3-v2,
  stake-scaling, gate-threshold); verdict-gate refusal; --force
  override; audit row append; atomic swap + backup.
- `test_promote_stage1_subcommand.py`: stage1 promote + demote
  subcommands (added 2026-05-17, closes the last gap in the
  promote.py coverage matrix).
- `test_promote_side_field.py`: `direction: "promote"|"demote"` tag
  on audit rows; backward-compat read of pre-field rows.
- `test_auto_promote_demote_daemon.py`: daemon `--mode preview|act|off`;
  14-day cooldown; per-lever opt-outs; subprocess isolation; audit
  symmetry with manual promotions.
- `test_daemon_retrospective.py`: post-daemon outcome audit.
- `test_fast_wilson_demote.py`: fast Wilson-UB demotion path
  (Active #13); fires at N>=20 post-promotion fills when Wilson
  upper bound on WR < mean entry_ask (breakeven) at 95% one-sided
  confidence; daemon bypasses standard cooldown for `fast_demote`.
- `test_stage3_v2_promotion_check.py`: Stage-3 v2 promotion-gate
  verdict (compares phase4_models.json vs production weights).
- `test_stage2_promotion_stability_gate.py`: Stage-2 staging
  validation-Brier stability gate before promote.

### Lineage / artifact tracking

- `test_artifact_lineage.py`: build-time + promotion-time lineage
  block schema (git_sha, builder_path, input_hashes,
  input_dir_summaries, built_at_utc, python_version).
- `test_artifact_lineage_freshness_report.py`:
  `build_artifact_lineage_freshness_report.py` writer (per-artifact
  generated/max dates, row/family counts, stale-downstream flags).
- `test_lineage_v2_builder_wiring.py`: V2 lineage stamped on
  Stage-1, Stage-2, Stage-3 v2, EV-policy, walk-forward cert.
- `test_lineage_v3_startup_logging.py`: startup-time INFO log per
  cache load; `cache_lineage_freshness_health` daily-review block.
- `test_cross_artifact_consistency.py`: V4 cross-artifact
  consistency check via `compare_input_hash` helper.

### Drift detection (the 7-dimension surface)

- `test_build_concept_drift_report.py`: PSI on continuous features,
  TVD on categorical; trailing 7d vs prior 30d baseline.
- `test_build_drift_in_drift_report.py`: OLS slope on
  `psi_history.jsonl`; project 30d forward; insufficient-history
  guard.
- `test_calibration_input_drift_audit.py`: input-drift audit hook
  in `calibrate_signal_probabilities.py` (Phase 3 select-method
  audit dict).
- `test_calibration_stability_gate.py`: method-stability gate
  preserved from earlier (2026-05-14); 5/14 platt-vs-isotonic
  modal selection.

### Settlement-truth, loss-attribution, gate counterfactual

- `test_verify_settlement_truth.py`: 7 result codes (`ok`,
  `resolution_mismatch`, `total_mismatch`, `stale_filled`, etc.,
  Active #12). Catches scraper-timing snapshots vs real
  data-refresh gaps.
- `test_build_loss_attribution_report.py`: per-bet 4-stage
  probability decomposition; logit-additive FV chain identity
  verified to 0.001 tolerance.
- `test_build_gate_counterfactual_report.py`: per-gate threshold
  sweep + top_recommendations ranking by $/30d saved.

### Quote engine + parallel engines (multi-engine architecture)

- `test_live_quote_engine.py`: two-sided quote engine shadow.
- `test_quote_engine_shadow_report.py`: shadow report builder.
- `test_parallel_engines_mvp.py`: 14 tests covering
  `launch_parallel_engines.py` PRESETS, EngineConfig, RunningEngine,
  RESERVED + LIVE_ONLY flag rejection, label regex, _wait_for_engines.
- `test_inventory_tracker.py`: per-game/per-line exposure tracker
  used by correlated-line exposure cap and (future) quote engine.

### Weekly rollup + stake scaling

- `test_build_weekly_drift_rollup.py`: trailing-7d HTML rollup with
  KPI bar + alerts feed + sparklines.
- `test_analyze_stake_scaling_promotion.py`: Active #6 part 2
  promotion-gate analyzer.

### CLV / FV disagreement / market-anchored alpha

- `test_build_clv_report.py`: CLV diagnostics by family/gate/bucket.
- `test_build_fv_disagreement_quality_report.py`: market-benchmark
  diagnostic for raw FV.
- `test_fv_disagreement_quality_walk_forward.py`: anti-overfit
  validation lane for FV disagreement.
- `test_calibration_market_anchored_alpha_walk_forward.py`:
  rolling family-separated alpha walk-forward.
- `test_build_fv_trust_shrinkage_experiment.py`: support-weighted
  logit shrinkage toward market anchors.

### Misc

- `test_evaluate_stage1_prior_candidates.py`: broad Stage-1 prior
  proxy screen (rolling 3-10y, exponential recency, scoring-env
  weights, blends).
- `test_scoring_path_features.py`: linescore-inning-run feature
  computation (`scoring_inning_rate`, `burst_share`, etc.).
- `test_runtime_refit_artifact_purpose.py`: `--artifact-purpose
  evaluation|runtime-refit` split on calibration + EV-policy
  artifacts.
- `test_backup_retention_and_psi_history_gc.py`:
  `.prior_promote_archive/` backup retention (keep 5 most recent)
  + PSI-history GC (PSI_HISTORY_RETENTION_DAYS=365 on every append).
- `test_build_analysis_safe_trade_table.py`: canonical session/ledger
  trade table; excludes order_status=error attempts; labels live vs
  paper fallback.
- `test_live_engine_overrides.py`: runtime config overrides layer
  (Active #15 — daemon-actuated lever changes without engine restart).

## Cross-Folder Dependencies

The suite intentionally crosses package boundaries -- this is the only place
where `scripts/trading` runtime objects and `scripts/analysis` builders are
exercised against each other on the same data.

- `test_probability_calibration_families.py` instantiates the runtime
  `ProbabilityCalibrator` AND drives the offline
  `calibrate_signal_probabilities.py` builder over tempfile artifacts.
- `test_signal_engine_phase1_characterization.py` asserts on
  `candidate_table.OUTPUT_COLUMNS` (analysis) using rows produced by
  `signal_engine` (trading), so a schema-drifting change in either side
  surfaces here.
- `test_observability_noise_rollups.py` covers monitor (`scripts/monitor`)
  and engine (`scripts/trading`) jointly; do not split it without rethinking
  fixture setup.

When a single change touches both sides of one of these pairs, run the
relevant cross-folder test before claiming the patch works.

## Test Conventions

- **Import path**: leaf trading modules import directly from `scripts/trading`
  via the conftest-managed `sys.path`. Analysis-side tests do their own
  `sys.path.insert(0, str(ANALYSIS_DIR))` -- match that style for new
  analysis tests instead of relying on global config.
- **Repo root discovery**: `PROJECT_DIR = Path(__file__).resolve().parents[1]`
  is the canonical pattern. Don't hardcode absolute paths.
- **Engine stubs**: replace methods on the instance, don't subclass the
  engine. The trading AGENT_CONTEXT.md lists the supported stub points
  (`_record_candidate_decision`, `_candidate_log_path`, `_save_session`,
  `_start_tape_capture`, `_compute_stake`, `_evaluate_ev_policy`,
  `_build_shadow_order_diagnostics`); keep new tests inside that surface.
- **Tempfile workflow**: builder/replay tests use
  `tempfile.TemporaryDirectory()` + `_write_signal_rows` / `_read_jsonl`.
  Don't write into the real `data/` tree from tests.
- **Golden assertions**: when a characterization test breaks, the question
  to ask is "did I intend to change this row?", not "how do I make the
  assertion pass?". Update the golden values explicitly when the change is
  intentional.
- **Log testing**: `assertLogs()` is the pattern in
  `test_observability_noise_rollups.py`. Preserve logger-level state across
  setup/teardown -- some tests assert noisy-library loggers stay
  suppressed.

## Daily Data Efficiency

- Tests should not write under `data/` or any production path; use
  `tempfile.TemporaryDirectory()` and pass the temp root explicitly.
- Synthetic JSONL fixtures should be the smallest set that exercises the
  branch under test. The unified-table and walk-forward suites already
  follow this -- match the pattern instead of dumping a full session
  capture into the repo.
- When adding a fixture file (rare; most tests build inputs in-Python),
  put it under `tests/fixtures/` and document the schema it represents
  in the test docstring.

## Safe-Edit Checklist

- Do not loosen golden assertions just to make a failing run green.
  `test_signal_engine_phase1_characterization.py`,
  `test_build_state_value_transition_report.py`, and
  `test_build_daily_human_review_report.py` exist specifically to catch
  silent schema/decision drift.
- When a candidate row schema, ledger row schema, or report payload changes,
  update the trading AGENT_CONTEXT.md / analysis AGENT_CONTEXT.md and the
  matching tests in the same patch.
- Live-execution invariants in `test_trading_live_execution_fixes.py`
  (USDC->shares, line release after unfilled, Kelly floor-to-min opt-in,
  fail-closed EV policy, calibration family routing,
  legacy `sig_type=2` vs deposit-wallet `sig_type=3` selection) mirror the
  trading AGENT_CONTEXT.md "Live Execution Invariants" section. Changes to
  one require changes to the other.
- Probability-calibration tests assume separate per-family curves
  (`score_event_transition` vs `no_score_drift`) and fail-closed enforce
  behavior. Don't pool families to make a test pass.
- Cross-folder schema tests
  (`test_signal_engine_phase1_characterization.py` against
  `candidate_table.OUTPUT_COLUMNS`,
  `test_probability_calibration_families.py` against
  `calibrate_signal_probabilities.py`) are the canary for trading/analysis
  drift -- keep them green before claiming an analysis or trading-runtime
  patch is done.
- Run `python -m compileall -q tests` plus `pytest tests` before handing
  off. Many tests stage tempfiles or JSONL fixtures, so a green compile is
  not enough.
