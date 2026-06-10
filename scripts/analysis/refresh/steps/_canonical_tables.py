"""Step cluster: canonical analysis tables, calibration retrains,
   and core research reports that run after sessions are available.

These are the steps inside the big `steps.extend([...])` block.
Preserved verbatim from build_refresh_steps lines 1865-2511.
"""
from __future__ import annotations

from typing import List

from .. import config as _config
from ..config import (
    RefreshConfig,
    RefreshStep,
    StalenessCheck,
)
from ..helpers import _python, _script


def build_canonical_table_steps(
    config: RefreshConfig,
    max_date: str,
) -> List[RefreshStep]:
    strict_flag = ["--strict"] if config.strict else []
    max_date_args = ["--max-date", max_date]
    return [
        RefreshStep(
            name="analysis_safe_trade_table",
            description=(
                "Rebuild canonical analysis-safe trade table from session "
                "JSONs plus deduped live/master ledgers; excludes "
                "order_status=error attempts by default."
            ),
            command=[
                _python(),
                _script("scripts/analysis/build_analysis_safe_trade_table.py"),
                "--max-date",
                max_date,
                *strict_flag,
            ],
        ),
        RefreshStep(
            name="candidate_universe_table",
            description="Rebuild decision-level candidate table.",
            command=[
                _python(),
                _script("scripts/analysis/build_candidate_universe_table.py"),
                "--mode",
                "live",
                *max_date_args,
                *strict_flag,
            ],
        ),
        RefreshStep(
            name="calibration_opportunity_training",
            description="Rebuild model-bearing calibration-opportunity training table.",
            command=[
                _python(),
                _script("scripts/analysis/build_calibration_opportunity_training_table.py"),
                "--mode",
                "live",
                *max_date_args,
                *strict_flag,
            ],
        ),
        RefreshStep(
            name="model_maturity_report",
            description="Rebuild model family maturity/readiness report.",
            command=[
                _python(),
                _script("scripts/analysis/build_model_maturity_report.py"),
                "--mode",
                "live",
                *max_date_args,
            ],
        ),
        RefreshStep(
            name="fair_value_stage_ablation",
            description="Rebuild FV stage ablation report (market, inference, Stage-2, Stage-3, final FV).",
            command=[
                _python(),
                _script("scripts/analysis/fair_value_stage_ablation_report.py"),
                "--mode",
                "live",
                *max_date_args,
            ],
        ),
        RefreshStep(
            name="fv_gap_decomposition",
            description="Rebuild FV gap decomposition report (market/no-vig vs Poisson, empirical, Stage-2/3, final FV).",
            command=[
                _python(),
                _script("scripts/analysis/build_fv_gap_decomposition_report.py"),
                "--mode",
                "live",
                *max_date_args,
            ],
        ),
        RefreshStep(
            name="fv_trust_shrinkage",
            description="Rebuild FV trust/shrinkage experiment (support-weighted market anchoring).",
            command=[
                _python(),
                _script("scripts/analysis/build_fv_trust_shrinkage_experiment.py"),
                "--mode",
                "live",
                *max_date_args,
            ],
        ),
        RefreshStep(
            name="calibration_market_anchored_alpha",
            description="Train family-separated market-anchored alpha research models from calibration opportunities.",
            command=[
                _python(),
                _script("scripts/analysis/train_calibration_market_anchored_alpha.py"),
                "--mode",
                "live",
                "--artifact-purpose",
                "runtime-refit",
                *max_date_args,
            ],
        ),
        RefreshStep(
            name="stage1_inferred_empirical_audit",
            description="Rebuild score-event Stage-1 Poisson-vs-empirical inferred-state audit.",
            command=[
                _python(),
                _script("scripts/analysis/audit_stage1_inferred_empirical.py"),
                "--mode",
                "live",
                *max_date_args,
            ],
        ),
        RefreshStep(
            name="unified_signals",
            description=(
                "Rebuild canonical event-level signal table. "
                "Mode=both folds paper sessions in alongside live "
                "(2026-05-19 fix discovered during paper-trading "
                "audit; previously hardcoded live-only, which "
                "blocked the paper-mode runway from feeding "
                "loss-attribution + shadow-override + training "
                "table). Mode tag on each row preserves the "
                "live/paper distinction for downstream consumers "
                "that need to filter (any metric using "
                "realized P&L or fill behavior)."
            ),
            command=[
                _python(),
                _script("scripts/analysis/build_unified_signal_table.py"),
                "--mode",
                "both",
                *max_date_args,
                *strict_flag,
            ],
        ),
        RefreshStep(
            name="concept_drift_report",
            description=(
                "Leading-indicator drift detection: PSI/TVD on the "
                "model's input features (weather, Stage-2/3 deltas, "
                "base FV, stadium mix) over a trailing 7d window vs "
                "the prior 30d. Catches shifts in the inputs the live "
                "model consumes BEFORE calibration error / cohort "
                "losses materialize."
            ),
            command=[
                _python(),
                _script("scripts/analysis/build_concept_drift_report.py"),
                "--active-date", config.active_date,
            ],
            staleness_check=StalenessCheck(
                output_path=_config.PROJECT_DIR / "data" / "analysis_output"
                / "concept_drift" / "concept_drift_report.json",
                input_paths=(
                    _config.PROJECT_DIR / "data" / "analysis_output"
                    / "unified_signals" / "signals_master.jsonl",
                ),
            ),
        ),
        RefreshStep(
            name="calibrate_signal_probabilities",
            description=(
                "Refit fair_value probability calibration (per-family Platt/"
                "isotonic) from the calibration-opportunity training table. "
                "2026-06-06: also fits per-(family, line) curves when a "
                "line has >=100 labeled rows; runtime uses per-line first, "
                "falls back to family-pooled. Addresses miscalibrated "
                "line 5.5 cohort (realized WR 55% at raw FV>=0.90)."
            ),
            command=[
                _python(),
                _script("scripts/analysis/calibrate_signal_probabilities.py"),
                "--input-path",
                str(
                    _config.PROJECT_DIR
                    / "data"
                    / "analysis_output"
                    / "calibration_opportunity_training"
                    / "calibration_opportunity_training_table.jsonl"
                ),
                "--input-kind",
                "auto",
                "--family-mode",
                "separate",
                "--artifact-purpose",
                "runtime-refit",
                "--mode",
                "live",
                "--per-line-min-rows",
                "100",
                *max_date_args,
                *strict_flag,
            ],
        ),
        RefreshStep(
            name="calibrate_signal_probabilities_under",
            description=(
                "Phase A2 (2026-05-16): refit the UNDER-side fair_value "
                "probability calibration with flipped labels + raw probs. "
                "Same per-family Platt/isotonic machinery as Over; "
                "separate artifact (signal_win_calibration_under.json) "
                "and separate stability-gate selection history. UNDER "
                "calibration stays offline / shadow until Phase B/C "
                "wire it into the live engine."
            ),
            command=[
                _python(),
                _script("scripts/analysis/calibrate_signal_probabilities.py"),
                "--side",
                "under",
                "--input-path",
                str(
                    _config.PROJECT_DIR
                    / "data"
                    / "analysis_output"
                    / "calibration_opportunity_training"
                    / "calibration_opportunity_training_table.jsonl"
                ),
                "--input-kind",
                "auto",
                "--family-mode",
                "separate",
                "--artifact-purpose",
                "runtime-refit",
                "--mode",
                "live",
                *max_date_args,
                *strict_flag,
            ],
        ),
        RefreshStep(
            name="drift_in_drift_report",
            description=(
                "Slow-creep drift: linear-trend fit on the trailing 30d "
                "of psi_history.jsonl, projected 30d forward. Catches "
                "features that drift <0.25 PSI per day but accumulate "
                "past the major threshold over weeks -- a failure mode "
                "the day-vs-baseline concept_drift_report can't see."
            ),
            command=[
                _python(),
                _script("scripts/analysis/build_drift_in_drift_report.py"),
                "--active-date", config.active_date,
            ],
            staleness_check=StalenessCheck(
                output_path=_config.PROJECT_DIR / "data" / "analysis_output"
                / "concept_drift" / "drift_in_drift_report.json",
                input_paths=(
                    _config.PROJECT_DIR / "data" / "analysis_output"
                    / "concept_drift" / "psi_history.jsonl",
                ),
            ),
        ),
        RefreshStep(
            name="signal_training_table",
            description=(
                "Rebuild leakage-aware training table. Mode=both "
                "pairs with the unified_signals --mode both change "
                "(2026-05-19) so paper bets carrying Alt-A shadow "
                "fields reach loss-attribution + shadow-override "
                "reports. Safe because both reports use the `won` "
                "boolean (counterfactual: did the over/under hit), "
                "which is identical for paper and live bets -- "
                "paper's 100% taker assumption only distorts "
                "realized_profit/realized_executed, which these "
                "reports do not read. Other refresh steps that "
                "DO read fill behavior (clv_report, "
                "execution_diagnostics, ev_policy_backtest, "
                "queue_aware_execution_replay) stay --mode live "
                "intentionally."
            ),
            command=[
                _python(),
                _script("scripts/analysis/build_signal_training_table.py"),
                "--mode",
                "both",
                *max_date_args,
                *strict_flag,
            ],
        ),
        RefreshStep(
            name="clv_report",
            description=(
                "Rebuild closing/late-price value diagnostics: entry "
                "price vs late captured mid, grouped by family/gate/bucket "
                "and compared with realized ROI."
            ),
            command=[
                _python(),
                _script("scripts/analysis/build_clv_report.py"),
                "--mode",
                "live",
                *max_date_args,
                *strict_flag,
            ],
            staleness_check=StalenessCheck(
                output_path=_config.PROJECT_DIR / "data" / "analysis_output" / "clv" / "clv_summary.json",
                input_paths=(
                    _config.PROJECT_DIR / "data" / "analysis_output" / "unified_signals" / "signals_master.jsonl",
                    _config.PROJECT_DIR / "data" / "analysis_output" / "unified_signals" / "signal_book_snapshots.jsonl",
                    _config.PROJECT_DIR / "data" / "analysis_output" / "analysis_safe_trades" / "analysis_safe_trades.jsonl",
                    _config.PROJECT_DIR / "data" / "analysis_output" / "calibration_opportunity_training" / "calibration_opportunity_training_table.jsonl",
                ),
            ),
        ),
        RefreshStep(
            name="fv_disagreement_quality",
            description=(
                "Rebuild FV-vs-market disagreement quality diagnostics: "
                "when raw FV disagrees with market, report calibration "
                "gain, CLV, ROI, support/trust, and family bucket ranks."
            ),
            command=[
                _python(),
                _script("scripts/analysis/build_fv_disagreement_quality_report.py"),
                "--mode",
                "live",
                *max_date_args,
                *strict_flag,
            ],
            staleness_check=StalenessCheck(
                output_path=_config.PROJECT_DIR
                / "data" / "analysis_output"
                / "fv_disagreement_quality"
                / "fv_disagreement_quality_summary.json",
                input_paths=(
                    _config.PROJECT_DIR
                    / "data" / "analysis_output"
                    / "calibration_opportunity_training"
                    / "calibration_opportunity_training_table.jsonl",
                    _config.PROJECT_DIR / "data" / "analysis_output" / "clv" / "clv_rows.jsonl",
                ),
            ),
        ),
        RefreshStep(
            name="calibration_edge_shaving",
            description=(
                "Quantify how much edge the current probability calibrator "
                "shaves off post-structural score-event candidates and "
                "whether the shrinkage is justified by realized win rates. "
                "Emits a recommended --prob-calibration-enforce-min-raw "
                "(0.90->0.95 as of first run); feeds the manual lever "
                "decision + the L_enforce_min_raw_095 paper A/B."
            ),
            command=[
                _python(),
                _script("scripts/analysis/analyze_calibration_edge_shaving.py"),
            ],
            staleness_check=StalenessCheck(
                output_path=_config.PROJECT_DIR
                / "data" / "analysis_output"
                / "calibration_edge_shaving"
                / "calibration_edge_shaving.json",
                input_paths=(
                    _config.PROJECT_DIR
                    / "data" / "analysis_output"
                    / "calibration_opportunity_training"
                    / "by_family"
                    / "calibration_opportunity_training_table_score_event_transition.jsonl",
                    _config.PROJECT_DIR
                    / "data" / "analysis_output"
                    / "calibration"
                    / "signal_win_calibration.json",
                ),
            ),
        ),
        RefreshStep(
            name="train_baseline_models",
            description=(
                "Rebuild EV-policy win + fill baseline models from the "
                "leakage-aware training table. Consumed by EV-policy "
                "shadow scoring at next live-engine startup."
            ),
            command=[
                _python(),
                _script("scripts/analysis/train_baseline_models.py"),
                *strict_flag,
            ],
            staleness_check=StalenessCheck(
                output_path=_config.PROJECT_DIR / "data" / "analysis_output"
                / "model_baselines" / "signal_win_model.json",
                input_paths=(
                    _config.PROJECT_DIR / "data" / "analysis_output"
                    / "training_tables" / "signal_training_table.jsonl",
                ),
            ),
        ),
        RefreshStep(
            name="ev_policy_backtest",
            description=(
                "Rebuild EV-policy backtest report. Produces "
                "ev_policy_report.json that runtime EV scoring reads."
            ),
            command=[
                _python(),
                _script("scripts/analysis/backtest_ev_policy.py"),
                "--policy-mode",
                "live",
                "--artifact-purpose",
                "runtime-refit",
                *strict_flag,
            ],
            staleness_check=StalenessCheck(
                output_path=_config.PROJECT_DIR / "data" / "analysis_output"
                / "ev_policy" / "ev_policy_report.json",
                input_paths=(
                    _config.PROJECT_DIR / "data" / "analysis_output"
                    / "training_tables" / "signal_training_table.jsonl",
                    _config.PROJECT_DIR / "data" / "analysis_output"
                    / "model_baselines" / "signal_win_model.json",
                    _config.PROJECT_DIR / "data" / "analysis_output"
                    / "model_baselines" / "execution_fill_model.json",
                ),
            ),
        ),
        RefreshStep(
            name="stage2_run_env_retrain_staging",
            description=(
                "Refit Stage-2 run-env model on the latest game corpus and "
                "write to a STAGING path (cache/mlb_stage2_run_env.staging.json). "
                "The comparison step downstream alerts when the staged model "
                "would change Brier; the production cache is NEVER overwritten "
                "by this step."
            ),
            command=[
                _python(),
                _script("cache/build_mlb_stage2_run_env.py"),
                "--out",
                str(_config.PROJECT_DIR / "cache" / "mlb_stage2_run_env.staging.json"),
            ],
            staleness_check=StalenessCheck(
                output_path=_config.PROJECT_DIR / "cache" / "mlb_stage2_run_env.staging.json",
                input_paths=(
                    _config.PROJECT_DIR / "cache" / "mlb_ou_cache.json",
                    _config.PROJECT_DIR / "cache" / "park_hr_factors.json",
                ),
                input_dir_mtime_roots=(
                    _config.PROJECT_DIR / "data" / "games" / "regular",
                ),
            ),
        ),
        RefreshStep(
            name="stage3_team_offense_features",
            description="Rebuild leakage-free team-offense feature matrix (Stage-3 v2 input).",
            command=[
                _python(),
                _script("scripts/analysis/build_team_offense_features.py"),
            ],
            staleness_check=StalenessCheck(
                output_path=_config.PROJECT_DIR / "data" / "analysis_output"
                / "team_offense_calibration" / "team_features.jsonl",
                input_paths=(
                    _config.PROJECT_DIR / "cache" / "team_game_log.json",
                ),
            ),
        ),
        RefreshStep(
            name="stage3_team_offense_calibration_table",
            description="Rebuild Stage-3 v2 calibration table (per (game, half, line) rows + features).",
            command=[
                _python(),
                _script("scripts/analysis/build_team_offense_calibration_table.py"),
            ],
            staleness_check=StalenessCheck(
                output_path=_config.PROJECT_DIR / "data" / "analysis_output"
                / "team_offense_calibration" / "team_offense_calibration_table.jsonl",
                input_paths=(
                    _config.PROJECT_DIR / "cache" / "mlb_ou_cache.json",
                    _config.PROJECT_DIR / "data" / "analysis_output"
                    / "team_offense_calibration" / "team_features.jsonl",
                ),
                input_dir_mtime_roots=(
                    _config.PROJECT_DIR / "data" / "games" / "regular",
                ),
            ),
        ),
        RefreshStep(
            name="stage3_team_offense_v2_fit",
            description=(
                "Refit Stage-3 v2 team-offense weights. Output goes to "
                "data/analysis_output/team_offense_calibration/phase4_models.json "
                "(research path; production weights are compiled into "
                "team_offense_model.py and require an explicit promotion)."
            ),
            command=[
                _python(),
                _script("scripts/analysis/calibrate_team_offense_v2.py"),
            ],
            staleness_check=StalenessCheck(
                output_path=_config.PROJECT_DIR / "data" / "analysis_output"
                / "team_offense_calibration" / "phase4_models.json",
                input_paths=(
                    _config.PROJECT_DIR / "data" / "analysis_output"
                    / "team_offense_calibration" / "team_offense_calibration_table.jsonl",
                    _config.PROJECT_DIR / "data" / "analysis_output"
                    / "team_offense_calibration" / "team_features.jsonl",
                ),
            ),
        ),
        RefreshStep(
            name="model_freshness_health",
            kind="inline",
            description=(
                "Compare Stage-2 staging vs production cache and surface "
                "any meaningful drift; flag stale model artifacts."
            ),
            command=[],
        ),
        RefreshStep(
            name="stage3_v2_promotion_check",
            kind="inline",
            description=(
                "Diff today's Stage-3 v2 research fit (model_3_blend in "
                "phase4_models.json) against the active production "
                "weights or compiled-in defaults. Stability gate prevents "
                "single-day fit noise from firing a promotion alert."
            ),
            command=[],
        ),
        RefreshStep(
            name="execution_diagnostics",
            description="Rebuild trade execution diagnostics.",
            command=[
                _python(),
                _script("scripts/analysis/build_execution_diagnostics_report.py"),
                "--mode",
                "live",
                *max_date_args,
                "--no-console-report",
                *strict_flag,
            ],
        ),
        RefreshStep(
            name="queue_aware_execution_replay",
            description="Rebuild queue-aware execution replay.",
            command=[
                _python(),
                _script("scripts/analysis/build_queue_aware_execution_replay.py"),
                "--mode",
                "live",
                *max_date_args,
                *strict_flag,
            ],
        ),
        RefreshStep(
            name="learn_execution_policy",
            description=(
                "Rebuild offline learned-execution-policy prototype "
                "from the queue-aware replay."
            ),
            command=[
                _python(),
                _script("scripts/analysis/learn_execution_policy.py"),
            ],
        ),
        RefreshStep(
            name="state_value_transition_report",
            description="Rebuild state-value transition diagnostics.",
            command=[
                _python(),
                _script("scripts/analysis/build_state_value_transition_report.py"),
                "--mode",
                "live",
                *max_date_args,
            ],
        ),
        RefreshStep(
            name="under_state_value_transition_report",
            description=(
                "Phase A3 (2026-05-16): UNDER-side state-value transition "
                "diagnostics. Flips outcome (under_hit = not over_hit), "
                "computes under-side ROI from under_best_ask, inverts "
                "regime classifiers (negative current-state edge is "
                "positive for Under). Pure offline; no live trading "
                "behavior change until Phase C."
            ),
            command=[
                _python(),
                _script(
                    "scripts/analysis/build_under_state_value_transition_report.py"
                ),
                "--mode",
                "live",
                *max_date_args,
            ],
        ),
        RefreshStep(
            name="under_candidate_universe",
            description=(
                "Phase B A5 prereq (2026-05-16): synthesize UNDER "
                "candidate-universe rows from OVER. For each OVER "
                "candidate with under_pair_available=True AND a "
                "computed fair_value_raw, emit a `<date>_under_"
                "candidates.jsonl` sibling with flipped FV (UNDER "
                "calibrator applied when loaded; fallback_flip from "
                "OVER calibrated value otherwise), under_best_ask as "
                "decision_ask, decision='shadow_under'. Downstream "
                "consumers: B1 side-aware drift alerts, B3 per-side "
                "session reporting, future side-aware walk-forward."
            ),
            command=[
                _python(),
                _script(
                    "scripts/analysis/build_under_candidate_universe.py"
                ),
                "--mode",
                "live",
            ],
        ),
        RefreshStep(
            name="no_score_drift_policy",
            description="Rebuild no-score drift policy evaluator.",
            command=[
                _python(),
                _script("scripts/analysis/evaluate_no_score_drift_policy.py"),
                "--mode",
                "live",
                *max_date_args,
            ],
        ),
        RefreshStep(
            name="no_score_drift_paper_ledger",
            description="Rebuild no-score drift paper policy ledger.",
            command=[
                _python(),
                _script("scripts/analysis/build_no_score_drift_paper_ledger.py"),
                "--mode",
                "live",
                *max_date_args,
                "--stake",
                f"{config.stake:g}",
                "--daily-budget",
                f"{config.daily_budget:g}",
                "--per-game-budget-fraction",
                f"{config.per_game_budget_fraction:g}",
                *strict_flag,
            ],
        ),
    ]
