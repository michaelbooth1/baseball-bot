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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


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
        is_filled = status == "filled"
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


def _count_log_health(log_path: Path) -> Dict[str, Any]:
    counts = Counter()
    if not log_path.exists():
        return {"log_path": str(log_path), "exists": False, "counts": dict(counts)}

    with log_path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if "Schedule refreshed" in line:
                counts["schedule_refreshed"] += 1
            if "Polling " in line and " token books" in line:
                counts["polling_token_books"] += 1
            if "Wrote " in line and " tick snapshots" in line:
                counts["wrote_tick_snapshots"] += 1
            if "Retiring book polling" in line:
                counts["retiring_book_polling"] += 1
            if "WARNING" in line:
                counts["warnings"] += 1
            if "ERROR" in line:
                counts["errors"] += 1
            if "fresh execution book missing" in line:
                counts["fresh_book_unavailable"] += 1

    return {"log_path": str(log_path), "exists": True, "counts": dict(counts)}


def _stage2_suppression_dollar_audit(
    *,
    session_date: str,
    candidate_dir: Path,
    stake_usdc: float,
) -> Dict[str, Any]:
    candidate_path = candidate_dir / f"{session_date}_candidates.jsonl"
    outcome_path = candidate_dir / f"{session_date}_outcomes.jsonl"
    raw_candidates = _load_jsonl(candidate_path)
    outcomes = _load_jsonl(outcome_path)
    outcome_by_line = {
        (
            str(row.get("mode") or "live"),
            str(row.get("session_date") or session_date),
            _safe_int(row.get("game_pk"), -1),
            _line_key(row.get("line")),
        ): row
        for row in outcomes
    }

    stage2_rows = [
        row for row in raw_candidates
        if "stage2_suppression" in str(row.get("decision_reason") or "")
    ]
    deduped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for row in stage2_rows:
        ask = _safe_float(row.get("decision_ask"), None)  # type: ignore[arg-type]
        key = (
            str(row.get("mode") or "live"),
            str(row.get("session_date") or session_date),
            _safe_int(row.get("game_pk"), -1),
            _line_key(row.get("line")),
            row.get("inning"),
            row.get("inning_state"),
            row.get("outs"),
            row.get("away_score_before"),
            row.get("home_score_before"),
            row.get("runners_on"),
            round(ask, 2) if ask is not None else None,
            row.get("decision_reason"),
        )
        deduped.setdefault(key, row)

    labeled = 0
    wins = 0
    losses = 0
    invalid_price = 0
    missing_outcome = 0
    blocked_winning_profit = 0.0
    blocked_losing_cost = 0.0
    net_profit = 0.0
    examples: List[Dict[str, Any]] = []

    for row in deduped.values():
        ask = _safe_float(row.get("decision_ask"), None)  # type: ignore[arg-type]
        if ask is None or ask <= 0:
            invalid_price += 1
            continue
        key = (
            str(row.get("mode") or "live"),
            str(row.get("session_date") or session_date),
            _safe_int(row.get("game_pk"), -1),
            _line_key(row.get("line")),
        )
        outcome = outcome_by_line.get(key)
        if not outcome:
            missing_outcome += 1
            continue
        labeled += 1
        over_hit = bool(outcome.get("over_hit"))
        shares = stake_usdc / ask
        profit = shares - stake_usdc if over_hit else -stake_usdc
        net_profit += profit
        if over_hit:
            wins += 1
            blocked_winning_profit += profit
        else:
            losses += 1
            blocked_losing_cost += stake_usdc
        if len(examples) < 8:
            examples.append({
                "game": f"{row.get('away_abbrev', '?')}@{row.get('home_abbrev', '?')}",
                "game_pk": row.get("game_pk"),
                "line": row.get("line"),
                "inning": row.get("inning"),
                "inning_state": row.get("inning_state"),
                "ask": ask,
                "fair_value": row.get("fair_value"),
                "stage2_run_env_delta": row.get("stage2_run_env_delta"),
                "over_hit": over_hit,
                "final_total": outcome.get("final_total"),
                "hypothetical_profit_usdc": round(profit, 2),
            })

    return {
        "description": (
            "Shadow dollar audit for rows blocked by gate_stage2_suppression. "
            "Profit is hypothetical taker-at-ask with the session stake; this is diagnostic only."
        ),
        "stake_usdc": round(stake_usdc, 2),
        "candidate_path": str(candidate_path),
        "outcome_path": str(outcome_path),
        "raw_rows": len(stage2_rows),
        "deduped_rows": len(deduped),
        "labeled_rows": labeled,
        "missing_outcome_rows": missing_outcome,
        "invalid_price_rows": invalid_price,
        "blocked_winning_rows": wins,
        "blocked_losing_rows": losses,
        "blocked_winning_profit_usdc": round(blocked_winning_profit, 2),
        "blocked_losing_cost_usdc": round(blocked_losing_cost, 2),
        "net_hypothetical_profit_usdc": round(net_profit, 2),
        "examples": examples,
    }


def _wilson_upper_bound(
    successes: int, trials: int, z: float = DRIFT_WILSON_Z
) -> Optional[float]:
    """Return the Wilson-score upper bound on a binomial proportion.

    Used to gate drift alerts on statistical significance: only alert when
    *even being generous about today's rate*, it's still below the
    trailing baseline. Prevents false positives at small daily samples
    (e.g. 1/3 fills doesn't fire a fill-rate alert against 80% baseline
    because the Wilson UB at 90% confidence is ~0.71 -- still suggests
    today *could* be consistent with baseline).

    Returns None if `trials <= 0`.
    """
    if trials <= 0:
        return None
    n = float(trials)
    p_hat = float(successes) / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p_hat + z2 / (2.0 * n)
    radius_sq = (p_hat * (1.0 - p_hat) + z2 / (4.0 * n)) / n
    if radius_sq < 0:
        radius_sq = 0.0
    radius = z * (radius_sq ** 0.5)
    return min(1.0, (center + radius) / denom)


def _shift_date(date_str: str, days: int) -> Optional[str]:
    try:
        base = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None
    from datetime import timedelta
    return (base + timedelta(days=days)).strftime("%Y-%m-%d")


def _artifact_age_days(generated_at_iso: str, today: str) -> Optional[float]:
    try:
        gen = datetime.fromisoformat(str(generated_at_iso).rstrip("Z"))
    except (TypeError, ValueError):
        return None
    try:
        today_dt = datetime.strptime(today, "%Y-%m-%d")
    except ValueError:
        return None
    return round((today_dt - gen.replace(tzinfo=None)).total_seconds() / 86400.0, 2)


def _per_family_calibration_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Sample today's candidate rows for per-family calibrator behavior.

    Computes mean(|calibrated - raw|), mean values, and the share of rows
    where ``fair_value_calibration_applied`` is True. Mean abs delta near
    zero is the smoking gun for "calibrator is identity in practice" --
    independent of what the artifact metadata claims.
    """
    by_family: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        family = str(
            row.get("fair_value_calibration_family")
            or row.get("signal_model_family")
            or "unknown"
        )
        by_family.setdefault(family, []).append(row)

    out: Dict[str, Dict[str, Any]] = {}
    for family, family_rows in sorted(by_family.items()):
        deltas: List[float] = []
        raws: List[float] = []
        cals: List[float] = []
        applied_flags: List[bool] = []
        methods: Counter = Counter()
        modes: Counter = Counter()
        for row in family_rows:
            raw = row.get("fair_value_raw")
            cal = row.get("fair_value_calibrated")
            method = str(row.get("fair_value_calibration_method") or "")
            if method:
                methods[method] += 1
            mode = str(row.get("fair_value_calibration_mode") or "")
            if mode:
                modes[mode] += 1
            applied = row.get("fair_value_calibration_applied")
            if applied is not None:
                applied_flags.append(bool(applied))
            try:
                if raw is None or cal is None:
                    continue
                rf = float(raw)
                cf = float(cal)
            except (TypeError, ValueError):
                continue
            raws.append(rf)
            cals.append(cf)
            deltas.append(abs(cf - rf))

        mode_total = sum(modes.values())
        shadow_share = (
            modes.get("shadow", 0) / mode_total if mode_total > 0 else None
        )

        if not deltas:
            out[family] = {
                "rows_total": len(family_rows),
                "rows_with_both_probs": 0,
                "mean_abs_delta": None,
                "max_abs_delta": None,
                "mean_raw": None,
                "mean_calibrated": None,
                "applied_share": None,
                "method_counts": dict(methods),
                "mode_counts": dict(modes),
                "shadow_share": round(shadow_share, 4) if shadow_share is not None else None,
            }
            continue

        applied_share = (
            sum(1 for f in applied_flags if f) / len(applied_flags)
            if applied_flags
            else None
        )
        out[family] = {
            "rows_total": len(family_rows),
            "rows_with_both_probs": len(deltas),
            "mean_abs_delta": round(sum(deltas) / len(deltas), 6),
            "max_abs_delta": round(max(deltas), 6),
            "mean_raw": round(sum(raws) / len(raws), 6),
            "mean_calibrated": round(sum(cals) / len(cals), 6),
            "applied_share": round(applied_share, 4) if applied_share is not None else None,
            "method_counts": dict(methods),
            "mode_counts": dict(modes),
            "shadow_share": round(shadow_share, 4) if shadow_share is not None else None,
        }
    return out


def _load_trailing_reviews(
    *, output_root: Path, today: str, days: int, mode: Optional[str]
) -> List[Dict[str, Any]]:
    """Load up to ``days`` prior daily-review JSONs preceding ``today``.

    Filters by ``mode`` so live runs aren't compared against paper runs.
    Returns most-recent first; days without a review are silently skipped.
    """
    out: List[Dict[str, Any]] = []
    for offset in range(1, days + 1):
        prior_date = _shift_date(today, -offset)
        if not prior_date:
            continue
        path = output_root / f"{prior_date}_human_review.json"
        if not path.exists():
            continue
        try:
            payload = _load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if mode is not None and payload.get("mode") not in (None, "", mode):
            continue
        out.append(payload)
    return out


def _fill_rate_health(
    *,
    today_bet_totals: Dict[str, Any],
    trailing_reviews: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compare today's placed-to-filled ratio against trailing baseline.

    Catches execution-side regressions (CLOB SDK fills missed, network
    issues, fee-band changes that price us out of the book). The
    orphan-fill bug fixed 2026-05-11 would have surfaced here as a fill-
    rate drop the day it started.
    """
    today_placed = _safe_int(today_bet_totals.get("count"))
    today_filled = _safe_int(today_bet_totals.get("filled"))
    today_rate = (today_filled / today_placed) if today_placed else None

    base_placed = 0
    base_filled = 0
    days_in_baseline = 0
    for review in trailing_reviews:
        bt = review.get("bet_totals") or {}
        placed = _safe_int(bt.get("count"))
        filled = _safe_int(bt.get("filled"))
        if placed > 0:
            base_placed += placed
            base_filled += filled
            days_in_baseline += 1
    base_rate = (base_filled / base_placed) if base_placed > 0 else None

    alerts: List[str] = []
    if (
        today_placed >= DRIFT_MIN_TODAY_SAMPLE
        and today_rate is not None
        and base_rate is not None
        and base_placed >= DRIFT_MIN_BASELINE_SAMPLE
    ):
        delta = today_rate - base_rate
        # Both a meaningful point-estimate drop AND a Wilson-UB test that
        # says today's rate is genuinely (statistically) below baseline,
        # not just point-estimate noise at small N. Prevents false
        # positives like "1/3 = 33% fill rate, alert!" when the Wilson UB
        # is still close to baseline.
        wilson_ub = _wilson_upper_bound(today_filled, today_placed)
        is_significant = (
            wilson_ub is not None and base_rate is not None
            and wilson_ub < base_rate
        )
        if delta <= -DRIFT_FILL_RATE_DROP_PP and is_significant:
            alerts.append(
                f"fill rate dropped {abs(delta) * 100:.0f}pp: "
                f"{today_filled}/{today_placed} ({today_rate:.0%}) today vs "
                f"{base_filled}/{base_placed} ({base_rate:.0%}) over trailing "
                f"{days_in_baseline} day(s) [Wilson UB={wilson_ub:.0%} < baseline]; "
                "investigate execution path "
                "(orphan fills, CLOB SDK errors, queue position)."
            )
    if today_placed >= DRIFT_ZERO_DAY_MIN_SAMPLE and today_rate == 0.0:
        alerts.append(
            f"zero-fill day: 0/{today_placed} placed bets filled. "
            "Check live_orders_ledger.jsonl for cancel reasons and "
            "reconciled_filled rows."
        )

    return {
        "today": {
            "placed": today_placed,
            "filled": today_filled,
            "fill_rate": round(today_rate, 4) if today_rate is not None else None,
        },
        "baseline": {
            "placed": base_placed,
            "filled": base_filled,
            "fill_rate": round(base_rate, 4) if base_rate is not None else None,
            "days_in_baseline": days_in_baseline,
        },
        "thresholds": {
            "min_today_sample": DRIFT_MIN_TODAY_SAMPLE,
            "min_baseline_sample": DRIFT_MIN_BASELINE_SAMPLE,
            "max_drop_pp": DRIFT_FILL_RATE_DROP_PP,
        },
        "alerts": alerts,
    }


def _signal_quality_health(
    *,
    today_bet_totals: Dict[str, Any],
    trailing_reviews: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compare today's filled win rate against trailing baseline.

    A material drop is the load-time complement to walk-forward calibration
    drift: it surfaces when the model + gate stack is putting us into
    losing trades faster than usual, even if individual gates look healthy.
    """
    today_filled = _safe_int(today_bet_totals.get("filled"))
    today_wins = _safe_int(today_bet_totals.get("wins"))
    today_wr = (today_wins / today_filled) if today_filled else None

    base_filled = 0
    base_wins = 0
    days_in_baseline = 0
    for review in trailing_reviews:
        bt = review.get("bet_totals") or {}
        filled = _safe_int(bt.get("filled"))
        wins = _safe_int(bt.get("wins"))
        if filled > 0:
            base_filled += filled
            base_wins += wins
            days_in_baseline += 1
    base_wr = (base_wins / base_filled) if base_filled > 0 else None

    alerts: List[str] = []
    if (
        today_filled >= DRIFT_MIN_TODAY_SAMPLE
        and today_wr is not None
        and base_wr is not None
        and base_filled >= DRIFT_MIN_BASELINE_SAMPLE
    ):
        delta = today_wr - base_wr
        wilson_ub = _wilson_upper_bound(today_wins, today_filled)
        is_significant = (
            wilson_ub is not None and base_wr is not None
            and wilson_ub < base_wr
        )
        if delta <= -DRIFT_WIN_RATE_DROP_PP and is_significant:
            alerts.append(
                f"filled win rate dropped {abs(delta) * 100:.0f}pp: "
                f"{today_wins}/{today_filled} ({today_wr:.0%}) today vs "
                f"{base_wins}/{base_filled} ({base_wr:.0%}) over trailing "
                f"{days_in_baseline} day(s) [Wilson UB={wilson_ub:.0%} < baseline]; "
                "review FV signal quality "
                "and recent gate changes."
            )
    if today_filled >= DRIFT_ZERO_DAY_MIN_SAMPLE and today_wr == 0.0:
        alerts.append(
            f"zero-win day: 0/{today_filled} filled bets won. "
            "Investigate phantom-risk and current-state-edge cohorts."
        )

    return {
        "today": {
            "filled": today_filled,
            "wins": today_wins,
            "win_rate": round(today_wr, 4) if today_wr is not None else None,
        },
        "baseline": {
            "filled": base_filled,
            "wins": base_wins,
            "win_rate": round(base_wr, 4) if base_wr is not None else None,
            "days_in_baseline": days_in_baseline,
        },
        "thresholds": {
            "min_today_sample": DRIFT_MIN_TODAY_SAMPLE,
            "min_baseline_sample": DRIFT_MIN_BASELINE_SAMPLE,
            "max_drop_pp": DRIFT_WIN_RATE_DROP_PP,
        },
        "alerts": alerts,
    }


def _reconciler_summary(session_bets: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Count today's orphan-fill reconciliations + raise an alert if the
    share of filled bets that needed reconciliation is non-trivial.

    Data path: `live_reconciliation.reconcile_orphan_fills` stamps
    `reconciliation_source` (and optionally `reconciliation_trade_id`)
    onto the LiveBetRecord when it patches a missed fill. Those fields
    serialize into the session JSON's `bets[*]` list, so we can count
    them here without re-reading `live_orders_ledger.jsonl`.

    Active #2 in the README treats a reconciled share >= 10% as the
    trigger to consider promoting the public data-api to the primary
    fill source.
    """
    bets_list = list(session_bets)
    filled_total = 0
    reconciled_total = 0
    by_source: Counter = Counter()
    examples: List[Dict[str, Any]] = []
    for bet in bets_list:
        status = str(bet.get("order_status") or "")
        if status == "filled":
            filled_total += 1
        source = bet.get("reconciliation_source")
        if not source:
            continue
        reconciled_total += 1
        by_source[str(source)] += 1
        if len(examples) < 8:
            examples.append({
                "bet_id": bet.get("bet_id"),
                "game": f"{bet.get('away_abbrev', '?')}@{bet.get('home_abbrev', '?')}",
                "line": bet.get("line"),
                "reconciliation_source": source,
                "reconciliation_trade_id": bet.get("reconciliation_trade_id"),
                "fill_price": bet.get("actual_fill_price") or bet.get("fill_price"),
            })

    reconciled_share = (
        reconciled_total / filled_total if filled_total > 0 else None
    )
    alerts: List[str] = []
    if (
        reconciled_share is not None
        and reconciled_share >= RECONCILER_HIGH_SHARE
        and filled_total >= 3
    ):
        alerts.append(
            f"orphan-fill reconciler recovered {reconciled_total}/{filled_total} "
            f"({reconciled_share:.0%}) of today's fills "
            f"(>= {RECONCILER_HIGH_SHARE:.0%} threshold). "
            "If this persists, consider promoting the public data-api to the "
            "primary fill source (see Active #2)."
        )
    return {
        "filled_total": filled_total,
        "reconciled_total": reconciled_total,
        "reconciled_share": (
            round(reconciled_share, 4) if reconciled_share is not None else None
        ),
        "by_source": dict(by_source),
        "examples": examples,
        "threshold_high_share": RECONCILER_HIGH_SHARE,
        "alerts": alerts,
    }


def _drift_ask_bucket(value: Any) -> str:
    """Same ask-bucket boundaries used by build_model_maturity_report.

    Kept inline here (instead of importing) so the daily review stays a
    self-contained operator script with no analysis-side dependencies.
    """
    try:
        ask = float(value)
    except (TypeError, ValueError):
        return "missing"
    if ask < 0.55:
        return "<0.55"
    if ask < 0.65:
        return "0.55-0.65"
    if ask < 0.75:
        return "0.65-0.75"
    if ask < 0.85:
        return "0.75-0.85"
    return ">=0.85"


def _drift_current_state_edge_bucket(value: Any) -> str:
    try:
        edge = float(value)
    except (TypeError, ValueError):
        return "missing"
    if edge < 0.03:
        return "<0.03"
    if edge < 0.08:
        return "0.03-0.08"
    return ">=0.08"


def _drift_phantom_band_bucket(value: Any) -> str:
    s = str(value or "").strip().lower()
    return s or "missing"


def _bet_distributions(bet_rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Per-dimension bucket counts over today's placed bets.

    Distribution is keyed by *placed* (not filled) so a regime shift in
    what the bot is sending to the book is visible even on zero-fill
    days -- precisely when we most need to know the cohort changed.
    """
    dims: Dict[str, Counter] = {
        "ask_bucket": Counter(),
        "current_state_edge_bucket": Counter(),
        "phantom_risk_band": Counter(),
    }
    for bet in bet_rows:
        dims["ask_bucket"][_drift_ask_bucket(bet.get("entry_ask"))] += 1
        dims["current_state_edge_bucket"][
            _drift_current_state_edge_bucket(bet.get("current_state_value_edge"))
        ] += 1
        dims["phantom_risk_band"][
            _drift_phantom_band_bucket(bet.get("phantom_risk_band"))
        ] += 1
    return {dim: dict(counter) for dim, counter in dims.items()}


def _total_variation_distance(
    today: Dict[str, int], base: Dict[str, int]
) -> Optional[float]:
    today_total = sum(today.values())
    base_total = sum(base.values())
    if today_total <= 0 or base_total <= 0:
        return None
    keys = set(today) | set(base)
    diff = 0.0
    for key in keys:
        diff += abs(
            (today.get(key, 0) / today_total) - (base.get(key, 0) / base_total)
        )
    return round(diff / 2.0, 4)


def _regime_mix_health(
    *,
    today_bet_rows: List[Dict[str, Any]],
    trailing_reviews: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compare today's placed-bet distribution to the trailing baseline.

    Catches the failure mode where outcome metrics look fine in aggregate
    but the bot is suddenly trading a meaningfully different cohort
    (e.g. drifted into >=0.85 ask bucket, or stopped seeing high-current-
    state-edge candidates). Outcome alerts will catch this *eventually*
    via win rate; this catches it on day one.
    """
    today_distributions = _bet_distributions(today_bet_rows)
    today_total_bets = (
        sum(today_distributions["ask_bucket"].values())
        if today_distributions
        else 0
    )

    base_distributions: Dict[str, Counter] = {
        dim: Counter() for dim in today_distributions
    }
    days_in_baseline = 0
    for review in trailing_reviews:
        prior = (review.get("regime_mix_health") or {}).get("today_distributions") or {}
        if not prior:
            continue
        contributed = False
        for dim, counts in prior.items():
            if dim not in base_distributions or not isinstance(counts, dict):
                continue
            for bucket, raw_count in counts.items():
                try:
                    base_distributions[dim][str(bucket)] += int(raw_count)
                    contributed = True
                except (TypeError, ValueError):
                    continue
        if contributed:
            days_in_baseline += 1
    base_distributions_out = {
        dim: dict(counter) for dim, counter in base_distributions.items()
    }
    baseline_total_bets = (
        sum(base_distributions_out.get("ask_bucket", {}).values())
    )

    tvd_by_dimension: Dict[str, Optional[float]] = {}
    for dim, today_counts in today_distributions.items():
        tvd_by_dimension[dim] = _total_variation_distance(
            today_counts, base_distributions_out.get(dim, {})
        )

    alerts: List[str] = []
    if (
        today_total_bets >= DRIFT_MIN_TODAY_SAMPLE
        and baseline_total_bets >= DRIFT_MIN_BASELINE_SAMPLE
    ):
        for dim, tvd_val in tvd_by_dimension.items():
            if tvd_val is None or tvd_val < DRIFT_REGIME_MIX_TVD:
                continue
            today_dist = today_distributions[dim]
            base_dist = base_distributions_out.get(dim, {})
            today_top_bucket, today_top_count = max(
                today_dist.items(), key=lambda item: item[1]
            )
            base_top_bucket, base_top_count = (
                max(base_dist.items(), key=lambda item: item[1])
                if base_dist
                else ("?", 0)
            )
            alerts.append(
                f"{dim}: TVD={tvd_val:.2f} (>= {DRIFT_REGIME_MIX_TVD}); "
                f"today top={today_top_bucket} "
                f"({today_top_count}/{today_total_bets}), "
                f"baseline top={base_top_bucket} "
                f"({base_top_count}/{baseline_total_bets}); "
                "trading a different cohort than recent sessions."
            )

    return {
        "today_distributions": today_distributions,
        "today_total_bets": today_total_bets,
        "baseline_distributions": base_distributions_out,
        "baseline_total_bets": baseline_total_bets,
        "days_in_baseline": days_in_baseline,
        "tvd_by_dimension": tvd_by_dimension,
        "thresholds": {
            "min_today_sample": DRIFT_MIN_TODAY_SAMPLE,
            "min_baseline_sample": DRIFT_MIN_BASELINE_SAMPLE,
            "max_tvd": DRIFT_REGIME_MIX_TVD,
        },
        "alerts": alerts,
    }


# ---------------------------------------------------------------------------
# Cohort-ROI drift health (companion to regime_mix_health on outcomes).
# ---------------------------------------------------------------------------


def _cohort_edge_bucket(value: Any) -> str:
    try:
        e = float(value)
    except (TypeError, ValueError):
        return "missing"
    if e < 0.15:
        return "<0.15"
    if e < 0.18:
        return "0.15-0.18"
    if e < 0.22:
        return "0.18-0.22"
    return ">=0.22"


def _cohort_inning_bucket(value: Any) -> str:
    try:
        i = int(value)
    except (TypeError, ValueError):
        return "missing"
    if i <= 5:
        return "<=5"
    if i == 6:
        return "6"
    if i == 7:
        return "7"
    return ">=8"


def _cohort_line_bucket(value: Any) -> str:
    # Line is stored as a string like "6.5" / "10.5". Parse to float to bucket.
    try:
        ln = float(value)
    except (TypeError, ValueError):
        return "missing"
    if ln <= 7.5:
        return "<=7.5"
    if ln <= 8.5:
        return "8.5"
    if ln <= 9.5:
        return "9.5"
    return ">=10.5"


# Reuse existing bucketers for ask + current_state_edge so the cohort cuts
# stay consistent with regime_mix_health's buckets on those same dimensions.


COHORT_DIMENSIONS: Tuple[Tuple[str, Callable[[Dict[str, Any]], str]], ...] = (
    ("edge_bucket", lambda b: _cohort_edge_bucket(b.get("edge"))),
    ("ask_bucket", lambda b: _drift_ask_bucket(b.get("entry_ask"))),
    ("inning_bucket", lambda b: _cohort_inning_bucket(b.get("inning"))),
    ("line_bucket", lambda b: _cohort_line_bucket(b.get("line"))),
    (
        "current_state_edge_bucket",
        lambda b: _drift_current_state_edge_bucket(b.get("current_state_value_edge")),
    ),
)


def _collect_window_filled_bets(reviews: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pull filled bets from prior daily-review JSONs' `bets` field.

    The compact bet rows produced by `_summarize_bets` carry the fields
    we need for cohort cuts: edge, entry_ask, inning, line,
    current_state_value_edge, won, profit, fill_cost_usdc, status. Pull
    only `status == "filled"` -- cancelled/errored bets have no realized
    P&L so they'd skew cohort ROI.
    """
    out: List[Dict[str, Any]] = []
    for review in reviews:
        bets = review.get("bets") or []
        for bet in bets:
            if str(bet.get("status") or "") == "filled":
                out.append(bet)
    return out


def _aggregate_cohort(
    bets: List[Dict[str, Any]], bucket_fn: Callable[[Dict[str, Any]], str]
) -> Dict[str, Dict[str, Any]]:
    """Group filled bets by `bucket_fn` and aggregate ROI / WR per bucket."""
    grouped: Dict[str, Dict[str, Any]] = {}
    for bet in bets:
        bucket = bucket_fn(bet)
        agg = grouped.setdefault(
            bucket,
            {"n": 0, "wins": 0, "losses": 0, "profit": 0.0, "stake": 0.0},
        )
        agg["n"] += 1
        profit = _safe_float(bet.get("profit"))
        cost = _safe_float(bet.get("fill_cost_usdc"), _safe_float(bet.get("stake_or_cost"), 0.0))
        agg["profit"] += profit
        agg["stake"] += cost
        won = bet.get("won")
        if won is True:
            agg["wins"] += 1
        elif won is False:
            agg["losses"] += 1
    # Derive WR + ROI + Wilson UB per bucket.
    for bucket, agg in grouped.items():
        n = agg["n"]
        agg["wr"] = (agg["wins"] / n) if n else None
        agg["roi"] = (agg["profit"] / agg["stake"]) if agg["stake"] else None
        agg["wilson_ub_wr"] = _wilson_upper_bound(agg["wins"], agg["wins"] + agg["losses"])
        # Round monetary + rate fields for readability.
        agg["profit"] = round(agg["profit"], 2)
        agg["stake"] = round(agg["stake"], 2)
        if agg["wr"] is not None:
            agg["wr"] = round(agg["wr"], 4)
        if agg["roi"] is not None:
            agg["roi"] = round(agg["roi"], 4)
        if agg["wilson_ub_wr"] is not None:
            agg["wilson_ub_wr"] = round(agg["wilson_ub_wr"], 4)
    return grouped


def _recent_events_by_direction(
    *,
    today: str,
    log_path: Path,
    direction: str,
    success_actions: Tuple[str, ...],
    window_days: int = PROMOTION_ATTRIBUTION_WINDOW_DAYS,
) -> List[Dict[str, Any]]:
    """Return audit-log events of one direction (promote|demote) in the
    last `window_days`. Backward-compat: rows without `direction` predate
    the field and are treated as promotions (so demote queries skip them
    cleanly)."""
    if not log_path.exists():
        return []
    try:
        rows = []
        with open(log_path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rows.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    cutoff = _shift_date(today, -window_days)
    out: List[Dict[str, Any]] = []
    for r in rows:
        row_direction = str(r.get("direction") or "promote")
        if row_direction != direction:
            continue
        if str(r.get("action") or "") not in success_actions:
            continue
        ts = str(r.get("generated_at_utc") or "")
        if not ts:
            continue
        ts_date = ts[:10]
        if ts_date < cutoff or ts_date > today:
            continue
        out.append(r)
    return out


def _recent_promotions(
    *,
    today: str,
    log_path: Path,
    window_days: int = PROMOTION_ATTRIBUTION_WINDOW_DAYS,
) -> List[Dict[str, Any]]:
    """Return promotion events that completed in the last `window_days`
    days (relative to `today`). Used to attribute drift alerts to recent
    promotions ("this cohort started losing right after we promoted X").
    """
    return _recent_events_by_direction(
        today=today, log_path=log_path,
        direction="promote", success_actions=("promoted", "forced"),
        window_days=window_days,
    )


def _recent_demotions(
    *,
    today: str,
    log_path: Path,
    window_days: int = PROMOTION_ATTRIBUTION_WINDOW_DAYS,
) -> List[Dict[str, Any]]:
    """Return demotion events that completed in the last `window_days`
    days. Used to add a "follows" suffix to drift alerts so the operator
    can see whether a recent demote DID NOT fix the cohort (different
    semantic from promotion-attribution -- demote was supposed to help)."""
    return _recent_events_by_direction(
        today=today, log_path=log_path,
        direction="demote", success_actions=("demoted", "forced"),
        window_days=window_days,
    )


def _attribute_alert_to_promotions(
    alert: str,
    promotions: List[Dict[str, Any]],
    *,
    today: str,
) -> str:
    """If any recent promotion exists, append a hint to the alert text.
    The hint lists the promoted lever(s) and how many days ago. We don't
    try to causally link to a specific lever (that would need feature-
    level attribution) -- just call out the temporal coincidence so the
    operator can investigate.
    """
    if not promotions:
        return alert
    # Group by lever, take the most recent per lever for compact display.
    by_lever: Dict[str, str] = {}
    for p in promotions:
        lever = str(p.get("lever") or "?")
        ts = str(p.get("generated_at_utc") or "")
        if lever not in by_lever or ts > by_lever[lever]:
            by_lever[lever] = ts
    parts: List[str] = []
    for lever, ts in sorted(by_lever.items(), key=lambda kv: kv[1], reverse=True):
        ts_date = ts[:10]
        try:
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            ts_dt = datetime.strptime(ts_date, "%Y-%m-%d")
            days_ago = (today_dt - ts_dt).days
            parts.append(f"{lever} promotion {days_ago}d ago")
        except ValueError:
            parts.append(f"{lever} promotion at {ts_date}")
    return alert + f"  [coincides with: {', '.join(parts)}]"


def _attribute_alert_to_demotions(
    alert: str,
    demotions: List[Dict[str, Any]],
    *,
    today: str,
) -> str:
    """If any recent demotion exists, append a "[follows: ...]" suffix.
    Different verb from the promotion suffix ("coincides with") on
    purpose: a demote was supposed to *help* the cohort, so an alert
    that fires after a demote means the demote either didn't help or
    the cohort has a different root cause. The operator should treat
    the follow-up signal differently than a fresh coincidence."""
    if not demotions:
        return alert
    # Group by lever, take the most recent per lever for compact display.
    by_lever: Dict[str, str] = {}
    for d in demotions:
        lever = str(d.get("lever") or "?")
        ts = str(d.get("generated_at_utc") or "")
        if lever not in by_lever or ts > by_lever[lever]:
            by_lever[lever] = ts
    parts: List[str] = []
    for lever, ts in sorted(by_lever.items(), key=lambda kv: kv[1], reverse=True):
        ts_date = ts[:10]
        try:
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            ts_dt = datetime.strptime(ts_date, "%Y-%m-%d")
            days_ago = (today_dt - ts_dt).days
            parts.append(f"{lever} demotion {days_ago}d ago")
        except ValueError:
            parts.append(f"{lever} demotion at {ts_date}")
    return alert + f"  [follows: {', '.join(parts)}]"


# Top-N drift features to mention in the cohort-ROI attribution suffix.
# 5 is the limit at which the suffix stays readable on the markdown
# table rows; further features are summarised as "(+N more)".
CONCEPT_DRIFT_ATTRIBUTION_TOP_N = 5


def _major_drift_features(
    concept_drift_health: Optional[Dict[str, Any]],
) -> List[Tuple[str, str, float]]:
    """Extract features with verdict='major' (PSI >= 0.25 / TVD past its
    own threshold) from the concept-drift health block. Returns a list of
    (feature_name, metric_label, value) tuples sorted by value desc, so
    the most-shifted feature appears first in the attribution suffix.
    """
    if not concept_drift_health:
        return []
    feature_verdicts = concept_drift_health.get("feature_verdicts") or {}
    out: List[Tuple[str, str, float]] = []
    for fname, info in feature_verdicts.items():
        if str(info.get("verdict") or "") != "major":
            continue
        metric = str(info.get("metric") or "PSI")
        value = info.get("value")
        if value is None:
            continue
        try:
            out.append((str(fname), metric, float(value)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda t: t[2], reverse=True)
    return out


def _attribute_alert_to_concept_drift(
    alert: str,
    drift_features: List[Tuple[str, str, float]],
    *,
    top_n: int = CONCEPT_DRIFT_ATTRIBUTION_TOP_N,
) -> str:
    """If any major-drift features exist, append a candidate-root-cause
    suffix to the alert. Format mirrors the promotion-attribution
    suffix: `[concept-drift: <feature> <metric> <value>, ...]`. We do
    not try to claim a causal link (the input shifted, the cohort lost
    money -- they MIGHT be related); the suffix just shrinks the
    operator's investigation surface from "all 7 features" to "these
    are the ones whose input distribution shifted".
    """
    if not drift_features:
        return alert
    top = drift_features[:top_n]
    parts = [f"{f} {m} {v:.2f}" for (f, m, v) in top]
    extra = len(drift_features) - len(top)
    suffix = ", ".join(parts)
    if extra > 0:
        suffix += f", (+{extra} more)"
    return alert + f"  [concept-drift: {suffix}]"


def _cohort_roi_health(
    *,
    today_bet_rows: List[Dict[str, Any]],
    trailing_reviews: List[Dict[str, Any]],
    baseline_reviews: List[Dict[str, Any]],
    session_date: Optional[str] = None,
    promotion_events_log_path: Path = DEFAULT_PROMOTION_EVENTS_LOG,
    concept_drift_health: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Track outcome cohorts (filled-bet ROI by edge/ask/inning/line/CSE
    bucket) over a trailing window, and surface alerts when a cohort is
    materially losing or has flipped sign vs a longer baseline.

    Today's filled bets join the trailing window so the report covers the
    operator's most-recent decisions. Cancelled/errored bets are excluded
    (no realized P&L). All thresholds live in COHORT_ROI_* constants.
    """
    # Recent window = today + trailing reviews (last 7 days inclusive).
    today_filled = [b for b in today_bet_rows if str(b.get("status") or "") == "filled"]
    recent_bets = today_filled + _collect_window_filled_bets(trailing_reviews)
    baseline_bets = today_filled + _collect_window_filled_bets(baseline_reviews)

    cohorts_by_dim: Dict[str, Dict[str, Dict[str, Any]]] = {}
    baseline_cohorts_by_dim: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for dim_name, bucket_fn in COHORT_DIMENSIONS:
        cohorts_by_dim[dim_name] = _aggregate_cohort(recent_bets, bucket_fn)
        baseline_cohorts_by_dim[dim_name] = _aggregate_cohort(baseline_bets, bucket_fn)

    alerts: List[str] = []

    # --- 1. Absolute-losing alert ---
    for dim_name, buckets in cohorts_by_dim.items():
        for bucket_label, agg in buckets.items():
            if bucket_label == "missing":
                continue
            if agg["n"] < COHORT_ROI_MIN_BETS_FOR_ALERT:
                continue
            roi = agg["roi"]
            if roi is None or roi > COHORT_ROI_LOSING_THRESHOLD:
                continue
            alerts.append(
                f"{dim_name}={bucket_label} cohort ROI {roi * 100:+.1f}% over "
                f"trailing {COHORT_ROI_TRAILING_WINDOW_DAYS}d "
                f"(n={agg['n']}, {agg['wins']}W/{agg['losses']}L, "
                f"profit=${agg['profit']:+.2f} on ${agg['stake']:.2f} stake); "
                f"absolute loss threshold {COHORT_ROI_LOSING_THRESHOLD * 100:+.0f}%."
            )

    # --- 2. Regime-change alert: recent vs baseline ROI diverges. ---
    for dim_name, buckets in cohorts_by_dim.items():
        baseline_buckets = baseline_cohorts_by_dim.get(dim_name, {})
        for bucket_label, recent_agg in buckets.items():
            if bucket_label == "missing":
                continue
            base_agg = baseline_buckets.get(bucket_label) or {}
            recent_roi = recent_agg.get("roi")
            base_roi = base_agg.get("roi")
            if recent_roi is None or base_roi is None:
                continue
            if recent_agg["n"] < COHORT_ROI_MIN_BETS_FOR_ALERT:
                continue
            if base_agg.get("n", 0) < COHORT_ROI_MIN_BETS_FOR_ALERT:
                continue
            # Avoid double-firing when the absolute-losing alert already fired
            # on this same cohort -- the regime alert adds no new information.
            if recent_roi <= COHORT_ROI_LOSING_THRESHOLD:
                continue
            delta = recent_roi - base_roi
            if delta > -COHORT_ROI_REGIME_DELTA:
                continue
            alerts.append(
                f"{dim_name}={bucket_label} cohort ROI flipped: "
                f"trailing {COHORT_ROI_TRAILING_WINDOW_DAYS}d {recent_roi * 100:+.1f}% "
                f"vs baseline {COHORT_ROI_BASELINE_WINDOW_DAYS}d {base_roi * 100:+.1f}% "
                f"(delta {delta * 100:+.1f}pp <= -{COHORT_ROI_REGIME_DELTA * 100:.0f}pp); "
                f"n_recent={recent_agg['n']}, n_baseline={base_agg['n']}."
            )

    # --- 3. Promotion-attribution: append "[coincides with X promotion N
    # days ago]" to every alert if any promotion happened in the last 14d.
    # Lets the operator see the temporal coincidence between drift and a
    # recent promotion without having to grep the audit log themselves.
    recent_proms: List[Dict[str, Any]] = []
    recent_demos: List[Dict[str, Any]] = []
    if session_date is not None:
        recent_proms = _recent_promotions(
            today=session_date, log_path=promotion_events_log_path,
        )
        recent_demos = _recent_demotions(
            today=session_date, log_path=promotion_events_log_path,
        )
    if recent_proms and alerts:
        alerts = [
            _attribute_alert_to_promotions(a, recent_proms, today=session_date or "")
            for a in alerts
        ]

    # --- 4. Demotion-attribution: append "[follows: X demotion N days ago]"
    # to every alert if any demotion happened in the last 14d. Symmetric
    # to promotion-attribution but with a different verb -- a demote was
    # supposed to FIX the cohort, so a continuing alert tells the
    # operator the demote either didn't help or there's a different
    # root cause. Both suffixes can appear on the same alert.
    if recent_demos and alerts:
        alerts = [
            _attribute_alert_to_demotions(a, recent_demos, today=session_date or "")
            for a in alerts
        ]

    # --- 5. Concept-drift attribution: append "[concept-drift: <feature>
    # <metric> <value>, ...]" to every alert if any feature shows
    # verdict=major (PSI >= 0.25 / TVD past its threshold). The cohort
    # lost money AND certain inputs shifted -- they MIGHT be linked.
    # We don't claim causation, just shrink the operator's investigation
    # surface from "all 7 features" to "these are the candidates".
    drift_features = _major_drift_features(concept_drift_health)
    if drift_features and alerts:
        alerts = [
            _attribute_alert_to_concept_drift(a, drift_features)
            for a in alerts
        ]

    return {
        "alerts": alerts,
        "window_days": COHORT_ROI_TRAILING_WINDOW_DAYS,
        "baseline_window_days": COHORT_ROI_BASELINE_WINDOW_DAYS,
        "n_recent_filled": len(recent_bets),
        "n_baseline_filled": len(baseline_bets),
        "cohorts_by_dimension": cohorts_by_dim,
        "baseline_cohorts_by_dimension": baseline_cohorts_by_dim,
        "recent_promotions_count": len(recent_proms),
        "recent_demotions_count": len(recent_demos),
        "concept_drift_major_features_count": len(drift_features),
        "thresholds": {
            "min_bets_for_alert": COHORT_ROI_MIN_BETS_FOR_ALERT,
            "losing_threshold": COHORT_ROI_LOSING_THRESHOLD,
            "regime_delta": COHORT_ROI_REGIME_DELTA,
        },
    }


# ---------------------------------------------------------------------------
# Active #9: cohort-calibration health (the 8th drift dimension).
# ---------------------------------------------------------------------------


def _bet_is_calibratable(bet: Dict[str, Any]) -> bool:
    """Filter for bets that contribute to a per-cohort reliability metric.

    Requires: status=='filled' (the outcome is realized), `won` is a
    Bool (True/False, not None), and `fair_value` is a finite float in
    [0,1]. Bets that fail any of these can't contribute to a Brier or
    reliability calculation.
    """
    if str(bet.get("status") or "") != "filled":
        return False
    won = bet.get("won")
    if won not in (True, False):
        return False
    fv = bet.get("fair_value")
    try:
        fv = float(fv)
    except (TypeError, ValueError):
        return False
    if not (0.0 <= fv <= 1.0):
        return False
    return True


def _aggregate_calibration(
    bets: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute Brier + reliability gap for one cohort of filled bets.

    Reliability gap = |mean(fair_value) - mean(won)|; the calibration
    error of the cohort. A perfectly-calibrated cohort has gap = 0.
    Brier = mean((fair_value - won)^2); the standard scoring rule.
    Both metrics are None for empty cohorts.
    """
    n = 0
    sum_fv = 0.0
    sum_won = 0.0
    sum_sq = 0.0
    for b in bets:
        try:
            fv = float(b.get("fair_value"))
        except (TypeError, ValueError):
            continue
        won = b.get("won")
        if won not in (True, False):
            continue
        n += 1
        sum_fv += fv
        sum_won += 1.0 if won else 0.0
        sum_sq += (fv - (1.0 if won else 0.0)) ** 2
    if n == 0:
        return {
            "n": 0,
            "mean_fair_value": None,
            "mean_won": None,
            "reliability_gap": None,
            "brier": None,
        }
    mean_fv = sum_fv / n
    mean_won = sum_won / n
    return {
        "n": n,
        "mean_fair_value": round(mean_fv, 4),
        "mean_won": round(mean_won, 4),
        "reliability_gap": round(abs(mean_fv - mean_won), 4),
        "brier": round(sum_sq / n, 4),
    }


def _cohort_calibration_health(
    *,
    today_bet_rows: List[Dict[str, Any]],
    trailing_reviews: List[Dict[str, Any]],
    session_date: Optional[str] = None,
    promotion_events_log_path: Path = DEFAULT_PROMOTION_EVENTS_LOG,
    concept_drift_health: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Per-cohort calibration drift detection (Active #9).

    Mirrors `cohort_roi_health`'s decomposition (edge / ask / inning /
    line / current-state-edge bucket) but on the CALIBRATION axis: how
    well does our calibrated fair value match realized win-rate per
    cohort? Fires when a cohort's reliability gap exceeds the aggregate
    reliability gap by >= COHORT_CALIBRATION_GAP_RATIO_ALERT AND the
    cohort has >= COHORT_CALIBRATION_MIN_N_FOR_ALERT bets.

    Why this matters: `calibration_health` (the existing block) scores
    the calibrator's choice + audit metadata aggregate-only. A model
    that is well-calibrated on average can still be systematically
    over- or under-predicting in a specific cohort (e.g.
    `inning_bucket=>=8` + `current_state_edge_bucket=<0.03`).
    Cohort-roi catches it via realized P&L, but only AFTER the cohort
    has bled real money. This block catches it via reliability
    deviation -- a leading indicator that fires before / alongside
    cohort-roi.

    Mirrors cohort_roi's promotion/demotion/concept-drift attribution
    suffixes via the same helpers so the operator gets the same alert
    enrichment shape across drift dimensions.

    Surfaces under top-level `notes` with prefix
    `Cohort-calibration:`.
    """
    today_calibratable = [
        b for b in today_bet_rows if _bet_is_calibratable(b)
    ]
    window_bets = (
        today_calibratable
        + [
            b for b in _collect_window_filled_bets(trailing_reviews)
            if _bet_is_calibratable(b)
        ]
    )

    aggregate = _aggregate_calibration(window_bets)
    cohorts_by_dim: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for dim_name, bucket_fn in COHORT_DIMENSIONS:
        per_bucket: Dict[str, List[Dict[str, Any]]] = {}
        for b in window_bets:
            bucket = bucket_fn(b)
            per_bucket.setdefault(bucket, []).append(b)
        cohorts_by_dim[dim_name] = {
            label: _aggregate_calibration(rows)
            for label, rows in per_bucket.items()
        }

    alerts: List[str] = []
    aggregate_gap = aggregate.get("reliability_gap")
    aggregate_n = aggregate.get("n", 0)

    # --- Aggregate-level alert ---
    # Fires when the WHOLE calibrator is materially off, regardless of
    # whether any cohort stands out. Critical because at current
    # ~24-fills/week volume, cohort n=30 rarely triggers; without this
    # the block would be silent during the very weeks the aggregate is
    # most wrong.
    if (
        aggregate_gap is not None
        and aggregate_gap >= COHORT_CALIBRATION_AGGREGATE_GAP_ALERT
        and aggregate_n >= COHORT_CALIBRATION_AGGREGATE_MIN_N
    ):
        mean_fv_agg = aggregate.get("mean_fair_value") or 0.0
        mean_won_agg = aggregate.get("mean_won") or 0.0
        direction = (
            "over-predicting" if mean_fv_agg > mean_won_agg
            else "under-predicting"
        )
        alerts.append(
            f"aggregate calibration reliability gap "
            f"{aggregate_gap * 100:.1f}pp over trailing "
            f"{COHORT_CALIBRATION_WINDOW_DAYS}d (n={aggregate_n}, "
            f"mean_fv {mean_fv_agg * 100:.1f}% vs mean_won "
            f"{mean_won_agg * 100:.1f}%) >= "
            f"{COHORT_CALIBRATION_AGGREGATE_GAP_ALERT * 100:.0f}pp "
            f"threshold. Model is {direction} systematically -- "
            "calibrator retrain or Stage-2/Stage-3 refresh likely "
            "warranted; cross-check concept_drift_health for input "
            "shift attribution."
        )

    # --- Per-cohort vs aggregate ratio alert ---
    # Only run ratio logic when the aggregate gap is meaningfully above
    # 0. With a perfectly-calibrated model, every cohort divides by a
    # near-zero baseline and the ratio test fires on noise.
    aggregate_gap_ok_for_ratio = (
        aggregate_gap is not None
        and aggregate_gap >= COHORT_CALIBRATION_MIN_AGGREGATE_GAP
    )

    for dim_name, buckets in cohorts_by_dim.items():
        for bucket_label, agg in buckets.items():
            if bucket_label == "missing":
                continue
            if agg.get("n", 0) < COHORT_CALIBRATION_MIN_N_FOR_ALERT:
                continue
            cohort_gap = agg.get("reliability_gap")
            if cohort_gap is None:
                continue
            if not aggregate_gap_ok_for_ratio:
                continue
            ratio = cohort_gap / aggregate_gap if aggregate_gap else 0.0
            agg["reliability_gap_ratio_vs_aggregate"] = round(ratio, 3)
            if ratio < COHORT_CALIBRATION_GAP_RATIO_ALERT:
                continue
            mean_fv = agg.get("mean_fair_value") or 0.0
            mean_won = agg.get("mean_won") or 0.0
            direction = (
                "over-predicting" if mean_fv > mean_won
                else "under-predicting"
            )
            alerts.append(
                f"{dim_name}={bucket_label} cohort reliability gap "
                f"{cohort_gap * 100:.1f}pp (n={agg['n']}, "
                f"mean_fv {mean_fv * 100:.1f}% vs mean_won "
                f"{mean_won * 100:.1f}%) >= "
                f"{COHORT_CALIBRATION_GAP_RATIO_ALERT:.1f}x aggregate "
                f"gap {aggregate_gap * 100:.1f}pp over trailing "
                f"{COHORT_CALIBRATION_WINDOW_DAYS}d. Model is "
                f"{direction} in this cohort -- candidate for a "
                "Stage-2 / Stage-3 retrain or cohort-specific "
                "calibration adjustment."
            )

    # Mirror cohort_roi_health's enrichment suffixes so the alert text
    # carries the same temporal-coincidence hints.
    recent_proms: List[Dict[str, Any]] = []
    recent_demos: List[Dict[str, Any]] = []
    if session_date is not None:
        recent_proms = _recent_promotions(
            today=session_date, log_path=promotion_events_log_path,
        )
        recent_demos = _recent_demotions(
            today=session_date, log_path=promotion_events_log_path,
        )
    if recent_proms and alerts:
        alerts = [
            _attribute_alert_to_promotions(
                a, recent_proms, today=session_date or "",
            )
            for a in alerts
        ]
    if recent_demos and alerts:
        alerts = [
            _attribute_alert_to_demotions(
                a, recent_demos, today=session_date or "",
            )
            for a in alerts
        ]
    drift_features = _major_drift_features(concept_drift_health)
    if drift_features and alerts:
        alerts = [
            _attribute_alert_to_concept_drift(a, drift_features)
            for a in alerts
        ]

    return {
        "alerts": alerts,
        "window_days": COHORT_CALIBRATION_WINDOW_DAYS,
        "n_filled_settled": len(window_bets),
        "aggregate": aggregate,
        "cohorts_by_dimension": cohorts_by_dim,
        "recent_promotions_count": len(recent_proms),
        "recent_demotions_count": len(recent_demos),
        "concept_drift_major_features_count": len(drift_features),
        "thresholds": {
            "min_n_for_alert": COHORT_CALIBRATION_MIN_N_FOR_ALERT,
            "gap_ratio_alert": COHORT_CALIBRATION_GAP_RATIO_ALERT,
            "min_aggregate_gap": COHORT_CALIBRATION_MIN_AGGREGATE_GAP,
            "aggregate_gap_alert": COHORT_CALIBRATION_AGGREGATE_GAP_ALERT,
            "aggregate_min_n": COHORT_CALIBRATION_AGGREGATE_MIN_N,
        },
    }


# ---------------------------------------------------------------------------
# Concept-drift health (leading-indicator drift on model inputs).
# ---------------------------------------------------------------------------


def _concept_drift_health(
    *,
    report_path: Path,
    session_date: str,
) -> Dict[str, Any]:
    """Read the concept-drift report and produce a compact summary block.

    The PSI/TVD math lives in `build_concept_drift_report.py`; we just
    surface its alerts and a per-feature verdict table here. Keeps the
    daily review fast (no 30-day-window aggregation work) and lets the
    artifact stay independently useful as a research output.
    """
    payload: Dict[str, Any] = {
        "artifact_path": str(report_path),
        "artifact_present": report_path.exists(),
        "alerts": [],
    }
    if not report_path.exists():
        payload["artifact_error"] = "concept-drift report missing; check refresh step ran"
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    payload["report_active_date"] = report.get("active_date")
    payload["current_window"] = report.get("current_window")
    payload["baseline_window"] = report.get("baseline_window")
    payload["thresholds"] = report.get("thresholds")

    age = _artifact_age_days(report.get("generated_at_utc", ""), session_date)
    payload["artifact_age_days"] = age
    if age is not None and age > CONCEPT_DRIFT_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"concept-drift report is {age:.1f}d old "
            f"(> {CONCEPT_DRIFT_STALE_AGE_DAYS}d threshold); "
            "rerun build_concept_drift_report or daily refresh."
        )

    # Compact per-feature verdict table for the JSON; no row-level data.
    feature_verdicts: Dict[str, Dict[str, Any]] = {}
    for fname, info in (report.get("features") or {}).items():
        feature_verdicts[fname] = {
            "kind": info.get("kind"),
            "metric": info.get("metric"),
            "value": info.get("value"),
            "verdict": info.get("verdict"),
            "current_n": info.get("current_n"),
            "baseline_n": info.get("baseline_n"),
        }
    payload["feature_verdicts"] = feature_verdicts

    # Mirror the report's alerts (already filtered to "major") into our
    # alerts list. The Notes-block roll-up downstream prefixes them with
    # "Concept-drift:" so they're visible in the markdown.
    for alert_text in report.get("alerts") or []:
        payload["alerts"].append(str(alert_text))

    return payload


def _drift_in_drift_health(
    *,
    report_path: Path,
    session_date: str,
) -> Dict[str, Any]:
    """Read the drift-in-drift report and produce a compact summary block.

    Mirror of `_concept_drift_health` but on the meta-trend artifact.
    The slope-fit math lives in `build_drift_in_drift_report.py`; we
    just surface its alerts and a per-feature verdict table here. This
    is the 7th drift dimension (the 2nd LEADING indicator, after
    concept_drift_health) -- catches features whose daily PSI never
    crosses 0.25 but whose linear trend projects past it.
    """
    payload: Dict[str, Any] = {
        "artifact_path": str(report_path),
        "artifact_present": report_path.exists(),
        "alerts": [],
    }
    if not report_path.exists():
        payload["artifact_error"] = "drift-in-drift report missing; check refresh step ran"
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    payload["report_active_date"] = report.get("active_date")
    payload["history_window_days"] = report.get("history_window_days")
    payload["projection_horizon_days"] = report.get("projection_horizon_days")
    payload["min_points_for_trend"] = report.get("min_points_for_trend")
    payload["n_history_rows_in_window"] = report.get("n_history_rows_in_window")
    payload["n_features_evaluated"] = report.get("n_features_evaluated")
    payload["thresholds"] = report.get("thresholds")

    age = _artifact_age_days(report.get("generated_at_utc", ""), session_date)
    payload["artifact_age_days"] = age
    if age is not None and age > DRIFT_IN_DRIFT_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"drift-in-drift report is {age:.1f}d old "
            f"(> {DRIFT_IN_DRIFT_STALE_AGE_DAYS}d threshold); "
            "rerun build_drift_in_drift_report or daily refresh."
        )

    # Compact per-feature trend table for the JSON; no row-level data.
    feature_verdicts: Dict[str, Dict[str, Any]] = {}
    for fname, info in (report.get("features") or {}).items():
        feature_verdicts[fname] = {
            "n_points": info.get("n_points"),
            "current_psi": info.get("current_psi"),
            "slope_per_day": info.get("slope_per_day"),
            "r_squared": info.get("r_squared"),
            "projected_psi": info.get("projected_psi"),
            "verdict": info.get("verdict"),
        }
    payload["feature_verdicts"] = feature_verdicts

    # Mirror the report's alerts (already filtered to "major") into our
    # alerts list. The Notes-block roll-up downstream prefixes them with
    # "Drift-in-drift:" so they're visible in the markdown.
    for alert_text in report.get("alerts") or []:
        payload["alerts"].append(str(alert_text))

    return payload


# Levers the daemon can auto-actuate (stage2/stage3-v2 file-swap +
# stake-scaling overrides-file). gate-threshold remains preview-only;
# we still check its staleness for completeness because the operator
# is supposed to act manually.
_DAEMON_LEVER_AUDIT_NAMES: Dict[str, str] = {
    "stage2": "stage2",
    "stage3-v2": "stage3_v2",
    "stake-scaling": "stake_scaling",
    "gate-threshold": "gate_threshold",
}
_DAEMON_STALENESS_SUCCESS_ACTIONS: Tuple[str, ...] = (
    "promoted", "forced", "demoted",
)


def _last_audit_event_for_lever(
    audit_rows: List[Dict[str, Any]], lever_underscore: str,
) -> Optional[Dict[str, Any]]:
    """Return the most recent successful action event for `lever_underscore`
    (audit-log spelling, e.g. 'stage3_v2'). Filters to action labels in
    `_DAEMON_STALENESS_SUCCESS_ACTIONS` -- 'blocked' / 'dry_run' don't
    count because nothing actually changed."""
    candidates = [
        r for r in audit_rows
        if str(r.get("lever") or "") == lever_underscore
        and str(r.get("action") or "") in _DAEMON_STALENESS_SUCCESS_ACTIONS
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: str(r.get("generated_at_utc") or ""))


def _load_audit_rows(log_path: Path) -> List[Dict[str, Any]]:
    """Read promote_events.jsonl. Missing/malformed lines are skipped --
    same forgiving semantics as `_recent_promotions`."""
    if not log_path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rows.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def _today_verdict_for_lever(
    retrospective_report: Dict[str, Any], lever_name: str,
) -> Optional[str]:
    """Pick today's verdict label for one lever from the retrospective
    report. For replay levers (stage2/stage3-v2) read the latest
    per-date entry's `daemon_verdict_label`; for snapshot levers
    (stake-scaling/gate-threshold) read the snapshot block's
    `verdict_label`. Returns None when nothing is recorded for the
    lever."""
    replays = retrospective_report.get("replays") or {}
    if lever_name in replays:
        per_date = replays[lever_name].get("per_date") or []
        if not per_date:
            return None
        # per_date is chronological; last entry is today's snapshot.
        return per_date[-1].get("daemon_verdict_label")
    snapshots = retrospective_report.get("snapshots") or {}
    if lever_name in snapshots:
        return snapshots[lever_name].get("verdict_label")
    return None


def _daemon_staleness_check(
    *,
    retrospective_report: Dict[str, Any],
    audit_log_path: Path,
    today: str,
    threshold_days: int = DAEMON_STALENESS_THRESHOLD_DAYS,
) -> List[Dict[str, Any]]:
    """For each lever where today's verdict says 'promote' or 'demote',
    check the audit log for a recent successful action. If the last
    action was > `threshold_days` ago (or never), emit a staleness
    record. Returns a list of dicts (one per stale lever).

    Records are suitable for both the JSON block (caller stuffs into
    `staleness_records`) and the alerts list (caller renders one
    alert per record).
    """
    out: List[Dict[str, Any]] = []
    if not today:
        return out
    audit_rows = _load_audit_rows(audit_log_path)
    cutoff = _shift_date(today, -threshold_days)
    for lever_name, audit_name in _DAEMON_LEVER_AUDIT_NAMES.items():
        verdict_label = _today_verdict_for_lever(retrospective_report, lever_name)
        if verdict_label not in ("promote", "demote"):
            continue
        last = _last_audit_event_for_lever(audit_rows, audit_name)
        last_ts = (last or {}).get("generated_at_utc") or ""
        last_date = last_ts[:10] if last_ts else ""
        if last_date and last_date >= cutoff:
            # Action within threshold -- not stale.
            continue
        try:
            today_dt = datetime.strptime(today, "%Y-%m-%d")
        except ValueError:
            continue
        if last_date:
            try:
                last_dt = datetime.strptime(last_date, "%Y-%m-%d")
                days_since = (today_dt - last_dt).days
            except ValueError:
                days_since = None
        else:
            days_since = None
        out.append({
            "lever": lever_name,
            "verdict_label": verdict_label,
            "last_action_date": last_date or None,
            "last_action_operator": (last or {}).get("operator"),
            "last_action_label": (last or {}).get("action"),
            "days_since_last_action": days_since,
            "threshold_days": threshold_days,
        })
    return out


def _settlement_truth_health(
    *,
    report_path: Path,
    session_date: str,
) -> Dict[str, Any]:
    """Surface settlement-truth verification metrics in the daily review.

    Active #12 (2026-05-17). Reads the artifact written by
    `verify_settlement_truth.py` (which cross-checks every settled
    bet against the MLB Stats API live-feed JSON) and surfaces the
    headline counts + a tiered alert ladder:

      - Critical (resolution_mismatch >= 1): ROI math is corrupted
        for at least one bet. Operator must investigate the offending
        row in the artifact's by_result_code.resolution_mismatch.
      - Moderate (stale_filled >= STALE_FILLED_ALERT_THRESHOLD):
        settlement event never reached a bet record. Inventory
        tracker (Phase C C2) will see this as "open" forever.
      - Moderate (missing_mlb_data share >= 10%): a chunk of game
        JSONs were never scraped or were deleted. Settlement-truth
        is degraded; verifier results aren't trustworthy yet.
      - Note-level (oldest_stale_filled > 7d): the
        ledger-cleanup-needed signal for Phase C v2 inventory.

    Surfaces under top-level `notes` with prefix "Settlement-truth:".
    """
    payload: Dict[str, Any] = {
        "artifact_path": str(report_path),
        "artifact_present": report_path.exists(),
        "alerts": [],
    }
    if not report_path.exists():
        payload["artifact_error"] = (
            "settlement_truth_report missing; check refresh step ran"
        )
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    age = _artifact_age_days(report.get("generated_at_utc", ""), session_date)
    payload["artifact_age_days"] = age
    if age is not None and age > SETTLEMENT_TRUTH_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"settlement_truth_report is {age:.1f}d old "
            f"(> {SETTLEMENT_TRUTH_STALE_AGE_DAYS}d threshold); "
            "rerun verify_settlement_truth or daily refresh."
        )

    counts = report.get("counts") or {}
    thresholds = report.get("thresholds") or {}
    n_filled = counts.get("filled_or_settled_total", 0)
    n_mismatch = counts.get("resolution_mismatch", 0)
    n_total_mismatch = counts.get("total_mismatch", 0)
    n_stale_filled = counts.get("stale_filled", 0)
    n_missing_mlb = counts.get("missing_mlb_data", 0)
    n_game_not_final = counts.get("game_not_final_yet", 0)
    payload["counts"] = {
        "filled_or_settled_total": n_filled,
        "ok": counts.get("ok", 0),
        "resolution_mismatch": n_mismatch,
        "total_mismatch": n_total_mismatch,
        "stale_filled": n_stale_filled,
        "game_not_final_yet": n_game_not_final,
        "missing_mlb_data": n_missing_mlb,
        "not_yet_settled": counts.get("not_yet_settled", 0),
    }
    payload["ok_share"] = report.get("ok_share")
    payload["missing_mlb_data_share"] = report.get("missing_mlb_data_share")
    payload["oldest_stale_filled_age_days"] = report.get(
        "oldest_stale_filled_age_days"
    )

    # ---- alert ladder ----
    if n_mismatch > 0:
        # Critical: a single mismatch is a data-integrity failure.
        payload["alerts"].append(
            f"{n_mismatch} resolution_mismatch row(s) -- engine_won "
            "disagrees with MLB final total. ROI math may be "
            "corrupted; inspect settlement_truth_report.md."
        )
    if n_total_mismatch > 0:
        # Less severe: won field is right but engine_total wrong.
        payload["alerts"].append(
            f"{n_total_mismatch} total_mismatch row(s) -- engine "
            "recorded a different final_total than MLB. ROI math is "
            "preserved (same side of line), but total field is wrong."
        )
    if n_stale_filled >= thresholds.get(
        "stale_filled_alert", STALE_FILLED_ALERT_THRESHOLD,
    ):
        oldest = report.get("oldest_stale_filled_age_days")
        suffix = (
            f" (oldest {oldest}d)" if oldest is not None else ""
        )
        payload["alerts"].append(
            f"{n_stale_filled} stale_filled bet(s){suffix} -- "
            "order_status=filled but no won/loss recorded despite "
            "MLB final. Phase C v2 inventory will treat these as "
            "open forever; clean up before live UNDER actuation."
        )
    if n_game_not_final > 0:
        payload["alerts"].append(
            f"{n_game_not_final} game_not_final_yet row(s) -- bet "
            "was settled before MLB JSON showed game-final. Likely "
            "a scraper-timing issue; investigate if persistent."
        )
    if n_filled > 0:
        missing_share = n_missing_mlb / n_filled
        thresh = thresholds.get(
            "missing_mlb_data_rate_alert",
            MISSING_MLB_DATA_RATE_ALERT_THRESHOLD,
        )
        if missing_share >= thresh:
            payload["alerts"].append(
                f"missing_mlb_data share {missing_share:.1%} >= "
                f"{thresh:.0%} -- local game JSONs are missing for a "
                "large chunk of settled bets. Verifier results are "
                "degraded until the game scraper backfills."
            )

    return payload


def _under_book_coverage_health(
    *,
    report_path: Path,
    session_date: str,
) -> Dict[str, Any]:
    """Surface the under-side book pairing coverage from model_maturity.

    Phase A1 foundation visibility (2026-05-16). The monitor already
    polls both Over and Under token books; signal_engine attaches the
    under-side fields when the under tick arrives in the same poll
    cycle. This block reads `model_maturity_report.json`'s
    `coverage_checks.overall.under_pair_available_rate` and surfaces
    it in the daily review, so operators can see the coverage level
    without opening the maturity report. Fires alerts when below the
    warn threshold (default 0.50) or when the maturity report itself
    is stale.

    Surfaces under top-level `notes` with prefix "Under-book-coverage:".
    """
    payload: Dict[str, Any] = {
        "artifact_path": str(report_path),
        "artifact_present": report_path.exists(),
        "alerts": [],
    }
    if not report_path.exists():
        payload["artifact_error"] = (
            "model_maturity_report missing; check refresh step ran"
        )
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    age = _artifact_age_days(report.get("generated_at_utc", ""), session_date)
    payload["artifact_age_days"] = age
    if age is not None and age > UNDER_BOOK_COVERAGE_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"model_maturity_report is {age:.1f}d old "
            f"(> {UNDER_BOOK_COVERAGE_STALE_AGE_DAYS}d threshold); "
            "rerun model_maturity_report or daily refresh."
        )

    def _opt_float(value: Any) -> Optional[float]:
        # Preserve None: distinguishes "measurement was 0%" from "no
        # measurement." `_safe_float` defaults to 0.0 which would
        # silently turn missing data into a false zero-coverage alert.
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    coverage_checks = report.get("coverage_checks") or {}
    overall = coverage_checks.get("overall") or {}
    rate = _opt_float(overall.get("under_pair_available_rate"))
    rows = overall.get("rows")
    payload["overall"] = {
        "rows": rows,
        "under_pair_available_rate": rate,
        "under_pair_available_rows": overall.get("under_pair_available_rows"),
        "under_pair_book_rate": _opt_float(overall.get("under_pair_book_rate")),
        "no_vig_market_rate": _opt_float(overall.get("no_vig_market_rate")),
    }
    payload["warn_threshold"] = UNDER_BOOK_COVERAGE_WARN_THRESHOLD

    # Per-family rates (just rates; full breakdown stays in maturity report).
    by_family: Dict[str, Dict[str, Any]] = {}
    for family, family_payload in (coverage_checks.get("by_family") or {}).items():
        if not isinstance(family_payload, dict):
            continue
        by_family[str(family)] = {
            "rows": family_payload.get("rows"),
            "under_pair_available_rate": _opt_float(
                family_payload.get("under_pair_available_rate")
            ),
        }
    payload["by_family"] = by_family

    if rate is not None and rate < UNDER_BOOK_COVERAGE_WARN_THRESHOLD:
        payload["alerts"].append(
            f"under_pair_available_rate {rate:.2f} below warn floor "
            f"{UNDER_BOOK_COVERAGE_WARN_THRESHOLD:.2f}; UNDER offline "
            f"analysis still works (None imputed), but Phase C live "
            f"UNDER quoting needs higher pairing rate. Investigate "
            f"tick-timing variance in monitor_mlb_polymarket_ou.py."
        )
    return payload


def _fast_demote_health(
    *,
    audit_log_path: Path,
    sessions_dir: Path,
    today: str,
) -> Dict[str, Any]:
    """Active #13 (2026-05-17): surface fast Wilson-UB demote verdicts.

    Computes the four per-lever fast-demote verdicts at refresh time
    by calling into promote.py's helpers directly. Any `fast_demote`
    fires a critical alert in the daily review -- the daemon should
    have already auto-acted on it (when --auto-daemon-mode act), but
    operators in preview mode see the recommendation here.

    Surfaces under top-level `notes` with prefix `Fast-demote:`.
    """
    payload: Dict[str, Any] = {
        "audit_log_path": str(audit_log_path),
        "sessions_dir": str(sessions_dir),
        "today": today,
        "alerts": [],
        "verdicts": {},
    }
    # Lazy import to keep build_daily_human_review_report's import
    # graph small for callers that only want the lighter blocks.
    # Try the package import first (test contexts that add the
    # project root to sys.path); fall back to the bare module name
    # (script context where the analysis folder is on sys.path).
    _promote = None
    try:
        from scripts.analysis import promote as _promote
    except ImportError:
        try:
            import promote as _promote  # type: ignore[no-redef]
        except ImportError:
            payload["alerts"].append(
                "promote module unavailable; fast-demote verdicts "
                "unavailable"
            )
            return payload

    try:
        events = _promote.load_promotion_events(audit_log_path)
    except OSError as exc:
        payload["alerts"].append(
            f"failed to read promotion events log: {exc!r}"
        )
        return payload

    verdict_fns = {
        "stage2": _promote.stage2_fast_demote_verdict,
        "stage3-v2": _promote.stage3_v2_fast_demote_verdict,
        "stake-scaling": _promote.stake_scaling_fast_demote_verdict,
        "gate-threshold": _promote.gate_threshold_fast_demote_verdict,
    }
    for lever, fn in verdict_fns.items():
        try:
            v = fn(
                events=events, sessions_dir=sessions_dir, today=today,
            )
        except (OSError, ValueError, KeyError) as exc:
            payload["verdicts"][lever] = {
                "verdict": "error",
                "error": repr(exc),
            }
            continue
        label = str(v.get("verdict") or "")
        # Compact per-lever snapshot for the JSON payload
        payload["verdicts"][lever] = {
            "verdict": label,
            "n_post_filled": v.get("n_post_filled"),
            "wins_post": v.get("wins_post"),
            "observed_win_rate": v.get("observed_win_rate"),
            "wilson_ub_win_rate": v.get("wilson_ub_win_rate"),
            "breakeven_win_rate": v.get("breakeven_win_rate"),
            "wilson_ub_vs_breakeven_delta": (
                v.get("wilson_ub_vs_breakeven_delta")
            ),
            "promotion_event_at": (
                (v.get("promotion_event") or {}).get("generated_at_utc")
            ),
            "post_window": v.get("post_window_dates"),
        }
        if label == "fast_demote":
            payload["alerts"].append(
                f"{lever} fast_demote fired: "
                f"N={v.get('n_post_filled')} post-fills, "
                f"WR_obs={(v.get('observed_win_rate') or 0) * 100:.1f}%, "
                f"Wilson UB={(v.get('wilson_ub_win_rate') or 0) * 100:.1f}% "
                f"< breakeven {(v.get('breakeven_win_rate') or 0) * 100:.1f}%. "
                "Run `promote.py demote " + lever + "` (or daemon will act in "
                "--auto-daemon-mode act, bypassing cooldown)."
            )
    return payload


def _gate_counterfactual_health(
    *,
    report_path: Path,
    session_date: str,
) -> Dict[str, Any]:
    """Active #11 (2026-05-17): surface gate counterfactual recommendations.

    Reads the daily artifact built by `build_gate_counterfactual_report.py`
    and surfaces the top tightening recommendations. Mirrors the
    highest-impact ones to the top-level Notes block with prefix
    `Gate-counterfactual:` so operators see them in the daily review
    without opening the dedicated artifact.

    The counterfactual report itself filters by min blocked-N and a
    $25 floor; this block applies an additional $40 floor on the
    Notes-mirror layer so day-to-day single-bet noise stays in the
    JSON artifact and only the larger signals climb to the operator's
    attention.

    Surfaces under top-level `notes` with prefix `Gate-counterfactual:`.
    """
    payload: Dict[str, Any] = {
        "artifact_path": str(report_path),
        "artifact_present": report_path.exists(),
        "alerts": [],
        "top_recommendations_30d": [],
        "top_recommendations_7d": [],
    }
    if not report_path.exists():
        payload["artifact_error"] = (
            "gate_counterfactual_report missing; check refresh step ran"
        )
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    age = _artifact_age_days(
        report.get("generated_at_utc", ""), session_date,
    )
    payload["artifact_age_days"] = age
    if age is not None and age > GATE_COUNTERFACTUAL_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"gate_counterfactual_report is {age:.1f}d old "
            f"(> {GATE_COUNTERFACTUAL_STALE_AGE_DAYS}d threshold); "
            "rerun build_gate_counterfactual_report or daily refresh."
        )

    payload["n_rows"] = report.get("n_rows")
    payload["date_span"] = report.get("date_span")

    # Compact: keep only the fields operators read in a glance.
    def _compact(r: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "gate": r.get("gate"),
            "from_threshold": r.get("from_threshold"),
            "to_threshold": r.get("to_threshold"),
            "counterfactual_profit_delta_usd": r.get(
                "counterfactual_profit_delta_usd",
            ),
            "blocked_n_filled": r.get("blocked_n_filled"),
            "blocked_roi": r.get("blocked_roi"),
            "kept_roi_after": r.get("kept_roi_after"),
            "kept_roi_delta_vs_current": r.get("kept_roi_delta_vs_current"),
            "confidence": r.get("confidence"),
            "window": r.get("window"),
        }

    recs_30 = report.get("top_recommendations") or []
    recs_7 = report.get("top_recommendations_trailing_7d") or []
    payload["top_recommendations_30d"] = [_compact(r) for r in recs_30]
    payload["top_recommendations_7d"] = [_compact(r) for r in recs_7]

    # Mirror the top-N (capped) recommendations whose $-savings clear
    # the Notes-mirror floor. Use trailing-30d ranking because that's
    # the higher-confidence window; the trailing-7d list stays in the
    # JSON for operators who want freshest-signal triage.
    above_floor = [
        r for r in recs_30
        if (r.get("counterfactual_profit_delta_usd") or 0.0)
        >= GATE_COUNTERFACTUAL_NOTES_MIN_DELTA_USD
    ]
    for r in above_floor[:GATE_COUNTERFACTUAL_NOTES_MAX_ALERTS]:
        gate = r.get("gate")
        cf = float(r.get("counterfactual_profit_delta_usd") or 0.0)
        n_blocked = int(r.get("blocked_n_filled") or 0)
        blocked_roi = r.get("blocked_roi")
        kept_roi = r.get("kept_roi_after")
        roi_delta = r.get("kept_roi_delta_vs_current")
        conf = r.get("confidence")
        msg_parts = [
            f"`{gate}` tighten {r.get('from_threshold')} -> "
            f"{r.get('to_threshold')} would have saved "
            f"${cf:+,.2f} over trailing-30d ",
            f"(blocked N={n_blocked}",
        ]
        if blocked_roi is not None:
            msg_parts.append(f", blocked ROI {blocked_roi * 100:+.1f}%")
        msg_parts.append(")")
        if kept_roi is not None and roi_delta is not None:
            msg_parts.append(
                f"; kept ROI lifts to {kept_roi * 100:+.1f}% "
                f"({roi_delta * 100:+.1f}pp vs current)"
            )
        msg_parts.append(
            f"; confidence={conf}. Cross-check the cert's verdict "
            "for this gate before changing the live threshold."
        )
        payload["alerts"].append("".join(msg_parts))
    return payload


def _loss_attribution_health(
    *,
    report_path: Path,
    session_date: str,
) -> Dict[str, Any]:
    """Active #10 (2026-05-17): surface bet-level loss attribution.

    Reads the daily artifact built by `build_loss_attribution_report.py`
    and exposes the trailing-30d aggregate + top culprit stage. Mirrors
    a Notes-block alert when:
      - |aggregate bias| >= LOSS_ATTRIBUTION_NOTES_MIN_ABS_BIAS, AND
      - some stage owns >= LOSS_ATTRIBUTION_NOTES_MIN_SHARE of the
        positive bias contribution
    so the operator sees "Stage X owns the over-prediction; retrain
    target identified" without opening the dedicated artifact.

    Stale-artifact threshold matches the rest of the drift family.
    """
    payload: Dict[str, Any] = {
        "artifact_path": str(report_path),
        "artifact_present": report_path.exists(),
        "alerts": [],
        "trailing_30d": None,
        "trailing_7d": None,
    }
    if not report_path.exists():
        payload["artifact_error"] = (
            "loss_attribution_report missing; check refresh step ran"
        )
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    age = _artifact_age_days(
        report.get("generated_at_utc", ""), session_date,
    )
    payload["artifact_age_days"] = age
    if age is not None and age > LOSS_ATTRIBUTION_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"loss_attribution_report is {age:.1f}d old "
            f"(> {LOSS_ATTRIBUTION_STALE_AGE_DAYS}d threshold); "
            "rerun build_loss_attribution_report or daily refresh."
        )

    windows = report.get("windows") or {}

    def _compact(window_name: str) -> Optional[Dict[str, Any]]:
        w = windows.get(window_name) or {}
        agg = w.get("aggregate") or {}
        if not agg or agg.get("n", 0) == 0:
            return None
        return {
            "n": agg.get("n"),
            "bias": agg.get("bias"),
            "abs_bias": agg.get("abs_bias"),
            "bias_direction": agg.get("bias_direction"),
            "mean_p0": agg.get("mean_p0"),
            "mean_p3": agg.get("mean_p3"),
            "mean_won": agg.get("mean_won"),
            "top_culprits": agg.get("top_culprits") or [],
            "date_range": w.get("date_range"),
        }

    payload["trailing_30d"] = _compact("trailing_30d")
    payload["trailing_7d"] = _compact("trailing_7d")

    primary = payload["trailing_30d"]
    if not primary:
        return payload

    abs_bias = primary.get("abs_bias") or 0.0
    if abs_bias < LOSS_ATTRIBUTION_NOTES_MIN_ABS_BIAS:
        # Aggregate near-calibrated; no actionable culprit to surface.
        return payload

    direction = primary.get("bias_direction") or "unknown"
    bias_pp = (primary.get("bias") or 0.0) * 100
    n = primary.get("n")
    top_culprits = primary.get("top_culprits") or []
    headline_culprit: Optional[Dict[str, Any]] = None
    for c in top_culprits:
        if (c.get("attribution_share") or 0.0) >= LOSS_ATTRIBUTION_NOTES_MIN_SHARE:
            headline_culprit = c
            break
    if headline_culprit is None:
        # Bias exists but no clear single-stage culprit. Still worth
        # noting the bias direction + breakdown briefly.
        payload["alerts"].append(
            f"trailing-30d aggregate bias {bias_pp:+.1f}pp "
            f"(model {direction}, n={n}); no single stage owns "
            f"{int(LOSS_ATTRIBUTION_NOTES_MIN_SHARE * 100)}%+ of "
            "the bias -- read the full report to triage."
        )
        return payload

    payload["alerts"].append(
        f"trailing-30d aggregate bias {bias_pp:+.1f}pp "
        f"(model {direction}, n={n}); `{headline_culprit['stage']}` "
        f"owns "
        f"{(headline_culprit['attribution_share'] or 0.0) * 100:.0f}% "
        f"of the bias direction (shift "
        f"{(headline_culprit['mean_shift_in_bias_direction'] or 0.0) * 100:+.1f}pp). "
        "This is the retrain target -- cross-check with cohort_calibration_health "
        "and concept_drift_health before changing the live cache."
    )
    return payload


def _stage1_cell_loss_health(
    *,
    report_path: Path,
    session_date: str,
) -> Dict[str, Any]:
    """Active #10 follow-up (2026-05-17): surface Stage-1 cell-conditional
    loss attribution.

    Reads the artifact built by `build_stage1_cell_loss_attribution.py`
    and surfaces the trailing-30d aggregate Stage-1 bias + top cohort
    culprit. Mirrors a Notes-line alert when:
      - |aggregate stage1_bias| >= STAGE1_CELL_LOSS_MIN_ABS_BIAS, AND
      - fallback_rate >= STAGE1_CELL_LOSS_FALLBACK_RATE_NOTES_FLOOR
        (i.e. the bias is concentrated in fallback cells, the
        signature that Active #8 should fix the fallback path rather
        than rebuild the cache wholesale)
    so the operator gets a one-line retrain-target pointer without
    opening the artifact.
    """
    payload: Dict[str, Any] = {
        "artifact_path": str(report_path),
        "artifact_present": report_path.exists(),
        "alerts": [],
        "trailing_30d": None,
        "top_culprits_30d": [],
    }
    if not report_path.exists():
        payload["artifact_error"] = (
            "stage1_cell_loss_attribution missing; check refresh step ran"
        )
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    age = _artifact_age_days(
        report.get("generated_at_utc", ""), session_date,
    )
    payload["artifact_age_days"] = age
    if age is not None and age > STAGE1_CELL_LOSS_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"stage1_cell_loss_attribution report is {age:.1f}d old "
            f"(> {STAGE1_CELL_LOSS_STALE_AGE_DAYS}d threshold); "
            "rerun build_stage1_cell_loss_attribution or daily refresh."
        )

    windows = report.get("windows") or {}
    w30 = windows.get("trailing_30d") or {}
    agg = w30.get("aggregate") or {}
    if agg.get("n", 0) == 0:
        # No data; nothing to surface. Block still informative as a
        # "module ran" canary.
        return payload

    payload["trailing_30d"] = {
        "n": agg.get("n"),
        "stage1_bias": agg.get("stage1_bias"),
        "mean_p0": agg.get("mean_p0"),
        "mean_won": agg.get("mean_won"),
        "fallback_rate": agg.get("fallback_rate"),
        "mean_poisson_minus_empirical": agg.get(
            "mean_poisson_minus_empirical",
        ),
        "n_with_empirical": agg.get("n_with_empirical"),
        "date_range": w30.get("date_range"),
    }
    payload["top_culprits_30d"] = w30.get("top_culprits") or []

    abs_bias = abs(agg.get("stage1_bias") or 0.0)
    fallback_rate = agg.get("fallback_rate") or 0.0
    if (
        abs_bias >= STAGE1_CELL_LOSS_MIN_ABS_BIAS
        and fallback_rate >= STAGE1_CELL_LOSS_FALLBACK_RATE_NOTES_FLOOR
    ):
        n = agg.get("n")
        bias_pp = (agg.get("stage1_bias") or 0.0) * 100
        gap = agg.get("mean_poisson_minus_empirical")
        culprit_msg = ""
        culprits = payload["top_culprits_30d"]
        if culprits:
            top = culprits[0]
            culprit_msg = (
                f" Top culprit: `{top['dimension']}={top['bucket']}` "
                f"(bias {(top['stage1_bias'] or 0) * 100:+.1f}pp, "
                f"n={top['n']}, "
                f"ratio_vs_agg="
                f"{(top.get('stage1_bias_vs_aggregate_ratio') or 0):.2f}x)."
            )
        gap_msg = ""
        if gap is not None:
            gap_pp = gap * 100
            if abs(gap_pp) >= 5:
                gap_msg = (
                    f" Poisson smoothing diverges from empirical by "
                    f"{gap_pp:+.1f}pp on average -- candidate fix is the "
                    "Stage-1 smoothing, not the fallback path."
                )
        payload["alerts"].append(
            f"trailing-30d Stage-1 bias {bias_pp:+.1f}pp on n={n} bets "
            f"with fallback_rate={fallback_rate * 100:.0f}% -- "
            "Active #8 retrain surface narrows to the Stage-1 "
            "fallback path." + culprit_msg + gap_msg
        )

    return payload


def _stage1_shadow_override_health(
    *,
    report_path: Path,
    session_date: str,
) -> Dict[str, Any]:
    """Active #8 prep (2026-05-17): surface Stage-1 shadow-override
    counterfactual evidence.

    Reads the trailing-30d aggregate from
    `build_stage1_shadow_override_report.py` and surfaces:
      - Alt A bias delta vs production (the empirical-when-available
        improvement)
      - Alt B blocked count + counterfactual P&L delta
      - any recommendations that fired (>= 1pp + >= 25% coverage for
        Alt A; >= $20 + >= 3 blocked for Alt B)

    Mirrors recommendation alerts to Notes with prefix
    `Stage1-shadow:` so the operator sees actionable shadow evidence
    in the daily review.
    """
    payload: Dict[str, Any] = {
        "artifact_path": str(report_path),
        "artifact_present": report_path.exists(),
        "alerts": [],
        "trailing_30d": None,
        "recommendations_30d": [],
    }
    if not report_path.exists():
        payload["artifact_error"] = (
            "stage1_shadow_override_report missing; "
            "check refresh step ran"
        )
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    age = _artifact_age_days(
        report.get("generated_at_utc", ""), session_date,
    )
    payload["artifact_age_days"] = age
    if age is not None and age > STAGE1_SHADOW_OVERRIDE_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"stage1_shadow_override_report is {age:.1f}d old "
            f"(> {STAGE1_SHADOW_OVERRIDE_STALE_AGE_DAYS}d threshold)."
        )

    windows = report.get("windows") or {}
    w30 = windows.get("trailing_30d") or {}
    n_bets = w30.get("n_bets", 0)
    if n_bets == 0:
        return payload

    prod = w30.get("production") or {}
    alt_a = w30.get("alt_a_empirical_when_available") or {}
    alt_b = w30.get("alt_b_block_fallback_level_2plus") or {}
    payload["trailing_30d"] = {
        "n_bets": n_bets,
        "production_bias": prod.get("bias"),
        "alt_a_bias": alt_a.get("bias"),
        "alt_a_bias_delta_pp": alt_a.get("bias_delta_vs_prod_pp"),
        "alt_a_n_changed": alt_a.get("n_changed"),
        "alt_a_coverage_rate": alt_a.get("n_coverage_rate"),
        "alt_b_n_blocked": alt_b.get("n_blocked"),
        "alt_b_n_kept": alt_b.get("n_kept"),
        "alt_b_counterfactual_profit_delta_usd": (
            alt_b.get("counterfactual_profit_delta_usd")
        ),
        "alt_b_blocked_n_wins": alt_b.get("blocked_n_wins"),
        "alt_b_blocked_n_losses": alt_b.get("blocked_n_losses"),
        "date_range": w30.get("date_range"),
    }
    payload["recommendations_30d"] = w30.get("recommendations") or []

    # Mirror recommendation rationales to Notes.
    for rec in payload["recommendations_30d"]:
        alt = rec.get("alt", "?")
        payload["alerts"].append(
            f"{alt}: {rec.get('verdict')} -- " + rec.get("rationale", "")
        )
    return payload


def _cache_lineage_freshness_health(
    *,
    stage1_path: Path = DEFAULT_STAGE1_CACHE_PATH,
    stage2_path: Path = DEFAULT_STAGE2_CACHE_PATH,
    stage3_v2_path: Path = DEFAULT_STAGE3_V2_WEIGHTS_PATH,
    calibrator_path: Path = DEFAULT_CALIBRATION_ARTIFACT,
    calibrator_under_path: Path = DEFAULT_CALIBRATION_ARTIFACT_UNDER,
    build_age_warn_days: float = CACHE_LINEAGE_BUILD_AGE_WARN_DAYS,
) -> Dict[str, Any]:
    """Active #16 v3 (2026-05-17): surface embedded lineage from each
    major cache + calibrator artifact.

    Reads the `lineage` block stamped by v2 (today's earlier shipment)
    on each cache file and produces:
      - per-artifact compact summary (builder_path, git_sha,
        built_at_utc, build_age_days, input_summary)
      - alert when any cache's build age exceeds the warn threshold

    Mirrors any stale-cache alerts to top-level Notes with prefix
    `Cache-lineage:`. Complementary to `artifact_lineage_freshness`
    (which checks mtime + upstream-input ordering); this block reads
    the embedded build-time metadata only.

    Caches that pre-date v2 (no lineage block yet) are surfaced with
    a "no lineage" status rather than treated as an error -- the
    block will go fully green after the next refresh stamps lineage
    on every cache.
    """
    payload: Dict[str, Any] = {
        "alerts": [],
        "artifacts": {},
        "thresholds": {
            "build_age_warn_days": build_age_warn_days,
        },
    }

    # Lazy import to keep build_daily_human_review_report's import
    # graph small for callers that only want the lighter blocks.
    try:
        from scripts.analysis.artifact_lineage import (  # noqa: WPS433
            _read_lineage_from_path,
            format_lineage_summary_line,
            _age_days,
        )
    except ImportError:
        try:
            from artifact_lineage import (  # type: ignore[no-redef]
                _read_lineage_from_path,
                format_lineage_summary_line,
                _age_days,
            )
        except ImportError:
            payload["alerts"].append(
                "artifact_lineage module unavailable; cache lineage "
                "freshness check skipped."
            )
            return payload

    artifact_specs = [
        ("stage1_cache", stage1_path, True),
        ("stage2_cache", stage2_path, True),
        ("stage3_v2_weights", stage3_v2_path, False),
        ("calibrator_over", calibrator_path, True),
        ("calibrator_under", calibrator_under_path, False),
    ]

    for label, path, expected in artifact_specs:
        artifact_info: Dict[str, Any] = {
            "path": str(path),
            "expected": expected,
            "exists": path.exists() if path is not None else False,
        }
        if not artifact_info["exists"]:
            artifact_info["status"] = (
                "missing_required" if expected else "missing_optional"
            )
            artifact_info["summary"] = (
                f"{label}: artifact not found"
            )
            payload["artifacts"][label] = artifact_info
            if expected:
                payload["alerts"].append(
                    f"{label} artifact not found at {path}; "
                    "engine boot would fail-closed on this cache."
                )
            continue
        lineage = _read_lineage_from_path(path)
        if lineage is None:
            artifact_info["status"] = "no_lineage_pre_v2"
            artifact_info["summary"] = format_lineage_summary_line(
                label, None,
            )
            payload["artifacts"][label] = artifact_info
            # Pre-V2 artifacts are a transient state; the next refresh
            # stamps lineage. Don't alert; surfacing in the panel is
            # enough.
            continue
        # Lineage present. Compact summary + build-age check.
        build_age = _age_days(lineage.get("built_at_utc"))
        artifact_info["status"] = "ok"
        artifact_info["built_at_utc"] = lineage.get("built_at_utc")
        artifact_info["build_age_days"] = (
            round(build_age, 2) if build_age is not None else None
        )
        artifact_info["git_sha"] = lineage.get("git_sha")
        artifact_info["git_dirty"] = lineage.get("git_dirty")
        artifact_info["git_branch"] = lineage.get("git_branch")
        artifact_info["builder_path"] = lineage.get("builder_path")
        artifact_info["input_hash_count"] = len(
            lineage.get("input_hashes") or {},
        )
        artifact_info["input_dir_count"] = len(
            lineage.get("input_dir_summaries") or {},
        )
        artifact_info["summary"] = format_lineage_summary_line(
            label, lineage,
        )
        if (
            build_age is not None
            and build_age > build_age_warn_days
        ):
            payload["alerts"].append(
                f"{label} cache built {build_age:.1f}d ago "
                f"(> {build_age_warn_days:.0f}d warn threshold); "
                "daily refresh may have skipped this builder. "
                "Check refresh_health_rollup or rerun the relevant "
                "refresh step."
            )
        payload["artifacts"][label] = artifact_info

    return payload


def _daemon_readiness_health(
    *,
    report_path: Path,
    session_date: str,
    audit_log_path: Path = DEFAULT_PROMOTION_EVENTS_LOG,
) -> Dict[str, Any]:
    """Surface the daemon retrospective's per-lever readiness verdict.

    The retrospective (built by `daemon_retrospective.py`) replays the
    auto-daemon's promote-decision logic against history and classifies
    each (date, lever) into MATCH / DAEMON_ONLY / OPERATOR_ONLY /
    DAEMON_DISAGREED / BOTH_NO_ACTION. This block surfaces:
      - per-lever readiness label (ready_for_act /
        needs_more_history / disagreements_present)
      - overall_ready_for_act (true iff every time-series lever ready)
      - alerts when stale, when disagreements present, or (positive
        signal) when every lever is ready and operator may flip from
        `--auto-daemon-mode preview` to `act`.

    Surfaces under top-level `notes` with prefix "Daemon-readiness:".
    """
    payload: Dict[str, Any] = {
        "artifact_path": str(report_path),
        "artifact_present": report_path.exists(),
        "alerts": [],
    }
    if not report_path.exists():
        payload["artifact_error"] = (
            "daemon retrospective report missing; check refresh step ran"
        )
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    payload["config"] = report.get("config")

    age = _artifact_age_days(report.get("generated_at_utc", ""), session_date)
    payload["artifact_age_days"] = age
    if age is not None and age > DAEMON_RETROSPECTIVE_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"retrospective is {age:.1f}d old "
            f"(> {DAEMON_RETROSPECTIVE_STALE_AGE_DAYS}d threshold); "
            "rerun daemon_retrospective or daily refresh."
        )

    # Per-lever compact summary
    levers: Dict[str, Dict[str, Any]] = {}
    replays = report.get("replays") or {}
    all_ready = bool(replays)  # false if no replays at all
    for lever_name, replay in replays.items():
        s = replay.get("summary") or {}
        readiness = s.get("readiness_for_act")
        levers[lever_name] = {
            "readiness_for_act": readiness,
            "n_dates_evaluated": s.get("n_dates_evaluated"),
            "match_count": s.get("match_count", 0),
            "daemon_only_count": s.get("daemon_only_count", 0),
            "operator_only_count": s.get("operator_only_count", 0),
            "daemon_disagreed_count": s.get("daemon_disagreed_count", 0),
            "both_no_action_count": s.get("both_no_action_count", 0),
            "last_disagreement_date": s.get("last_disagreement_date"),
        }
        if readiness != "ready_for_act":
            all_ready = False
        if readiness == "disagreements_present":
            payload["alerts"].append(
                f"{lever_name}: {s.get('daemon_disagreed_count', 0)} "
                f"disagreement(s) + {s.get('daemon_only_count', 0)} "
                f"daemon-only action(s); inspect retrospective before "
                f"flipping to act mode (last_disagreement="
                f"{s.get('last_disagreement_date')})."
            )
    payload["levers"] = levers

    # Snapshot levers (non-time-series): just carry the verdict label
    # forward so operators can see today's stake-scaling / gate-threshold
    # verdict alongside the readiness picture.
    snap_summary: Dict[str, Dict[str, Any]] = {}
    for lever_name, snap in (report.get("snapshots") or {}).items():
        snap_summary[lever_name] = {
            "verdict_label": snap.get("verdict_label"),
            "actuated_by_daemon": snap.get("actuated_by_daemon"),
        }
    payload["snapshots"] = snap_summary

    payload["overall_ready_for_act"] = all_ready
    if all_ready:
        payload["alerts"].append(
            "all time-series levers ready_for_act; operator may consider "
            "`--auto-daemon-mode act` after reviewing the per-date table "
            "in the retrospective markdown."
        )

    # Staleness check: a lever whose verdict says promote/demote but
    # hasn't been actuated for > N days indicates the daemon isn't
    # acting on its own signal -- cooldown stuck, mode=off, opt-out
    # flag stuck on, etc. Suffix the alert with the operator name +
    # action of the LAST successful action (or "never" if none).
    staleness_records = _daemon_staleness_check(
        retrospective_report=report,
        audit_log_path=audit_log_path,
        today=session_date,
        threshold_days=DAEMON_STALENESS_THRESHOLD_DAYS,
    )
    payload["staleness_records"] = staleness_records
    for rec in staleness_records:
        if rec.get("last_action_date"):
            tail = (
                f"last action {rec['days_since_last_action']}d ago "
                f"({rec.get('last_action_operator')} {rec.get('last_action_label')} "
                f"on {rec['last_action_date']})"
            )
        else:
            tail = "no successful action ever"
        payload["alerts"].append(
            f"{rec['lever']} verdict={rec['verdict_label']} but "
            f"{tail}; > {rec['threshold_days']}d staleness threshold. "
            "Check daemon mode, cooldown, opt-out flags."
        )

    return payload


def _calibration_artifact_metadata(
    *,
    artifact_path: Path,
    session_date: str,
    alert_side_prefix: str = "",
) -> Dict[str, Any]:
    """Read one calibration artifact and return its compact metadata.

    Phase B B1 (2026-05-16) helper: pure artifact-read so the same
    logic powers both the existing OVER section and the new UNDER
    sub-block. Returns:
        {
          artifact_path, artifact_present, artifact_error?,
          artifact_generated_at_utc, artifact_age_days,
          artifact_default_family, artifact_top_selected_method,
          artifact_methods_by_family, artifact_audit_by_family,
          alerts: [...],  # side-prefixed when alert_side_prefix is set
        }

    `alert_side_prefix` (e.g. "under: ") is prepended to every alert
    so the caller can merge OVER + UNDER alerts into one list without
    losing the side attribution.
    """
    payload: Dict[str, Any] = {
        "artifact_path": str(artifact_path),
        "artifact_present": artifact_path.exists(),
        "artifact_methods_by_family": {},
        "artifact_audit_by_family": {},
        "alerts": [],
    }
    alerts: List[str] = payload["alerts"]
    if not artifact_path.exists():
        alerts.append(
            f"{alert_side_prefix}calibration artifact missing at "
            f"{artifact_path}; runtime cannot calibrate."
        )
        return payload
    try:
        artifact = _load_json(artifact_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = artifact.get("generated_at_utc")
    payload["artifact_schema_version"] = artifact.get("schema_version")
    payload["artifact_default_family"] = artifact.get("default_family")
    payload["artifact_top_selected_method"] = artifact.get("selected_method")
    age = _artifact_age_days(artifact.get("generated_at_utc", ""), session_date)
    payload["artifact_age_days"] = age
    if age is not None and age > CALIBRATION_STALE_AGE_DAYS:
        alerts.append(
            f"{alert_side_prefix}calibration artifact is {age:.1f} days old "
            f"(> {CALIBRATION_STALE_AGE_DAYS}d threshold); "
            "rerun calibrate_signal_probabilities or daily refresh."
        )
    families = artifact.get("families") or {}
    methods: Dict[str, str] = {}
    audit: Dict[str, Any] = {}
    for family, family_payload in sorted(families.items()):
        if not isinstance(family_payload, dict):
            continue
        method = str(family_payload.get("selected_method") or "")
        methods[family] = method
        fam_audit = family_payload.get("selection_audit") or {}
        audit[family] = {
            "selected_method": method,
            "primary_winner": fam_audit.get("primary_winner"),
            "identity_rejection_applied": bool(
                fam_audit.get("identity_rejection_applied")
            ),
        }
        if method == "identity":
            alerts.append(
                f"{alert_side_prefix}calibration artifact selects identity "
                f"for family '{family}'; calibrated FV will equal raw FV "
                "in production."
            )
    if not families:
        top = str(artifact.get("selected_method") or "")
        if top == "identity":
            alerts.append(
                f"{alert_side_prefix}calibration artifact has no families "
                "block AND top selected_method=identity; runtime "
                "calibrator is a no-op."
            )
        payload.setdefault(
            "artifact_warning",
            "legacy single-family artifact (no families block); "
            "no_score_drift will fall back to identity in runtime.",
        )
    payload["artifact_methods_by_family"] = methods
    payload["artifact_audit_by_family"] = audit
    # Carry the artifact-stamped side label (Phase A2 adds `side:
    # "over"|"under"` to the artifact payload) so consumers can verify
    # the file actually matches the side they expected.
    payload["artifact_side"] = str(artifact.get("side") or "")
    return payload


def _calibration_health(
    *,
    session_date: str,
    candidate_dir: Path,
    artifact_path: Path,
    output_root: Path,
    artifact_path_under: Optional[Path] = None,
) -> Dict[str, Any]:
    """Calibration drift / load-time health check.

    Pulls together three independent signals:
      1. Artifact metadata (per-family selected method, age, audit).
      2. Sampled today's candidate rows (per-family abs-delta + applied share).
      3. Yesterday's review JSON (to flag method changes day-over-day).

    Each signal can fail independently without aborting the report.
    """
    payload: Dict[str, Any] = {
        "artifact_path": str(artifact_path),
        "artifact_present": artifact_path.exists(),
        "alerts": [],
        "notes": [],
    }
    alerts: List[str] = payload["alerts"]
    notes: List[str] = payload["notes"]

    # ---- 1. Artifact metadata ----
    artifact_methods: Dict[str, str] = {}
    artifact_audit: Dict[str, Any] = {}
    if artifact_path.exists():
        try:
            artifact = _load_json(artifact_path)
        except (OSError, json.JSONDecodeError) as exc:
            payload["artifact_error"] = f"failed to load: {exc}"
            artifact = {}
        else:
            payload["artifact_generated_at_utc"] = artifact.get("generated_at_utc")
            payload["artifact_schema_version"] = artifact.get("schema_version")
            payload["artifact_default_family"] = artifact.get("default_family")
            payload["artifact_top_selected_method"] = artifact.get("selected_method")
            age = _artifact_age_days(artifact.get("generated_at_utc", ""), session_date)
            payload["artifact_age_days"] = age
            if age is not None and age > CALIBRATION_STALE_AGE_DAYS:
                alerts.append(
                    f"calibration artifact is {age:.1f} days old "
                    f"(> {CALIBRATION_STALE_AGE_DAYS}d threshold); "
                    "rerun calibrate_signal_probabilities or daily refresh."
                )
            families = artifact.get("families") or {}
            for family, family_payload in sorted(families.items()):
                if not isinstance(family_payload, dict):
                    continue
                method = str(family_payload.get("selected_method") or "")
                artifact_methods[family] = method
                audit = family_payload.get("selection_audit") or {}
                artifact_audit[family] = {
                    "selected_method": method,
                    "primary_winner": audit.get("primary_winner"),
                    "identity_rejection_applied": bool(audit.get("identity_rejection_applied")),
                }
                if method == "identity":
                    alerts.append(
                        f"calibration artifact selects identity for family '{family}'; "
                        "calibrated FV will equal raw FV in production."
                    )
            if not families:
                # Legacy single-payload artifact: nothing keyed by family.
                top = str(artifact.get("selected_method") or "")
                if top == "identity":
                    alerts.append(
                        "calibration artifact has no families block AND top "
                        "selected_method=identity; runtime calibrator is a no-op."
                    )
                payload.setdefault("artifact_warning",
                    "legacy single-family artifact (no families block); "
                    "no_score_drift will fall back to identity in runtime.")
    else:
        alerts.append(f"calibration artifact missing at {artifact_path}; runtime cannot calibrate.")
    payload["artifact_methods_by_family"] = artifact_methods
    payload["artifact_audit_by_family"] = artifact_audit

    # ---- 2. Sampled candidate rows for today ----
    candidate_path = candidate_dir / f"{session_date}_candidates.jsonl"
    payload["candidate_sample_path"] = str(candidate_path)
    rows = _load_jsonl(candidate_path)
    sample_metrics = _per_family_calibration_metrics(rows)
    payload["sampled_metrics_by_family"] = sample_metrics
    payload["sampled_rows_total"] = len(rows)
    for family, metrics in sample_metrics.items():
        method_counts = metrics.get("method_counts") or {}
        method_total = sum(method_counts.values())
        # Method-level alert fires regardless of sample size: even a few rows
        # tagged method=identity is enough to know the runtime calibrator was
        # a no-op on those candidates. Catches the failure mode from
        # 2026-05-11's investigation where the artifact had been silently
        # serving identity for weeks.
        if method_total > 0:
            identity_share = method_counts.get("identity", 0) / method_total
            if identity_share >= 0.5:
                alerts.append(
                    f"family '{family}': {method_counts.get('identity', 0)}/"
                    f"{method_total} candidate rows used calibration_method="
                    f"identity ({identity_share:.0%}); runtime calibrator was "
                    "a no-op for this family."
                )
        n = metrics.get("rows_with_both_probs") or 0
        if n >= 25:
            mean_delta = metrics.get("mean_abs_delta")
            if mean_delta is not None and mean_delta < CALIBRATION_NEAR_IDENTITY_DELTA:
                alerts.append(
                    f"family '{family}': mean |calibrated-raw|={mean_delta:.4f} "
                    f"(< {CALIBRATION_NEAR_IDENTITY_DELTA}); calibrator behaving "
                    f"as identity over {n} sampled rows."
                )
            applied_share = metrics.get("applied_share")
            if applied_share is not None and applied_share < CALIBRATION_LOW_APPLIED_SHARE:
                shadow_share = metrics.get("shadow_share")
                if (
                    shadow_share is not None
                    and shadow_share >= CALIBRATION_SHADOW_MODE_DOMINANT_SHARE
                ):
                    # Shadow mode is the documented expected behaviour:
                    # applied=False on every row is by design, not a misconfig.
                    # Suppress the alert and add one informational note instead.
                    notes.append(
                        f"family '{family}': calibration in shadow mode "
                        f"({shadow_share:.0%} of {sum((metrics.get('mode_counts') or {}).values())} rows); "
                        f"applied={applied_share:.0%} is expected."
                    )
                else:
                    alerts.append(
                        f"family '{family}': fair_value_calibration_applied=True share "
                        f"is {applied_share:.1%} (< {CALIBRATION_LOW_APPLIED_SHARE:.0%}); "
                        "calibration mode may be off or family-missing."
                    )

    # ---- 3. Day-over-day method drift ----
    prior_date = _shift_date(session_date, -1)
    prior_methods: Dict[str, str] = {}
    if prior_date:
        prior_review = output_root / f"{prior_date}_human_review.json"
        if prior_review.exists():
            try:
                prior_payload = _load_json(prior_review)
                prior_health = prior_payload.get("calibration_health") or {}
                prior_methods = prior_health.get("artifact_methods_by_family") or {}
                payload["prior_review_path"] = str(prior_review)
                payload["prior_artifact_methods_by_family"] = prior_methods
            except (OSError, json.JSONDecodeError):
                payload["prior_review_error"] = "failed to load prior daily review"
    method_changes: Dict[str, Dict[str, str]] = {}
    for family, today_method in artifact_methods.items():
        yesterday_method = prior_methods.get(family)
        if yesterday_method and yesterday_method != today_method:
            method_changes[family] = {"from": yesterday_method, "to": today_method}
            alerts.append(
                f"calibration method changed for family '{family}': "
                f"{yesterday_method} -> {today_method} since previous daily review."
            )
    payload["method_changes_since_prior"] = method_changes

    # ---- 4. UNDER-side artifact metadata (Phase B B1, 2026-05-16) ----
    # The UNDER calibrator artifact has the same per-family Platt/
    # isotonic structure as the OVER one, refreshed daily by the new
    # `calibrate_signal_probabilities_under` step. We only surface
    # ARTIFACT-level signals here (method per family, age, identity
    # detection); sampled-candidate-row deltas are deferred until UNDER
    # candidate emission runs against the live signal pipeline (Phase
    # B/C). Method-change-since-yesterday compares per side.
    if artifact_path_under is not None:
        under_block = _calibration_artifact_metadata(
            artifact_path=artifact_path_under,
            session_date=session_date,
            alert_side_prefix="under: ",
        )
        # Mirror the method_changes machinery for UNDER (compares this
        # side's methods against the prior day's UNDER methods, which
        # live in the prior review's `calibration_health.under.
        # artifact_methods_by_family`).
        prior_under_methods: Dict[str, str] = {}
        if prior_date:
            prior_review = output_root / f"{prior_date}_human_review.json"
            if prior_review.exists():
                try:
                    prior_payload = _load_json(prior_review)
                    prior_health = prior_payload.get("calibration_health") or {}
                    prior_under = (prior_health or {}).get("under") or {}
                    prior_under_methods = (
                        prior_under.get("artifact_methods_by_family") or {}
                    )
                except (OSError, json.JSONDecodeError):
                    # Prior payload unreadable; silent -- this is a
                    # day-over-day comparison, not a hard dependency.
                    pass
        under_method_changes: Dict[str, Dict[str, str]] = {}
        under_alerts: List[str] = under_block["alerts"]
        for family, today_method in (
            under_block.get("artifact_methods_by_family") or {}
        ).items():
            yesterday_method = prior_under_methods.get(family)
            if yesterday_method and yesterday_method != today_method:
                under_method_changes[family] = {
                    "from": yesterday_method, "to": today_method,
                }
                under_alerts.append(
                    f"under: calibration method changed for family "
                    f"'{family}': {yesterday_method} -> {today_method} "
                    "since previous daily review."
                )
        under_block["method_changes_since_prior"] = under_method_changes
        under_block["prior_artifact_methods_by_family"] = prior_under_methods
        payload["under"] = under_block
        # Lift the UNDER alerts into the top-level `alerts` list so the
        # Notes block (which mirrors `calibration_health.alerts` with
        # the "Calibration drift:" prefix) surfaces them without
        # needing a second consumer for the `under` sub-block.
        alerts.extend(under_alerts)

    return payload


def _build_notes(
    session_summary: Dict[str, Any],
    bet_totals: Dict[str, Any],
    candidate_rollup: Dict[str, Any],
    log_health: Dict[str, Any],
    calibration_health: Optional[Dict[str, Any]] = None,
    fill_rate_health: Optional[Dict[str, Any]] = None,
    signal_quality_health: Optional[Dict[str, Any]] = None,
    regime_mix_health: Optional[Dict[str, Any]] = None,
    reconciler_summary: Optional[Dict[str, Any]] = None,
    cohort_roi_health: Optional[Dict[str, Any]] = None,
    concept_drift_health: Optional[Dict[str, Any]] = None,
    drift_in_drift_health: Optional[Dict[str, Any]] = None,
    daemon_readiness_health: Optional[Dict[str, Any]] = None,
    under_book_coverage_health: Optional[Dict[str, Any]] = None,
    settlement_truth_health: Optional[Dict[str, Any]] = None,
    fast_demote_health: Optional[Dict[str, Any]] = None,
    gate_counterfactual_health: Optional[Dict[str, Any]] = None,
    cohort_calibration_health: Optional[Dict[str, Any]] = None,
    loss_attribution_health: Optional[Dict[str, Any]] = None,
    cache_lineage_freshness_health: Optional[Dict[str, Any]] = None,
    stage1_cell_loss_health: Optional[Dict[str, Any]] = None,
    stage1_shadow_override_health: Optional[Dict[str, Any]] = None,
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
    calibration_health = _calibration_health(
        session_date=session_date,
        candidate_dir=candidate_dir,
        artifact_path=calibration_artifact,
        output_root=output_root,
        artifact_path_under=DEFAULT_CALIBRATION_ARTIFACT_UNDER,
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
    # concept_drift_health computed BEFORE cohort_roi_health so the
    # latter can append a "[concept-drift: <feature> PSI <value>, ...]"
    # candidate-root-cause suffix to each alert.
    concept_drift_health = _concept_drift_health(
        report_path=DEFAULT_CONCEPT_DRIFT_REPORT,
        session_date=session_date,
    )
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
    reconciler_summary = _reconciler_summary(session.get("bets") or [])
    notes = _build_notes(
        session_summary,
        bet_totals,
        candidate_rollup,
        log_health,
        calibration_health,
        fill_rate_health,
        signal_quality_health,
        regime_mix_health,
        reconciler_summary,
        cohort_roi_health,
        concept_drift_health,
        drift_in_drift_health,
        daemon_readiness_health,
        under_book_coverage_health,
        settlement_truth_health,
        fast_demote_health,
        gate_counterfactual_health,
        cohort_calibration_health,
        loss_attribution_health,
        cache_lineage_freshness_health,
        stage1_cell_loss_health,
        stage1_shadow_override_health,
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
        "fill_rate_health": fill_rate_health,
        "signal_quality_health": signal_quality_health,
        "regime_mix_health": regime_mix_health,
        "cohort_roi_health": cohort_roi_health,
        "cohort_calibration_health": cohort_calibration_health,
        "loss_attribution_health": loss_attribution_health,
        "cache_lineage_freshness_health": cache_lineage_freshness_health,
        "stage1_cell_loss_health": stage1_cell_loss_health,
        "stage1_shadow_override_health": stage1_shadow_override_health,
        "concept_drift_health": concept_drift_health,
        "drift_in_drift_health": drift_in_drift_health,
        "daemon_readiness_health": daemon_readiness_health,
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
