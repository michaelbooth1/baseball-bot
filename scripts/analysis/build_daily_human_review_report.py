#!/usr/bin/env python3
"""
Build a compact daily human-review report for MLB Polymarket live runs.

This script intentionally reads the durable daily artifacts instead of asking
operators or agents to scrape a 30MB runtime log:
  - data/live_trading/sessions/<date>_session.json
  - data/live_trading/candidate_universe/<date>_candidate_rollup.json
  - logs/real-logs/<date>.log (optional lightweight health counts only)

Outputs:
  data/analysis_output/daily_human_review/<date>_human_review.json
  data/analysis_output/daily_human_review/<date>_human_review.md

The report is descriptive only. It does not change gates, sizing, or execution.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

# Project-root bootstrap so bare `python scripts/analysis/build_daily_human_review_report.py`
# finds the `scripts.analysis.human_review` package. Without this, only
# `python -m scripts.analysis.build_daily_human_review_report` works -- and
# the daily-refresh subprocess uses bare invocation, so the refresh silently
# fails to build the review on this codepath.
_PROJECT_ROOT_FOR_IMPORTS = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT_FOR_IMPORTS) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT_FOR_IMPORTS))

from scripts.analysis.human_review import (
    _load_trailing_reviews,
    _count_log_health,
    _stage2_suppression_dollar_audit,
    _fill_rate_health,
    _signal_quality_health,
    _reconciler_summary,
    _fast_demote_health,
    _gate_counterfactual_health,
    _loss_attribution_health,
    _cohort_calibration_health,
    _cohort_roi_health,
    _calibration_health,
    _calibrator_enforce_shipment_health,
    _regime_mix_health,
    _concept_drift_health,
    _drift_in_drift_health,
    _stage1_cell_loss_health,
    _stage1_shadow_override_health,
    _stage1_alt_a_staging_health,
    _settlement_truth_health,
    _under_book_coverage_health,
    _cache_lineage_freshness_health,
    _cross_artifact_consistency_health,
    _promotion_lag_health,
    _daemon_readiness_health,
    _refresh_staleness_health,
    _under_emission_health,
    _under_outcomes_counterfactual_health,
    _under_paper_b4_milestone_health,
    _same_game_multi_fire_health,
    _fleet_paired_delta_health,
    _wilson_upper_bound,
    _shift_date,
    _parse_iso_to_epoch_safe,
    _latest_session_start_utc,
    _collect_window_filled_bets,
    _aggregate_cohort,
    _calibration_artifact_metadata,
    _recent_events_by_direction,
    _recent_promotions,
    _recent_demotions,
    _attribute_alert_to_promotions,
    _attribute_alert_to_demotions,
    _major_drift_features,
    _attribute_alert_to_concept_drift,
    _daemon_staleness_check,
    _collect_under_settled_rows,
    _aggregate_under_settled,
    _under_settled_by_cohort,
    _aggregate_calibration,
    _bet_is_calibratable,
    _cohort_edge_bucket,
    _cohort_inning_bucket,
    _cohort_line_bucket,
    _total_variation_distance,
)



# All constants moved to human_review.constants on 2026-05-25.
# This file used to carry ~440 lines of constant definitions that
# were exact duplicates of the subpackage; bulk-importing them keeps
# every existing reference working while removing the duplication.
from human_review.constants import *  # noqa: F401,F403
from human_review.constants import (  # noqa: F401  (explicit for static analysis)
    DEFAULT_SESSIONS_DIR,
    DEFAULT_CANDIDATE_DIR,
    DEFAULT_LOG_DIR,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_CALIBRATION_ARTIFACT,
    DEFAULT_CALIBRATION_ARTIFACT_UNDER,
    DEFAULT_PROMOTION_EVENTS_LOG,
    DEFAULT_CONCEPT_DRIFT_REPORT,
    DEFAULT_DRIFT_IN_DRIFT_REPORT,
    DEFAULT_DAEMON_RETROSPECTIVE_REPORT,
    DEFAULT_SETTLEMENT_TRUTH_REPORT,
    DEFAULT_MODEL_MATURITY_REPORT,
    DEFAULT_GATE_COUNTERFACTUAL_REPORT,
    DEFAULT_STARTUP_REFRESH_DIR,
    DEFAULT_PAPER_SESSIONS_DIR,
    LOG_PATTERNS,
)

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build compact daily human-review JSON/Markdown.")
    p.add_argument("--session-date", type=str, default="", help="YYYY-MM-DD. Defaults to latest live session.")
    p.add_argument("--sessions-dir", type=Path, default=DEFAULT_SESSIONS_DIR)
    p.add_argument("--candidate-dir", type=Path, default=DEFAULT_CANDIDATE_DIR)
    p.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument(
        "--calibration-artifact",
        type=Path,
        default=DEFAULT_CALIBRATION_ARTIFACT,
        help="Path to the runtime calibration JSON (for drift alerts).",
    )
    p.add_argument("--skip-log-counts", action="store_true", help="Do not scan the runtime log for health counts.")
    return p.parse_args(argv)


# All 13 small helpers (_now_iso, _load_json, _load_jsonl, _safe_float,
# _safe_int, _fmt_money, _fmt_pct, _line_key, _latest_session_date,
# _top_counter, _empty_side_totals, _finalize_side_totals,
# _summarize_bets) moved to human_review.helpers on 2026-05-25.
# Each was a duplicate of the existing subpackage version; this
# block re-imports them so every reference in this file keeps working.
from human_review.helpers import (  # noqa: F401  (used by build_report)
    _now_iso,
    _load_json,
    _load_jsonl,
    _safe_float,
    _safe_int,
    _fmt_money,
    _fmt_pct,
    _line_key,
    _latest_session_date,
    _top_counter,
    _empty_side_totals,
    _finalize_side_totals,
    _summarize_bets,
)


def _build_notes(
    session_summary: Dict[str, Any],
    bet_totals: Dict[str, Any],
    candidate_rollup: Dict[str, Any],
    log_health: Dict[str, Any],
    calibration_health: Optional[Dict[str, Any]] = None,
    calibrator_enforce_shipment_health: Optional[Dict[str, Any]] = None,
    fill_rate_health: Optional[Dict[str, Any]] = None,
    signal_quality_health: Optional[Dict[str, Any]] = None,
    regime_mix_health: Optional[Dict[str, Any]] = None,
    reconciler_summary: Optional[Dict[str, Any]] = None,
    cohort_roi_health: Optional[Dict[str, Any]] = None,
    concept_drift_health: Optional[Dict[str, Any]] = None,
    drift_in_drift_health: Optional[Dict[str, Any]] = None,
    daemon_readiness_health: Optional[Dict[str, Any]] = None,
    refresh_staleness_health: Optional[Dict[str, Any]] = None,
    under_book_coverage_health: Optional[Dict[str, Any]] = None,
    settlement_truth_health: Optional[Dict[str, Any]] = None,
    fast_demote_health: Optional[Dict[str, Any]] = None,
    gate_counterfactual_health: Optional[Dict[str, Any]] = None,
    cohort_calibration_health: Optional[Dict[str, Any]] = None,
    loss_attribution_health: Optional[Dict[str, Any]] = None,
    cache_lineage_freshness_health: Optional[Dict[str, Any]] = None,
    stage1_cell_loss_health: Optional[Dict[str, Any]] = None,
    stage1_shadow_override_health: Optional[Dict[str, Any]] = None,
    cross_artifact_consistency_health: Optional[Dict[str, Any]] = None,
    stage1_alt_a_staging_health: Optional[Dict[str, Any]] = None,
    promotion_lag_health: Optional[Dict[str, Any]] = None,
    under_emission_health: Optional[Dict[str, Any]] = None,
    under_outcomes_counterfactual_health: Optional[Dict[str, Any]] = None,
    under_paper_b4_milestone_health: Optional[Dict[str, Any]] = None,
    fleet_paired_delta_health: Optional[Dict[str, Any]] = None,
) -> List[str]:
    notes: List[str] = []
    roi = bet_totals.get("roi")
    win_rate = bet_totals.get("win_rate")
    if roi is not None:
        notes.append(f"Filled ROI was {_fmt_pct(roi)} with filled win rate {_fmt_pct(win_rate)}.")

    ev_allow = _safe_int(session_summary.get("ev_policy_shadow_allow"))
    ev_block = _safe_int(session_summary.get("ev_policy_shadow_block"))
    if ev_allow or ev_block:
        notes.append(f"EV policy shadow split: allow={ev_allow}, block={ev_block}; keep shadow-only until walk-forward evidence is larger.")

    current_bands = session_summary.get("current_state_edge_band_diagnostics") or {}
    danger = current_bands.get("current_edge_lt_0p03") or {}
    if _safe_int(danger.get("placed")):
        notes.append(
            "Current-state edge <0.03 had "
            f"{_safe_int(danger.get('placed'))} placed / {_safe_int(danger.get('filled'))} filled "
            f"with ROI {_fmt_pct(danger.get('filled_roi'))}; continue shadow-auditing before gating."
        )

    feature_diag = session_summary.get("shadow_feature_diagnostics") or {}
    low_ask_high_edge = (feature_diag.get("regimes") or {}).get("low_ask_high_edge") or {}
    if _safe_int(low_ask_high_edge.get("placed")):
        notes.append(
            "Low-ask/high-edge shadow cohort had "
            f"{_safe_int(low_ask_high_edge.get('placed'))} placed / "
            f"{_safe_int(low_ask_high_edge.get('filled'))} filled with ROI "
            f"{_fmt_pct(low_ask_high_edge.get('filled_roi'))}."
        )

    by_reason = candidate_rollup.get("by_decision_reason") or {}
    if by_reason:
        top_reason = max(by_reason.items(), key=lambda item: _safe_int(item[1]))
        notes.append(f"Top candidate blocker was {top_reason[0]} ({_safe_int(top_reason[1])} attempted rows).")

    counts = log_health.get("counts") or {}
    polling = _safe_int(counts.get("polling_token_books"))
    snapshots = _safe_int(counts.get("wrote_tick_snapshots"))
    if polling > 500 or snapshots > 500:
        notes.append(f"Log noise remains high: polling lines={polling}, snapshot-write lines={snapshots}.")

    # Surface calibration / fill / signal-quality drift alerts as top-level
    # notes so they show up in the markdown without operators having to
    # drill into the JSON.
    for alert in (calibration_health or {}).get("alerts") or []:
        notes.append(f"Calibration drift: {alert}")
    for alert in (
        calibrator_enforce_shipment_health or {}
    ).get("alerts") or []:
        notes.append(f"Calibrator-enforce-shipment: {alert}")
    for alert in (fill_rate_health or {}).get("alerts") or []:
        notes.append(f"Fill-rate drift: {alert}")
    for alert in (signal_quality_health or {}).get("alerts") or []:
        notes.append(f"Signal-quality drift: {alert}")
    for alert in (regime_mix_health or {}).get("alerts") or []:
        notes.append(f"Regime-mix drift: {alert}")
    for alert in (cohort_roi_health or {}).get("alerts") or []:
        notes.append(f"Cohort-ROI drift: {alert}")
    for alert in (cohort_calibration_health or {}).get("alerts") or []:
        notes.append(f"Cohort-calibration: {alert}")
    for alert in (concept_drift_health or {}).get("alerts") or []:
        notes.append(f"Concept-drift: {alert}")
    for alert in (drift_in_drift_health or {}).get("alerts") or []:
        notes.append(f"Drift-in-drift: {alert}")
    for alert in (daemon_readiness_health or {}).get("alerts") or []:
        notes.append(f"Daemon-readiness: {alert}")
    for alert in (refresh_staleness_health or {}).get("alerts") or []:
        notes.append(f"Refresh-staleness: {alert}")
    for alert in (under_book_coverage_health or {}).get("alerts") or []:
        notes.append(f"Under-book-coverage: {alert}")
    for alert in (settlement_truth_health or {}).get("alerts") or []:
        notes.append(f"Settlement-truth: {alert}")
    for alert in (fast_demote_health or {}).get("alerts") or []:
        notes.append(f"Fast-demote: {alert}")
    for alert in (gate_counterfactual_health or {}).get("alerts") or []:
        notes.append(f"Gate-counterfactual: {alert}")
    for alert in (loss_attribution_health or {}).get("alerts") or []:
        notes.append(f"Loss-attribution: {alert}")
    for alert in (cache_lineage_freshness_health or {}).get("alerts") or []:
        notes.append(f"Cache-lineage: {alert}")
    for alert in (stage1_cell_loss_health or {}).get("alerts") or []:
        notes.append(f"Stage1-cell-loss: {alert}")
    for alert in (stage1_shadow_override_health or {}).get("alerts") or []:
        notes.append(f"Stage1-shadow: {alert}")
    for alert in (cross_artifact_consistency_health or {}).get("alerts") or []:
        notes.append(f"Cross-artifact: {alert}")
    for alert in (stage1_alt_a_staging_health or {}).get("alerts") or []:
        notes.append(f"Stage1-alt-a-staging: {alert}")
    for alert in (promotion_lag_health or {}).get("alerts") or []:
        notes.append(f"Promotion-lag: {alert}")
    for alert in (under_emission_health or {}).get("alerts") or []:
        notes.append(f"Under-coverage: {alert}")
    for alert in (under_outcomes_counterfactual_health or {}).get("alerts") or []:
        notes.append(f"Under-outcomes: {alert}")
    # Phase C-paper follow-up (2026-05-27): B4 milestone alerts.
    # The helper already prefixes alerts with `Under-B4:`; append
    # them directly so the existing prefix-scan filters in
    # _count_persistent_under_drift_alerts can ignore them (B4
    # verdict alerts are not drift alerts).
    for alert in (under_paper_b4_milestone_health or {}).get("alerts") or []:
        notes.append(alert)
    # 2026-06-11: fleet paired-delta conclusions. Alerts carry their
    # own `Fleet-delta:` prefix (CONCLUSIVE_* = promotion/retirement
    # evidence ready; DEAD = preset produces no distinct decisions).
    for alert in (fleet_paired_delta_health or {}).get("alerts") or []:
        notes.append(alert)
    for alert in (reconciler_summary or {}).get("alerts") or []:
        notes.append(f"Reconciler watch: {alert}")

    return notes


def build_report(
    *,
    session_date: str,
    sessions_dir: Path = DEFAULT_SESSIONS_DIR,
    candidate_dir: Path = DEFAULT_CANDIDATE_DIR,
    log_dir: Path = DEFAULT_LOG_DIR,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    calibration_artifact: Path = DEFAULT_CALIBRATION_ARTIFACT,
    include_log_counts: bool = True,
) -> Dict[str, Any]:
    session_path = sessions_dir / f"{session_date}_session.json"
    rollup_path = candidate_dir / f"{session_date}_candidate_rollup.json"
    log_path = log_dir / f"{session_date}.log"

    session = _load_json(session_path)
    candidate_rollup = _load_json(rollup_path) if rollup_path.exists() else (
        session.get("summary", {}).get("candidate_rollup") or {}
    )
    bet_rows, bet_totals = _summarize_bets(session.get("bets", []))
    log_health = _count_log_health(log_path) if include_log_counts else {
        "log_path": str(log_path),
        "exists": log_path.exists(),
        "counts": {},
        "skipped": True,
    }
    session_summary = session.get("summary") or {}
    session_summary_compact = {
        key: value
        for key, value in session_summary.items()
        if key not in {
            "candidate_rollup",
            "shadow_order_diagnostics",
            "shadow_feature_diagnostics",
            "current_state_edge_band_diagnostics",
        }
    }
    # concept_drift_health computes BEFORE calibration_health so the
    # latter can read its `upgrade_attribution_summary` and reword the
    # input-drift-triggered alert when all major-PSI features are
    # attributable to known model upgrades (Hygiene #23, 2026-05-20).
    concept_drift_health = _concept_drift_health(
        report_path=DEFAULT_CONCEPT_DRIFT_REPORT,
        session_date=session_date,
    )
    calibration_health = _calibration_health(
        session_date=session_date,
        candidate_dir=candidate_dir,
        artifact_path=calibration_artifact,
        output_root=output_root,
        artifact_path_under=DEFAULT_CALIBRATION_ARTIFACT_UNDER,
        concept_drift_health=concept_drift_health,
    )
    trailing_reviews = _load_trailing_reviews(
        output_root=output_root,
        today=session_date,
        days=DRIFT_TRAILING_WINDOW_DAYS,
        mode=session.get("mode"),
    )
    fill_rate_health = _fill_rate_health(
        today_bet_totals=bet_totals,
        trailing_reviews=trailing_reviews,
        session_mode=session.get("mode"),
    )
    # Shipment-effect monitor for band-gated calibrator-enforce
    # (2026-05-20). Counterfactual under shadow, attribution under
    # enforce. Read after _calibration_health since both consume the
    # candidate log; this one specifically answers "is the calibrator-
    # enforce gate biting at the right tail with the right volume."
    calibrator_enforce_shipment_health = _calibrator_enforce_shipment_health(
        session_date=session_date,
        candidate_dir=candidate_dir,
        trailing_reviews=trailing_reviews,
    )
    # 2026-06-03 plumbing add: catch dedup-leak bugs the same day they
    # happen instead of via P&L pattern-spotting in audits. Reads raw
    # session bets (not the summarized rows from _summarize_bets) so it
    # has access to game_pk, placed_at, and inning directly.
    same_game_multi_fire_health = _same_game_multi_fire_health(
        session_date=session_date,
        bets=session.get("bets", []),
    )
    signal_quality_health = _signal_quality_health(
        today_bet_totals=bet_totals,
        trailing_reviews=trailing_reviews,
    )
    regime_mix_health = _regime_mix_health(
        today_bet_rows=bet_rows,
        trailing_reviews=trailing_reviews,
    )
    # Cohort-ROI drift uses two windows: the same 7d trailing window as the
    # other drift checks for "recent" cohort outcomes, and a 30d baseline
    # for regime-change detection. The 30d window is loaded separately
    # rather than re-using trailing_reviews so the window-day constants
    # can diverge cleanly without a refactor.
    baseline_reviews = _load_trailing_reviews(
        output_root=output_root,
        today=session_date,
        days=COHORT_ROI_BASELINE_WINDOW_DAYS,
        mode=session.get("mode"),
    )
    # concept_drift_health was already computed earlier (so
    # calibration_health could consume its attribution summary);
    # cohort_roi_health also reads it to append a "[concept-drift:
    # <feature> PSI <value>, ...]" candidate-root-cause suffix.
    cohort_roi_health = _cohort_roi_health(
        today_bet_rows=bet_rows,
        trailing_reviews=trailing_reviews,
        baseline_reviews=baseline_reviews,
        session_date=session_date,
        promotion_events_log_path=DEFAULT_PROMOTION_EVENTS_LOG,
        concept_drift_health=concept_drift_health,
    )
    # Active #9 (2026-05-17): per-cohort calibration drift. Uses the
    # same trailing-7d window as cohort_roi so the two reports compare
    # cleanly. Pulls promotion/demotion/concept-drift attribution from
    # the same helpers.
    cohort_calibration_health = _cohort_calibration_health(
        today_bet_rows=bet_rows,
        trailing_reviews=trailing_reviews,
        session_date=session_date,
        promotion_events_log_path=DEFAULT_PROMOTION_EVENTS_LOG,
        concept_drift_health=concept_drift_health,
    )
    drift_in_drift_health = _drift_in_drift_health(
        report_path=DEFAULT_DRIFT_IN_DRIFT_REPORT,
        session_date=session_date,
    )
    daemon_readiness_health = _daemon_readiness_health(
        report_path=DEFAULT_DAEMON_RETROSPECTIVE_REPORT,
        session_date=session_date,
    )
    # 2026-05-21 (P1b): refresh-staleness alert. Fires when the
    # daily refresh hasn't run in N days -- caught a real outage
    # where the refresh stopped firing for 48h and downstream
    # artifacts silently went stale.
    refresh_staleness_health = _refresh_staleness_health(
        session_date=session_date,
    )
    under_book_coverage_health = _under_book_coverage_health(
        report_path=DEFAULT_MODEL_MATURITY_REPORT,
        session_date=session_date,
    )
    settlement_truth_health = _settlement_truth_health(
        report_path=DEFAULT_SETTLEMENT_TRUTH_REPORT,
        session_date=session_date,
    )
    fast_demote_health = _fast_demote_health(
        audit_log_path=DEFAULT_PROMOTION_EVENTS_LOG,
        sessions_dir=sessions_dir,
        today=session_date,
    )
    gate_counterfactual_health = _gate_counterfactual_health(
        report_path=DEFAULT_GATE_COUNTERFACTUAL_REPORT,
        session_date=session_date,
    )
    loss_attribution_health = _loss_attribution_health(
        report_path=DEFAULT_LOSS_ATTRIBUTION_REPORT,
        session_date=session_date,
    )
    # Active #16 v3 (2026-05-17): surface the embedded lineage block
    # from each major cache + calibrator artifact in the daily review.
    # Complementary to the artifact_lineage_freshness mtime check.
    cache_lineage_freshness_health = _cache_lineage_freshness_health()
    # Active #10 follow-up (2026-05-17): drill the Stage-1 ownership
    # of the aggregate bias into Stage-1-internal cohorts so the
    # operator sees which cells need surgical attention.
    stage1_cell_loss_health = _stage1_cell_loss_health(
        report_path=DEFAULT_STAGE1_CELL_LOSS_REPORT,
        session_date=session_date,
    )
    # Active #8 prep: surface the shadow-override counterfactual.
    stage1_shadow_override_health = _stage1_shadow_override_health(
        report_path=DEFAULT_STAGE1_SHADOW_OVERRIDE_REPORT,
        session_date=session_date,
    )
    # Active #16 v4 (2026-05-17): cross-artifact consistency check.
    # Catches "calibrator built against Stage-1 sha X but production
    # Stage-1 is sha Y" silent inconsistencies.
    cross_artifact_consistency_health = _cross_artifact_consistency_health()
    # Active #8 (2026-05-17): Stage-1 Alt-A staging cache existence
    # + freshness + override-stats surface. Operator runs `promote.py
    # stage1` to flip it into production after paper-mode validation.
    stage1_alt_a_staging_health = _stage1_alt_a_staging_health()
    # Active #15 (2026-05-19): per-lever "is my promote in effect?"
    # status comparing each cache mtime against latest engine-boot
    # proxy (first-bet placed_at of most-recent session file).
    promotion_lag_health = _promotion_lag_health()
    # Phase A5 follow-up (2026-05-19): UNDER emission observability.
    # Surfaces coverage rate + decision breakdown + price quality of
    # the new UNDER candidate emission so the operator can validate
    # `--under-emission-mode shadow` is working before any UNDER
    # paper-bet validation milestone.
    under_emission_health = _under_emission_health(
        session_date=session_date,
        candidate_dir=candidate_dir,
    )
    # Phase A5 follow-up #2 (2026-05-19): UNDER outcomes counterfactual.
    # Settles every shadow_under candidate against final_total +
    # computes the counterfactual P&L the bot WOULD have realized.
    # Drives the eventual Phase B4 paper-bet milestone decision.
    under_outcomes_counterfactual_health = _under_outcomes_counterfactual_health(
        session_date=session_date,
        candidate_dir=candidate_dir,
    )
    # Phase C-paper follow-up (2026-05-27): B4 milestone tracker.
    # Walks paper_sessions_dir + live_sessions_dir for the trailing
    # 60d and reports verdict status against the 5 ROADMAP B4
    # conditions using ACTUAL side="under" paper bets (the prior
    # block tracks SHADOW counterfactuals which does not advance B4).
    under_paper_b4_milestone_health = _under_paper_b4_milestone_health(
        session_date=session_date,
        paper_sessions_dir=DEFAULT_PAPER_SESSIONS_DIR,
        live_sessions_dir=sessions_dir,
        output_root=output_root,
    )
    # 2026-06-11: fleet paired-delta block. Computes per-preset
    # shared/unique bet cohorts vs the A_current baseline so fleet
    # conclusions (CONCLUSIVE / DEAD presets) surface here instead of
    # requiring a manual audit. The aggregator's marginal tables stay
    # in parallel_engine_comparison/; this block is the decision lens.
    fleet_paired_delta_health = _fleet_paired_delta_health(
        session_date=session_date,
    )
    reconciler_summary = _reconciler_summary(session.get("bets") or [])
    notes = _build_notes(
        session_summary,
        bet_totals,
        candidate_rollup,
        log_health,
        calibration_health,
        calibrator_enforce_shipment_health,
        fill_rate_health,
        signal_quality_health,
        regime_mix_health,
        reconciler_summary,
        cohort_roi_health,
        concept_drift_health,
        drift_in_drift_health,
        daemon_readiness_health,
        refresh_staleness_health,
        under_book_coverage_health,
        settlement_truth_health,
        fast_demote_health,
        gate_counterfactual_health,
        cohort_calibration_health,
        loss_attribution_health,
        cache_lineage_freshness_health,
        stage1_cell_loss_health,
        stage1_shadow_override_health,
        cross_artifact_consistency_health,
        stage1_alt_a_staging_health,
        promotion_lag_health,
        under_emission_health,
        under_outcomes_counterfactual_health,
        under_paper_b4_milestone_health,
        fleet_paired_delta_health,
    )
    stake_usdc = _safe_float((session.get("params") or {}).get("stake"), 10.0)
    stage2_audit = _stage2_suppression_dollar_audit(
        session_date=session_date,
        candidate_dir=candidate_dir,
        stake_usdc=stake_usdc,
    )

    return {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "session_date": session_date,
        "mode": session.get("mode"),
        "source_files": {
            "session": str(session_path),
            "candidate_rollup": str(rollup_path) if rollup_path.exists() else None,
            "log": str(log_path) if log_path.exists() else None,
        },
        "session_summary": session_summary_compact,
        "bet_totals": bet_totals,
        "bets": bet_rows,
        "candidate_rollup_compact": {
            "attempted_rows": candidate_rollup.get("attempted_rows"),
            "written_rows": candidate_rollup.get("written_rows"),
            "dedup_suppressed_rows": candidate_rollup.get("dedup_suppressed_rows"),
            "write_error_rows": candidate_rollup.get("write_error_rows"),
            "by_decision": candidate_rollup.get("by_decision") or {},
            "by_state_value_strategy": candidate_rollup.get("by_state_value_strategy") or {},
            "by_shadow_diagnostic": candidate_rollup.get("by_shadow_diagnostic") or {},
            "top_decision_reasons": _top_counter(candidate_rollup.get("by_decision_reason") or {}, 15),
            "top_game_line_reasons": _top_counter(candidate_rollup.get("top_game_line_reasons") or {}, 15),
        },
        "shadow_order_diagnostics": session_summary.get("shadow_order_diagnostics") or {},
        "shadow_feature_diagnostics": session_summary.get("shadow_feature_diagnostics") or {},
        "current_state_edge_band_diagnostics": session_summary.get("current_state_edge_band_diagnostics") or {},
        "stage2_suppression_dollar_audit": stage2_audit,
        "calibration_health": calibration_health,
        "calibrator_enforce_shipment_health": (
            calibrator_enforce_shipment_health
        ),
        "same_game_multi_fire_health": same_game_multi_fire_health,
        "fill_rate_health": fill_rate_health,
        "signal_quality_health": signal_quality_health,
        "regime_mix_health": regime_mix_health,
        "cohort_roi_health": cohort_roi_health,
        "cohort_calibration_health": cohort_calibration_health,
        "loss_attribution_health": loss_attribution_health,
        "cache_lineage_freshness_health": cache_lineage_freshness_health,
        "stage1_cell_loss_health": stage1_cell_loss_health,
        "stage1_shadow_override_health": stage1_shadow_override_health,
        "cross_artifact_consistency_health": cross_artifact_consistency_health,
        "stage1_alt_a_staging_health": stage1_alt_a_staging_health,
        "promotion_lag_health": promotion_lag_health,
        "under_emission_health": under_emission_health,
        "under_outcomes_counterfactual_health": (
            under_outcomes_counterfactual_health
        ),
        "under_paper_b4_milestone_health": (
            under_paper_b4_milestone_health
        ),
        "fleet_paired_delta_health": fleet_paired_delta_health,
        "concept_drift_health": concept_drift_health,
        "drift_in_drift_health": drift_in_drift_health,
        "daemon_readiness_health": daemon_readiness_health,
        "refresh_staleness_health": refresh_staleness_health,
        "under_book_coverage_health": under_book_coverage_health,
        "settlement_truth_health": settlement_truth_health,
        "fast_demote_health": fast_demote_health,
        "gate_counterfactual_health": gate_counterfactual_health,
        "reconciler_summary": reconciler_summary,
        "log_health": log_health,
        "notes": notes,
    }


# render_markdown + _markdown_table moved to
# human_review/render_md.py on 2026-05-25. Re-exported here for
# back-compat with any caller importing them by their old path.
from human_review.render_md import (  # noqa: F401  (re-export)
    render_markdown,
    _markdown_table,
)


def write_report(report: Dict[str, Any], output_root: Path) -> Tuple[Path, Path]:
    output_root.mkdir(parents=True, exist_ok=True)
    session_date = str(report["session_date"])
    json_path = output_root / f"{session_date}_human_review.json"
    md_path = output_root / f"{session_date}_human_review.md"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    # Auto-derive candidate-dir from sessions-dir when the operator
    # overrode --sessions-dir (e.g. to point at paper_trading) but left
    # --candidate-dir at its live_trading default. Without this, UNDER
    # health blocks silently `check_error` because they look in the wrong
    # mode's candidate_universe. Folder convention: <mode_root>/sessions
    # is a sibling of <mode_root>/candidate_universe.
    if (
        args.candidate_dir == DEFAULT_CANDIDATE_DIR
        and args.sessions_dir != DEFAULT_SESSIONS_DIR
    ):
        derived = args.sessions_dir.parent / "candidate_universe"
        if derived.exists():
            args.candidate_dir = derived
    session_date = args.session_date or _latest_session_date(args.sessions_dir)
    report = build_report(
        session_date=session_date,
        sessions_dir=args.sessions_dir,
        candidate_dir=args.candidate_dir,
        log_dir=args.log_dir,
        output_root=args.output_root,
        calibration_artifact=args.calibration_artifact,
        include_log_counts=not args.skip_log_counts,
    )
    json_path, md_path = write_report(report, args.output_root)
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
