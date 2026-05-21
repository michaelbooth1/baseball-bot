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



PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SESSIONS_DIR = PROJECT_DIR / "data" / "live_trading" / "sessions"
DEFAULT_CANDIDATE_DIR = PROJECT_DIR / "data" / "live_trading" / "candidate_universe"
DEFAULT_LOG_DIR = PROJECT_DIR / "logs" / "real-logs"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "daily_human_review"
DEFAULT_CALIBRATION_ARTIFACT = (
    PROJECT_DIR / "data" / "analysis_output" / "calibration" / "signal_win_calibration.json"
)
# Phase B B1 (2026-05-16): the daily-review calibration_health block
# reads the UNDER calibrator artifact alongside the OVER one and
# surfaces both in a side-aware breakdown. The OVER fields stay at
# the top level (back-compat); UNDER adds a `under` sub-block with
# the same metadata shape + side-prefixed alerts.
DEFAULT_CALIBRATION_ARTIFACT_UNDER = (
    PROJECT_DIR / "data" / "analysis_output" / "calibration"
    / "signal_win_calibration_under.json"
)

# Thresholds for calibration drift alerts. The sampled mean absolute delta
# between calibrated and raw FV is the load-time signal that mirrors what
# the calibrator artifact says at fit time -- if it is essentially zero, the
# runtime is behaving identity even when the artifact claims otherwise.
CALIBRATION_NEAR_IDENTITY_DELTA = 0.005
CALIBRATION_LOW_APPLIED_SHARE = 0.05
# When this share of sampled rows carry fair_value_calibration_mode="shadow",
# applied=False is the documented expected behaviour, so the low-applied-share
# alert is a false positive. Replace it with one informational note.
CALIBRATION_SHADOW_MODE_DOMINANT_SHARE = 0.95
CALIBRATION_STALE_AGE_DAYS = 14

# Drift-alert thresholds. Daily samples are small (~2-10 bets), so the
# windows and absolute drops are deliberately wide -- we want to catch
# regressions like the orphan-fill bug, not noise from one bad session.
DRIFT_TRAILING_WINDOW_DAYS = 7
DRIFT_MIN_TODAY_SAMPLE = 3
DRIFT_MIN_BASELINE_SAMPLE = 10
DRIFT_FILL_RATE_DROP_PP = 0.20
DRIFT_WIN_RATE_DROP_PP = 0.20
DRIFT_ZERO_DAY_MIN_SAMPLE = 5
# Wilson-interval Z-score for the upper-bound test that gates point-
# estimate drops on statistical significance. 1.645 = 90% one-sided
# (i.e. we alert only when today's rate is significantly below baseline,
# not just below by some fixed delta -- prevents false positives at n=3).
DRIFT_WILSON_Z = 1.645
# Total Variation Distance threshold for regime-mix shift alerts. TVD = 0.5
# would mean half the placed bets moved between buckets -- 0.30 catches a
# clearly different cohort while staying tolerant of small-sample noise.
DRIFT_REGIME_MIX_TVD = 0.30
# Share of filled bets that had to be recovered by the orphan-fill reconciler.
# Anything above this strongly suggests the CLOB SDK fill path is missing
# real fills and the data-api should become the primary source (Active #2).
RECONCILER_HIGH_SHARE = 0.10

# Cohort-ROI drift alert thresholds (added 2026-05-15). Companion to
# regime_mix_health: that one fires on pre-trade *distribution* shifts;
# this one fires on *outcome* shifts -- a cohort that was profitable but
# is now losing money. Two flavours of alert:
#   1. Absolute losing: cohort has been trading at scale (>= MIN_BETS) and
#      its trailing-WINDOW-day ROI is below LOSING_THRESHOLD.
#   2. Regime change: cohort's trailing-WINDOW-day ROI is materially worse
#      (>= REGIME_DELTA pp) than its trailing-BASELINE-day baseline.
# Sample-size floor matters: at ~3.4 fills/day, a 7-day window has ~24
# bets total = ~6 per 4-bucket dimension, on the edge of usable.
COHORT_ROI_TRAILING_WINDOW_DAYS = 7
COHORT_ROI_BASELINE_WINDOW_DAYS = 30
COHORT_ROI_MIN_BETS_FOR_ALERT = 5
COHORT_ROI_LOSING_THRESHOLD = -0.10
COHORT_ROI_REGIME_DELTA = 0.15

# Active #9 (2026-05-17): per-cohort calibration drift detection. The
# 8th drift dimension. The aggregate `calibration_health` block tells
# the operator "is the model calibrated on average"; this block answers
# "is any cohort SYSTEMATICALLY mis-calibrated even when the aggregate
# looks fine." Mirrors COHORT_DIMENSIONS used by cohort_roi_health so
# cuts are consistent across the drift family.
#
# Reliability gap = |mean(fair_value) - mean(won)| within the cohort.
# Fires when a cohort's reliability gap exceeds the aggregate gap by
# >= 2x AND the cohort has >= 30 filled+settled bets. The 30-bet floor
# is stricter than cohort_roi's 5-bet floor because reliability noise
# is dominated by sample variance at low n -- below 30, a single
# unlucky win/loss row swings the gap by 1/n.
#
# Today's CHC@CWS miss (2026-05-17) is the canonical failure mode
# this block catches: aggregate calibration looks OK while a specific
# cohort (high-line, late-inning, Stage-2-suppressed) is systematically
# under-predicting Overs.
COHORT_CALIBRATION_WINDOW_DAYS = 7
COHORT_CALIBRATION_MIN_N_FOR_ALERT = 30
COHORT_CALIBRATION_GAP_RATIO_ALERT = 2.0
# Don't fire on micro aggregate gaps -- if the aggregate model is
# perfectly calibrated to 4 decimals, a cohort at 1pp deviation is
# 25x the aggregate but trivial in absolute terms. Require the
# aggregate gap to be at least 1pp before the ratio test has meaning.
COHORT_CALIBRATION_MIN_AGGREGATE_GAP = 0.01
# Aggregate-level alert thresholds. Separate from the cohort-vs-
# aggregate ratio alert -- this fires when the WHOLE model is mis-
# calibrated by a material amount, even if no single cohort stands
# out. At ~24 fills/week, cohort n=30 rarely fires; the aggregate
# alert keeps the block useful at current volume. Threshold: 10pp
# reliability gap is well past noise (a perfectly-calibrated model
# settles to ~2pp at n=50 by sample variance alone) and warrants
# operator attention.
COHORT_CALIBRATION_AGGREGATE_GAP_ALERT = 0.10
COHORT_CALIBRATION_AGGREGATE_MIN_N = 15

# Concept-drift health (added 2026-05-15). Reads the leading-indicator
# drift report built by `build_concept_drift_report.py`. We don't
# recompute PSI here -- we just summarise the artifact and roll its
# alerts up into the daily review's Notes block. Stale-artifact threshold
# matches the calibration-artifact threshold so a missed refresh on
# either dimension fires the same shape alert.
DEFAULT_CONCEPT_DRIFT_REPORT = (
    PROJECT_DIR / "data" / "analysis_output" / "concept_drift" / "concept_drift_report.json"
)
DEFAULT_DRIFT_IN_DRIFT_REPORT = (
    PROJECT_DIR / "data" / "analysis_output" / "concept_drift" / "drift_in_drift_report.json"
)
DEFAULT_DAEMON_RETROSPECTIVE_REPORT = (
    PROJECT_DIR / "data" / "analysis_output" / "daemon_retrospective" / "daemon_retrospective.json"
)
CONCEPT_DRIFT_STALE_AGE_DAYS = 14
DRIFT_IN_DRIFT_STALE_AGE_DAYS = 14
DAEMON_RETROSPECTIVE_STALE_AGE_DAYS = 14
# Daemon staleness: a lever whose verdict says "promote"/"demote" but
# hasn't been actuated for this long indicates something is wrong --
# cooldown stuck, file path wrong, opt-out flag stuck on, daemon-mode
# `off` by accident. 60 days is well past the 14-day cooldown so a
# legitimate cooldown-blocked sequence doesn't false-fire.
DAEMON_STALENESS_THRESHOLD_DAYS = 60

# Promotion-attribution: when a drift alert fires, check whether any
# promotion happened in the last N days and append a hint to the alert
# text. Lets the operator see "this cohort started losing right after
# we promoted X" without having to grep the audit log themselves.
DEFAULT_PROMOTION_EVENTS_LOG = (
    PROJECT_DIR / "data" / "analysis_output" / "promotion_events.jsonl"
)
PROMOTION_ATTRIBUTION_WINDOW_DAYS = 14

# Settlement-truth verification (Active #12, 2026-05-17). Cross-
# checks each settled bet against the MLB Stats API ground truth.
# This block surfaces the report's headline metrics so operators
# see ledger-vs-truth disagreements in the daily review without
# opening the dedicated artifact. Critical: ANY resolution_mismatch
# is treated as a data-integrity alert (ROI math may be corrupted).
DEFAULT_SETTLEMENT_TRUTH_REPORT = (
    PROJECT_DIR / "data" / "analysis_output" / "settlement_truth"
    / "settlement_truth_report.json"
)
SETTLEMENT_TRUTH_STALE_AGE_DAYS = 14
# Thresholds mirrored from verify_settlement_truth so the daily
# review can fall back to them when the artifact omits its own.
STALE_FILLED_ALERT_THRESHOLD = 1
MISSING_MLB_DATA_RATE_ALERT_THRESHOLD = 0.10


# Under-side book coverage (Phase A1, 2026-05-16). The monitor polls
# both Over and Under token books and signal_engine attaches the
# under-side fields when the under tick arrives in the same poll cycle.
# In production, under_pair_available is roughly 50% -- the
# tick-timing variance prevents pairing on every cycle. For Phase A
# (UNDER offline analysis), this is fine; the candidate_universe /
# calibration table / walk-forward table get the under-side columns
# when present and accept None when absent. The daily review surfaces
# the rate so operators see the coverage level; Phase C (market-maker
# two-sided quoting) will need to raise it before live UNDER quoting.
DEFAULT_MODEL_MATURITY_REPORT = (
    PROJECT_DIR / "data" / "analysis_output" / "model_maturity" / "model_maturity_report.json"
)
UNDER_BOOK_COVERAGE_WARN_THRESHOLD = 0.50
UNDER_BOOK_COVERAGE_STALE_AGE_DAYS = 14


# Gate counterfactual report (Active #11, 2026-05-17). Reads the
# `gate_counterfactual_report.json` artifact built daily by
# build_gate_counterfactual_report.py and surfaces the top tightening
# recommendations as a daily-review block. Mirrors top recommendations
# whose counterfactual_profit_delta_usd clears the alert threshold to
# the top-level Notes block with prefix "Gate-counterfactual:". This
# is the leading-indicator complement to the cert's once-per-month
# verdict: the cert says "this gate is structurally sound on average,"
# this block says "tightening this gate by one click would have saved
# $X last week."
DEFAULT_GATE_COUNTERFACTUAL_REPORT = (
    PROJECT_DIR / "data" / "analysis_output" / "gate_counterfactual"
    / "gate_counterfactual_report.json"
)
GATE_COUNTERFACTUAL_STALE_AGE_DAYS = 14

# Loss attribution (Active #10, 2026-05-17). The cohort_calibration
# block (Active #9) tells the operator the model is mis-calibrated;
# this block tells them WHICH STAGE owns it. Reads the artifact
# produced by build_loss_attribution_report.py and surfaces the
# trailing-30d top culprit + bias direction. Mirrors any culprit
# stage owning >= LOSS_ATTRIBUTION_NOTES_MIN_SHARE to Notes with
# prefix `Loss-attribution:`. A stage owning >= 50% of the bias is
# a clear retrain target; below that, multiple stages share blame.
DEFAULT_LOSS_ATTRIBUTION_REPORT = (
    PROJECT_DIR / "data" / "analysis_output" / "loss_attribution"
    / "loss_attribution_report.json"
)
LOSS_ATTRIBUTION_STALE_AGE_DAYS = 14

# Stage-1 cell loss attribution (Active #10 follow-up, 2026-05-17).
# Reads the per-cohort drill-down built by
# build_stage1_cell_loss_attribution.py and surfaces the top Stage-1
# cohort culprit + an alert when the aggregate Stage-1 bias is material
# AND fallback_rate is high (the signal that Active #8 needs surgical
# attention to the fallback path).
DEFAULT_STAGE1_CELL_LOSS_REPORT = (
    PROJECT_DIR / "data" / "analysis_output"
    / "stage1_cell_loss_attribution"
    / "stage1_cell_loss_attribution.json"
)
STAGE1_CELL_LOSS_STALE_AGE_DAYS = 14

# Stage-1 shadow override report (Active #8 prep, 2026-05-17).
# Replays two candidate Stage-1 fixes (Alt A: empirical-when-available,
# Alt B: block deep fallback) against actual outcomes. This block
# surfaces the trailing-30d bias delta from Alt A + the counterfactual
# $ delta from Alt B, plus any recommendation that fired.
DEFAULT_STAGE1_SHADOW_OVERRIDE_REPORT = (
    PROJECT_DIR / "data" / "analysis_output"
    / "stage1_shadow_override"
    / "stage1_shadow_override_report.json"
)
STAGE1_SHADOW_OVERRIDE_STALE_AGE_DAYS = 14
# Notes-mirror floor for the aggregate Stage-1 bias. Below 5pp the
# Stage-1 contribution is approximately neutral and there's nothing
# surgical to attack.
STAGE1_CELL_LOSS_MIN_ABS_BIAS = 0.05
# Fallback-rate notes floor. A fallback rate above this AND a
# material Stage-1 bias is the signature that the fallback path
# (not the exact-cell math) owns the over/under-prediction.
STAGE1_CELL_LOSS_FALLBACK_RATE_NOTES_FLOOR = 0.50

# Cache lineage freshness (Active #16 v3, 2026-05-17). Each major
# cache file (Stage-1, Stage-2, Stage-3 v2 weights, calibrator) now
# carries an embedded lineage block (v2 shipped earlier today). This
# block surfaces the build_at_utc + git_sha summary in the daily
# review so operators see "Stage-1 cache last built 12 days ago on
# data through 2026-05-15" without opening each artifact.
# Mirrors any cache whose build_age exceeds the warn threshold to
# Notes with prefix `Cache-lineage:`.
DEFAULT_STAGE1_CACHE_PATH = PROJECT_DIR / "cache" / "mlb_ou_cache.json"
DEFAULT_STAGE2_CACHE_PATH = PROJECT_DIR / "cache" / "mlb_stage2_run_env.json"
DEFAULT_STAGE3_V2_WEIGHTS_PATH = (
    PROJECT_DIR / "cache" / "team_offense_v2_weights.json"
)
# 14d default threshold for "your cache is getting stale." The Stage-1
# cache currently rebuilds inside the daily refresh on a StalenessCheck
# (rebuilds when input data has changed), so this threshold should
# rarely fire under healthy operation. It DOES fire when the daily
# refresh is broken or skipped for two weeks; cheap canary.
CACHE_LINEAGE_BUILD_AGE_WARN_DAYS = 14

# Active #8 (2026-05-17): Stage-1 Alt-A staging cache. The refresh
# step `stage1_ou_cache_alt_a` materializes the runtime's on-the-fly
# Alt-A shadow (today's shadow-override report: -6pp aggregate bias
# on 30d) as a real cache file at this staging path. Operator runs
# `promote.py stage1 --source <this>` to atomically swap it into
# production after paper-mode validation clears its bar. The daily
# review surfaces existence + age + override stats so operators see
# the staging cache is being maintained.
DEFAULT_STAGE1_ALT_A_STAGING_PATH = (
    PROJECT_DIR / "cache" / "mlb_ou_cache_alt_a.staging.json"
)
# Same 14d threshold as cache_lineage_freshness_health, for the same
# reason: under healthy refresh the StalenessCheck rebuilds the
# staging cache when game data changes. Past 14d without rebuild =
# the refresh step is broken or the host has been offline.
STAGE1_ALT_A_STAGING_AGE_WARN_DAYS = 14

# Active #15 (2026-05-19): Promotion-lag tracker.
# Every promote.py file-swap changes a cache file (or the runtime-
# overrides JSON), but the live engine loads those files at NEXT
# SESSION BOOT. Operators have asked "is my promote in effect yet?"
# enough times to deserve a structured answer in the daily review.
# Per-lever logic: compare each lever's cache mtime against the most
# recent engine-boot timestamp (proxied by first-bet `placed_at`
# from the latest session file). If cache mtime > latest engine boot
# → the promote hasn't taken effect yet, lag clock starts.
PROMOTION_LAG_LEVERS: Tuple[Tuple[str, str], ...] = (
    # (lever_name, repo-relative cache/override path)
    ("stage1", "cache/mlb_ou_cache.json"),
    ("stage2", "cache/mlb_stage2_run_env.json"),
    ("stage3_v2", "cache/team_offense_v2_weights.json"),
    # stake-scaling + gate-threshold both mutate the same overrides
    # JSON. The lag verdict is identical for both -- any promote of
    # either lever bumps the file mtime, so both report the same
    # status. We surface them separately so the operator who promoted
    # one sees the line under that lever's name.
    ("stake_scaling", "cache/live_engine_overrides.json"),
    ("gate_threshold", "cache/live_engine_overrides.json"),
)
# Sessions can be either paper or live; the engine boots the same way
# in both modes. Walk both dirs so an operator running in paper-mode
# (today's scenario) still gets accurate "is my promote in effect"
# answers.
PROMOTION_LAG_SESSION_ROOTS: Tuple[str, ...] = (
    "data/live_trading/sessions",
    "data/paper_trading/sessions",
)
# Alert threshold: if a promote has been pending engine-boot for more
# than this many hours, surface a Notes line. 24h matches the typical
# daily-session cadence -- a pending promote that survives one
# overnight window almost certainly means the operator promoted but
# never restarted the engine.
PROMOTION_LAG_PENDING_HOURS_WARN = 24.0

# Phase A5 follow-up (2026-05-19): UNDER candidate emission
# observability. The A5 ship turned on `--under-emission-mode shadow`
# which emits a sibling UNDER candidate row alongside every OVER
# FV-phase tick. This block closes the loop by surfacing the rate
# of UNDER emission + decision breakdown + price-quality summary
# the operator needs to validate the runtime.
#
# Three statuses possible:
#   - `not_emitting`: 0 `side=under` candidate rows today. The
#     operator did not pass `--under-emission-mode shadow`. No alert;
#     valid operator choice.
#   - `no_liquidity`: UNDER rows exist but 100% are
#     `gate_no_under_liquidity` skips. Mode active, but the UNDER
#     book is empty across the session. Surface but no alert
#     (could be a session-wide market issue, not actionable).
#   - `ok`: UNDER emission is producing decisions; alerts gated on
#     sample-size thresholds below.
#
# Alert thresholds (only fire when status == ok):
UNDER_COVERAGE_RATE_LOW_WARN = 0.50
UNDER_COVERAGE_MIN_N_FOR_ALERT = 50
# `shadow_under` rate above this = either genuine UNDER edge or
# UNDER gates too loose (most likely the borrowed OVER edge_threshold
# is wrong for UNDER's price dynamics). Surfaces a human-read
# prompt, not a directional alert.
UNDER_SHADOW_UNDER_RATE_HIGH_WARN = 0.50
UNDER_SHADOW_UNDER_MIN_N_FOR_ALERT_HIGH = 20
# `shadow_under` rate below this AND enough samples to be confident =
# UNDER gates likely too tight; tune UNDER-specific min_edge.
UNDER_SHADOW_UNDER_RATE_LOW_WARN = 0.02
UNDER_SHADOW_UNDER_MIN_N_FOR_ALERT_LOW = 100
# FV histogram buckets for the per-day UNDER price-distribution
# panel. Asymmetric -- UNDER FVs cluster low (high-scoring environments
# are common), so the smaller buckets carry more granularity.
UNDER_FV_BUCKETS: Tuple[Tuple[str, float, float], ...] = (
    ("0.00-0.20", 0.00, 0.20),
    ("0.20-0.40", 0.20, 0.40),
    ("0.40-0.60", 0.40, 0.60),
    ("0.60-0.80", 0.60, 0.80),
    ("0.80-1.00", 0.80, 1.00),
)

# Phase A5 follow-up #2 (2026-05-19): UNDER outcomes counterfactual.
# For each `shadow_under` candidate the engine emitted today, settle
# against the game's final_total: UNDER wins iff `final_total < line`.
# Counterfactual P&L = (stake / entry_ask) - stake if won, -stake if
# lost (same paper-mode taker formula production uses for OVER bets).
# Answers the operationally critical question "would the bot have made
# money trading UNDER?" -- the data point Phase B4 (60-session UNDER
# paper-bet validation milestone) ultimately depends on.
UNDER_OUTCOMES_DEFAULT_STAKE = 10.0
UNDER_OUTCOMES_PROFITABLE_ROI_WARN = 0.05  # +5%
UNDER_OUTCOMES_UNPROFITABLE_ROI_WARN = -0.05  # -5%
UNDER_OUTCOMES_MIN_N_FOR_ALERT = 30
# Trailing-7d aggregate thresholds (2026-05-19 follow-up). Higher
# min-n than the per-day window because the trailing aggregate has
# ~7x the reach -- we want stronger evidence before alerting on it.
UNDER_OUTCOMES_TRAILING_DAYS = 7
UNDER_OUTCOMES_TRAILING_MIN_N_FOR_ALERT = 50

# Cross-artifact consistency check (Active #16 v4, 2026-05-17).
# Every artifact stamped by lineage v2 records `input_hashes[path]`
# at build time. After build, those inputs may be updated by the
# next daily refresh -- when that happens the downstream artifact
# is "stale relative to its inputs" until it rebuilds. This block
# reads each artifact's lineage and surfaces:
#   (a) per-(artifact, input) stale verdicts (recorded hash !=
#       current file hash)
#   (b) cross-artifact divergence (two artifacts share an input
#       path but recorded different hashes -- one was built before
#       a refresh, the other after)
# Mirrored alerts surface with prefix `Cross-artifact:`. Complements
# the existing `cache_lineage_freshness_health` (which surfaces
# build-age of each artifact independently); this block surfaces
# the DEPENDENCY graph between them.
CROSS_ARTIFACT_CONSISTENCY_PATHS: Tuple[Tuple[str, str], ...] = (
    # (label, repo-relative-path-from-project-root)
    ("stage1_cache", "cache/mlb_ou_cache.json"),
    ("stage2_cache", "cache/mlb_stage2_run_env.json"),
    ("stage3_v2_weights", "cache/team_offense_v2_weights.json"),
    ("calibrator_over",
     "data/analysis_output/calibration/signal_win_calibration.json"),
    ("calibrator_under",
     "data/analysis_output/calibration/signal_win_calibration_under.json"),
    ("walk_forward_cert",
     "data/analysis_output/walk_forward_certification/"
     "walk_forward_certification.json"),
    ("ev_policy_report",
     "data/analysis_output/ev_policy/ev_policy_report.json"),
    ("loss_attribution",
     "data/analysis_output/loss_attribution/loss_attribution_report.json"),
    ("stage1_shadow_override",
     "data/analysis_output/stage1_shadow_override/"
     "stage1_shadow_override_report.json"),
    ("stage1_cell_loss_attribution",
     "data/analysis_output/stage1_cell_loss_attribution/"
     "stage1_cell_loss_attribution.json"),
)
# Don't fire on tiny biases -- if the model's aggregate bias is
# < 5pp we have a near-calibrated model and attribution is noise.
# The aggregate_health block (Active #9) handles the "is there a
# bias to attribute" question; this floor avoids double-firing on
# weeks where the model is fine.
LOSS_ATTRIBUTION_NOTES_MIN_ABS_BIAS = 0.05
# A stage with this share or more of the bias direction is reported
# in the operator's Notes. 0.50 means "owns at least half" -- a
# crisp retrain target. Below 50%, the bias is structurally
# distributed across stages and the operator should read the full
# artifact, not act on a Notes-line alert.
LOSS_ATTRIBUTION_NOTES_MIN_SHARE = 0.50
# Alert floor: only mirror recommendations whose savings clear this
# threshold (the builder's own min_delta floor is already $25, but we
# can be MORE conservative at the Notes-mirror layer so day-to-day
# noise doesn't crowd out higher-priority alerts). $40 = ~4 average
# stakes; below that, even a real signal is competing with single-bet
# noise on a low-volume day.
GATE_COUNTERFACTUAL_NOTES_MIN_DELTA_USD = 40.0
# Max top recommendations to mirror per refresh; the rest stay in the
# JSON artifact + the dedicated markdown report.
GATE_COUNTERFACTUAL_NOTES_MAX_ALERTS = 3

LOG_PATTERNS = {
    "schedule_refreshed": "Schedule refreshed",
    "polling_token_books": "Polling ",
    "wrote_tick_snapshots": "Wrote ",
    "retiring_book_polling": "Retiring book polling",
    "warnings": "WARNING",
    "errors": "ERROR",
    "fresh_book_unavailable": "fresh execution book missing",
}


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if raw:
                rows.append(json.loads(raw))
    return rows


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _fmt_money(value: Any) -> str:
    return f"${_safe_float(value):+.2f}"


def _fmt_pct(value: Any) -> str:
    if value is None or value == "":
        return "n/a"
    return f"{_safe_float(value) * 100:.1f}%"


def _line_key(value: Any) -> str:
    try:
        return f"{float(value):.1f}"
    except Exception:
        return str(value or "")


def _latest_session_date(sessions_dir: Path) -> str:
    files = sorted(sessions_dir.glob("*_session.json"))
    if not files:
        raise FileNotFoundError(f"No session files found in {sessions_dir}")
    latest = files[-1].name.replace("_session.json", "")
    if not latest:
        raise FileNotFoundError(f"Could not infer latest session date from {files[-1]}")
    return latest


def _top_counter(mapping: Dict[str, Any], limit: int = 12) -> List[Dict[str, Any]]:
    items = []
    for key, value in (mapping or {}).items():
        items.append({"key": str(key), "count": _safe_int(value)})
    items.sort(key=lambda row: row["count"], reverse=True)
    return items[:limit]


def _empty_side_totals() -> Dict[str, Any]:
    """Per-side counters used by `_summarize_bets`. Mirrors the
    top-level shape so consumers can read either."""
    return {
        "count": 0,
        "filled": 0,
        "wins": 0,
        "losses": 0,
        "profit": 0.0,
        "stake_or_cost": 0.0,
    }


def _finalize_side_totals(t: Dict[str, Any]) -> Dict[str, Any]:
    if t["filled"]:
        t["win_rate"] = t["wins"] / t["filled"]
        t["roi"] = (
            t["profit"] / t["stake_or_cost"] if t["stake_or_cost"] else None
        )
    else:
        t["win_rate"] = None
        t["roi"] = None
    return t


def _summarize_bets(bets: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    totals = {
        "count": 0,
        "filled": 0,
        "wins": 0,
        "losses": 0,
        "profit": 0.0,
        "stake_or_cost": 0.0,
        "avg_entry_ask": None,
        "avg_limit_price": None,
        "avg_fair_value": None,
        "avg_current_state_value_edge": None,
        "avg_phantom_risk_score": None,
    }
    # Phase B B3 (2026-05-16): per-side subtotals. `side` defaults to
    # "over" for legacy bets predating the field. Today all live bets
    # are Over, so `under` totals are all-zero. The structure is in
    # place so Phase C UNDER trading will populate it without further
    # plumbing.
    by_side: Dict[str, Dict[str, Any]] = {
        "over": _empty_side_totals(),
        "under": _empty_side_totals(),
    }
    entry_asks: List[float] = []
    limits: List[float] = []
    fvs: List[float] = []
    current_edges: List[float] = []
    phantom_scores: List[float] = []

    for bet in bets:
        side = str(bet.get("side") or "over").lower()
        if side not in by_side:
            # Unknown side (e.g. typo in a future row). Bucket under
            # the literal value rather than silently dropping so the
            # operator sees the anomaly. Per-side totals dict stays
            # extensible by design.
            by_side[side] = _empty_side_totals()
        side_totals = by_side[side]
        status = str(bet.get("order_status") or "")
        # Paper bets don't set order_status (only live execution does),
        # so without this fallback the wins/losses/profit counters
        # stayed at 0 for every paper session. 2026-05-21 fix: treat
        # the absence of order_status as paper-mode and count as filled
        # when the bet is settled. Live behavior unchanged.
        if status:
            is_filled = (status == "filled")
        else:
            is_filled = bool(bet.get("settled"))
        won = bet.get("won")
        profit = _safe_float(bet.get("profit"))
        fill_cost = bet.get("fill_cost_usdc", bet.get("fill_cost"))
        stake_or_cost = _safe_float(fill_cost, _safe_float(bet.get("stake")))
        side_totals["count"] += 1
        if is_filled:
            totals["filled"] += 1
            totals["stake_or_cost"] += stake_or_cost
            totals["profit"] += profit
            side_totals["filled"] += 1
            side_totals["stake_or_cost"] += stake_or_cost
            side_totals["profit"] += profit
            if won is True:
                totals["wins"] += 1
                side_totals["wins"] += 1
            elif won is False:
                totals["losses"] += 1
                side_totals["losses"] += 1

        entry_ask = _safe_float(bet.get("entry_ask"), None)  # type: ignore[arg-type]
        limit_price = _safe_float(bet.get("limit_price"), None)  # type: ignore[arg-type]
        fair_value = _safe_float(bet.get("fair_value"), None)  # type: ignore[arg-type]
        current_edge = bet.get("current_state_value_edge")
        phantom = bet.get("shadow_phantom_risk_score")
        if entry_ask is not None:
            entry_asks.append(entry_ask)
        if limit_price is not None:
            limits.append(limit_price)
        if fair_value is not None:
            fvs.append(fair_value)
        if current_edge is not None:
            current_edges.append(_safe_float(current_edge))
        if phantom is not None:
            phantom_scores.append(_safe_float(phantom))

        rows.append({
            "bet_id": bet.get("bet_id"),
            "side": side,
            "game": f"{bet.get('away_abbrev', '?')}@{bet.get('home_abbrev', '?')}",
            "line": bet.get("line"),
            "inning": bet.get("inning"),
            "status": status,
            "entry_ask": bet.get("entry_ask"),
            "limit_price": bet.get("limit_price"),
            "actual_fill_price": bet.get("actual_fill_price") or bet.get("fill_price"),
            "filled_shares": bet.get("filled_shares", bet.get("fill_size")),
            "fill_cost_usdc": fill_cost,
            "payout_usdc": bet.get("payout_usdc", bet.get("payout")),
            "fair_value": bet.get("fair_value"),
            "edge": bet.get("edge"),
            "current_state_value_edge": current_edge,
            "current_state_value_empirical_edge": bet.get("current_state_value_empirical_edge"),
            "phantom_risk_band": bet.get("shadow_phantom_risk_band"),
            "phantom_risk_score": phantom,
            "won": won,
            "profit": bet.get("profit"),
            "final_total": bet.get("final_total"),
        })

    totals["count"] = len(rows)
    if totals["filled"]:
        totals["win_rate"] = totals["wins"] / totals["filled"]
        totals["roi"] = totals["profit"] / totals["stake_or_cost"] if totals["stake_or_cost"] else None
    else:
        totals["win_rate"] = None
        totals["roi"] = None

    def _avg(values: List[float]) -> Optional[float]:
        return round(sum(values) / len(values), 6) if values else None

    totals["avg_entry_ask"] = _avg(entry_asks)
    totals["avg_limit_price"] = _avg(limits)
    totals["avg_fair_value"] = _avg(fvs)
    totals["avg_current_state_value_edge"] = _avg(current_edges)
    totals["avg_phantom_risk_score"] = _avg(phantom_scores)
    totals["by_side"] = {
        side: _finalize_side_totals(t) for side, t in by_side.items()
    }
    return rows, totals


# Note: Health check functions have been refactored into the scripts/analysis/human_review package.


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


def _markdown_table(headers: List[str], rows: List[List[Any]]) -> List[str]:
    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        out.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return out


def render_markdown(report: Dict[str, Any]) -> str:
    summary = report.get("session_summary") or {}
    bet_totals = report.get("bet_totals") or {}
    compact = report.get("candidate_rollup_compact") or {}
    log_counts = (report.get("log_health") or {}).get("counts") or {}

    lines: List[str] = [
        f"# MLB Polymarket Human Review - {report.get('session_date')}",
        "",
        "## Session",
        f"- Mode: {report.get('mode')}",
        f"- Bets: {_safe_int(summary.get('orders_placed'))} placed / {_safe_int(summary.get('orders_filled'))} filled",
        f"- Result: {_safe_int(summary.get('wins'))}-{_safe_int(summary.get('losses'))}, profit {_fmt_money(summary.get('total_profit'))}, ROI {_fmt_pct(summary.get('roi'))}",
        f"- Avg ask/limit/FV: {bet_totals.get('avg_entry_ask')} / {bet_totals.get('avg_limit_price')} / {bet_totals.get('avg_fair_value')}",
        "",
        "## Bets",
    ]

    bet_rows = []
    for bet in report.get("bets") or []:
        result = "W" if bet.get("won") is True else ("L" if bet.get("won") is False else "?")
        bet_rows.append([
            bet.get("game"),
            f"O{bet.get('line')}",
            bet.get("entry_ask"),
            bet.get("limit_price"),
            bet.get("actual_fill_price"),
            bet.get("fair_value"),
            bet.get("current_state_value_edge"),
            bet.get("phantom_risk_band"),
            result,
            _fmt_money(bet.get("profit")),
        ])
    lines.extend(_markdown_table(
        ["Game", "Line", "Ask", "Limit", "Fill", "FV", "Current Edge", "Phantom", "Result", "P&L"],
        bet_rows or [["none", "", "", "", "", "", "", "", "", ""]],
    ))

    lines.extend([
        "",
        "## Candidate Rollup",
        f"- Attempted rows: {_safe_int(compact.get('attempted_rows'))}",
        f"- Written rows: {_safe_int(compact.get('written_rows'))}",
        f"- Dedup suppressed: {_safe_int(compact.get('dedup_suppressed_rows'))}",
        f"- Write errors: {_safe_int(compact.get('write_error_rows'))}",
        "",
        "Top decision reasons:",
    ])
    for row in compact.get("top_decision_reasons") or []:
        lines.append(f"- {row['key']}: {row['count']}")

    lines.extend([
        "",
        "## Shadow Diagnostics",
        f"- EV shadow allow/block: {_safe_int(summary.get('ev_policy_shadow_allow'))}/{_safe_int(summary.get('ev_policy_shadow_block'))}",
        f"- Prob calibration shadow scored: {_safe_int(summary.get('prob_calibration_shadow_scored'))}",
        f"- No-score drift candidates: {_safe_int((compact.get('by_decision') or {}).get('shadow_no_score_drift'))}",
    ])
    feature_diag = report.get("shadow_feature_diagnostics") or {}
    feature_regimes = feature_diag.get("regimes") or {}
    for key in ("low_ask_high_edge", "runs_needed_exact_3p5", "home_skip_bottom9_risk"):
        row = feature_regimes.get(key) or {}
        if row:
            lines.append(
                f"- {key}: {_safe_int(row.get('placed'))} placed / "
                f"{_safe_int(row.get('filled'))} filled, P&L {_fmt_money(row.get('filled_profit'))}, "
                f"ROI {_fmt_pct(row.get('filled_roi'))}"
            )

    stage2_audit = report.get("stage2_suppression_dollar_audit") or {}
    lines.extend([
        "",
        "## Blocked Gate Dollar Audits",
        "- Stage-2 suppression: "
        f"{_safe_int(stage2_audit.get('labeled_rows'))} labeled blocked rows, "
        f"{_safe_int(stage2_audit.get('blocked_winning_rows'))} eventual winners / "
        f"{_safe_int(stage2_audit.get('blocked_losing_rows'))} eventual losers, "
        f"net hypothetical {_fmt_money(stage2_audit.get('net_hypothetical_profit_usdc'))}.",
    ])

    lines.extend([
        "",
        "## Log Health",
        f"- Schedule refresh lines: {_safe_int(log_counts.get('schedule_refreshed'))}",
        f"- Polling token-book lines: {_safe_int(log_counts.get('polling_token_books'))}",
        f"- Tick snapshot write lines: {_safe_int(log_counts.get('wrote_tick_snapshots'))}",
        f"- Warnings/errors: {_safe_int(log_counts.get('warnings'))}/{_safe_int(log_counts.get('errors'))}",
    ])

    cal = report.get("calibration_health") or {}
    cal_alerts = cal.get("alerts") or []
    artifact_methods = cal.get("artifact_methods_by_family") or {}
    sampled = cal.get("sampled_metrics_by_family") or {}
    method_changes = cal.get("method_changes_since_prior") or {}
    age = cal.get("artifact_age_days")
    lines.extend([
        "",
        "## Calibration Health",
        f"- Artifact present: {bool(cal.get('artifact_present'))}"
        + (f", age {age:.1f} days" if isinstance(age, (int, float)) else "")
        + f", default_family={cal.get('artifact_default_family')}",
        f"- Alerts: {len(cal_alerts)}",
    ])
    if artifact_methods:
        lines.append("- Artifact methods by family:")
        for family, method in sorted(artifact_methods.items()):
            lines.append(f"  - {family}: {method}")
    if sampled:
        rows = []
        for family, metrics in sorted(sampled.items()):
            rows.append([
                family,
                metrics.get("rows_with_both_probs", 0),
                metrics.get("mean_abs_delta"),
                metrics.get("max_abs_delta"),
                metrics.get("applied_share"),
            ])
        lines.append("- Sampled candidate-row deltas:")
        lines.extend(_markdown_table(
            ["Family", "N", "Mean |cal-raw|", "Max |cal-raw|", "Applied Share"],
            rows,
        ))
    if method_changes:
        lines.append("- Method changes vs prior daily review:")
        for family, change in method_changes.items():
            lines.append(f"  - {family}: {change.get('from')} -> {change.get('to')}")
    if cal_alerts:
        lines.append("- Active alerts:")
        for alert in cal_alerts:
            lines.append(f"  - {alert}")

    ce = report.get("calibrator_enforce_shipment_health") or {}
    ce_today = ce.get("today") or {}
    ce_effect = ce_today.get("enforce_effect") or {}
    ce_cal_metrics = ce_today.get("calibrator_metrics") or {}
    ce_baseline = ce.get("trailing_baseline") or {}
    ce_alerts = ce.get("alerts") or []
    lines.extend([
        "",
        "## Calibrator-Enforce Shipment (2026-05-19 patch)",
        f"- Decision mode: {ce.get('session_mode_at_decision_time')} "
        f"({ce.get('read_mode')}); status: {ce.get('status')}",
        f"- Candidates today: {ce_today.get('total_candidates_evaluated', 0)} "
        f"(trade={ce_today.get('trade_decisions', 0)}, "
        f"skip:gate_min_edge={ce_today.get('skip_due_to_gate_min_edge', 0)})",
        f"- In-band-gated (raw_fv>={ce.get('thresholds', {}).get('band_gate_threshold', 0.9):.2f}): "
        f"{ce_cal_metrics.get('in_band_gate_range_count', 0)}; "
        f"mean |cal-raw| in-band: "
        f"{_fmt_pct(ce_cal_metrics.get('mean_abs_delta_in_band'))}",
        f"- {ce_effect.get('attribution_label', 'effect')}: "
        f"{ce_effect.get('blocked_count', 0)}/"
        f"{ce_effect.get('candidate_pool_size', 0)} "
        f"({_fmt_pct(ce_effect.get('blocked_rate'))}) | by raw_fv: "
        f">=0.95={(ce_effect.get('blocked_by_raw_fv_bucket') or {}).get('>=0.95', 0)}, "
        f"0.90-0.95={(ce_effect.get('blocked_by_raw_fv_bucket') or {}).get('0.90-0.95', 0)}",
        (
            lambda bo, cf: (
                f"- Blocked outcomes: {bo.get('would_have_won', 0)}W / "
                f"{bo.get('would_have_lost', 0)}L of "
                f"{bo.get('settled_count', 0)} settled "
                f"({bo.get('undecided_count', 0)} undecided); "
                f"WR={_fmt_pct(bo.get('win_rate_among_settled'))}; "
                f"counterfactual save=${cf.get('saved_dollars', 0.0):+.2f} "
                f"@ default-stake ${cf.get('default_stake', 0.0):.0f} "
                f"[outcomes: {bo.get('outcomes_source_status', '?')}]"
            )
        )(
            ce_effect.get("blocked_outcomes") or {},
            (ce_effect.get("blocked_outcomes") or {}).get("counterfactual_pnl") or {},
        ),
        f"- Trailing baseline: "
        f"today {ce_baseline.get('today_trades', 0)} trades vs "
        f"{ce_baseline.get('baseline_days_used', 0)}d mean "
        f"{ce_baseline.get('mean_daily_trades') or 'n/a'} "
        f"(ratio: {_fmt_pct(ce_baseline.get('today_volume_ratio_vs_baseline'))})",
    ])
    if ce_alerts:
        lines.append("- Active alerts:")
        for alert in ce_alerts:
            lines.append(f"  - {alert}")

    fill = report.get("fill_rate_health") or {}
    sig = report.get("signal_quality_health") or {}
    fill_today = fill.get("today") or {}
    fill_base = fill.get("baseline") or {}
    sig_today = sig.get("today") or {}
    sig_base = sig.get("baseline") or {}
    lines.extend([
        "",
        "## Drift Health",
        f"- Fill rate today: {fill_today.get('filled', 0)}/{fill_today.get('placed', 0)} "
        f"({_fmt_pct(fill_today.get('fill_rate'))}); trailing "
        f"{fill_base.get('days_in_baseline', 0)}d baseline: "
        f"{fill_base.get('filled', 0)}/{fill_base.get('placed', 0)} "
        f"({_fmt_pct(fill_base.get('fill_rate'))}).",
        f"- Filled win rate today: {sig_today.get('wins', 0)}/{sig_today.get('filled', 0)} "
        f"({_fmt_pct(sig_today.get('win_rate'))}); trailing "
        f"{sig_base.get('days_in_baseline', 0)}d baseline: "
        f"{sig_base.get('wins', 0)}/{sig_base.get('filled', 0)} "
        f"({_fmt_pct(sig_base.get('win_rate'))}).",
    ])
    regime = report.get("regime_mix_health") or {}
    tvds = regime.get("tvd_by_dimension") or {}
    if tvds:
        def _fmt_tvd(val: Any) -> str:
            return "n/a" if val is None else f"{float(val):.2f}"
        lines.append(
            f"- Regime-mix TVD vs trailing {regime.get('days_in_baseline', 0)}d "
            f"({regime.get('today_total_bets', 0)} bets today, "
            f"{regime.get('baseline_total_bets', 0)} baseline): "
            + ", ".join(
                f"{dim}={_fmt_tvd(val)}"
                for dim, val in sorted(tvds.items())
            )
            + "."
        )
    drift_alerts = (
        list(fill.get("alerts") or [])
        + list(sig.get("alerts") or [])
        + list(regime.get("alerts") or [])
    )
    if drift_alerts:
        lines.append("- Active drift alerts:")
        for alert in drift_alerts:
            lines.append(f"  - {alert}")

    rec = report.get("reconciler_summary") or {}
    rec_share = rec.get("reconciled_share")
    lines.extend([
        "",
        "## Orphan-Fill Reconciler",
        f"- Filled today: {rec.get('filled_total', 0)}; "
        f"recovered by reconciler: {rec.get('reconciled_total', 0)} "
        f"({_fmt_pct(rec_share)})."
    ])
    if rec.get("by_source"):
        for source, count in sorted((rec.get("by_source") or {}).items()):
            lines.append(f"  - {source}: {count}")
    for alert in rec.get("alerts") or []:
        lines.append(f"- Alert: {alert}")

    lines.extend(["", "## Notes"])
    notes = report.get("notes") or []
    if notes:
        lines.extend(f"- {note}" for note in notes)
    else:
        lines.append("- No automatic notes.")

    return "\n".join(lines) + "\n"


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
