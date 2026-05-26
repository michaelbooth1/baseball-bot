# Master Context

High-level map of this repo for an agent walking in cold. Read this first, then
follow the link for whichever folder you actually need to touch. Each linked
file is the authoritative per-folder context; this index only summarizes what
lives where and how the pieces fit.

**Companion top-level docs:**
- **[README.md](README.md)** — operator-facing overview, gate stack, evidence
  snapshot, CLI usage. Read this when you need the *what* and *why* before
  touching code.
- **[ROADMAP.md](ROADMAP.md)** — Active Priorities + Hygiene + bidirectional
  pivot phases. Start with the "Verdict status dashboard" at the top to see
  what's currently in flight.
- **[ROADMAP_ARCHIVE_2026_H1.md](ROADMAP_ARCHIVE_2026_H1.md)** — shipped work
  from 2026-05-17 and earlier (~80 entries). Carved out of ROADMAP on
  2026-05-25 to keep the active document scannable.

Last checked against the linked files: 2026-05-26. Recent major shifts:

- **2026-05-26: frontend multi-engine day view.** New
  `frontend/src/components/MultiEngineDayView.tsx` (~360 lines).
  When the operator clicks a date in the sidebar that has ≥2 sessions
  with `config_label` set, the per-day panel now auto-switches to a
  single page that shows ALL engines' results side-by-side: top
  comparison table (per-config bets / W-L / P&L / ROI / mean ask &
  FV) + per-engine detail cards (summary + bets table). Anchor-scroll
  links jump from the comparison row to the engine detail.
  Single-engine days and the per-config sidebar pin behavior are
  unchanged. App.tsx loader now stamps `_modeFolder` + `_configLabel`
  on each loaded SessionFile so the view can map sessions back to
  configs without an extra lookup. Build clean (TS + Vite, 11.17 KB CSS,
  178.69 KB JS bundles).

- **2026-05-26: parallel-engine fleet doubled from 5 → 10 presets.**
  Added F_no_dedup (operator-requested: strip event-dedup +
  inning-dedup + correlated-line cap; keep edge/ask floors),
  G_loose_edge (-5pp edge floors), H_late_innings (`--min-inning 6`),
  I_extreme_018 (`--extreme-edge-max 0.18`, Phase 6 prep),
  J_no_phantom_filter (`--extreme-edge-max 1.0`). Each maps to one
  open Active or Hygiene roadmap question; I+J pair to form a
  clean {off, current, tightened} sweep on the TR19 knob. Default
  `--config` list expanded so `python scripts/trading/launch_parallel_engines.py`
  now launches all 10. 6 new pytest cases lock in the per-preset
  flag shapes and assert paper-safety.

- **2026-05-26: Scoped Alt-A apply log dedup'd.** The per-tick "Scoped
  Alt-A applied" INFO in `signal_pipeline_gates_post_fv._apply_stage1_alt_a_scope`
  now emits ONCE per `(game_pk, line, inning, inning_state, matched_rule)`
  cohort. Subsequent ticks increment `_scoped_alt_a_rollup_counts` and
  a single summary line fires every 30 min (`flush_scoped_alt_a_rollup`)
  + force-flushed on shutdown. Closes F2 from the 2026-05-25 multi-engine
  audit: A_current's launch_log shrinks **2,270 → ~80 lines (96.5%)** for
  the same workload. 4 new unit tests in `ScopedAltAEnforceTests`.

- **2026-05-26: post-session aggregator hook for parallel engines.**
  `launch_parallel_engines.py` now calls
  `aggregate_parallel_engines.py` for the active date when the last
  engine exits, and echoes the daily read to stdout. Fail-open so
  aggregator errors don't change the launcher's exit code; opt out with
  `--no-post-session-aggregate`. Closes the F1 finding from the
  2026-05-25 multi-engine audit (canonical
  `data/analysis_output/parallel_engine_comparison/parallel_engine_comparison.md`
  was silently stuck on 2026-05-24 data overnight because the
  aggregator was only invoked manually). 4 new pytest cases lock in
  the default-on, opt-out, dry-launch-skip, and missing-script
  fail-open behaviors.

- **2026-05-25: multi-engine parallel paper runs.** New
  `scripts/trading/launch_parallel_engines.py` with 5 built-in PRESETS
  (`A_current`, `B_cal_only`, `C_raw`, `D_scope_only`, `E_tight_edge`)
  launches N paper SignalEngine processes against the same live market
  day, each with its own `paper_root` and `config_label`. Shared
  book/tape captures via `shared_market_watcher.py` +
  `shared_market_data.py` so N configs don't poll the CLOB N times.
  `LIVE_ONLY_ENGINE_FLAGS` rejects Kelly / daily-budget / stake-mode
  to prevent live-only flags leaking into paper presets. Offline
  comparison via `scripts/analysis/aggregate_parallel_engines.py` ->
  `data/analysis_output/parallel_engine_comparison/`.
- **2026-05-25 (today): calibrate_signal_probabilities split (Tier 2.5
  refactor).** The 311-line `_fit_calibration_bundle` was decomposed
  into 4 phase helpers under new `scripts/analysis/calibration/bundle_phases.py`
  (`_score_methods_on_splits`, `_select_method_with_audits`,
  `_build_calibration_payload`, `_build_prediction_rows`).
  `calibrate_signal_probabilities.py` shrunk 1152 -> 991 lines (under
  1k for the first time); 45 targeted calibration tests + full 1430+
  pytest run green. Public surface unchanged.
- **2026-05-21: Scoped Alt-A enforce (cohort-aware empirical override,
  TR25).** Per-candidate runtime decision in `_apply_stage1_alt_a_scope`:
  when `--stage1-alt-a-scope-mode enforce` AND the candidate's cohort
  doesn't match a `hold_poisson` rule AND the upstream shadow path
  computed an alt empirical FV, swap production `fair_value` to use it.
  Initial rule list: one explicit `hold_poisson` for `inning>=8` (known
  -23.8pp regression cohort); everything else `apply` by default.
  Default mode `shadow` so operators audit per-candidate decisions before
  flipping enforce.
- **2026-05-19: band-gated calibrator ENFORCE (TR23).** The 2026-05-19
  FV-overconfidence audit confirmed raw FV is overconfident by +28pp at
  raw>=0.95 (487 settled predictions: claimed avg 0.97, realized 0.70).
  `DEFAULT_PROB_CALIBRATION_MODE` flipped `shadow` -> `enforce`; new
  `DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW = 0.90`. The Platt calibrator
  now overwrites raw FV only when raw>=0.90 (captures the dangerous-tail
  correction) and leaves mid-band [0.80, 0.90) alone (where Platt
  over-pulls). Per-candidate diag adds `below_min_raw_kept_raw` columns.
  Runbook: `docs/operational/fv-recalibration-2026-05-19.md`.
- **2026-05-19: UNDER candidate emission (Phase A5 keystone).** New
  `--under-emission-mode {off, shadow}` emits an UNDER candidate row
  alongside every OVER candidate that reaches the FV phase, with its
  own calibrated UNDER FV (`signal_win_calibration_under.json`), UNDER
  ask, and gate evaluation (`decision=shadow_under` when UNDER gates
  pass). **NO UNDER bets placed in either mode** — pure observability
  so the paper-mode runway accumulates UNDER signal-quality data. New
  `_under_emission_health` + `_under_outcomes_counterfactual_health` +
  trailing-7d UNDER outcomes aggregate daily-review blocks.
- **2026-05-18: Stage-1 Alt-A staging cache (Active #8 prep).** New
  `--smoothing-mode {poisson, empirical_when_available}` flag on
  `cache/build_mlb_ou_cache.py` materializes the runtime's on-the-fly
  Alt-A shadow as a real cache file. New `stage1_ou_cache_alt_a` refresh
  step writes to `cache/mlb_ou_cache_alt_a.staging.json` (NEVER
  auto-promoted; operator runs `promote.py stage1 --source ...` after
  paper-mode validation). First production build: 4,200 of 4,298 cells
  (97.7%) overridden, mean signed delta -2.74pp.
- **2026-05-17: Stage-1 owns ~100% of the 27pp bias (Active #10
  loss-attribution + Active #11 gate counterfactual + Active #16 lineage
  + Active #13 fast Wilson-UB demote).** Bet-level loss attribution
  shipped: probability-space decomposition via the logit-additive FV chain.
  First production run on 87 filled+settled bets reveals Stage-1 owns
  ~100% of the 27pp over-prediction bias (mean_p0=92.7%, mean_won=65.5%).
  Stage-2 contributes +0.04pp, Stage-3 actively *helps* by -0.05pp.
  Re-targeted Active #8 to "rebuild Stage-1" instead of Stage-2/3.
  Same day: `promote.py stage1` subcommand shipped, model-lineage v1-v4
  shipped (build-time + promotion-time git_sha / input_hashes;
  cross-artifact consistency check), fast Wilson-UB demotion shipped
  (5-6 day reaction vs 14d windowed).
- **2026-05-15: leading-indicator drift (concept_drift + drift_in_drift,
  7th and 8th drift dimensions).** `build_concept_drift_report.py`
  computes PSI/TVD on model inputs (trailing 7d vs prior 30d); fires
  before calibration / cohort drift materializes. Sister
  `build_drift_in_drift_report.py` fits OLS slope on 30d of PSI history
  per feature and projects 30d forward to catch slow-creep drift that
  never crosses the daily PSI threshold but accumulates past it.
- **2026-05-14 (preserved): calibration method-stability gate.** New
  `_apply_stability_gate` in `calibrate_signal_probabilities.py` stops
  the platt<->isotonic flip-flop observed 2026-05-11..13 by overriding
  today's pick to the trailing-7-day modal selection when the two
  differ. Reads/writes a new
  `data/analysis_output/calibration/selection_history.jsonl`. Backfilled
  with 10 days of prior selections from `daily_human_review` snapshots.
  Disable with `--no-stability-gate` for backfills.
- **2026-05-13: walk-forward certification report (Active #1
  prep).** New `scripts/analysis/build_walk_forward_certification.py`
  reads `signal_training_table.jsonl` and emits the per-cohort + per-gate
  scorecard Active #1 needs: sample-readiness verdict
  (READY/PRELIMINARY/INSUFFICIENT), per-band cohort metrics
  (edge/ask/inning/runs-needed/current-state-edge/phantom-risk/family),
  and a sweep-driven keep/retune/retire verdict for each enforced gate
  with confidence based on filtered-vs-kept cohort sizes. Wired into
  the daily refresh. Today's preliminary read: extreme-edge cohort
  (>0.22) is -69% ROI vs +11% kept -- gate is doing real work.
- **2026-05-13 (today): wallet-aware paper fallback.** When CLOB
  rejects with `not enough balance / allowance`, live placement no
  longer drops the bet or retries; instead the bet is synthesized as a
  paper-fallback (filled at limit, tracked through settlement, marked
  `placement_mode="paper_fallback"`). A session-level cooldown
  (`--wallet-exhausted-cooldown-secs`, default 300) skips the CLOB for
  subsequent placements until the wallet frees up. Replaces yesterday's
  retry-storm pattern (62 errored attempts on the same handful of
  signals) with continuous data flow even when wallet runs short.
- **2026-05-13: stake-scaling promotion analyzer.** New
  `scripts/analysis/analyze_stake_scaling_promotion.py` reads filled+
  settled bets carrying `calibrated_stake_multiplier`, buckets them by
  multiplier tercile, and emits need_more_data / hold / promote against
  the Active #6 part 2 promotion gate (≥30 sessions, high cohort beats
  low by ≥5pp WR AND ≥5pp ROI). Wired into the daily refresh; renders
  a new panel in the weekly rollup HTML. Current: 11 filled bets across
  2 sessions, verdict need_more_data; high cohort already trending
  +9.5pp WR / +1.7pp ROI vs low (well inside noise at n=11).
- **2026-05-13: weekly drift rollup ships (Active #5 closed).**
  `scripts/analysis/build_weekly_drift_rollup.py` renders a trailing-7d
  one-page HTML rollup of the per-date `*_human_review.json` files: KPI
  bar, alerts feed, 8 inline-SVG sparklines, per-day detail table.
  Output at `data/analysis_output/weekly_rollup/`. Wired as the
  penultimate step of the daily refresh, so the artifact stays fresh
  with no manual rerun. Also fixed `_stage2_validation_brier` to read
  the canonical `validation_metrics[<line>]["stage2_brier"]` schema (was
  silently returning `None`, killing the Stage-2 promotion alert),
  fixed `LogisticJsonScorer.missing_input_cols` to differentiate
  structurally-absent columns from value-null (no_score_drift candidates
  no longer trip a misleading "EV policy missing runtime features"
  warning in shadow / fail closed in enforce), and refreshed the stale
  notes block in the refresh manifest writer.
- **2026-05-12: self-correcting refresh.** Daily refresh expanded
  to 32 steps (now ~63 as of 2026-05-25). Stage-2 + Stage-3 + EV-policy models all retrain
  automatically on the latest data (Stage-2 to a staging path; the
  production cache is never auto-swapped). A `model_freshness_health`
  inline handler diffs staging vs production and surfaces drift alerts;
  `refresh_health_rollup` prints one end-of-refresh INFO block answering
  "is the project healthy?". `StalenessCheck` policy lets heavy retrains
  skip when their inputs are unchanged (Stage-2 saved 956s/16min on the
  next-day re-run). `live_engine.py` refactored into four impl modules
  (`live_engine_cli.py`, `live_engine_placement.py`,
  `live_engine_session_io.py`, `live_engine_setup.py`) to keep every
  file under 1,200 lines.
- **2026-05-12: Active #6 risk controls.** Correlated-line exposure
  cap (max 2 over-side bets per game, gap >= 1.5 runs) ships enforced;
  blocked the historical O7.5+O8.5 pattern. Calibrated-edge stake
  scaling ships in **shadow** (multiplier on bet record but not
  applied); promotion to enforce after ~30 shadow sessions.
- **2026-05-11: calibration anomalies fixed.** Identity-rejection guard
  in `calibrate_signal_probabilities.py`; build wired into daily
  refresh; new `calibration_opportunity_training` input schema
  supported. `score_confirmed_60s_raw_fv` metric replaced with
  `shadow_p_score_event_proxy`-based proxy (AUC 0.91 vs prior 0.37).
  Daily human-review now carries seven-dimension drift alerts
  (calibration / fill / signal / regime / cohort-ROI / concept-drift /
  drift-in-drift) plus reconciler tracking, all with Wilson-UB gating
  on rate-drop alerts. The cohort-ROI dimension shipped 2026-05-15
  and tracks outcome metrics by edge / ask / inning / line /
  current-state-edge bucket. Two of the seven are *leading* indicators:
  `concept_drift_health` (shipped 2026-05-15) does PSI/TVD on input
  features for trailing 7d vs prior 30d; `drift_in_drift_health`
  (shipped 2026-05-16) fits a linear trend on the psi_history file
  itself and projects 30d forward to catch slow-creep drift that
  never crosses the daily PSI threshold but accumulates past it.
- **2026-05-11: orphan-fill reconciliation.** New
  `scripts/trading/live_reconciliation.py` recovers fills the CLOB SDK
  misses (caught the 2026-05-10 MIN@CLE 7.5 silent miss).
- **2026-05-08: startup refresh promoted to canonical daily refresh** —
  scrape + team_game_log + preflight steps wired in.

## What this repo is

A **self-improving** automated MLB Over/Under trading bot on Polymarket. Live
trading is active. The self-improvement claim is concrete: the startup script
(`scripts/analysis/run_daily_refresh.py`, run automatically before
`LiveTradingEngine` boots) re-ingests data, refits every learned artifact
whose inputs changed, re-runs walk-forward validation, and surfaces drift
alerts -- so the live runtime always loads models trained on the latest
available data and the operator only acts on the small set of decisions that
the system explicitly flags for human approval (Stage-2 promotion, Stage-3 v2
promotion, gate threshold changes, shadow-to-enforce promotions). All four
operator-facing decisions are wrapped behind one CLI with both directions:
`scripts/analysis/promote.py {status, <lever>, demote <lever>}` reads the
relevant verdict file, refuses unless approved, performs the change, and
appends to `data/analysis_output/promotion_events.jsonl` with a `direction:
"promote" | "demote"` tag. File-swap promotions back up the prior production
first so `demote` can roll back; outcome-based demotion verdicts compute
post-vs-pre filled-bet ROI on a 14-day window to flag bad promotions.
Cohort-ROI drift alerts get auto-tagged with `[coincides with: <lever>
promotion N days ago]` for fast attribution.

As of 2026-05-16 the refresh also runs an `auto_promote_demote_daemon` step
(ships `--mode preview` by default; operator opts into `--mode act` after
reviewing preview output). In `act` mode the daemon auto-invokes
`promote.py` for file-swap levers (`stage2`, `stage3-v2`) when a verdict
says go AND a 14-day cooldown has elapsed since the last action. The
transition from "self-improving with human approver" to "self-improving
with human reviewer" -- every piece of the
measure→recommend→act→audit→regression-detect→revert loop now exists and
can run unattended.

Current strategic frame: **state-value Over trading around market overreaction
to score and no-score transitions**, with score-event transitions enforced and
no-score drift / EV policy kept in shadow until walk-forward evidence supports
promotion. **Probability calibration runs in band-gated enforce mode since
2026-05-19** (overwrites raw FV only when raw>=0.90; see
`DEFAULT_PROB_CALIBRATION_MODE` / `DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW`
in [scripts/trading/signal_config.py](scripts/trading/signal_config.py#L177)).

## Pipeline at a glance

```
MLB Stats API + Polymarket CLOB
        |
        v
scripts/scraping + scripts/monitor   --> data/games, data/schedules,
                                         data/polymarket, data/manifests
        |
        v
cache/ builders + startup refresh caches          --> cache/*.json
        |
        v
scripts/trading (signal_engine + live_engine)    --> data/live_trading,
                                                     data/paper_trading
        |
        v
scripts/analysis (unified table, calibration,    --> data/analysis_output/<topic>
                  walk-forward, EV policy,
                  execution diagnostics)
        |
        v
tests/ (pytest contract for both trading + analysis)
```

## Top-level documents

### [README.md](README.md)
The user-facing overview and operator runbook. Explains the trading objective
(state-value Over trading on score / no-score transitions), how the 3-stage
fair-value model works (Stage-1 Poisson lookup, Stage-2 park/weather residual,
Stage-3 team offense), the startup weather-cache enrichment path, the full gate
stack with TR1-TR21 evolution history, order execution + Kelly sizing rules,
and the production CLI for both paper and live runs. Includes the current
Evidence Snapshot (fill rate, edge-cohort P&L splits) and the prioritized
roadmap (walk-forward validation is item #1). Read this when you need the
*what* and *why* before touching code.

### [tests/AGENT_CONTEXT.md](tests/AGENT_CONTEXT.md)
Pytest suite contract covering both `scripts/trading` and `scripts/analysis`.
Eighty-four modules (as of 2026-05-25) split between trading runtime characterization
(`test_signal_engine_phase1_characterization.py`,
`test_trading_live_execution_fixes.py`), analysis builders / report writers
(`test_build_unified_signal_table.py`,
`test_build_state_value_transition_report.py`,
`test_build_daily_human_review_report.py`), modeling / calibration
(`test_train_baseline_models.py`,
`test_probability_calibration_families.py`), and walk-forward / policy
backtests (`test_walk_forward_runner.py`, `test_backtest_ev_policy.py`,
`test_no_score_drift_walk_forward.py`). Many are golden / characterization
tests -- update field-by-field, never by loosening matchers. Engine tests
stub helper methods on the engine instance; the supported stub points are
listed in the trading context. Read this before changing decision logic,
schemas, or report shapes.

### [scripts/monitor/AGENT_CONTEXT.md](scripts/monitor/AGENT_CONTEXT.md)
Live monitor base layer that the trading runtime subclasses. Owns the polling
loop (schedule refresh -> Polymarket discovery -> active-game sync -> CLOB book
poll -> `_on_tick_batch` hook), the `MLBStatsClient` (schedule fetch + pitcher
ERA cache with TR14 stale-fallback), Polymarket Gamma discovery, the CLOB book
client with Gamma-price fallback, and the per-(game, line, side) stale-book
retirement state machine. Split as of 2026-05-09 across ten focused modules
(`monitor_constants.py`, `monitor_utils.py`, `monitor_system.py`,
`monitor_models.py`, `monitor_recorder.py`, `monitor_stats_client.py`,
`monitor_discovery.py`, `monitor_book_client.py`, `monitor_cli.py`, plus the
orchestrator `monitor_mlb_polymarket_ou.py` which keeps a backward-compat
`__all__` block so trading / test imports stay stable). Read this before
touching tick cadence, the `is_final()` glitch guard, recorder file layout, or
the subclass `_on_tick_batch` contract.

### [scripts/trading/AGENT_CONTEXT.md](scripts/trading/AGENT_CONTEXT.md)
Production-like trading runtime. Owns the per-tick gate pipeline, FV
inference, candidate / skip logging, sidecar capture, paper + live session
serialization, CLOB order placement, and order-lifecycle management. Key
modules: `signal_engine.py` / `live_engine.py` (orchestration cores),
`signal_pipeline*.py` (six-module pipeline split: payload, state-value,
no-score drift, capture, pre-FV gates, post-FV gates),
`live_pricing.py` / `live_order_lifecycle.py` /
`live_ev_policy_runtime.py` / `live_diagnostics.py` /
`live_session_loading.py`, `polymarket_client.py` (CLOB SDK wrapper with
legacy `sig_type=2` and deposit-wallet `sig_type=3` paths),
`probability_calibration.py`, `model_families.py`,
`candidate_logging.py` + helpers, `weather_client.py` (active-date local
weather cache enrichment). Lists Live Execution Invariants that the
test suite mirrors (USDC->shares conversion, decision vs execution prices,
line-release-after-unfilled, fail-closed EV policy). Extreme-edge gate is
enforced at 0.22 (TR17 + TR19); probability calibration is in band-gated
enforce (raw>=0.90) since 2026-05-19; no-score drift / EV stay in shadow.
Read this when changing gate logic, candidate schema, or live execution.

### [scripts/analysis/AGENT_CONTEXT.md](scripts/analysis/AGENT_CONTEXT.md)
Offline research + reporting layer. Read-only with respect to live trading
state -- writes go under `data/analysis_output/<topic>/` and shared caches
go under `cache/`. Owns the canonical unified signal table
(`build_unified_signal_table.py` + the `unified_signal_table/` subpackage),
the leakage-aware training table (`build_signal_training_table.py`), the
candidate-universe table, modeling + calibration
(`train_baseline_models.py`, `calibrate_signal_probabilities.py`,
`backtest_ev_policy.py`), the score-event walk-forward harness
(`walk_forward_runner.py`) and the **separate** no-score drift walk-forward
(`no_score_drift_walk_forward.py` -- do not pool families), per-trade
execution diagnostics + queue-aware replay, the state-value transition
report, the no-score drift policy / paper ledger family, and operator
rollups (`session_report.py`, `build_daily_human_review_report.py`,
`refresh_game_weather.py`, `run_daily_refresh.py`). Several
scripts import from `scripts/trading` (`model_families`,
`remaining_opportunity`, `shadow_diagnostic_features`); keep those in sync.
Promotion from shadow to live should be backed by a fresh walk-forward run
from this folder.

### [data/AGENT_CONTEXT.md](data/AGENT_CONTEXT.md)
On-disk store for everything the rest of the repo reads or writes. Source
corpora: `games/regular/<year>/<month>/<day>/<game_pk>.json` (raw MLB
live-feed JSONs, ~2.4k per season),
`schedules/<year>/<month>/schedule_*.json`,
`polymarket/mlb_ou/<date>/<game>/meta.json` (token IDs per OU line). Trading
output: `live_trading/` and `paper_trading/` carry `master_ledger.jsonl`,
`live_orders_ledger.jsonl`, per-date `sessions/`, and per-bet
`book_captures/`, `book_decision_snapshots/`, `tape_captures/`,
`velocity_snapshots/`, plus `candidate_universe/` rollups. `manifests/`
holds monthly + per-run scraper bookkeeping. `analysis_output/` is the
disposable report sink with per-builder subfolders
(`unified_signals/`, `training_tables/`, `walk_forward/`,
`execution_diagnostics/`, `execution_replay/`,
`state_value_transition/`, `no_score_drift_*`, `calibration/`, etc.); the
`_check`, `_step2_check`, `_review_<date>`, `_day2_<date>` siblings are
scratch -- treat the unsuffixed folder as authoritative. Loose
`overreact_*.json` at the root of `analysis_output/` are pinned ad-hoc
snapshots, not regenerated. Never hand-edit corpora or ledgers; rerun the
producer.
`scoring_trends/` is the local scoring-environment research bundle used to
evaluate 10-season vs recent/weighted Stage-1 cache ideas; it is generated by
`scripts/analysis/analyze_scoring_environment_trends.py`.
`stage1_prior_candidates/` is the companion broad prior screen generated by
`scripts/analysis/evaluate_stage1_prior_candidates.py`; it exports candidate
weight CSVs plus row-level and deduped proxy metrics for rolling windows,
recency weighting, scoring-environment similarity, and blends.

### [cache/AGENT_CONTEXT.md](cache/AGENT_CONTEXT.md)
Offline-built model artifacts the live runtime loads at startup. Two
builders live here: `build_mlb_ou_cache.py` (Stage-1 empirical-first /
Poisson-fallback per-state OU table -> `mlb_ou_cache.json`; explicit
date/season windows, duplicate-`gamePk` skipping, and history coverage
metadata; 2026 startup uses the five completed prior seasons as the
conservative production fallback, while rolling4/10y/weighted10 remain
research side artifacts) and
`build_mlb_stage2_run_env.py` (Stage-2 park / temp / wind / park_wind
residual deltas with per-line family-weight tuning ->
`mlb_stage2_run_env.json`). `stage2_run_env_model.py` is the runtime
applier (`FAMILY_ORDER`, bucket parsers, `enforce_over_monotonic`,
`RunEnvContext`, `Stage2RunEnvModel`). Two more JSON files
(`pitcher_cache.json`, `team_game_log.json`) are written here by builders
in `scripts/analysis/`. Hard contracts: state-key string format, line-key
encoding (`o<digits>` / `po<digits>`), `FAMILY_ORDER`, and
`UNKNOWN_BUCKET = "__UNK__"` -- all are read by trading runtime, so
schema-breaking changes need coordinated updates in `scripts/trading/`
and `scripts/analysis/`. Stage-2 weights collapse to zero when validation
Brier gain is below `--min-brier-improvement`; do not promote them by
hand. Never hand-edit the JSON files; rerun the matching builder.

## How to use this index

- **Adding a gate or changing decision logic**: start with the trading
  context, then check `tests/AGENT_CONTEXT.md` for which characterization
  tests will need golden updates.
- **Adding a report or analysis script**: start with the analysis context;
  output goes under `data/analysis_output/<topic>/` per the data context.
- **Changing the FV model or its inputs**: cache context first, then trading
  (the runtime loads the JSON), then analysis (training/calibration tables
  consume the same shapes).
- **Touching schemas (candidate row, ledger row, unified table, calibration
  artifact)**: the schema lives in the trading or analysis context; update
  it, then update the matching test in `tests/AGENT_CONTEXT.md` in the same
  patch.
- **Doing data ops (delete, move, regenerate)**: read the data context's
  Safe-Edit Checklist before touching anything under `data/`.
