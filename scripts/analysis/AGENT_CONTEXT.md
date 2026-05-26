# scripts/analysis Agent Context

This package is the offline research and reporting layer for MLB Polymarket Over
trading. It consumes durable artifacts produced by `scripts/trading` (sessions,
ledgers, candidate universe, book / tape sidecars) plus raw MLB feeds and
Polymarket tick data, and emits structured reports, training tables, calibration
artifacts, and walk-forward validation results.

Last checked against the active `scripts/analysis/*.py` files: 2026-05-16
(daily refresh expanded to a 45-step base pipeline with artifact lineage/
freshness auditing; **as of 2026-05-25 the refresh has grown to ~63 steps**
with the addition of UNDER-family side-aware coverage, drift detection
(concept_drift + drift_in_drift + cross_artifact_consistency), Stage-1
loss attribution + shadow-override + cell-conditional, gate
counterfactuals, settlement-truth verification, Stage-1 Alt-A staging,
Stage-3 v2 promotion check, auto-promote/demote daemon + retrospective,
and the live quote-engine shadow; the original 2026-05-16 step list
below is preserved but no longer exhaustive)
freshness auditing, FV gap decomposition,
FV trust/shrinkage experiment, calibration market-anchored alpha research
models plus rolling alpha walk-forward validation, CLV/late-price diagnostics,
FV disagreement quality diagnostics,
and the analysis-safe trade table;
Stage-2/3 + EV-policy retraining;
`model_freshness_health` + `refresh_health_rollup` inline handlers added;
`StalenessCheck` skip-if-fresh policy lets heavy retrains skip when
their inputs haven't changed; `learn_execution_policy.py` prototype
wired in; daily human-review now carries seven-dimension drift alerts
(calibration / fill / signal / regime / cohort-ROI / concept-drift /
drift-in-drift) + reconciler tracking, with Wilson-interval gating on
the rate-drop alerts; two leading indicators -- concept-drift (PSI/TVD
on model inputs vs prior-30d baseline) and drift-in-drift (linear
trend on psi_history projected 30d forward, catches slow-creep).
(2026-05-08: startup refresh promoted to canonical daily refresh: scrape
+ team_game_log + preflight steps wired in.)
(post TR20 Stage-3 v2 swap; v1 `team_offense_model.py` deleted, v2 contents
installed under the same filename + class name).
(post weather-v2 feature propagation).

## Recent changes

_Append dated bullets here when you change anything in this folder.
Mirrors `MASTER_CONTEXT.md`'s "Recent major shifts" pattern; bump
"Last checked" above when you sweep the whole doc. The canonical
daily-refresh step list lives in `build_refresh_steps()` in
`run_daily_refresh.py` -- run `python scripts/analysis/dump_refresh_steps.py`
to print the live list rather than maintaining it here by hand._

- **2026-05-25 (Tier 2.5)** — `_fit_calibration_bundle` (311 lines)
  split into 4 phase helpers under new `calibration/bundle_phases.py`.
  `calibrate_signal_probabilities.py` shrunk 1152 → 991 lines.
  Public surface preserved.
- **2026-05-25** — `dump_refresh_steps.py` helper added: prints the
  canonical refresh step list from `build_refresh_steps()` as
  Markdown/names/count. Single source of truth for the step list.
- **2026-05-21** — Scoped Alt-A enforce (TR25) shipped; runtime
  decision in `_apply_stage1_alt_a_scope` with `hold_poisson` rule
  for `inning>=8` cohort.
- **2026-05-19** — Band-gated calibrator enforce (TR23) shipped:
  `DEFAULT_PROB_CALIBRATION_MODE=enforce`, `*_MIN_RAW=0.90`.
- **2026-05-19** — UNDER candidate emission shipped (TR26):
  `signal_win_calibration_under.json` artifact + new `under_*`
  refresh steps.

Strategic frame: every script in this folder is read-only with respect to live
trading state. Analysis writes go under `data/analysis_output/<topic>/` and to
`cache/` (for shared intermediate caches like `team_game_log.json` and
`pitcher_cache.json`). Promotion from shadow to live trading should be backed by
a walk-forward run from this folder, not ad-hoc spreadsheets.

The two active model families are score-event transition (the live-enforced
strategy) and no-score drift (shadow-only). Each has its own dedicated
walk-forward + paper-ledger pipeline; do not pool them.

## Analysis Dataflow

1. Runtime drops durable artifacts under `data/{live_trading,paper_trading}/`:
   sessions JSON, master ledger, candidate_universe (candidates + outcomes),
   book/tape sidecars, calibration opportunities, score confirmations.
2. `build_unified_signal_table.py` joins sessions + ledgers + candidates + book
   captures into the canonical `signals_master.jsonl` with a snapshot sidecar.
   This is the central event-level table that downstream training and
   calibration scripts read. It now carries flattened weather-v2 fields from
   candidate/session rows so walk-forward can test local weather without
   reparsing cache files.
3. `build_signal_training_table.py` derives a leakage-aware training table
   (pre-signal vs. post-signal feature gating) used by baseline training, EV
   policy backtests, and walk-forward windows. Weather-v2 raw fields are
   pre-signal audit columns; model feature lists use
   `WEATHER_MODEL_FEATURE_FIELD_KEYS`, which excludes cache/provenance IDs and
   raw error text.
4. `build_candidate_universe_table.py` produces a parallel decision-level table
   (trade + no-trade rows) joined with final outcomes for gate audits.
5. Specialized rollups -- execution diagnostics, queue-aware execution replay,
   state-value transition report, no-score drift policy / paper ledger -- read
   from the unified or candidate-universe tables, not raw runtime logs.
6. Calibration and walk-forward scripts emit artifacts that runtime can load
   (probability calibration JSONs) or that gate decisions consume manually
   (extreme-edge threshold sweeps, EV policy reports).

## Output Conventions

- All persistent analysis writes land under `data/analysis_output/<topic>/`.
  Existing topics: `unified_signals/`, `training_tables/`, `model_baselines/`,
  `calibration/`, `execution_diagnostics/`, `execution_replay/`,
  `state_value_transition/`, `no_score_drift_policy/`,
  `no_score_drift_paper_ledger/`, `no_score_drift_walk_forward/`,
  `side_neutral_opportunities/`, `under_paper_ledger/`,
  `market_anchored_alpha/`,
  `fv_trust_shrinkage/`,
  `artifact_lineage_freshness/`,
  `candidate_universe/`, `daily_human_review/`.
- Shared intermediate caches go in `cache/` (consumed by both runtime and
  analysis): `team_game_log.json`, `pitcher_cache.json`,
  `weather/game_weather_<date>.json`, `mlb_ou_cache.json`,
  `mlb_stage2_run_env.json`.
- Reports usually emit a triple: `*_summary.json` (scalar metrics), `*_rows.jsonl`
  (one row per evaluated unit), and `*_rows.csv` (same content for spreadsheet
  consumers). New scripts should follow this convention unless there's a reason
  not to.
- Build manifests (`manifest.json` / `build_manifest.json`) record arg state,
  schema version, row counts, warnings, and hard errors. Keep them compact and
  parseable.

## File Ownership

### Runtime support caches (pure builders, no analysis output)

- `build_team_game_log.py`: scans raw MLB game feeds under
  `data/games/regular/{year}/` and writes per-team final-score history to
  `cache/team_game_log.json`. Required input for `team_offense_model.py` and
  `analyze_polymarket_overreactions.py`.
- `build_pitcher_cache.py`: pulls current-season ERA from the MLB Stats API
  (filtered by min-IP) and writes `cache/pitcher_cache.json`. Falls back to
  league-average ERA when missing.
- `build_park_hr_factors.py`: scans raw MLB game feeds under
  `data/games/regular/{year}/`, counts `result.eventType == "home_run"` plays
  per game, aggregates by `(park, year)`, computes per-year league HR/game
  baselines, and writes per-(park, year) shrunk HR factors to
  `cache/park_hr_factors.json` (shrinkage prior n=30 games of league-average
  HRs). Stage-2 input for the `hr_factor` family added 2026-05-08 (TR21).
  Read by `cache/stage2_run_env_model._load_park_hr_factors()`. Wired into
  the canonical daily refresh as the `park_hr_factors` step.
- `team_offense_model.py`: Stage-3 runtime applier. Deployed 2026-05-07
  (TR20). Blends three EB-shrunk per-team windows -- `prior_season_rpg`
  (NEGATIVE coef -0.1514, regression-to-mean correction), `season_rpg_to_date`
  (+0.1407), `momentum_rpg_10` (+0.1503) -- multiplied by a linear inning-
  weight ramp. Coefficients are the full-window 2021-2024 fit from Phase 4
  Model 3. Replaced the prior single-50-game-window model
  (`LOGIT_DELTA_PER_RUN=0.20` + hard clamp) which Phase 1 showed was ~3.2x
  too aggressive on a 1.13M-row leakage-free residual table over 2021-2026.
  Imported by runtime; CLI mode is a demo. See
  `model_improvements/team_offense_v2_*` for the full audit trail.
- `calibrate_team_offense_v2.py`, `build_team_offense_calibration_table.py`,
  `build_team_offense_features.py`, `report_team_offense_residuals.py`,
  `report_team_offense_mle_deltas.py`, `report_team_offense_features.py`,
  `phase45_stability_check.py`: offline calibration + diagnostic pipeline
  used to fit and audit the TR20 Stage-3 model. Outputs land in
  `data/analysis_output/team_offense_calibration/`. Re-run any of these to
  refit / re-audit; coefficients in `team_offense_model.py` should be
  updated only after a fresh Phase 4.5 stability check + Phase 5 dollar
  test.

### Unified table builders (canonical inputs to other scripts)

- `build_unified_signal_table.py`: thin CLI / orchestration shell that joins
  sessions, ledgers, candidate rows, and book captures into
  `signals_master.jsonl` + `signal_book_snapshots.jsonl` + `order_events.jsonl`
  + `manifest.json`. Heavy lifting lives in the `unified_signal_table/`
  subpackage (see below).
- `build_candidate_universe_table.py`: decision-level table (trade + no-trade)
  built from candidate_universe JSONLs joined with final game outcomes. Adds
  remaining-opportunity, weather-v2, and shadow-diagnostic fields. Output:
  `candidates_master.{jsonl,csv}` + `build_manifest.json`. Imports the
  `scripts.trading.{model_families, remaining_opportunity,
  shadow_diagnostic_features, weather_client}` modules so feature definitions
  stay in sync.
- `build_analysis_safe_trade_table.py`: canonical session/ledger trade table
  for P&L and execution measurement. Session JSONs are primary, ledger rows are
  deduped by `(bet_id, order_id, event)`, `order_status="error"` attempts are
  excluded by default, and execution mode is explicit (`live`,
  `paper_fallback`, `dry_run`, `unknown`). Outputs
  `analysis_safe_trades.{jsonl,csv}` + `analysis_safe_trades_summary.json`.
- `build_calibration_opportunity_training_table.py`: model-bearing opportunity
  table from compact runtime calibration rows. Score-event rows are deduped by
  `(game,line,inning,half,outs,runners,score,reason)` by default before model
  training output is written; `--score-event-repeat-policy weight|none`
  remains available for targeted audits. It also backfills selected
  `inferred_state_*` summary fields from same-day raw candidates when older
  compact calibration sidecars predate those fields, then derives missing
  selected summaries from the +1/+2/+3 run-count panel where possible.
- `build_signal_training_table.py`: leakage-aware training rows from
  `signals_master.jsonl`. Includes flattened weather-v2 fields as pre-signal
  audit columns when present, while the manifest exposes only model-safe
  weather fields for training. Outputs `signal_training_table.{jsonl,csv}` and
  a manifest under `data/analysis_output/training_tables/`.
- `backfill_candidate_outcomes.py`: appends missing outcome rows to
  candidate_universe JSONLs from local game data. Useful when a runtime crash
  left candidate rows without matching outcomes (which would otherwise block
  shadow-gate evaluation).

### Modeling and calibration

- `train_baseline_models.py`: trains two logistic baselines on the leakage-aware
  table -- `signal_win` (target_win, pre-signal features only) and
  `execution_fill` (target_filled, pre + post features). Writes model JSONs +
  per-row predictions JSONLs + a summary report. Reused by the walk-forward
  runner.
- `calibrate_signal_probabilities.py`: fits Platt scaling and isotonic
  calibration on settled `signals_master` rows using `won_counterfactual`
  labels. Output: `signal_win_calibration.json` + report + predictions.
  Probability calibration consumed by runtime should come from this artifact.
  Supports `--artifact-purpose evaluation|runtime-refit`; canonical startup
  uses `runtime-refit`, meaning method selection and metrics still come from
  train/validation/test splits, while exported calibration params are refit on
  all eligible labeled rows after selection.
  Method selection runs three layers: (1) validation logloss winner;
  (2) identity-rejection guard (overrides "raw" when train ECE shows
  challenger is materially better calibrated); (3) **method-stability
  gate** (2026-05-14): reads trailing-7-day pre-override selections from
  `data/analysis_output/calibration/selection_history.jsonl` and
  overrides today's pick to the modal when they differ (after ≥ 5 days
  of history). Suppresses platt<->isotonic flip-flops on small
  validation samples. Disable with `--no-stability-gate` for backfills
  or debug runs; tune via `--stability-window` and
  `--stability-min-history`.
- `backtest_ev_policy.py`: builds an EV-ranked policy from baseline models.
  Retrains runtime-safe win/fill artifacts from reliable decision-time fields
  plus a separate strict fill artifact for offline post-signal execution
  diagnostics. Scores rows with `ev_if_filled / ev_realized / ev_per_stake`,
  sweeps a tuning grid (`min_ev_per_stake`, `min_p_fill`, daily-trade cap),
  and reports chosen policy + out-of-sample metrics. Canonical startup passes
  `--artifact-purpose runtime-refit`: policy/hyperparameter selection stays
  validation-based, then exported model weights are refit on all eligible
  labeled rows. Live/shadow runtime must use
  `ev_execution_fill_runtime_model.json`, not the strict fill model.
- `walk_forward_runner.py`: rolling train/val/test harness that reuses
  `build_signal_training_table.build_training_rows()` and
  `train_baseline_models.train_task()` per window. Reports per-window brier /
  logloss / AUC plus baseline live-engine trade metrics. The score-event
  validation lane.
- `no_score_drift_walk_forward.py`: separate walk-forward pipeline for the
  no-score drift family. Row unit is the first eligible shadow candidate per
  game-line/same-score segment to avoid counting repeated ticks as
  independent. The no-score validation lane; do not use the score-event
  walk-forward runner here.

### Reporting and operator-facing rollups

- `session_report.py`: full structured analytics report from
  `data/live_trading/sessions/*.json` -- P&L (filled vs. counterfactual
  cancelled), fill win-rate by inning / edge bucket / Stage-2 delta,
  ask_drop_5s distribution, per-session table, current gate simulation,
  per-venue/park breakdown, near-gate bets.
- `build_walk_forward_certification.py`: Active #1 certification
  report builder. Reads `signal_training_table.jsonl` (filled+settled
  per-bet rows) and emits a per-cohort + per-gate scorecard:
  (a) sample-readiness verdict (`READY` at 150 filled / 30 dates,
  `PRELIMINARY` at 75 / 14, else `INSUFFICIENT`); (b) cohort
  breakdowns across edge / ask / inning / runs-needed /
  current-state-edge / phantom-risk / family; (c) per-gate
  scorecard for **15 enforced gates** in `GATE_DEFS`:
  `gate_extreme_edge`, `gate_min_edge`, `gate_min_inning`,
  `gate_min_entry_ask`, `gate_runs_needed_max`, `gate_max_base_fv`,
  `gate_fv_ask_gap_max`, `gate_min_current_total`, `gate_inn5_rn_max`,
  `gate_inn6_rn_max`, `gate_close_game_rn`, `gate_s2_suppress_max`,
  `gate_high_line_min_edge`, `gate_high_line_min_inning`, plus the
  shadow-only `shadow_gate_current_state_edge_min`. Each sweeps
  alternative thresholds and emits `KEEP` / `RETUNE` / `RETIRE`
  with confidence based on filtered-vs-kept cohort sizes; (d) weekly
  drift table. Output JSON + Markdown to
  `data/analysis_output/walk_forward_certification/`. Wired as a
  refresh step. Note the limitation: gates with zero blocked bets
  in the training table (e.g. `gate_min_edge` -- the table only
  contains bets that already passed) auto-degrade to KEEP with low
  confidence; sweep coverage there will improve once
  candidate_universe rows are wired in as a follow-up.
- `analyze_stake_scaling_promotion.py`: Active #6 part 2 (calibrated
  stake scaling) promotion-gate analyzer. Reads filled+settled bets
  from `data/live_trading/sessions/*_session.json` that carry
  `calibrated_stake_multiplier`, splits them by tercile (bimodal-safe:
  inclusive cuts on both sides so the floor/ceiling-clamped bets route
  cleanly to low/high), aggregates per-bucket WR + ROI + avg
  multiplier + avg edge, and emits a `need_more_data` / `hold` /
  `promote` verdict. Promotion thresholds: ≥30 sessions of shadow data
  AND high-vs-low WR delta ≥ 0.05 AND high-vs-low ROI delta ≥ 0.05;
  all tunable via CLI. Output:
  `data/analysis_output/stake_scaling_analysis/stake_scaling_analysis.{json,md}`.
  Wired as a refresh step before `weekly_drift_rollup`; the rollup
  embeds the verdict + per-bucket table as its own section.
- `build_weekly_drift_rollup.py`: trailing-N-day HTML drift rollup
  (default N=7) consumed by the operator after each refresh. Pure
  read-only over `data/analysis_output/daily_human_review/*.json`;
  no dependency on game / market state. Output:
  `data/analysis_output/weekly_rollup/<end_date>_weekly_rollup.html`
  plus canonical `weekly_rollup.html`. KPI bar (window P&L, ROI,
  attempt-denominator fill rate, filled WR, filled-bet count, active
  alert count), alerts feed (newest first, color-coded by dimension),
  eight inline-SVG sparklines (daily ROI, cumulative P&L, fill rate,
  filled WR, filled bets/day, calibration alerts, regime-mix max TVD,
  reconciler recovered share), and a per-day detail table. Pure
  stdlib, no JS, no third-party deps. Wired as the penultimate step
  of `run_daily_refresh.py`.
- `build_daily_human_review_report.py`: compact daily JSON + Markdown report
  built from durable daily artifacts (session JSON, candidate rollup) plus
  optional log files. Designed to skip multi-MB runtime logs entirely.
  Carries a six-dimension drift-alert surface (shipped 2026-05-11/12,
  cohort-ROI + concept-drift added 2026-05-15): `calibration_health`
  (artifact age, sampled-row identity check, day-over-day method drift,
  with shadow-mode suppression of false-positive applied-share alerts),
  `fill_rate_health` (today vs trailing 7d baseline; alert gated on
  Wilson-UB-significance), `signal_quality_health` (filled-win-rate;
  same Wilson gating), `regime_mix_health` (ask / current-state-edge /
  phantom-band *pre-trade* distribution TVD vs trailing baseline),
  `cohort_roi_health` (filled-bet *outcome* ROI by edge / ask / inning
  / line / current-state-edge bucket; absolute-losing alert at >= 5
  bets / ROI <= -10% AND regime-change alert at >= 15pp delta vs 30d
  baseline), `concept_drift_health` (the only *leading* indicator --
  reads the artifact built by `build_concept_drift_report.py` which
  computes PSI on continuous model inputs and TVD on categorical ones
  vs prior-30d baseline; fires before calibration / cohort drift
  materializes), and `reconciler_summary` (orphan-fill recovery share
  over filled bets). Alerts surface as Notes-block lines so they appear
  at the top of the markdown without drilling into JSON. Also includes
  a shadow Stage-2 suppression dollar audit by joining same-day
  candidate rows to outcome rows.
- `fair_value_stage_ablation_report.py`: startup research diagnostic built
  from the calibration-opportunity table. Compares market ask, current-state
  Stage-1, score-event inference, Stage-2 run-env/weather, Stage-3 team
  offense, and final runtime FV by Brier/logloss/AUC/calibration slope.
  It also accepts `--stage1-cache-path` / `--stage1-cache-label` to
  recompute Stage-1 from a candidate cache while carrying logged Stage-2/3
  deltas forward. Use this for 5y-vs-10y cache comparisons by family, line,
  ask bucket, current-state-edge bucket, and post-2023 validation slices.
- `build_fv_gap_decomposition_report.py`: startup research diagnostic built
  from the calibration-opportunity table. Treats market ask/no-vig midpoint as
  the baseline and decomposes FV separation into current-state Stage-1,
  inferred-state Stage-1, Stage-2/3 movement, and final runtime FV. Also reads
  the +1/+2/+3 run-count inference panel once fresh runtime rows include it.
- `build_fv_trust_shrinkage_experiment.py`: startup research diagnostic built
  from the calibration-opportunity table. Compares raw runtime FV against
  support-weighted logit shrinkage toward market ask and no-vig midpoint,
  separately by `score_event_transition` and `no_score_drift`. Diagnostic-only;
  do not promote a shrinkage policy without walk-forward stability.
- `build_model_maturity_report.py`: startup/shutdown readiness report from
  calibration opportunities. In addition to family metrics/ROI, it now reports
  Under-pair/no-vig and run-count inference-panel coverage by family so
  missing market-context fields are visible before any artifact promotion.
- `train_calibration_market_anchored_alpha.py`: research-only family-separated
  alpha trainer from calibration opportunities. Uses
  `logit(p_over_win) = logit(market_price) + alpha(features)` so market is the
  prior and the model learns only residual disagreement. Outputs live-disabled
  artifacts under `data/analysis_output/calibration_market_anchored_alpha/`.
- `calibration_market_anchored_alpha_walk_forward.py`: rolling
  family-separated validation lane for the alpha trainer. Evaluates
  `score_event_transition` and `no_score_drift` separately, compares alpha
  probabilities against market ask and no-vig baselines, chooses policy
  thresholds on validation only, and reports test P&L at executable ask with
  clustered bootstrap CIs by game/date/line. Diagnostic-only; this is the
  promotion evidence lane for market-anchored alpha.
- `build_clv_report.py`: closing/late-price value diagnostics. Joins
  `signals_master`, `signal_book_snapshots`, `analysis_safe_trades`, and
  calibration-opportunity rows. Reports entry price vs last captured post-signal
  midpoint plus fixed horizons, grouped by family/gate/bucket and compared with
  realized filled-trade ROI. Rows without captured late books remain as
  candidate coverage rows so CLV missingness is explicit.
- `build_fv_disagreement_quality_report.py`: market-benchmark diagnostic for
  raw FV. Starts from calibration-opportunity rows, enriches with CLV when
  available, and ranks family-specific disagreement buckets by outcome
  calibration gain over market, CLV, realized ROI, and Stage-1 support/trust.
  Diagnostic-only; use it to find where independent FV actually beats the
  market baseline before promoting any new gate or alpha policy.
- `fv_disagreement_quality_walk_forward.py`: anti-overfit validation lane for
  the FV disagreement report. For each rolling window it selects candidate
  bucket keys on train dates, requires validation survival, then applies those
  trusted buckets to the next test date. Reports whether trusted disagreements
  improve calibration, CLV, or ROI out of sample versus all disagreement rows.
- `audit_stage1_inferred_empirical.py`: startup research diagnostic built
  from raw candidate universe rows and outcomes. Reconstructs the empirical
  `oXX` sibling for the inferred score-event Stage-1 cell, including support
  counts and line fallback metadata, then compares market ask, Poisson, and
  empirical probabilities on deduped game-line-state rows. This is the daily
  overconfidence audit for the Poisson base FV; it is diagnostic-only and
  must not be treated as a gate/promotion without walk-forward confirmation.
- `analyze_scoring_environment_trends.py`: local-only scoring-environment
  research report for Stage-1 weighting. Dedupes raw game feeds by `gamePk`,
  reports season/month/line Over-rate drift, simple park-adjusted season
  effects, current-season full-year projection from same-date historical
  adjustments, one-step-ahead weighting-scheme backtests, and research-only
  proposed season weights. Outputs to
  `data/analysis_output/scoring_trends/`.
  The paired Stage-1 experiment is
  `cache/build_mlb_ou_cache.py --season-weights-path ... --season-weight-mode allocation`
  writing `cache/mlb_ou_cache_weighted_scoring_env_candidate.json`; this stays
  research-only until cache-swapped FV ablation and walk-forward reports pass.
- `analyze_scoring_path_effects.py`: local-only scoring-path timing research.
  Builds end-of-full-inning observations from 2016-2025 full-length regular
  games and tests whether steady scoring, burst concentration, scoreless
  streaks, and recent-run timing predict future runs after same-score-state
  controls. First pass found directional examples but no useful broad
  out-of-sample lift, so treat scoring path as a candidate feature/logging
  surface, not as a gate or FV change.
  Runtime/analysis now carries the recommended model fields
  (`scoring_inning_rate`, `scoring_half_rate`, `burst_share`,
  `scoreless_streak`, `recent2_run_share`, `weighted_run_inning_norm`,
  `inning_run_slope`) through candidate, unified, calibration, no-score, and
  alpha-model surfaces for walk-forward testing.
- `evaluate_stage1_prior_candidates.py`: broad proxy screen for Stage-1 prior
  choices. Generates rolling 3-10y, equal-season, exponential recency,
  scoring-environment similarity, and blend candidates; exports candidate
  weight CSVs; reports row-level and deduped calibration/profit proxies. Use
  it to decide which expensive full cache artifacts are worth building.
- `refresh_game_weather.py`: active-date weather-cache refresher. Joins MLB
  schedule games to `data/reference/mlb_stadium_weather_metadata.json`, fetches
  hourly local stadium weather by coordinate via `scripts.trading.weather_client`,
  and writes `cache/weather/game_weather_<date>.json`. Runtime flattens this
  cache through `WEATHER_FEATURE_FIELD_KEYS`; live Stage-2 FV uses the same
  Weather v2 fields as its only weather source. Downstream candidate, unified,
  training, no-score policy, paper-ledger, and no-score walk-forward rows all
  preserve those fields when available.
- `build_artifact_lineage_freshness_report.py`: canonical artifact integrity
  report. Checks durable runtime inputs, caches, analysis tables, model
  artifacts, and operator reports for primary path existence, generated/max
  dates, input mtimes/content hashes (directory listing hashes for folders),
  row/family counts, and downstream staleness vs. upstream inputs. Output:
  `data/analysis_output/artifact_lineage_freshness/`.
- `build_drift_in_drift_report.py`: **slow-creep drift detection** --
  the 7th drift dimension and 2nd leading indicator. The companion
  to `build_concept_drift_report.py`: where concept-drift fires on
  day-vs-baseline PSI (>= 0.25), this script catches features whose
  daily PSI never crosses the threshold but whose cumulative drift
  over weeks projects past it. Reads `psi_history.jsonl` (append-only,
  one row per feature per day), filters to a trailing 30d window per
  feature, fits OLS slope on (day_index, psi_value), and projects 30d
  forward: `intercept + slope * (last_day + horizon)`. Verdict:
  projected_psi >= 0.25 -> major (alert); >= 0.10 -> minor;
  otherwise stable. Insufficient (<7 distinct points) ->
  `insufficient_history` -- no alert. Wired as a refresh step right
  after `concept_drift_report` (its data source); staleness-checked.
  Daily review's `_drift_in_drift_health` reads the artifact + mirrors
  major alerts into Notes. Becomes meaningful in ~6 weeks when ~6
  weeks of psi history has accumulated.
- `build_concept_drift_report.py`: **leading-indicator drift detection**
  on the model's *input* features. Computes Population Stability Index
  (PSI) on continuous features (weather temp/wind/density,
  Stage-2/Stage-3 deltas, base FV) and Total Variation Distance on
  categorical (`stadium_id`), comparing a trailing 7-day window against
  the prior 30 days. Verdict per feature: stable (`PSI < 0.10`) /
  minor (`0.10-0.25`) / major (`>= 0.25`). Equal-frequency 10-bin
  histograms on baseline; small smoothing constant prevents
  `log(0)` on empty current bins. Sample-size guard at 30 rows per
  feature per window prevents per-feature false alarms. Outputs JSON
  + Markdown + an append-only `psi_history.jsonl` (one row per feature
  per day) so future analysis can detect *drift in the drift* --
  features gradually shifting over weeks even if no single day's PSI
  crossed the alert threshold. Wired as a refresh step after
  `unified_signals` (data source); staleness-checked. Daily review
  loads the artifact via `_concept_drift_health` and mirrors major
  alerts into top-level Notes. The first leading indicator in the
  drift family -- catches input-distribution shifts BEFORE
  calibration error / cohort losses materialize on the lagging
  outcome dimensions.
- `auto_promote_demote_daemon.py`: **auto-promote/demote daemon** that
  reads today's stability-gate verdicts + the promote_events log and
  (in `--mode act`) invokes `promote.py` for file-swap levers when a
  verdict says go AND a 14-day cooldown has elapsed since the last
  action (manual or daemon). Ships `--mode preview` by DEFAULT (logs
  decisions, takes no action); `--mode off` skips entirely. Wired as
  the `auto_promote_demote_daemon` refresh step between
  `weekly_drift_rollup` and `artifact_lineage_freshness`. Trusts the
  existing 5/7-day stability gates as the multi-day check; adds only
  the cooldown as a second safety. CLI-flag levers (`stake-scaling`,
  `gate-threshold`) get notes only -- daemon doesn't actuate those
  (they need a runtime-overrides config layer the system doesn't have
  yet). Per-lever opt-outs (`--no-auto-promote-stage2` etc.) for
  finer control. Subprocess isolation: a failed promote.py call
  doesn't crash the daemon or the refresh. Audit symmetry: daemon
  actions go through promote.py with `operator=auto_daemon`, so the
  same audit log captures manual + automated actions distinguishably.
- `promote.py`: **unified promotion + demotion CLI** for the four manual
  self-improvement levers (Stage-2 cache swap, Stage-3 v2 weights swap,
  stake-scaling shadow→enforce, gate-threshold RETUNE). Both directions
  share one command shape:
    - `status` shows all four promotion verdicts + the four demotion
      verdicts (post-promotion outcome-regression check) in one summary.
    - `stage2` / `stage3-v2` / `stake-scaling` / `gate-threshold <name>
      <value>` are the promote subcommands; each reads the relevant
      verdict file built by `run_daily_refresh.py`'s stability gates,
      refuses unless verdict says go (or `--force`), performs the change,
      and appends a row to `data/analysis_output/promotion_events.jsonl`.
    - `demote {stage2|stage3-v2|stake-scaling|gate-threshold}` is the
      symmetric inverse. Each computes an outcome-based demotion verdict
      by comparing filled-bet ROI in the 14 days BEFORE the most recent
      promotion against the 14 days AFTER; verdict is `demote` when post
      regressed by >=10pp AND both windows have >=10 filled bets.
  Stage-2/Stage-3-v2 swap files atomically AND back up the prior
  production to `<file>.prior_promote.json` before the swap, so demote
  can restore from a known-good state. Stake-scaling/gate-threshold
  print the recommended `live_engine.py` CLI flag change instead of
  mutating runtime state (operator's saved command stays single source
  of truth for runtime config). Audit log rows carry a `direction:
  "promote" | "demote"` tag; rows from before this field shipped are
  treated as `promote` for backward-compat reads. The
  `_cohort_roi_health` daily-review block auto-tags drift alerts with
  `[coincides with: <lever> promotion N days ago]` when any promotion
  lands in the trailing 14d, providing the fast attribution signal
  that complements the slow 14-day post-hoc demotion verdict. All
  tests route the event log under `tmp_path` so the canonical
  promotion_events.jsonl never gets polluted.
- `run_daily_refresh.py`: **canonical daily refresh** invoked by
  `live_engine.py` at startup. **~63-step base pipeline as of 2026-05-25**
  (was 45 steps on 2026-05-16; new steps include the UNDER-family,
  drift detection, loss-attribution, gate counterfactual, settlement
  verification, Alt-A staging, auto-daemon, and quote-engine shadow —
  see top-of-file freshness note)
  plus one `daily_human_review:YYYY-MM-DD` step per stale completed session. No
  per-day artifact should require a manual rerun; if something seems
  missing from the refresh, add a `RefreshStep` instead of writing a
  one-off README instruction. Steps in order:
    1. `preflight_env_secrets` (inline) -- `.env` / `POLY_PRIVATE_KEY` check;
       warning by default, hard-fail with `require_poly_private_key=True`.
    2. `scrape_recent_games` -- last `recent_games_lookback_days` days.
    3. `stage1_ou_cache` -- `cache/build_mlb_ou_cache.py` rebuild for the
       five completed prior regular seasons for the active year. For 2026 this
       is the conservative 2021-2025 production fallback; rolling4 2022-2025
       remains a research candidate side artifact.
    4. `scrape_active_schedule` -- active month, `--dry-run`.
    5. `game_weather_cache` -- `refresh_game_weather.py`.
    6. `pitcher_cache` -- `build_pitcher_cache.py`.
    7. `team_game_log` -- `build_team_game_log.py` rebuild.
    8. `park_hr_factors` -- `build_park_hr_factors.py`.
    9. `preflight_artifacts` (inline) -- Stage-1/2/3 cache loads, Stage-1
       expected production coverage metadata check, and team coverage check.
    Optional. Per-date `daily_human_review:YYYY-MM-DD` steps for any sessions
        not yet reviewed.
    10. `analysis_safe_trade_table` -- builds
        `data/analysis_output/analysis_safe_trades/` from session JSONs plus
        deduped `live_orders_ledger.jsonl`/`master_ledger.jsonl`; excludes
        `order_status=error` attempts and labels live vs paper fallback.
    11. `candidate_universe_table`, 12. `calibration_opportunity_training`,
        13. `calibrate_signal_probabilities` (`--artifact-purpose
        runtime-refit`), 14. `model_maturity_report`,
        15. `fair_value_stage_ablation`, 16. `fv_gap_decomposition`,
        17. `fv_trust_shrinkage`, 18. `calibration_market_anchored_alpha`
        (`--artifact-purpose runtime-refit`),
        19. `stage1_inferred_empirical_audit`, 20. `unified_signals`,
        21. `signal_training_table`.
    22. `clv_report` -- entry price vs late captured midpoint, grouped by
        family/gate/bucket and compared with realized ROI. Staleness-checked.
    23. `fv_disagreement_quality` -- FV-vs-market disagreement quality by
        family, calibration gain, CLV, ROI, and support/trust. Staleness-checked.
    24. `train_baseline_models` -- EV-policy win + fill models (Active
        #6 part 2 prerequisite). **Carries staleness check** -- skips
        when `signal_training_table.jsonl` mtime <= prior output.
    25. `ev_policy_backtest` -- rebuilds `ev_policy_report.json` and
        runtime-safe EV model artifacts with `--artifact-purpose
        runtime-refit`. Staleness-checked.
    26. `stage2_run_env_retrain_staging` -- refits Stage-2 from full
        2021-2026 corpus to `cache/mlb_stage2_run_env.staging.json`
        (NOT the production cache; promotion stays manual).
        Staleness-checked against `data/games/regular/` dir mtimes;
        biggest single saver (~16 min when corpus unchanged).
    27. `stage3_team_offense_features`, 28. `stage3_team_offense_calibration_table`,
        29. `stage3_team_offense_v2_fit` -- the three-step Stage-3 v2
        retrain chain. All staleness-checked. Output is research-only
        (`phase4_models.json`); production weights are compiled into
        `scripts/analysis/team_offense_model.py` and require an explicit
        promotion.
    30. `model_freshness_health` (inline) -- diffs Stage-2 staging vs
        production validation Brier and surfaces drift alerts at the
        0.001 (0.1pp) level; age-checks every model artifact; flags
        anything > 30 days old.
    31-35. `execution_diagnostics`, `queue_aware_execution_replay`,
        `learn_execution_policy` (Active #7 prototype),
        `state_value_transition_report`, `no_score_drift_policy`.
    36. `no_score_drift_paper_ledger`.
    37. `walk_forward_score_event` -- rolling train/val/test;
        staleness-checked. 38. `walk_forward_no_score_drift`.
        39. `walk_forward_market_anchored_alpha` -- family-separated
        alpha walk-forward with ask/no-vig baselines and clustered
        policy-P&L intervals; staleness-checked.
        40. `walk_forward_fv_disagreement_quality` -- train/validation-
        selected FV disagreement bucket validation applied to test dates;
        staleness-checked.
    41. `stake_scaling_promotion_analyzer` -- runs
        `analyze_stake_scaling_promotion.py` over all session JSONs that
        carry `calibrated_stake_multiplier`. Emits the Active #6 part 2
        promotion verdict (need_more_data / hold / promote) and a
        per-bucket WR/ROI breakdown. Read-only.
    42. `walk_forward_certification` -- runs
        `build_walk_forward_certification.py` to rebuild the Active #1
        per-cohort + per-gate scorecard from `signal_training_table.jsonl`.
        Read-only; auto-degrades verdicts to KEEP-low-confidence when the
        sample is too thin to act on.
    43. `weekly_drift_rollup` -- renders the trailing-7d HTML rollup
        (`build_weekly_drift_rollup.py`) into
        `data/analysis_output/weekly_rollup/` (dated +
        `weekly_rollup.html`). Reads only the per-date daily-review
        JSONs; fail-open. The rollup uses `fill_rate_health.today.placed`
        as the fill-rate denominator (NOT `session_summary.orders_placed`)
        so wallet-balance error days surface clearly in the KPI bar.
    44. `artifact_lineage_freshness` -- runs
        `build_artifact_lineage_freshness_report.py`; records canonical
        artifact generated/max dates, row/family counts, input hashes/mtimes,
        and stale-downstream flags. Diagnostic-only; health rollup surfaces
        the first few warnings.
    45. `refresh_health_rollup` (inline) -- reads the per-step
        results + latest daily human-review alert counts + walk-forward
        summary + `model_freshness_health` notes +
        `artifact_lineage_freshness` summary and prints one INFO block. The
        operator-facing answer to "is the project healthy?".
  **`StalenessCheck` policy** -- subprocess steps with a declared
  `output_path` + input set are skipped (`status="skipped_fresh"`) when
  output mtime >= every input mtime. `--force-retrain` bypasses every
  check; useful for full rebuilds. The manifest emits `summary`
  (now also counts `skipped-fresh`), `summary_status`,
  `failed_step_names`, `logs_dir_bytes`, and a `phase6_reminder` (fires
  after 2026-06-07 = 30 days post-TR20). Plan-only runs write
  `<date>_startup_refresh_plan.json`; completed startup runs write
  `<date>_startup_refresh.json`. Inline handlers are registered in
  `INLINE_HANDLERS` and dispatched by step name; `refresh_health_rollup`
  is special-cased in the runner because it needs visibility into the
  in-progress results list.
- `analyze_book_captures.py`: book-capture analysis for limit-order pricing
  calibration -- ask retrace timing, optimal limit-price candidates, fill
  probabilities by spread, ask velocity, multi-line same-game exposure,
  estimated limit-vs-taker P&L delta, crossed/invalid book detection.
- `analyze_polymarket_overreactions.py`: detects market overreactions by
  comparing actual post-event price moves against fair-value moves from the
  Stage-1 cache. Large overreactions flag mean-reversion edge candidates.
- `build_execution_diagnostics_report.py`: joins unified rows with per-snapshot
  book captures and emits one diagnostics row per trade -- limit_touch,
  first_touch_seconds, shadow touches/fills at +1c/+2c, cancel_reason,
  counterfactual win-if-filled. Output: JSONL + CSV + summary.
- `build_queue_aware_execution_replay.py`: offline policy comparison across
  current_limit / +1c / +2c / taker_like, reporting both touch_fill and a
  conservative queue_adjusted_fill that assumes resting bids may be behind
  liquidity.
- `learn_execution_policy.py`: Active #7 prototype. Consumes the
  queue-aware replay output and quantifies the realized-ROI headroom
  between today's `current_limit` baseline and the per-bet optimal
  policy. Computes baseline / oracle / cohort lookups and evaluates
  candidate rules under leave-one-out cross-validation so n~=71 doesn't
  overfit. Outputs
  `data/analysis_output/execution_policy_prototype/learned_execution_policy_report.{json,md}`.
  Research-only; promotion to live execution requires Active #1's
  walk-forward conclusions plus a larger bet sample.
- `build_state_value_transition_report.py`: post-run report on state-value
  Over trading around score and no-score transitions. Reads candidate_universe
  (live + paper) and reports diagnostics; does not change trading behavior.
- `evaluate_no_score_drift_policy.py`: shadow-only paper policy evaluation for
  the no-score drift family. Collapses to first eligible row per
  game-line/score segment, joins outcomes, reports Poisson + empirical
  support separately, adds Poisson-edge x empirical-edge x ask x drawdown
  regime cuts, and preserves weather-v2 fields for regime testing.
- `build_no_score_drift_paper_ledger.py`: extends the policy evaluator into an
  order-like paper ledger with daily budget, per-game/per-game-line caps,
  first-candidate-per-segment dedup, and explicit fill / price / payout math
  matching live settlement. Weather-v2 fields and no-score regime buckets are
  carried through unchanged from the policy rows.
- `build_side_neutral_opportunity_table.py`: raw-tick side-neutral research
  table. Pairs `over_yes` and `under_no` Polymarket books, samples the stream
  to avoid tick bloat, computes fair Over / fair Under from the existing FV
  stack, joins final labels, and reports Over/Under ask-edge side preference.
  Analysis-only; live trading still ignores `under_no` ticks.
- `build_under_paper_ledger.py`: UNDER-only paper replay over the side-neutral
  table. Uses first eligible row per game-line/score segment, daily budget,
  per-game caps, and live-style share/cost/payout accounting. Summary includes
  `min_under_edge=0.10` and `0.15` threshold variants. Paper-only; no live
  order placement.
- `train_market_anchored_alpha.py`: trains the side-aware alpha model as
  `logit(p_win)=logit(market_ask)+alpha(features)` so the market is the prior
  and the model learns residual disagreement. This is a research artifact, not
  a runtime promotion artifact.

### Backtests and one-shot audits

- `backtest_gates.py`: full-season historical gate-model backtest. Simulates
  every potential bet at each half-inning where a run scores using
  inning-by-inning linescore data, with `assumed_ask = fv - assumed_discount`
  ROI sweeps. The Stage-1 FV cache is partially in-sample, so gate
  performance (not ROI) is the primary signal.
- `backtest_extreme_edge_threshold.py`: TR19 walk-forward certification via
  counterfactual replay. Sweeps `extreme_edge_max` thresholds
  (0.18 / 0.20 / 0.22 / 0.25 / 0.30 / inf) against historical live bets and
  reports kept-vs-blocked W/L decomposition. Used to justify the 2026-05-03
  threshold tightening to 0.22.
- `analyze_window_2026_04_28_to_05_03.py`: one-shot dated analysis over the
  2026-04-28 to 2026-05-03 live window. Per-day overview, winner/loser
  feature contrast, loss anatomy, cancel anatomy, pattern flags. No CLI
  args; rerun-ability via copy-and-edit. Treat as a frozen audit, not a
  reusable tool.

## Subpackage: `unified_signal_table/`

Pure helper library backing `build_unified_signal_table.py`. No CLI entry
points; intentionally split so an LLM agent can inspect a single layer
without opening the orchestrator.

- `unified_signal_table/__init__.py`: docstring only describing the layout.
- `unified_signal_table/schema.py`: column groups
  (`BASE_MASTER_COLUMNS`, `ORDER_EVENT_COLUMNS`, `SNAPSHOT_COLUMNS`,
  `PARAM_COLUMNS`, `PHASE2_STATIC_COLUMNS`), parameter key sets
  (`PARAM_KEYS_COMMON`, `PARAM_KEYS_LIVE`), `CaptureData` dataclass,
  `SCHEMA_VERSION`, master-column builders. Single source of truth for
  table structure.
- `unified_signal_table/utils.py`: shared coercion / parsing helpers --
  timestamp parse + delta, safe float/int/bool coercion, JSON / JSONL
  reading, event-time coalescing, date-range filtering, session-date
  inference.
- `unified_signal_table/loaders.py`: input loaders for sessions, ledgers,
  candidate rows, and book captures. Returns dicts keyed by `session_date`
  or `bet_id`; collects warnings + hard errors for the manifest.
- `unified_signal_table/snapshot_features.py`: book-snapshot row extraction
  and horizon feature computation -- ask/bid at horizons, simulated fill
  features (`fill_time`, `filled`, `cents_saved_vs_taker`) at +0c / +1c /
  +2c repricer levels, best_bid / best_ask / spread / mid extraction.
- `unified_signal_table/row_builder.py`: canonical signals_master row
  assembly + row-level quality checks. Extracts param values, validates
  timestamp ordering, infers signal model family, computes
  remaining-opportunity and shadow-diagnostic fields. Imports
  `scripts.trading.{model_families, remaining_opportunity,
  shadow_diagnostic_features}` so feature definitions stay aligned with
  runtime.
- `unified_signal_table/writers.py`: JSONL / CSV / manifest writers. Sorts
  master rows by `(mode, session_date, placed_at, bet_id)`; manifest
  includes schema version, arg state, column names, row counts, warnings,
  errors.

## Subpackage: `human_review/`

Tier-1 refactor (2026-05-25): the daily-review reporter
(`build_daily_human_review_report.py`) was decomposed into a 12-module
sibling package. The orchestrator file stays the CLI entry point;
each module below owns one block of the report.

- `human_review/__init__.py`: re-exports the public surface so old
  `from build_daily_human_review_report import ...` paths still work.
- `human_review/constants.py`: alert thresholds, Wilson-UB gating
  constants, dimension labels.
- `human_review/helpers.py`: shared coercion helpers
  (`_safe_float(value, default=0.0) -> float` -- note this signature
  differs from the analysis-side `_safe_float(v) -> Optional[float]`
  used inside the calibration package; do not unify naively).
- `human_review/calibration_buckets.py`: cohort bucket labellers
  (`_cohort_edge_bucket`, `_cohort_inning_bucket`,
  `_cohort_line_bucket`, `COHORT_DIMENSIONS`).
- `human_review/calibration_attribution.py`: per-alert attribution
  to promotions / demotions / concept-drift features
  (`_recent_promotions`, `_recent_demotions`, `_major_drift_features`,
  `_attribute_alert_*`). `CONCEPT_DRIFT_ATTRIBUTION_TOP_N = 5`.
- `human_review/calibration_health.py`: the main `_calibration_health`
  block + `_cohort_calibration_health` + `_cohort_roi_health` +
  per-family calibration metrics + window filled-bets aggregator.
  Largest module in the package (~860 lines).
- `human_review/calibrator_enforce_shipment.py`: the
  `_calibrator_enforce_shipment_health` block — surfaces when the
  band-gated enforce flip (TR23) is actively triggering on the day's
  bets vs. when it's silent.
- `human_review/core_health.py`: stable per-day blocks
  (`_count_log_health`, `_stage2_suppression_dollar_audit`,
  `_fill_rate_health`, `_signal_quality_health`,
  `_reconciler_summary`, `_fast_demote_health`,
  `_gate_counterfactual_health`, `_loss_attribution_health`).
- `human_review/drift_health.py`: the 7-dimension drift surface
  (regime mix, concept drift, drift-in-drift, etc.).
- `human_review/stage1_health.py`: Stage-1-specific health blocks
  (Alt-A staging health, Alt-A scope decision audit).
- `human_review/system_health.py`: system-level blocks
  (`_cross_artifact_consistency_health`, `_promotion_lag_health`,
  `_cache_lineage_freshness_health`).
- `human_review/under_health.py`: UNDER-side blocks
  (`_under_emission_health`, `_under_outcomes_counterfactual_health`,
  trailing-7d UNDER aggregate).
- `human_review/render_md.py`: the Markdown renderer +
  `_markdown_table` helper. Pure render; takes the assembled report
  dict and returns a string.

## Subpackage: `calibration/`

Tier-2 + Tier-2.5 refactor (2026-05-25): `calibrate_signal_probabilities.py`
shrunk from 1684 -> 1152 -> 991 lines by extracting reusable internals to
five sibling modules. Public surface re-exported by the original module for
back-compat.

- `calibration/__init__.py`: 1-line stub.
- `calibration/scoring.py`: pure math helpers (`_clip_prob`,
  `_stable_sigmoid`, `_logit`, `_logloss`, `_brier`, `_ece`,
  `_reliability_bins`, `_slice_overconfidence`). 128 lines.
- `calibration/methods.py`: Platt + isotonic fit/predict +
  `_metrics_bundle` + `_select_best_method` (with identity-rejection
  guard via `DEFAULT_IDENTITY_REJECTION_TRAIN_ECE_DELTA = 0.05`).
  242 lines.
- `calibration/stability_gate.py`: 5/14 platt-vs-isotonic stability
  gate. Reads/writes `data/analysis_output/calibration/selection_history.jsonl`
  and overrides today's pick to the trailing-7-day modal selection
  when they differ (after >= 5 days of history). Constants:
  `DEFAULT_STABILITY_WINDOW = 7`, `DEFAULT_STABILITY_MIN_HISTORY = 5`.
  185 lines.
- `calibration/input_drift.py`: `_load_input_drift_status` with PSI
  threshold constants (`INPUT_DRIFT_TRIGGER_PSI_THRESHOLD = 0.25`,
  `INPUT_DRIFT_TRIGGER_MIN_MAJOR_FEATURES = 2`). 77 lines.
- `calibration/bundle_phases.py` (Tier 2.5, 2026-05-25): the 4 phase
  helpers extracted from the 311-line `_fit_calibration_bundle`:
  `_score_methods_on_splits` (Phase 2 — raw/platt/isotonic predictions
  + metrics across train/val/test splits), `_select_method_with_audits`
  (Phase 3 — best-method selection + stability gate + input-drift
  audit), `_build_calibration_payload` (Phase 4 — assembles
  `calibration_payload` + `report_payload` dicts), `_build_prediction_rows`
  (Phase 5 — per-bet prediction row list). 364 lines. The 991-line
  `calibrate_signal_probabilities.py` now keeps Phase 1 (splits +
  train-fit) inline because of tight coupling with function args; the
  rest is a ~140-line orchestrator that calls these 4 helpers.

## Cross-Folder Dependencies

These scripts import from `scripts/trading`. Keep an eye on these when
refactoring trading helpers:

- `train_baseline_models.py` -> `scripts.trading.model_families`
- `calibrate_signal_probabilities.py` -> `scripts.trading.model_families`
- `backtest_ev_policy.py` -> `scripts.trading.model_families` (+
  `scripts.analysis.train_baseline_models`)
- `build_candidate_universe_table.py` -> `scripts.trading.{model_families,
  remaining_opportunity, shadow_diagnostic_features, weather_client}`
- `unified_signal_table/row_builder.py` -> same quartet as above

Internal reuse inside `scripts/analysis`:

- `walk_forward_runner.py` reuses `build_signal_training_table` +
  `train_baseline_models`.
- `no_score_drift_walk_forward.py` reuses `evaluate_no_score_drift_policy`,
  `build_no_score_drift_paper_ledger`, and `train_baseline_models`.
- `build_no_score_drift_paper_ledger.py` reuses
  `evaluate_no_score_drift_policy`.

## Daily Data Efficiency

- Prefer reading from `signals_master.jsonl` and `candidates_master.jsonl`
  rather than re-scraping raw runtime logs. Both have manifests describing
  schema version and arg state.
- `*_calibration_opportunities.jsonl` (written by runtime) is the compact
  per-opportunity stream for calibration modeling -- use it instead of
  walking the full candidate_universe.
- Raw candidate JSONL is sampled for high-volume early pre-FV skip rows:
  rollups count every gate attempt, while raw writes keep the first row per
  coarse state/price bucket plus periodic samples. Those early raw rows also
  omit verbose Weather v2 fields and legacy `weather_mlb_schedule_*` fields.
  Model-bearing/calibration rows keep full weather context.
- Score-event vs. no-score drift: do not pool. Each family has its own
  walk-forward, paper ledger, and policy evaluation script. Pooling would
  contaminate calibration and policy tuning across strategies.
- Sidecars (`book_captures`, `tape_captures`, `velocity_snapshots`) are
  large; report builders should consume the per-row feature summaries
  baked into candidate rows where available, and reach into raw sidecars
  only when a specific question requires it (e.g.
  `analyze_book_captures.py`).
- One-shot dated analysis scripts (e.g.
  `analyze_window_2026_04_28_to_05_03.py`) are frozen audits. Don't
  generalize them in place -- copy and re-date.

## Safe-Edit Checklist

- Don't alter the unified-signals schema (`unified_signal_table/schema.py`)
  without bumping `SCHEMA_VERSION` and verifying every downstream consumer
  (`build_signal_training_table`, `train_baseline_models`,
  `calibrate_signal_probabilities`, `walk_forward_runner`,
  `backtest_ev_policy`, `build_execution_diagnostics_report`,
  `build_queue_aware_execution_replay`).
- Don't change feature definitions imported from `scripts/trading`
  (`model_families`, `remaining_opportunity`, `shadow_diagnostic_features`)
  without checking both runtime and analysis callers; the analysis layer
  re-uses them so candidate rows match what runtime writes.
- Walk-forward / calibration / EV-policy artifacts that runtime can load
  must keep stable on-disk schemas. If you change the schema, update both
  the writer here and the runtime loader in the same patch.
- New analysis outputs go under `data/analysis_output/<topic>/`. Don't write
  back into `data/live_trading/` or `data/paper_trading/` -- those are
  runtime-owned.
- Promotion from shadow to live trading (gate flips, calibration enables,
  EV policy enables) should be supported by a fresh walk-forward run from
  this folder. Cite the run when proposing the change.
- Run `python -m compileall -q scripts/analysis` before handing off; many
  of these scripts have no test coverage and broken imports surface only at
  CLI invocation otherwise.
