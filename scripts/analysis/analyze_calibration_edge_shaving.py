#!/usr/bin/env python3
"""analyze_calibration_edge_shaving.py -- is the calibrator over-shrinking? (2026-05-28).

Deep dive motivated by the 2026-05-27 paper-model audit: production
(A_current, band-gated calibrator enforce) bets ~10x less than the
uncalibrated engine, and the dominant lever is probability calibration,
not a broken gate. The audit's open question was:

  "Pull the actual calibration curve and quantify how much edge it's
   shaving off the post-structural candidates -- is the shrinkage
   justified by realized win rates?"

This script answers that. For the score-event-transition family it:

  1. Re-applies the CURRENT calibration artifact's Platt curve (under the
     production band-gated-enforce rule) to every post-structural
     candidate's `fair_value_raw`. We re-apply rather than trust the
     logged `fair_value_calibrated`, because most rows were logged while
     calibration was still in shadow (logged calibrated == raw).
  2. Quantifies the FV haircut ("edge shaved") on the enforce band.
  3. Builds a reliability table by raw-FV band: avg raw FV vs avg
     calibrated FV vs REALIZED win rate, and which estimate is closer to
     truth -- the direct "is the shrinkage justified" measurement.
  4. Isolates the *calibration-suppression cohort*: candidates that would
     clear the edge gates on raw FV but are killed once calibration
     shrinks the edge below the floor. Reports their realized win rate,
     breakeven (avg ask), counterfactual taker ROI, and a Wilson lower
     bound so small-sample noise is explicit.
  5. Sweeps `enforce_min_raw` x `min_edge` and reports, per cell, how many
     bets pass + their realized WR / breakeven / taker ROI / Wilson LB --
     the operator's lever-decision grid.
  6. Emits a verdict + a recommended enforce_min_raw that best separates
     the +EV high-FV band from the -EV overconfident tail.

Descriptive over a small, recent labeled sample. It measures realized
outcomes on candidates we already logged; it is NOT a walk-forward
promotion certificate. Treat the recommendation as evidence to feed the
manual `--prob-calibration-enforce-min-raw` decision, not an auto-promote.

Inputs:
  - data/analysis_output/calibration_opportunity_training/by_family/
      calibration_opportunity_training_table_<family>.jsonl
  - data/analysis_output/calibration/signal_win_calibration.json

Output:
  data/analysis_output/calibration_edge_shaving/calibration_edge_shaving.json
  data/analysis_output/calibration_edge_shaving/calibration_edge_shaving.md
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Sequence, Tuple

PROJECT_DIR = Path(__file__).resolve().parents[2]
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
if str(TRADING_DIR) not in sys.path:
    sys.path.insert(0, str(TRADING_DIR))

from probability_calibration import ProbabilityCalibrator  # noqa: E402

DEFAULT_FAMILY = "score_event_transition"
DEFAULT_FAMILY_TABLE = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "calibration_opportunity_training"
    / "by_family"
    / "calibration_opportunity_training_table_score_event_transition.jsonl"
)
DEFAULT_CALIBRATION_ARTIFACT = (
    PROJECT_DIR / "data" / "analysis_output" / "calibration" / "signal_win_calibration.json"
)
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "data" / "analysis_output" / "calibration_edge_shaving"
DEFAULT_OUTPUT_STEM = "calibration_edge_shaving"

# Production defaults (signal_config.py). Mirror them so the "as-configured"
# scenario matches what the live engine actually does.
DEFAULT_ENFORCE_MIN_RAW = 0.90
DEFAULT_MIN_EDGE = 0.15
DEFAULT_EXTREME_EDGE_MAX = 0.22

# Raw-FV bands for the reliability table. The high band is split at 0.95
# because that is where realized behavior flips sign in the current data.
DEFAULT_RAW_FV_BANDS: Tuple[Tuple[float, float], ...] = (
    (0.0, 0.55),
    (0.55, 0.70),
    (0.70, 0.80),
    (0.80, 0.85),
    (0.85, 0.90),
    (0.90, 0.95),
    (0.95, 1.0001),
)

DEFAULT_ENFORCE_MIN_RAW_SWEEP: Tuple[float, ...] = (0.90, 0.93, 0.95, 0.97, 1.01)
DEFAULT_MIN_EDGE_SWEEP: Tuple[float, ...] = (0.10, 0.12, 0.15, 0.20)

# A suppression cohort needs at least this many settled bets before we will
# call calibration over-shrinking or justified; below it we say insufficient.
MIN_N_FOR_VERDICT = 20


# --------------------------------------------------------------------------
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------
def band_gated_fv(
    raw: float,
    calibrated: float,
    *,
    mode: str = "enforce",
    enforce_min_raw: float = DEFAULT_ENFORCE_MIN_RAW,
) -> float:
    """Mirror signal_engine._calibrate_fair_value band-gated enforce.

    enforce: overwrite raw with calibrated only when raw >= enforce_min_raw;
    otherwise keep raw (shadow-like for the mid band).
    shadow / off: always keep raw.
    """
    if mode != "enforce":
        return raw
    if raw < enforce_min_raw:
        return raw
    return calibrated


def wilson_interval(wins: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Two-sided Wilson score interval for a binomial proportion."""
    if n <= 0:
        return (0.0, 0.0)
    phat = wins / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _safe_int01(value: Any) -> Optional[int]:
    f = _safe_float(value)
    if f is None:
        return None
    return 1 if f >= 0.5 else 0


def raw_fv_band_label(raw: float, bands: Sequence[Tuple[float, float]]) -> Optional[str]:
    for lo, hi in bands:
        if lo <= raw < hi:
            hi_disp = min(hi, 1.0)
            return f"[{lo:.2f},{hi_disp:.2f})"
    return None


def passes_edge_gates(edge: float, min_edge: float, extreme_edge_max: float) -> bool:
    """Production post-FV edge admission: min_edge <= edge <= extreme_edge_max."""
    return (edge >= min_edge) and (edge <= extreme_edge_max)


# --------------------------------------------------------------------------
# Row model
# --------------------------------------------------------------------------
@dataclass
class Candidate:
    raw_fv: float
    ask: float
    won: int
    taker_units: Optional[float]
    limit_units: Optional[float]
    split: str
    session_date: str
    line: Optional[float]
    decision: str
    decision_reason: str
    logged_min_edge_effective: Optional[float]
    cal_fv: float = 0.0  # filled in after calibrator applied

    @property
    def raw_edge(self) -> float:
        return self.raw_fv - self.ask

    @property
    def cal_edge(self) -> float:
        return self.cal_fv - self.ask

    @property
    def fv_shaved(self) -> float:
        return self.raw_fv - self.cal_fv


def load_candidates(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def build_candidates(
    rows: Sequence[Dict[str, Any]],
    *,
    splits: Optional[Sequence[str]] = None,
) -> List[Candidate]:
    """Project raw table rows into settled post-structural candidates.

    A row qualifies if it reached FV/edge evaluation (has fair_value_raw +
    decision_ask) and is settled (has a 0/1 over-win label).
    """
    out: List[Candidate] = []
    split_set = set(splits) if splits else None
    for r in rows:
        raw_fv = _safe_float(r.get("fair_value_raw"))
        ask = _safe_float(r.get("decision_ask"))
        won = _safe_int01(r.get("target_over_win"))
        if raw_fv is None or ask is None or won is None:
            continue
        if not (0.0 < ask < 1.0):
            continue
        split = str(r.get("split") or "unknown")
        if split_set is not None and split not in split_set:
            continue
        out.append(
            Candidate(
                raw_fv=raw_fv,
                ask=ask,
                won=won,
                taker_units=_safe_float(r.get("target_taker_profit_units")),
                limit_units=_safe_float(r.get("target_limit_profit_units")),
                split=split,
                session_date=str(r.get("session_date") or ""),
                line=_safe_float(r.get("line")),
                decision=str(r.get("decision") or ""),
                decision_reason=str(r.get("decision_reason") or ""),
                logged_min_edge_effective=_safe_float(r.get("min_edge_effective")),
            )
        )
    return out


# --------------------------------------------------------------------------
# Cohort aggregation
# --------------------------------------------------------------------------
def cohort_stats(cands: Sequence[Candidate]) -> Dict[str, Any]:
    n = len(cands)
    if n == 0:
        return {
            "n": 0,
            "win_rate": None,
            "avg_ask": None,
            "avg_raw_fv": None,
            "avg_cal_fv": None,
            "edge_over_breakeven": None,
            "taker_roi": None,
            "limit_roi": None,
            "wilson_lo": None,
            "wilson_hi": None,
        }
    wins = sum(c.won for c in cands)
    wr = wins / n
    avg_ask = sum(c.ask for c in cands) / n
    avg_raw = sum(c.raw_fv for c in cands) / n
    avg_cal = sum(c.cal_fv for c in cands) / n
    taker_vals = [c.taker_units for c in cands if c.taker_units is not None]
    limit_vals = [c.limit_units for c in cands if c.limit_units is not None]
    lo, hi = wilson_interval(wins, n)
    return {
        "n": n,
        "wins": wins,
        "win_rate": wr,
        "avg_ask": avg_ask,
        "avg_raw_fv": avg_raw,
        "avg_cal_fv": avg_cal,
        "edge_over_breakeven": wr - avg_ask,
        "taker_roi": (sum(taker_vals) / len(taker_vals)) if taker_vals else None,
        "limit_roi": (sum(limit_vals) / len(limit_vals)) if limit_vals else None,
        "wilson_lo": lo,
        "wilson_hi": hi,
    }


def reliability_by_raw_band(
    cands: Sequence[Candidate],
    bands: Sequence[Tuple[float, float]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for lo, hi in bands:
        sub = [c for c in cands if lo <= c.raw_fv < hi]
        stats = cohort_stats(sub)
        if stats["n"] == 0:
            continue
        wr = stats["win_rate"]
        raw_err = abs(stats["avg_raw_fv"] - wr)
        cal_err = abs(stats["avg_cal_fv"] - wr)
        # Which estimate is closer to realized truth?
        if abs(raw_err - cal_err) < 1e-9:
            closer = "tie"
        else:
            closer = "calibrated" if cal_err < raw_err else "raw"
        out.append(
            {
                "band": f"[{lo:.2f},{min(hi, 1.0):.2f})",
                **stats,
                "raw_calibration_error": raw_err,
                "cal_calibration_error": cal_err,
                "closer_to_realized": closer,
                # +EV iff realized WR exceeds breakeven (taker @ ask)
                "is_positive_ev": wr > stats["avg_ask"],
            }
        )
    return out


def edge_shaving_summary(
    cands: Sequence[Candidate],
    enforce_min_raw: float,
) -> Dict[str, Any]:
    affected = [c for c in cands if c.raw_fv >= enforce_min_raw]
    shaves = [c.fv_shaved for c in affected]
    if not shaves:
        return {
            "enforce_min_raw": enforce_min_raw,
            "n_in_enforce_band": 0,
            "n_total": len(cands),
            "share_in_enforce_band": 0.0,
        }
    shaves_sorted = sorted(shaves)

    def pct(p: float) -> float:
        if len(shaves_sorted) == 1:
            return shaves_sorted[0]
        idx = min(len(shaves_sorted) - 1, max(0, int(round(p * (len(shaves_sorted) - 1)))))
        return shaves_sorted[idx]

    return {
        "enforce_min_raw": enforce_min_raw,
        "n_total": len(cands),
        "n_in_enforce_band": len(affected),
        "share_in_enforce_band": len(affected) / len(cands) if cands else 0.0,
        "fv_shaved_mean": sum(shaves) / len(shaves),
        "fv_shaved_median": median(shaves),
        "fv_shaved_p10": pct(0.10),
        "fv_shaved_p90": pct(0.90),
        "fv_shaved_max": max(shaves),
        "avg_raw_fv_in_band": sum(c.raw_fv for c in affected) / len(affected),
        "avg_cal_fv_in_band": sum(c.cal_fv for c in affected) / len(affected),
    }


def suppression_cohort(
    cands: Sequence[Candidate],
    *,
    min_edge: float,
    extreme_edge_max: float,
    enforce_min_raw: float,
) -> Dict[str, Any]:
    """Candidates calibration UNIQUELY kills.

    pass on raw FV (would-trade if calibration off) but fail once the
    band-gated calibrator shrinks the edge below min_edge.
    """
    suppressed: List[Candidate] = []
    for c in cands:
        passes_raw = passes_edge_gates(c.raw_edge, min_edge, extreme_edge_max)
        passes_cal = passes_edge_gates(c.cal_edge, min_edge, extreme_edge_max)
        if passes_raw and not passes_cal:
            suppressed.append(c)
    stats = cohort_stats(suppressed)
    # Verdict for this cohort: would these bets have made money as a taker?
    verdict = "insufficient_evidence"
    if stats["n"] >= MIN_N_FOR_VERDICT:
        wr = stats["win_rate"]
        avg_ask = stats["avg_ask"]
        wilson_lo = stats["wilson_lo"]
        if wilson_lo > avg_ask:
            verdict = "over_shrinking"  # reliably +EV bets are being killed
        elif wr <= avg_ask:
            verdict = "justified"  # killed bets were -EV
        else:
            verdict = "marginal"  # +EV point estimate but Wilson LB below breakeven
    return {
        "min_edge": min_edge,
        "extreme_edge_max": extreme_edge_max,
        "enforce_min_raw": enforce_min_raw,
        "verdict": verdict,
        **stats,
    }


def gate_scenarios(
    cands: Sequence[Candidate],
    *,
    enforce_min_raw_sweep: Sequence[float],
    min_edge_sweep: Sequence[float],
    extreme_edge_max: float,
    calibrator: ProbabilityCalibrator,
    model_family: str,
) -> List[Dict[str, Any]]:
    """For each (enforce_min_raw, min_edge) cell, what passes + its realized
    outcomes. enforce_min_raw=1.01 effectively disables calibration (raw FV
    can never reach it), giving the calibration-off baseline."""
    out: List[Dict[str, Any]] = []
    # Cache the calibrated value per candidate (curve is fixed); the
    # band gate is what changes per enforce_min_raw.
    cal_cache = {id(c): c.cal_fv for c in cands}
    for emr in enforce_min_raw_sweep:
        for me in min_edge_sweep:
            passing: List[Candidate] = []
            for c in cands:
                fv = band_gated_fv(c.raw_fv, cal_cache[id(c)], mode="enforce", enforce_min_raw=emr)
                edge = fv - c.ask
                if passes_edge_gates(edge, me, extreme_edge_max):
                    passing.append(c)
            stats = cohort_stats(passing)
            out.append(
                {
                    "enforce_min_raw": emr,
                    "min_edge": me,
                    "calibration_effectively_off": emr > 1.0,
                    **stats,
                }
            )
    return out


def recommend_enforce_min_raw(
    cands: Sequence[Candidate],
    *,
    min_edge: float,
    extreme_edge_max: float,
    candidate_thresholds: Sequence[float],
    baseline_enforce_min_raw: float,
) -> Dict[str, Any]:
    """Pick the enforce_min_raw that maximizes total realized taker units of
    the admitted bets at the production min_edge. Higher threshold = more
    raw FV trusted = more bets admitted; we want the one whose marginal
    admits are net +EV, not the one that simply admits the most."""

    def admitted(emr: float) -> List[Candidate]:
        keep: List[Candidate] = []
        for c in cands:
            fv = band_gated_fv(c.raw_fv, c.cal_fv, mode="enforce", enforce_min_raw=emr)
            if passes_edge_gates(fv - c.ask, min_edge, extreme_edge_max):
                keep.append(c)
        return keep

    def total_taker(cohort: Sequence[Candidate]) -> float:
        return sum(c.taker_units for c in cohort if c.taker_units is not None)

    rows: List[Dict[str, Any]] = []
    for emr in candidate_thresholds:
        adm = admitted(emr)
        stats = cohort_stats(adm)
        rows.append(
            {
                "enforce_min_raw": emr,
                "n_admitted": stats["n"],
                "win_rate": stats["win_rate"],
                "avg_ask": stats["avg_ask"],
                "total_taker_units": total_taker(adm),
                "taker_roi": stats["taker_roi"],
                "wilson_lo": stats["wilson_lo"],
            }
        )
    # Best = highest total realized taker units (net dollars per unit stake).
    scored = [r for r in rows if r["n_admitted"] > 0]
    if scored:
        best = max(scored, key=lambda r: r["total_taker_units"])
        recommended = best["enforce_min_raw"]
    else:
        recommended = baseline_enforce_min_raw
    return {
        "baseline_enforce_min_raw": baseline_enforce_min_raw,
        "recommended_enforce_min_raw": recommended,
        "objective": "maximize total realized taker units of admitted bets at production min_edge",
        "by_threshold": rows,
    }


# --------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------
def build_report(
    rows: Sequence[Dict[str, Any]],
    calibrator: ProbabilityCalibrator,
    *,
    model_family: str,
    enforce_min_raw: float,
    min_edge: float,
    extreme_edge_max: float,
    raw_fv_bands: Sequence[Tuple[float, float]],
    enforce_min_raw_sweep: Sequence[float],
    min_edge_sweep: Sequence[float],
    calibration_artifact: str,
    family_table: str,
) -> Dict[str, Any]:
    cands = build_candidates(rows)
    # Apply current curve to every candidate's raw FV.
    for c in cands:
        c.cal_fv = float(calibrator.calibrate(c.raw_fv, model_family=model_family))

    overall = cohort_stats(cands)
    reliability = reliability_by_raw_band(cands, raw_fv_bands)
    shaving = edge_shaving_summary(cands, enforce_min_raw)
    suppression = suppression_cohort(
        cands,
        min_edge=min_edge,
        extreme_edge_max=extreme_edge_max,
        enforce_min_raw=enforce_min_raw,
    )
    scenarios = gate_scenarios(
        cands,
        enforce_min_raw_sweep=enforce_min_raw_sweep,
        min_edge_sweep=min_edge_sweep,
        extreme_edge_max=extreme_edge_max,
        calibrator=calibrator,
        model_family=model_family,
    )
    recommendation = recommend_enforce_min_raw(
        cands,
        min_edge=min_edge,
        extreme_edge_max=extreme_edge_max,
        candidate_thresholds=enforce_min_raw_sweep,
        baseline_enforce_min_raw=enforce_min_raw,
    )

    # Per-split high-FV regime note (train/val were wildly overconfident;
    # test was much better -- surface so the reader doesn't over-trust one).
    by_split: Dict[str, Any] = {}
    for split in sorted({c.split for c in cands}):
        sub = [c for c in cands if c.split == split and c.raw_fv >= enforce_min_raw]
        by_split[split] = cohort_stats(sub)

    # Curve sample anchor points.
    curve_points = []
    for raw in (0.80, 0.85, 0.90, 0.93, 0.95, 0.97, 0.99):
        cal = float(calibrator.calibrate(raw, model_family=model_family))
        curve_points.append({"raw": raw, "calibrated": cal, "fv_shaved": raw - cal})

    # Top-level verdict synthesizes the band reliability + suppression cohort.
    verdict = _synthesize_verdict(reliability, suppression, recommendation, enforce_min_raw)

    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_family": model_family,
        "config": {
            "family_table": family_table,
            "calibration_artifact": calibration_artifact,
            "enforce_min_raw": enforce_min_raw,
            "min_edge": min_edge,
            "extreme_edge_max": extreme_edge_max,
            "calibrator_method": calibrator.method_for_family(model_family),
        },
        "verdict": verdict,
        "recommendation": recommendation,
        "calibration_curve": {
            "method": calibrator.method_for_family(model_family),
            "anchor_points": curve_points,
        },
        "overall": overall,
        "edge_shaving": shaving,
        "reliability_by_raw_band": reliability,
        "suppression_cohort": suppression,
        "high_fv_band_by_split": by_split,
        "gate_scenarios": scenarios,
    }


def _synthesize_verdict(
    reliability: Sequence[Dict[str, Any]],
    suppression: Dict[str, Any],
    recommendation: Dict[str, Any],
    enforce_min_raw: float,
) -> Dict[str, Any]:
    """Plain-language verdict + headline numbers.

    Keyed off the recommendation grid (realized total taker units of the
    admitted set at each enforce_min_raw), NOT the blended suppression
    cohort -- which can read "justified" in aggregate because the large
    -EV overconfident tail dominates it by count, masking a wrongly-killed
    +EV sub-band. Logic:
      - OVER_SHRINKING_PARTIAL: a higher enforce_min_raw produces materially
        more realized units than current AND its admitted set is reliably
        +EV (Wilson LB > breakeven) AND a +EV band exists inside the
        current enforce zone (the band being wrongly flattened).
      - JUSTIFIED: no higher threshold beats current and every enforce-zone
        band is realized -EV (calibration is correctly killing losers).
      - INSUFFICIENT_EVIDENCE: otherwise (thin sample / no clear signal).
    """
    enforce_bands = [
        b for b in reliability
        if _band_low(b["band"]) is not None and _band_low(b["band"]) >= enforce_min_raw - 1e-9
    ]
    pos_bands = [b for b in enforce_bands if b.get("is_positive_ev") and b["n"] >= 10]
    neg_bands = [b for b in enforce_bands if not b.get("is_positive_ev") and b["n"] >= 10]

    grid = {round(float(r["enforce_min_raw"]), 4): r for r in recommendation.get("by_threshold", [])}
    cur = grid.get(round(float(enforce_min_raw), 4))
    rec_thr = float(recommendation.get("recommended_enforce_min_raw", enforce_min_raw))
    rec_row = grid.get(round(rec_thr, 4))

    cur_units = cur.get("total_taker_units") if cur else None
    rec_units = rec_row.get("total_taker_units") if rec_row else None

    raising_helps = (
        rec_row is not None
        and cur is not None
        and rec_thr > enforce_min_raw + 1e-9
        and rec_units is not None
        and cur_units is not None
        and (rec_units - cur_units) > 0
        and rec_row.get("n_admitted", 0) >= MIN_N_FOR_VERDICT
        and rec_row.get("wilson_lo") is not None
        and rec_row.get("avg_ask") is not None
        and rec_row["wilson_lo"] > rec_row["avg_ask"]
    )

    sup_verdict = suppression.get("verdict")
    if raising_helps and pos_bands:
        label = "OVER_SHRINKING_PARTIAL"
        summary = (
            "Band-gated calibrator is too blunt: it flattens a realized +EV "
            f"high-FV sub-band ({', '.join(b['band'] for b in pos_bands)}) together "
            "with the -EV overconfident tail. Raising enforce_min_raw "
            f"{enforce_min_raw:.2f} -> {rec_thr:.2f} grows the admitted set "
            f"{cur.get('n_admitted')} -> {rec_row.get('n_admitted')} bets and "
            f"realized taker units {cur_units:+.1f} -> {rec_units:+.1f}, while the "
            "0.95+ tail stays shrunk. The aggregate suppression cohort still reads "
            f"'{sup_verdict}' because the -EV tail dominates it by count -- that is "
            "the masking effect this report exists to catch."
        )
    elif enforce_bands and not pos_bands:
        label = "JUSTIFIED"
        summary = (
            "Every raw-FV band inside the enforce zone is realized -EV; "
            "calibration is correctly suppressing overconfident losers. "
            "No threshold change indicated."
        )
    else:
        label = "INSUFFICIENT_EVIDENCE"
        summary = (
            "Sample too thin to call the shrinkage justified or excessive; "
            "accumulate more settled high-FV candidates before changing "
            "enforce_min_raw."
        )
    return {
        "label": label,
        "summary": summary,
        "positive_ev_enforce_bands": [b["band"] for b in pos_bands],
        "negative_ev_enforce_bands": [b["band"] for b in neg_bands],
        "suppression_cohort_verdict": sup_verdict,
        "current_enforce_min_raw": enforce_min_raw,
        "current_admitted_n": cur.get("n_admitted") if cur else None,
        "current_total_taker_units": cur_units,
        "recommended_enforce_min_raw": rec_thr,
        "recommended_admitted_n": rec_row.get("n_admitted") if rec_row else None,
        "recommended_total_taker_units": rec_units,
    }


def _band_low(band_label: str) -> Optional[float]:
    try:
        return float(band_label.strip("[)").split(",")[0])
    except (ValueError, AttributeError, IndexError):
        return None


# --------------------------------------------------------------------------
# Markdown renderer
# --------------------------------------------------------------------------
def _fmt(v: Any, nd: int = 3, pct: bool = False) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        if pct:
            return f"{v * 100:+.1f}%"
        return f"{v:.{nd}f}"
    return str(v)


def render_markdown(report: Dict[str, Any]) -> str:
    cfg = report["config"]
    v = report["verdict"]
    lines: List[str] = []
    lines.append("# Calibration Edge-Shaving Deep Dive")
    lines.append("")
    lines.append(f"_Generated {report['generated_at_utc']} | family `{report['model_family']}`_")
    lines.append("")
    lines.append(f"**Verdict: `{v['label']}`** — {v['summary']}")
    lines.append("")
    rec = report["recommendation"]
    lines.append(
        f"**Recommended `enforce_min_raw`: {rec['recommended_enforce_min_raw']:.2f}** "
        f"(current production: {rec['baseline_enforce_min_raw']:.2f})."
    )
    lines.append("")
    lines.append(
        f"Config: enforce_min_raw={cfg['enforce_min_raw']}, min_edge={cfg['min_edge']}, "
        f"extreme_edge_max={cfg['extreme_edge_max']}, calibrator={cfg['calibrator_method']}."
    )
    lines.append("")

    # Curve
    lines.append("## Calibration curve (raw -> calibrated)")
    lines.append("")
    lines.append("| raw FV | calibrated | FV shaved |")
    lines.append("|---|---|---|")
    for p in report["calibration_curve"]["anchor_points"]:
        lines.append(f"| {p['raw']:.2f} | {p['calibrated']:.3f} | {p['fv_shaved']:+.3f} |")
    lines.append("")

    # Reliability table -- the core "is shrinkage justified" answer.
    lines.append("## Reliability by raw-FV band (is the shrinkage justified?)")
    lines.append("")
    lines.append(
        "| raw band | n | avg raw | avg cal | realized WR | avg ask | edge vs BE | taker ROI | closer to truth | +EV? |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for b in report["reliability_by_raw_band"]:
        lines.append(
            "| {band} | {n} | {avg_raw} | {avg_cal} | {wr} | {ask} | {ev} | {roi} | {closer} | {pos} |".format(
                band=b["band"],
                n=b["n"],
                avg_raw=_fmt(b["avg_raw_fv"]),
                avg_cal=_fmt(b["avg_cal_fv"]),
                wr=_fmt(b["win_rate"]),
                ask=_fmt(b["avg_ask"]),
                ev=_fmt(b["edge_over_breakeven"], pct=True),
                roi=_fmt(b["taker_roi"], pct=True),
                closer=b["closer_to_realized"],
                pos="yes" if b["is_positive_ev"] else "no",
            )
        )
    lines.append("")

    # Edge shaving
    sh = report["edge_shaving"]
    lines.append("## Edge shaved on the enforce band")
    lines.append("")
    if sh.get("n_in_enforce_band"):
        lines.append(
            f"- {sh['n_in_enforce_band']} of {sh['n_total']} candidates "
            f"({sh['share_in_enforce_band'] * 100:.1f}%) sit in the enforce band "
            f"(raw >= {sh['enforce_min_raw']:.2f})."
        )
        lines.append(
            f"- FV haircut: mean {sh['fv_shaved_mean']:.3f}, median {sh['fv_shaved_median']:.3f}, "
            f"p10 {sh['fv_shaved_p10']:.3f}, p90 {sh['fv_shaved_p90']:.3f}, max {sh['fv_shaved_max']:.3f}."
        )
        lines.append(
            f"- Avg raw FV {sh['avg_raw_fv_in_band']:.3f} -> calibrated {sh['avg_cal_fv_in_band']:.3f}."
        )
    else:
        lines.append("- No candidates in the enforce band.")
    lines.append("")

    # Suppression cohort
    sc = report["suppression_cohort"]
    lines.append("## Calibration-suppression cohort (bets calibration uniquely kills)")
    lines.append("")
    lines.append(
        f"Pass edge gates on raw FV but fail once calibrated (min_edge={sc['min_edge']}, "
        f"extreme_edge_max={sc['extreme_edge_max']}, enforce_min_raw={sc['enforce_min_raw']})."
    )
    lines.append("")
    lines.append(f"- **n = {sc['n']}**, cohort verdict: `{sc['verdict']}`")
    if sc["n"]:
        lines.append(
            f"- realized WR {_fmt(sc['win_rate'])} "
            f"(Wilson 95% [{_fmt(sc['wilson_lo'])}, {_fmt(sc['wilson_hi'])}]) "
            f"vs breakeven ask {_fmt(sc['avg_ask'])}"
        )
        lines.append(
            f"- edge over breakeven {_fmt(sc['edge_over_breakeven'], pct=True)}, "
            f"counterfactual taker ROI {_fmt(sc['taker_roi'], pct=True)}, "
            f"limit ROI {_fmt(sc['limit_roi'], pct=True)}"
        )
    lines.append("")

    # Per-split high-FV regime
    lines.append("## High-FV band (raw >= enforce_min_raw) realized WR by split")
    lines.append("")
    lines.append("| split | n | realized WR | avg ask | taker ROI |")
    lines.append("|---|---|---|---|---|")
    for split, st in report["high_fv_band_by_split"].items():
        lines.append(
            f"| {split} | {st['n']} | {_fmt(st['win_rate'])} | {_fmt(st['avg_ask'])} | "
            f"{_fmt(st['taker_roi'], pct=True)} |"
        )
    lines.append("")

    # Recommendation grid
    lines.append("## enforce_min_raw recommendation grid (at production min_edge)")
    lines.append("")
    lines.append("| enforce_min_raw | n admitted | realized WR | avg ask | total taker units | taker ROI | Wilson LB |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in rec["by_threshold"]:
        lines.append(
            "| {emr:.2f}{star} | {n} | {wr} | {ask} | {tot} | {roi} | {lo} |".format(
                emr=r["enforce_min_raw"],
                star=" *" if abs(r["enforce_min_raw"] - rec["recommended_enforce_min_raw"]) < 1e-9 else "",
                n=r["n_admitted"],
                wr=_fmt(r["win_rate"]),
                ask=_fmt(r["avg_ask"]),
                tot=_fmt(r["total_taker_units"], nd=2),
                roi=_fmt(r["taker_roi"], pct=True),
                lo=_fmt(r["wilson_lo"]),
            )
        )
    lines.append("")
    lines.append("`*` = recommended. `enforce_min_raw >= 1.01` ⇒ calibration effectively off.")
    lines.append("")

    # Full scenario grid
    lines.append("## Gate scenario sweep (enforce_min_raw x min_edge)")
    lines.append("")
    lines.append("| enforce_min_raw | min_edge | n pass | realized WR | avg ask | taker ROI | Wilson LB |")
    lines.append("|---|---|---|---|---|---|---|")
    for s in report["gate_scenarios"]:
        lines.append(
            "| {emr:.2f} | {me:.2f} | {n} | {wr} | {ask} | {roi} | {lo} |".format(
                emr=s["enforce_min_raw"],
                me=s["min_edge"],
                n=s["n"],
                wr=_fmt(s["win_rate"]),
                ask=_fmt(s["avg_ask"]),
                roi=_fmt(s["taker_roi"], pct=True),
                lo=_fmt(s["wilson_lo"]),
            )
        )
    lines.append("")
    lines.append(
        "_Descriptive over a small recent labeled sample. Not a walk-forward "
        "promotion certificate; feed the recommendation into the manual "
        "`--prob-calibration-enforce-min-raw` decision._"
    )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _parse_float_list(text: str) -> List[float]:
    return [float(x.strip()) for x in text.split(",") if x.strip()]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--family-table", type=Path, default=DEFAULT_FAMILY_TABLE)
    p.add_argument("--calibration-artifact", type=Path, default=DEFAULT_CALIBRATION_ARTIFACT)
    p.add_argument("--model-family", default=DEFAULT_FAMILY)
    p.add_argument("--enforce-min-raw", type=float, default=DEFAULT_ENFORCE_MIN_RAW)
    p.add_argument("--min-edge", type=float, default=DEFAULT_MIN_EDGE)
    p.add_argument("--extreme-edge-max", type=float, default=DEFAULT_EXTREME_EDGE_MAX)
    p.add_argument(
        "--enforce-min-raw-sweep",
        type=str,
        default=",".join(str(x) for x in DEFAULT_ENFORCE_MIN_RAW_SWEEP),
    )
    p.add_argument(
        "--min-edge-sweep",
        type=str,
        default=",".join(str(x) for x in DEFAULT_MIN_EDGE_SWEEP),
    )
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--output-stem", type=str, default=DEFAULT_OUTPUT_STEM)
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if not args.family_table.exists():
        print(f"ERROR: family table not found: {args.family_table}", file=sys.stderr)
        return 2
    if not args.calibration_artifact.exists():
        print(f"ERROR: calibration artifact not found: {args.calibration_artifact}", file=sys.stderr)
        return 2

    rows = load_candidates(args.family_table)
    calibrator = ProbabilityCalibrator.from_path(args.calibration_artifact)

    report = build_report(
        rows,
        calibrator,
        model_family=args.model_family,
        enforce_min_raw=args.enforce_min_raw,
        min_edge=args.min_edge,
        extreme_edge_max=args.extreme_edge_max,
        raw_fv_bands=DEFAULT_RAW_FV_BANDS,
        enforce_min_raw_sweep=_parse_float_list(args.enforce_min_raw_sweep),
        min_edge_sweep=_parse_float_list(args.min_edge_sweep),
        calibration_artifact=str(args.calibration_artifact),
        family_table=str(args.family_table),
    )

    args.output_root.mkdir(parents=True, exist_ok=True)
    json_path = args.output_root / f"{args.output_stem}.json"
    md_path = args.output_root / f"{args.output_stem}.md"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(report))

    v = report["verdict"]
    print(f"[calibration_edge_shaving] verdict={v['label']} "
          f"recommended_enforce_min_raw={report['recommendation']['recommended_enforce_min_raw']:.2f}")
    print(f"  wrote {json_path}")
    print(f"  wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
