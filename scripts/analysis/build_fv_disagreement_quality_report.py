#!/usr/bin/env python3
"""
Build FV-vs-market disagreement quality diagnostics.

This report answers the practical calibration question behind market anchoring:
when the independent fair-value model disagrees with the market, is that
disagreement useful?

The market is treated as the benchmark to beat, not the source of truth. For
each labeled calibration opportunity, the report compares raw runtime FV
against a market anchor and records:

  - FV minus market
  - row-level Brier/logloss gain over market
  - direction correctness of the disagreement
  - CLV / late-price enrichment when available
  - realized live ROI enrichment when available
  - Stage-1 support / trust metadata

Outputs:
  data/analysis_output/fv_disagreement_quality/
    fv_disagreement_quality_summary.json
    fv_disagreement_quality_summary.md
    fv_disagreement_quality_rows.jsonl
    fv_disagreement_quality_rows.csv
    fv_disagreement_quality_buckets.jsonl
    fv_disagreement_quality_buckets.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CALIBRATION_TABLE = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "calibration_opportunity_training"
    / "calibration_opportunity_training_table.jsonl"
)
DEFAULT_CLV_ROWS = PROJECT_DIR / "data" / "analysis_output" / "clv" / "clv_rows.jsonl"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "fv_disagreement_quality"
DEFAULT_OUTPUT_STEM = "fv_disagreement_quality"

SCORE_EVENT_TRANSITION = "score_event_transition"
NO_SCORE_DRIFT = "no_score_drift"
KNOWN_FAMILIES = (SCORE_EVENT_TRANSITION, NO_SCORE_DRIFT)

ROW_COLUMNS = [
    "schema_version",
    "row_id",
    "mode",
    "session_date",
    "candidate_id",
    "bet_id",
    "game_pk",
    "away_abbrev",
    "home_abbrev",
    "line",
    "side",
    "family",
    "decision",
    "decision_reason",
    "anchor_price_source",
    "market_probability",
    "market_mid_no_vig",
    "decision_ask",
    "fair_value",
    "fair_value_calibrated",
    "fv_minus_market",
    "abs_fv_minus_market",
    "fv_gap_bucket",
    "disagreement_direction",
    "is_disagreement",
    "label_over_win",
    "market_brier",
    "fv_brier",
    "brier_gain_vs_market",
    "market_logloss",
    "fv_logloss",
    "logloss_gain_vs_market",
    "fv_direction_correct",
    "taker_profit_units",
    "limit_profit_units",
    "clv_match_source",
    "has_late_price",
    "late_mid",
    "clv_mid_vs_entry",
    "clv_mid_vs_execution",
    "realized_roi",
    "realized_profit_usdc",
    "fill_cost_usdc",
    "stage1_trust_weight",
    "stage1_effective_n",
    "stage1_trust_bucket",
    "stage1_effective_n_bucket",
    "ask_bucket",
    "edge_bucket",
    "current_state_value_edge",
    "current_state_edge_bucket",
    "shadow_phantom_risk_score",
    "shadow_phantom_risk_bucket",
    "current_phantom_combo_bucket",
    "inning",
    "inning_bucket",
    "runs_needed",
    "runs_needed_bucket",
    "home_skip_bottom9_risk",
    "home_skip_bottom9_risk_bucket",
]

BUCKET_COLUMNS = [
    "schema_version",
    "bucket_scope",
    "bucket_dimension",
    "bucket_value",
    "family",
    "rows",
    "labeled_rows",
    "late_price_rows",
    "realized_roi_rows",
    "win_rate",
    "mean_market_probability",
    "mean_fair_value",
    "mean_fv_minus_market",
    "mean_abs_fv_minus_market",
    "mean_brier_gain_vs_market",
    "mean_logloss_gain_vs_market",
    "brier_market",
    "brier_fv",
    "logloss_market",
    "logloss_fv",
    "fv_direction_correct_rate",
    "mean_clv_mid_vs_entry",
    "positive_clv_rate_vs_entry",
    "mean_clv_mid_vs_execution",
    "mean_realized_roi",
    "profit_usdc",
    "cost_usdc",
    "roi_on_cost",
    "mean_taker_profit_units",
    "mean_limit_profit_units",
    "mean_stage1_trust_weight",
    "median_stage1_effective_n",
    "evidence_score",
]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build FV disagreement quality diagnostics.")
    p.add_argument("--calibration-table", type=Path, default=DEFAULT_CALIBRATION_TABLE)
    p.add_argument("--clv-rows", type=Path, default=DEFAULT_CLV_ROWS)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--output-stem", type=str, default=DEFAULT_OUTPUT_STEM)
    p.add_argument("--mode", choices=["live", "paper", "both"], default="live")
    p.add_argument("--family", choices=["all", *KNOWN_FAMILIES], default="all")
    p.add_argument("--min-date", type=str, default="")
    p.add_argument("--max-date", type=str, default="")
    p.add_argument(
        "--market-anchor",
        choices=["ask", "mid_no_vig", "mid_no_vig_or_ask"],
        default="ask",
        help="Market benchmark for calibration gain. Default matches the model maturity report.",
    )
    p.add_argument(
        "--min-abs-disagreement",
        type=float,
        default=0.03,
        help="Minimum absolute FV-market gap counted as a meaningful disagreement.",
    )
    p.add_argument("--min-bucket-rows", type=int, default=10)
    p.add_argument("--strict", action="store_true")
    return p.parse_args(argv)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _clip_prob(value: Any) -> Optional[float]:
    prob = _safe_float(value)
    if prob is None or not 0.0 < prob < 1.0:
        return None
    return min(max(prob, 1e-6), 1.0 - 1e-6)


def _logloss_one(label: int, prob: float) -> float:
    prob = min(max(float(prob), 1e-6), 1.0 - 1e-6)
    return -(label * math.log(prob) + (1 - label) * math.log(1.0 - prob))


def _label(row: Mapping[str, Any]) -> Optional[int]:
    value = row.get("target_over_win")
    if value in (None, ""):
        value = row.get("target_win")
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)) and int(value) in (0, 1):
        return int(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "win", "won"}:
        return 1
    if text in {"0", "false", "no", "n", "loss", "lost"}:
        return 0
    return None


def _date_value(row: Mapping[str, Any]) -> str:
    raw = str(row.get("session_date") or row.get("date") or "").strip()
    if len(raw) >= 10:
        return raw[:10]
    ts = str(row.get("ts") or row.get("recorded_at") or "").strip()
    return ts[:10] if len(ts) >= 10 else ""


def _family(row: Mapping[str, Any]) -> str:
    value = str(row.get("signal_model_family") or row.get("state_value_strategy") or "").strip()
    return value or "unknown"


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _read_table(path: Path, warnings: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    if warnings is None:
        warnings = []
    if not path.exists():
        warnings.append(f"missing input path: {path}")
        return []
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as f:
            return [dict(row) for row in csv.DictReader(f)]
    rows: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line_no, raw in enumerate(f, 1):
            text = raw.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                warnings.append(f"bad JSON {path}:{line_no}: {exc}")
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _filter_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    mode: str,
    family: str,
    min_date: str,
    max_date: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        row_mode = str(row.get("mode") or "")
        if mode != "both" and row_mode and row_mode != mode:
            continue
        row_family = _family(row)
        if family != "all" and row_family != family:
            continue
        date = _date_value(row)
        if min_date and date and date < min_date:
            continue
        if max_date and date and date > max_date:
            continue
        out.append(row)
    return out


def _market_probability(row: Mapping[str, Any], anchor: str) -> Tuple[Optional[float], str]:
    ask = _clip_prob(row.get("decision_ask"))
    mid_no_vig = _clip_prob(row.get("decision_market_mid_no_vig"))
    if anchor == "mid_no_vig":
        return mid_no_vig, "decision_market_mid_no_vig" if mid_no_vig is not None else "missing"
    if anchor == "mid_no_vig_or_ask":
        if mid_no_vig is not None:
            return mid_no_vig, "decision_market_mid_no_vig"
        return ask, "decision_ask_fallback" if ask is not None else "missing"
    return ask, "decision_ask" if ask is not None else "missing"


def _support_values(row: Mapping[str, Any], family: str) -> Tuple[Optional[float], Optional[float]]:
    if family == NO_SCORE_DRIFT:
        trust = _safe_float(
            _coalesce(
                row.get("current_state_value_stage1_trust_weight"),
                row.get("inferred_state_stage1_trust_weight"),
            )
        )
        n_eff = _safe_float(
            _coalesce(
                row.get("current_state_value_effective_n_proxy"),
                row.get("current_state_value_effective_n"),
                row.get("inferred_state_effective_n_proxy"),
                row.get("inferred_state_effective_n"),
            )
        )
    else:
        trust = _safe_float(
            _coalesce(
                row.get("inferred_state_stage1_trust_weight"),
                row.get("current_state_value_stage1_trust_weight"),
            )
        )
        n_eff = _safe_float(
            _coalesce(
                row.get("inferred_state_effective_n_proxy"),
                row.get("inferred_state_effective_n"),
                row.get("current_state_value_effective_n_proxy"),
                row.get("current_state_value_effective_n"),
            )
        )
    return trust, n_eff


def _price_bucket(value: Any) -> str:
    price = _safe_float(value)
    if price is None:
        return "missing"
    if price < 0.40:
        return "<0.40"
    if price < 0.55:
        return "0.40-0.55"
    if price < 0.70:
        return "0.55-0.70"
    if price < 0.85:
        return "0.70-0.85"
    return ">=0.85"


def _edge_bucket(value: Any) -> str:
    edge = _safe_float(value)
    if edge is None:
        return "missing"
    if edge < -0.05:
        return "<-0.05"
    if edge < 0.0:
        return "-0.05-0"
    if edge < 0.03:
        return "0-0.03"
    if edge < 0.05:
        return "0.03-0.05"
    if edge < 0.08:
        return "0.05-0.08"
    if edge < 0.12:
        return "0.08-0.12"
    if edge < 0.20:
        return "0.12-0.20"
    return ">=0.20"


def _gap_bucket(value: Any, *, threshold: float) -> str:
    gap = _safe_float(value)
    if gap is None:
        return "missing"
    if abs(gap) < threshold:
        return f"flat_abs<{threshold:.2f}"
    if gap < -0.15:
        return "<-0.15"
    if gap < -0.10:
        return "-0.15..-0.10"
    if gap < -0.05:
        return "-0.10..-0.05"
    if gap < -threshold:
        return f"-0.05..-{threshold:.2f}"
    if gap < 0.05:
        return f"{threshold:.2f}..0.05"
    if gap < 0.10:
        return "0.05..0.10"
    if gap < 0.15:
        return "0.10..0.15"
    return ">=0.15"


def _trust_bucket(value: Any) -> str:
    trust = _safe_float(value)
    if trust is None:
        return "missing"
    if trust < 0.25:
        return "<0.25"
    if trust < 0.50:
        return "0.25-0.50"
    if trust < 0.75:
        return "0.50-0.75"
    return ">=0.75"


def _effective_n_bucket(value: Any) -> str:
    n_eff = _safe_float(value)
    if n_eff is None:
        return "missing"
    if n_eff < 25:
        return "<25"
    if n_eff < 75:
        return "25-74"
    if n_eff < 150:
        return "75-149"
    return ">=150"


def _inning_bucket(value: Any) -> str:
    inning = _safe_int(value)
    if inning is None:
        return "missing"
    if inning <= 4:
        return "<=4"
    if inning <= 6:
        return "5-6"
    if inning <= 8:
        return "7-8"
    return "9+"


def _runs_needed_bucket(value: Any) -> str:
    runs = _safe_float(value)
    if runs is None:
        return "missing"
    if runs <= 1.5:
        return "<=1.5"
    if runs <= 2.5:
        return "1.5-2.5"
    if runs <= 3.5:
        return "2.5-3.5"
    return ">3.5"


def _home_skip_bucket(value: Any) -> str:
    risk = _safe_float(value)
    if risk is None:
        return "missing"
    if risk <= 0:
        return "none"
    if risk < 0.25:
        return "low"
    if risk < 0.50:
        return "medium"
    return "high"


def _key_token(value: Any) -> str:
    return "" if value is None or value == "" else str(value)


def _state_key(row: Mapping[str, Any]) -> Tuple[Any, ...]:
    decision_ask = _safe_float(row.get("decision_ask") or row.get("entry_price"))
    return (
        _key_token(row.get("mode")),
        _date_value(row),
        _key_token(row.get("game_pk")),
        _key_token(row.get("line")),
        _key_token(row.get("inning")),
        _key_token(row.get("inning_state")),
        _key_token(row.get("outs")),
        _key_token(row.get("runners_on")),
        _key_token(row.get("current_total")),
        None if decision_ask is None else round(decision_ask, 3),
    )


def _better_clv_row(existing: Optional[Mapping[str, Any]], candidate: Mapping[str, Any]) -> bool:
    if existing is None:
        return True
    candidate_has_late = bool(candidate.get("has_late_price"))
    existing_has_late = bool(existing.get("has_late_price"))
    if candidate_has_late != existing_has_late:
        return candidate_has_late
    candidate_has_roi = _safe_float(candidate.get("realized_roi")) is not None
    existing_has_roi = _safe_float(existing.get("realized_roi")) is not None
    if candidate_has_roi != existing_has_roi:
        return candidate_has_roi
    return False


def _build_clv_indexes(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[Any, Dict[str, Any]]]:
    by_candidate: Dict[Any, Dict[str, Any]] = {}
    by_bet: Dict[Any, Dict[str, Any]] = {}
    by_state: Dict[Any, Dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        candidate_id = row.get("candidate_id")
        if candidate_id:
            key = str(candidate_id)
            if _better_clv_row(by_candidate.get(key), row):
                by_candidate[key] = row
        bet_id = row.get("bet_id")
        if bet_id:
            key = str(bet_id)
            if _better_clv_row(by_bet.get(key), row):
                by_bet[key] = row
        key = _state_key(row)
        if any(key) and _better_clv_row(by_state.get(key), row):
            by_state[key] = row
    return {"candidate": by_candidate, "bet": by_bet, "state": by_state}


def _match_clv(row: Mapping[str, Any], indexes: Mapping[str, Mapping[Any, Dict[str, Any]]]) -> Tuple[Optional[Dict[str, Any]], str]:
    best: Optional[Dict[str, Any]] = None
    best_source = "none"
    candidate_id = row.get("candidate_id")
    if candidate_id:
        match = indexes.get("candidate", {}).get(str(candidate_id))
        if match and _better_clv_row(best, match):
            best = match
            best_source = "candidate_id"
    bet_id = row.get("bet_id")
    if bet_id:
        match = indexes.get("bet", {}).get(str(bet_id))
        if match and _better_clv_row(best, match):
            best = match
            best_source = "bet_id"
    match = indexes.get("state", {}).get(_state_key(row))
    if match and _better_clv_row(best, match):
        best = match
        best_source = "state_key"
    return best, best_source


def _build_quality_row(
    row: Mapping[str, Any],
    *,
    clv_indexes: Mapping[str, Mapping[Any, Dict[str, Any]]],
    market_anchor: str,
    min_abs_disagreement: float,
) -> Optional[Dict[str, Any]]:
    family = _family(row)
    market, market_source = _market_probability(row, market_anchor)
    fv = _clip_prob(row.get("fair_value"))
    if market is None or fv is None:
        return None
    label = _label(row)
    gap = fv - market
    abs_gap = abs(gap)
    if abs_gap >= min_abs_disagreement:
        direction = "model_above_market" if gap > 0 else "model_below_market"
    else:
        direction = "flat"

    market_brier: Optional[float] = None
    fv_brier: Optional[float] = None
    brier_gain: Optional[float] = None
    market_logloss: Optional[float] = None
    fv_logloss: Optional[float] = None
    logloss_gain: Optional[float] = None
    direction_correct: Optional[bool] = None
    if label is not None:
        market_brier = (market - label) ** 2
        fv_brier = (fv - label) ** 2
        brier_gain = market_brier - fv_brier
        market_logloss = _logloss_one(label, market)
        fv_logloss = _logloss_one(label, fv)
        logloss_gain = market_logloss - fv_logloss
        if direction == "model_above_market":
            direction_correct = label == 1
        elif direction == "model_below_market":
            direction_correct = label == 0

    trust, n_eff = _support_values(row, family)
    clv_row, clv_source = _match_clv(row, clv_indexes)
    decision_ask = _clip_prob(row.get("decision_ask"))
    current_edge = _safe_float(row.get("current_state_value_edge"))
    phantom_score = _safe_float(row.get("shadow_phantom_risk_score"))
    runs_needed = _safe_float(row.get("runs_needed"))
    home_skip = _safe_float(row.get("home_skip_bottom9_risk"))

    row_id = row.get("candidate_id") or row.get("bet_id")
    if not row_id:
        row_id = "|".join(str(part) for part in _state_key(row))

    return {
        "schema_version": 1,
        "row_id": str(row_id),
        "mode": row.get("mode"),
        "session_date": _date_value(row),
        "candidate_id": row.get("candidate_id"),
        "bet_id": row.get("bet_id"),
        "game_pk": row.get("game_pk"),
        "away_abbrev": row.get("away_abbrev"),
        "home_abbrev": row.get("home_abbrev"),
        "line": row.get("line"),
        "side": row.get("side") or "over",
        "family": family,
        "decision": row.get("decision"),
        "decision_reason": row.get("decision_reason"),
        "anchor_price_source": market_source,
        "market_probability": _round(market),
        "market_mid_no_vig": _round(_clip_prob(row.get("decision_market_mid_no_vig"))),
        "decision_ask": _round(decision_ask),
        "fair_value": _round(fv),
        "fair_value_calibrated": _round(_clip_prob(row.get("fair_value_calibrated"))),
        "fv_minus_market": _round(gap),
        "abs_fv_minus_market": _round(abs_gap),
        "fv_gap_bucket": _gap_bucket(gap, threshold=min_abs_disagreement),
        "disagreement_direction": direction,
        "is_disagreement": bool(abs_gap >= min_abs_disagreement),
        "label_over_win": label,
        "market_brier": _round(market_brier),
        "fv_brier": _round(fv_brier),
        "brier_gain_vs_market": _round(brier_gain),
        "market_logloss": _round(market_logloss),
        "fv_logloss": _round(fv_logloss),
        "logloss_gain_vs_market": _round(logloss_gain),
        "fv_direction_correct": direction_correct,
        "taker_profit_units": _round(_safe_float(row.get("target_taker_profit_units"))),
        "limit_profit_units": _round(_safe_float(row.get("target_limit_profit_units"))),
        "clv_match_source": clv_source,
        "has_late_price": bool(clv_row and clv_row.get("has_late_price")),
        "late_mid": _round(_safe_float(clv_row.get("late_mid")) if clv_row else None),
        "clv_mid_vs_entry": _round(_safe_float(clv_row.get("clv_mid_vs_entry")) if clv_row else None),
        "clv_mid_vs_execution": _round(_safe_float(clv_row.get("clv_mid_vs_execution")) if clv_row else None),
        "realized_roi": _round(_safe_float(clv_row.get("realized_roi")) if clv_row else None),
        "realized_profit_usdc": _round(_safe_float(clv_row.get("realized_profit_usdc")) if clv_row else None),
        "fill_cost_usdc": _round(_safe_float(clv_row.get("fill_cost_usdc")) if clv_row else None),
        "stage1_trust_weight": _round(trust),
        "stage1_effective_n": _round(n_eff),
        "stage1_trust_bucket": _trust_bucket(trust),
        "stage1_effective_n_bucket": _effective_n_bucket(n_eff),
        "ask_bucket": row.get("ask_bucket") or _price_bucket(decision_ask),
        "edge_bucket": row.get("edge_bucket") or _edge_bucket(row.get("edge")),
        "current_state_value_edge": _round(current_edge),
        "current_state_edge_bucket": row.get("shadow_current_state_edge_bucket") or _edge_bucket(current_edge),
        "shadow_phantom_risk_score": _round(phantom_score),
        "shadow_phantom_risk_bucket": (
            row.get("shadow_phantom_risk_bucket")
            or row.get("shadow_phantom_risk_band")
            or row.get("phantom_risk_band")
            or "missing"
        ),
        "current_phantom_combo_bucket": row.get("shadow_current_phantom_combo_bucket") or "missing",
        "inning": row.get("inning"),
        "inning_bucket": row.get("shadow_inning_bucket") or _inning_bucket(row.get("inning")),
        "runs_needed": _round(runs_needed),
        "runs_needed_bucket": row.get("runs_needed_bucket") or _runs_needed_bucket(runs_needed),
        "home_skip_bottom9_risk": _round(home_skip),
        "home_skip_bottom9_risk_bucket": (
            row.get("shadow_home_skip_bottom9_risk_bucket") or _home_skip_bucket(home_skip)
        ),
    }


def build_quality_rows(
    *,
    calibration_rows: Sequence[Mapping[str, Any]],
    clv_rows: Sequence[Mapping[str, Any]],
    mode: str,
    family: str,
    min_date: str,
    max_date: str,
    market_anchor: str,
    min_abs_disagreement: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    filtered = _filter_rows(
        [dict(row) for row in calibration_rows],
        mode=mode,
        family=family,
        min_date=min_date,
        max_date=max_date,
    )
    clv_indexes = _build_clv_indexes(clv_rows)
    out: List[Dict[str, Any]] = []
    skipped_missing_prob = 0
    for row in filtered:
        quality_row = _build_quality_row(
            row,
            clv_indexes=clv_indexes,
            market_anchor=market_anchor,
            min_abs_disagreement=min_abs_disagreement,
        )
        if quality_row is None:
            skipped_missing_prob += 1
            continue
        out.append(quality_row)
    out.sort(
        key=lambda r: (
            str(r.get("session_date") or ""),
            str(r.get("family") or ""),
            str(r.get("game_pk") or ""),
            str(r.get("line") or ""),
            str(r.get("row_id") or ""),
        )
    )
    stats = {
        "calibration_rows_loaded": len(calibration_rows),
        "calibration_rows_filtered": len(filtered),
        "clv_rows_loaded": len(clv_rows),
        "quality_rows": len(out),
        "skipped_missing_market_or_fv": skipped_missing_prob,
        "clv_candidate_index_rows": len(clv_indexes.get("candidate", {})),
        "clv_bet_index_rows": len(clv_indexes.get("bet", {})),
        "clv_state_index_rows": len(clv_indexes.get("state", {})),
    }
    return out, stats


def _mean(values: Iterable[Any]) -> Optional[float]:
    vals = [_safe_float(v) for v in values]
    vals = [v for v in vals if v is not None]
    return statistics.mean(vals) if vals else None


def _median(values: Iterable[Any]) -> Optional[float]:
    vals = [_safe_float(v) for v in values]
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def _rate(values: Iterable[Any]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(1 for v in vals if bool(v)) / len(vals)


def _sum_float(values: Iterable[Any]) -> Optional[float]:
    vals = [_safe_float(v) for v in values]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def _bucket_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    bucket_scope: str,
    bucket_dimension: str,
    bucket_value: str,
    family: str,
) -> Dict[str, Any]:
    labeled = [r for r in rows if _safe_int(r.get("label_over_win")) in (0, 1)]
    labels = [_safe_int(r.get("label_over_win")) for r in labeled]
    labels = [int(v) for v in labels if v is not None]
    market_brier = _mean(r.get("market_brier") for r in labeled)
    fv_brier = _mean(r.get("fv_brier") for r in labeled)
    market_logloss = _mean(r.get("market_logloss") for r in labeled)
    fv_logloss = _mean(r.get("fv_logloss") for r in labeled)
    profits = _sum_float(r.get("realized_profit_usdc") for r in rows)
    costs = _sum_float(r.get("fill_cost_usdc") for r in rows)
    brier_gain = _mean(r.get("brier_gain_vs_market") for r in labeled)
    clv = _mean(r.get("clv_mid_vs_entry") for r in rows)
    roi_on_cost = profits / costs if profits is not None and costs and costs > 0 else None
    n = len(labeled)
    sample_weight = math.sqrt(n / (n + 25.0)) if n > 0 else 0.0
    # This is only a ranking aid. The component metrics remain exposed and
    # should drive decisions; the score prevents tiny buckets from floating to
    # the top on one lucky row.
    evidence_score = None
    if brier_gain is not None:
        evidence_score = brier_gain * sample_weight
        if clv is not None:
            evidence_score += 0.20 * clv * sample_weight
        if roi_on_cost is not None:
            evidence_score += 0.05 * roi_on_cost * sample_weight

    return {
        "schema_version": 1,
        "bucket_scope": bucket_scope,
        "bucket_dimension": bucket_dimension,
        "bucket_value": bucket_value,
        "family": family,
        "rows": len(rows),
        "labeled_rows": len(labeled),
        "late_price_rows": sum(1 for r in rows if bool(r.get("has_late_price"))),
        "realized_roi_rows": sum(1 for r in rows if _safe_float(r.get("realized_roi")) is not None),
        "win_rate": _round(sum(labels) / len(labels) if labels else None),
        "mean_market_probability": _round(_mean(r.get("market_probability") for r in rows)),
        "mean_fair_value": _round(_mean(r.get("fair_value") for r in rows)),
        "mean_fv_minus_market": _round(_mean(r.get("fv_minus_market") for r in rows)),
        "mean_abs_fv_minus_market": _round(_mean(r.get("abs_fv_minus_market") for r in rows)),
        "mean_brier_gain_vs_market": _round(brier_gain),
        "mean_logloss_gain_vs_market": _round(_mean(r.get("logloss_gain_vs_market") for r in labeled)),
        "brier_market": _round(market_brier),
        "brier_fv": _round(fv_brier),
        "logloss_market": _round(market_logloss),
        "logloss_fv": _round(fv_logloss),
        "fv_direction_correct_rate": _round(_rate(r.get("fv_direction_correct") for r in rows)),
        "mean_clv_mid_vs_entry": _round(clv),
        "positive_clv_rate_vs_entry": _round(
            _rate(
                (_safe_float(r.get("clv_mid_vs_entry")) or 0.0) > 0
                for r in rows
                if _safe_float(r.get("clv_mid_vs_entry")) is not None
            )
        ),
        "mean_clv_mid_vs_execution": _round(_mean(r.get("clv_mid_vs_execution") for r in rows)),
        "mean_realized_roi": _round(_mean(r.get("realized_roi") for r in rows)),
        "profit_usdc": _round(profits),
        "cost_usdc": _round(costs),
        "roi_on_cost": _round(roi_on_cost),
        "mean_taker_profit_units": _round(_mean(r.get("taker_profit_units") for r in rows)),
        "mean_limit_profit_units": _round(_mean(r.get("limit_profit_units") for r in rows)),
        "mean_stage1_trust_weight": _round(_mean(r.get("stage1_trust_weight") for r in rows)),
        "median_stage1_effective_n": _round(_median(r.get("stage1_effective_n") for r in rows)),
        "evidence_score": _round(evidence_score),
    }


def _add_bucket(
    bucket_rows: List[Dict[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    *,
    scope: str,
    dimension: str,
    value: str,
    family: str,
    min_bucket_rows: int,
) -> None:
    if len(rows) < min_bucket_rows:
        return
    bucket_rows.append(
        _bucket_summary(
            rows,
            bucket_scope=scope,
            bucket_dimension=dimension,
            bucket_value=value,
            family=family,
        )
    )


def _compound(*values: Any) -> str:
    return "|".join(str(v if v not in (None, "") else "missing") for v in values)


def build_bucket_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    min_bucket_rows: int,
) -> List[Dict[str, Any]]:
    disagreement_rows = [r for r in rows if bool(r.get("is_disagreement"))]
    bucket_rows: List[Dict[str, Any]] = []
    families = sorted({str(r.get("family") or "unknown") for r in rows})
    dimensions = {
        "fv_gap_bucket": lambda r: r.get("fv_gap_bucket"),
        "disagreement_direction": lambda r: r.get("disagreement_direction"),
        "ask_bucket_x_gap": lambda r: _compound(r.get("ask_bucket"), r.get("fv_gap_bucket")),
        "support_trust_x_gap": lambda r: _compound(r.get("stage1_trust_bucket"), r.get("fv_gap_bucket")),
        "support_n_x_gap": lambda r: _compound(r.get("stage1_effective_n_bucket"), r.get("fv_gap_bucket")),
        "current_state_edge_x_phantom": lambda r: _compound(
            r.get("current_state_edge_bucket"), r.get("shadow_phantom_risk_bucket")
        ),
        "inning_x_runs_needed": lambda r: _compound(r.get("inning_bucket"), r.get("runs_needed_bucket")),
        "home_skip_bottom9_x_gap": lambda r: _compound(
            r.get("home_skip_bottom9_risk_bucket"), r.get("fv_gap_bucket")
        ),
        "decision_reason_x_gap": lambda r: _compound(r.get("decision_reason"), r.get("fv_gap_bucket")),
    }

    for family in families:
        family_rows = [r for r in rows if str(r.get("family") or "unknown") == family]
        _add_bucket(
            bucket_rows,
            family_rows,
            scope="all",
            dimension="family_overall",
            value=family,
            family=family,
            min_bucket_rows=min_bucket_rows,
        )
        family_disagreements = [
            r for r in disagreement_rows if str(r.get("family") or "unknown") == family
        ]
        _add_bucket(
            bucket_rows,
            family_disagreements,
            scope="disagreement_only",
            dimension="family_disagreement_overall",
            value=family,
            family=family,
            min_bucket_rows=min_bucket_rows,
        )
        for dimension, key_fn in dimensions.items():
            grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
            for row in family_disagreements:
                grouped[str(key_fn(row) or "missing")].append(row)
            for value, group_rows in sorted(grouped.items()):
                _add_bucket(
                    bucket_rows,
                    group_rows,
                    scope="disagreement_only",
                    dimension=dimension,
                    value=value,
                    family=family,
                    min_bucket_rows=min_bucket_rows,
                )

    bucket_rows.sort(
        key=lambda r: (
            str(r.get("family") or ""),
            str(r.get("bucket_scope") or ""),
            str(r.get("bucket_dimension") or ""),
            str(r.get("bucket_value") or ""),
        )
    )
    return bucket_rows


def _top_buckets(
    bucket_rows: Sequence[Mapping[str, Any]],
    *,
    family: Optional[str] = None,
    limit: int = 12,
    reverse: bool = True,
) -> List[Dict[str, Any]]:
    rows = [
        dict(row)
        for row in bucket_rows
        if row.get("bucket_scope") == "disagreement_only"
        and row.get("bucket_dimension") != "family_disagreement_overall"
        and _safe_float(row.get("mean_brier_gain_vs_market")) is not None
    ]
    if family is not None:
        rows = [row for row in rows if row.get("family") == family]
    rows.sort(
        key=lambda r: (
            _safe_float(r.get("mean_brier_gain_vs_market")) or 0.0,
            _safe_float(r.get("mean_clv_mid_vs_entry")) or -999.0,
            _safe_float(r.get("evidence_score")) or -999.0,
            _safe_int(r.get("labeled_rows")) or 0,
        ),
        reverse=reverse,
    )
    return rows[:limit]


def build_summary(
    rows: Sequence[Mapping[str, Any]],
    bucket_rows: Sequence[Mapping[str, Any]],
    *,
    config: Mapping[str, Any],
    load_stats: Mapping[str, Any],
    warnings: Sequence[str],
) -> Dict[str, Any]:
    disagreement_rows = [r for r in rows if bool(r.get("is_disagreement"))]
    by_family: Dict[str, Dict[str, Any]] = {}
    families = sorted({str(r.get("family") or "unknown") for r in rows})
    for family in families:
        family_rows = [r for r in rows if str(r.get("family") or "unknown") == family]
        family_disagreement = [r for r in family_rows if bool(r.get("is_disagreement"))]
        by_family[family] = {
            "all": _bucket_summary(
                family_rows,
                bucket_scope="all",
                bucket_dimension="family_overall",
                bucket_value=family,
                family=family,
            ),
            "disagreement_only": _bucket_summary(
                family_disagreement,
                bucket_scope="disagreement_only",
                bucket_dimension="family_disagreement_overall",
                bucket_value=family,
                family=family,
            )
            if family_disagreement
            else None,
            "top_helpful_buckets": _top_buckets(bucket_rows, family=family, limit=8, reverse=True),
            "top_harmful_buckets": _top_buckets(bucket_rows, family=family, limit=8, reverse=False),
        }

    return {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "description": (
            "FV disagreement quality report. Positive Brier/logloss gain means "
            "runtime FV was better calibrated than the selected market anchor."
        ),
        "config": dict(config),
        "load_stats": dict(load_stats),
        "row_counts": {
            "quality_rows": len(rows),
            "disagreement_rows": len(disagreement_rows),
            "rows_with_late_price": sum(1 for r in rows if bool(r.get("has_late_price"))),
            "disagreement_rows_with_late_price": sum(
                1 for r in disagreement_rows if bool(r.get("has_late_price"))
            ),
            "rows_with_realized_roi": sum(
                1 for r in rows if _safe_float(r.get("realized_roi")) is not None
            ),
            "bucket_rows": len(bucket_rows),
        },
        "overall": _bucket_summary(
            rows,
            bucket_scope="all",
            bucket_dimension="overall",
            bucket_value="overall",
            family="all",
        )
        if rows
        else None,
        "disagreement_only": _bucket_summary(
            disagreement_rows,
            bucket_scope="disagreement_only",
            bucket_dimension="overall",
            bucket_value="overall",
            family="all",
        )
        if disagreement_rows
        else None,
        "by_family": by_family,
        "top_helpful_buckets": _top_buckets(bucket_rows, limit=15, reverse=True),
        "top_harmful_buckets": _top_buckets(bucket_rows, limit=15, reverse=False),
        "warnings": list(warnings)[:200],
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(dict(row), sort_keys=True) + "\n")


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _bucket_table_rows(rows: Sequence[Mapping[str, Any]], limit: int = 12) -> List[str]:
    lines = [
        "| family | dimension | bucket | n | Brier gain | CLV entry | ROI | trust | score |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows[:limit]:
        lines.append(
            "| "
            f"{row.get('family')} | "
            f"{row.get('bucket_dimension')} | "
            f"{row.get('bucket_value')} | "
            f"{row.get('labeled_rows')} | "
            f"{_fmt(row.get('mean_brier_gain_vs_market'))} | "
            f"{_fmt(row.get('mean_clv_mid_vs_entry'))} | "
            f"{_fmt(row.get('roi_on_cost'))} | "
            f"{_fmt(row.get('mean_stage1_trust_weight'))} | "
            f"{_fmt(row.get('evidence_score'))} |"
        )
    return lines


def _write_markdown(path: Path, summary: Mapping[str, Any]) -> None:
    config = summary.get("config") or {}
    overall = summary.get("disagreement_only") or {}
    lines = [
        "# FV Disagreement Quality Report",
        "",
        f"Generated: {summary.get('generated_at_utc')}",
        "",
        (
            "This report asks whether runtime fair value adds information when "
            "it disagrees with the selected market anchor."
        ),
        "",
        "## Config",
        "",
        f"- Market anchor: `{config.get('market_anchor')}`",
        f"- Minimum absolute disagreement: `{config.get('min_abs_disagreement')}`",
        "",
        "## Disagreement Summary",
        "",
        f"- Rows: `{overall.get('rows')}`",
        f"- Win rate: `{overall.get('win_rate')}`",
        f"- Mean FV-market gap: `{overall.get('mean_fv_minus_market')}`",
        f"- Mean Brier gain vs market: `{overall.get('mean_brier_gain_vs_market')}`",
        f"- Mean logloss gain vs market: `{overall.get('mean_logloss_gain_vs_market')}`",
        f"- Mean CLV vs entry: `{overall.get('mean_clv_mid_vs_entry')}`",
        f"- ROI on realized filled cost: `{overall.get('roi_on_cost')}`",
        "",
        "## By Family",
        "",
        "| family | disagreement rows | win rate | Brier gain | CLV entry | ROI | trust |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for family, payload in (summary.get("by_family") or {}).items():
        item = (payload or {}).get("disagreement_only") or {}
        lines.append(
            f"| {family} | {item.get('rows')} | {item.get('win_rate')} | "
            f"{item.get('mean_brier_gain_vs_market')} | "
            f"{item.get('mean_clv_mid_vs_entry')} | {item.get('roi_on_cost')} | "
            f"{item.get('mean_stage1_trust_weight')} |"
        )
    lines.extend(["", "## Most Helpful Buckets", ""])
    lines.extend(_bucket_table_rows(summary.get("top_helpful_buckets") or [], limit=15))
    lines.extend(["", "## Most Harmful Buckets", ""])
    lines.extend(_bucket_table_rows(summary.get("top_harmful_buckets") or [], limit=15))
    lines.extend(
        [
            "",
            "Interpretation: positive Brier/logloss gain means FV was closer to the final outcome than market price. "
            "CLV uses the available late captured midpoint, not guaranteed true close.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    if args.min_abs_disagreement < 0:
        raise SystemExit("--min-abs-disagreement must be non-negative.")
    if args.min_bucket_rows < 1:
        raise SystemExit("--min-bucket-rows must be >= 1.")

    warnings: List[str] = []
    calibration_rows = _read_table(args.calibration_table, warnings)
    clv_rows = _read_table(args.clv_rows, warnings)
    rows, load_stats = build_quality_rows(
        calibration_rows=calibration_rows,
        clv_rows=clv_rows,
        mode=args.mode,
        family=args.family,
        min_date=args.min_date,
        max_date=args.max_date,
        market_anchor=args.market_anchor,
        min_abs_disagreement=args.min_abs_disagreement,
    )
    bucket_rows = build_bucket_rows(rows, min_bucket_rows=args.min_bucket_rows)
    config = {
        "calibration_table": str(args.calibration_table),
        "clv_rows": str(args.clv_rows),
        "mode": args.mode,
        "family": args.family,
        "min_date": args.min_date or None,
        "max_date": args.max_date or None,
        "market_anchor": args.market_anchor,
        "min_abs_disagreement": args.min_abs_disagreement,
        "min_bucket_rows": args.min_bucket_rows,
    }
    summary = build_summary(
        rows,
        bucket_rows,
        config=config,
        load_stats=load_stats,
        warnings=warnings,
    )
    if args.strict and not summary["row_counts"]["quality_rows"]:
        raise SystemExit("Strict mode failed: no FV disagreement quality rows.")

    args.output_root.mkdir(parents=True, exist_ok=True)
    rows_jsonl = args.output_root / f"{args.output_stem}_rows.jsonl"
    rows_csv = args.output_root / f"{args.output_stem}_rows.csv"
    buckets_jsonl = args.output_root / f"{args.output_stem}_buckets.jsonl"
    buckets_csv = args.output_root / f"{args.output_stem}_buckets.csv"
    summary_json = args.output_root / f"{args.output_stem}_summary.json"
    summary_md = args.output_root / f"{args.output_stem}_summary.md"
    _write_jsonl(rows_jsonl, rows)
    _write_csv(rows_csv, rows, ROW_COLUMNS)
    _write_jsonl(buckets_jsonl, bucket_rows)
    _write_csv(buckets_csv, bucket_rows, BUCKET_COLUMNS)
    _write_json(summary_json, summary)
    _write_markdown(summary_md, summary)
    print(f"Wrote {summary_json}")
    print(f"Wrote {rows_jsonl}")


if __name__ == "__main__":
    main()
