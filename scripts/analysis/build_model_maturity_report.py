#!/usr/bin/env python3
"""
Build a model-maturity/readiness report for the MLB Polymarket research loop.

The report is intentionally conservative. It summarizes family-specific
evidence from the broad calibration-opportunity table, compares model/FV
probabilities against the market-price baseline, and refuses to mark artifacts
as promotable until minimum sample-size and class-balance thresholds are met.

Inputs:
  data/analysis_output/calibration_opportunity_training/
    calibration_opportunity_training_table.jsonl

Outputs:
  data/analysis_output/model_maturity/model_maturity_report.json
  data/analysis_output/model_maturity/model_maturity_report.md
  data/analysis_output/model_maturity/<as_of_date>_model_maturity_report.json
  data/analysis_output/model_maturity/<as_of_date>_model_maturity_report.md
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_TABLE_PATH = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "calibration_opportunity_training"
    / "calibration_opportunity_training_table.jsonl"
)
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "model_maturity"
DEFAULT_OUTPUT_STEM = "model_maturity_report"

SCORE_EVENT_TRANSITION = "score_event_transition"
NO_SCORE_DRIFT = "no_score_drift"
KNOWN_FAMILIES = (SCORE_EVENT_TRANSITION, NO_SCORE_DRIFT)

COVERAGE_MIN_WARN = {
    "under_pair_available_rate": 0.50,
    "no_vig_market_rate": 0.50,
    "run_count_panel_rate": 0.50,
    "stage1_support_rate": 0.50,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        if not math.isfinite(out):
            return None
        return out
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def _label_value(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return 1 if value else 0
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        if value in (0, 1):
            return int(value)
        return None
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in {"1", "true", "yes", "y", "win", "won"}:
            return 1
        if norm in {"0", "false", "no", "n", "loss", "lost"}:
            return 0
    return None


def _clip_prob(value: Any) -> Optional[float]:
    prob = _safe_float(value)
    if prob is None:
        return None
    if not 0.0 < prob < 1.0:
        return None
    return min(max(prob, 1e-6), 1.0 - 1e-6)


def _has_value(row: Mapping[str, Any], key: str) -> bool:
    value = row.get(key)
    return value is not None and value != ""


def _valid_probability(row: Mapping[str, Any], key: str) -> bool:
    return _clip_prob(row.get(key)) is not None


def _logit(prob: float) -> float:
    prob = min(max(prob, 1e-6), 1.0 - 1e-6)
    return math.log(prob / (1.0 - prob))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _brier(labels: Sequence[int], probs: Sequence[float]) -> Optional[float]:
    if not labels:
        return None
    return sum((p - y) ** 2 for y, p in zip(labels, probs)) / len(labels)


def _logloss(labels: Sequence[int], probs: Sequence[float]) -> Optional[float]:
    if not labels:
        return None
    total = 0.0
    for y, p in zip(labels, probs):
        p = min(max(float(p), 1e-6), 1.0 - 1e-6)
        total += -(y * math.log(p) + (1 - y) * math.log(1.0 - p))
    return total / len(labels)


def _auc_pairwise(labels: Sequence[int], probs: Sequence[float]) -> Optional[float]:
    positives = [p for y, p in zip(labels, probs) if y == 1]
    negatives = [p for y, p in zip(labels, probs) if y == 0]
    if not positives or not negatives:
        return None
    wins = 0.0
    total = 0
    for pos in positives:
        for neg in negatives:
            total += 1
            if pos > neg:
                wins += 1.0
            elif pos == neg:
                wins += 0.5
    return wins / total if total else None


def _calibration_slope_intercept(
    labels: Sequence[int],
    probs: Sequence[float],
) -> Dict[str, Any]:
    """Fit y ~ intercept + slope * logit(p) using Newton steps."""
    labels = list(labels)
    probs = list(probs)
    positives = sum(1 for y in labels if y == 1)
    negatives = sum(1 for y in labels if y == 0)
    if len(labels) < 3 or positives == 0 or negatives == 0:
        return {
            "n": len(labels),
            "intercept": None,
            "slope": None,
            "status": "not_enough_class_balance",
        }

    x = [_logit(p) for p in probs]
    beta0 = 0.0
    beta1 = 1.0
    ridge = 1e-6
    status = "ok"
    for _ in range(50):
        g0 = 0.0
        g1 = 0.0
        h00 = ridge
        h01 = 0.0
        h11 = ridge
        for y, xi in zip(labels, x):
            mu = _sigmoid(beta0 + beta1 * xi)
            diff = mu - y
            weight = max(mu * (1.0 - mu), 1e-9)
            g0 += diff
            g1 += diff * xi
            h00 += weight
            h01 += weight * xi
            h11 += weight * xi * xi

        det = h00 * h11 - h01 * h01
        if abs(det) < 1e-12:
            status = "singular"
            break
        delta0 = (h11 * g0 - h01 * g1) / det
        delta1 = (-h01 * g0 + h00 * g1) / det
        beta0 -= delta0
        beta1 -= delta1
        if abs(beta0) > 100.0 or abs(beta1) > 100.0:
            status = "unstable_or_separated"
            break
        if abs(delta0) + abs(delta1) < 1e-7:
            break

    if abs(beta0) > 50.0 or abs(beta1) > 50.0:
        return {
            "n": len(labels),
            "intercept": None,
            "slope": None,
            "status": "unstable_or_separated",
        }
    return {
        "n": len(labels),
        "intercept": round(beta0, 6),
        "slope": round(beta1, 6),
        "status": status,
    }


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL row: {exc}") from exc
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _session_date(row: Mapping[str, Any]) -> str:
    return str(row.get("session_date") or row.get("date") or "")[:10]


def _filter_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    mode: str,
    min_date: str = "",
    max_date: str = "",
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        if mode != "both" and str(row.get("mode") or "") != mode:
            continue
        date_str = _session_date(row)
        if min_date and date_str and date_str < min_date:
            continue
        if max_date and date_str and date_str > max_date:
            continue
        out.append(row)
    return out


def _family(row: Mapping[str, Any]) -> str:
    family = str(row.get("signal_model_family") or row.get("model_family") or "").strip()
    return family or "unknown"


def _final_label(row: Mapping[str, Any]) -> Optional[int]:
    if not _boolish(row.get("label_final_available")):
        return None
    return _label_value(row.get("target_over_win"))


def _score_confirmed_60s_label(row: Mapping[str, Any]) -> Optional[int]:
    if not _boolish(row.get("label_score_confirmation_available")):
        return None
    return _label_value(row.get("target_score_confirmed_60s"))


def _no_score_60s_label(row: Mapping[str, Any]) -> Optional[int]:
    if not _boolish(row.get("label_score_confirmation_available")):
        return None
    return _label_value(row.get("target_no_score_change_60s"))


def _probability_metrics(
    rows: Sequence[Mapping[str, Any]],
    *,
    label_fn: Callable[[Mapping[str, Any]], Optional[int]],
    probability_key: str,
) -> Dict[str, Any]:
    labels: List[int] = []
    probs: List[float] = []
    missing = 0
    for row in rows:
        label = label_fn(row)
        prob = _clip_prob(row.get(probability_key))
        if label is None:
            continue
        if prob is None:
            missing += 1
            continue
        labels.append(label)
        probs.append(prob)

    positives = sum(1 for y in labels if y == 1)
    negatives = sum(1 for y in labels if y == 0)
    calib = _calibration_slope_intercept(labels, probs)
    return {
        "probability_key": probability_key,
        "n": len(labels),
        "positive_labels": positives,
        "negative_labels": negatives,
        "missing_probability_rows": missing,
        "brier": _round(_brier(labels, probs)),
        "logloss": _round(_logloss(labels, probs)),
        "auc": _round(_auc_pairwise(labels, probs)),
        "calibration_intercept": calib.get("intercept"),
        "calibration_slope": calib.get("slope"),
        "calibration_status": calib.get("status"),
    }


def _round(value: Optional[float], places: int = 6) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), places)


def _ask_bucket(row: Mapping[str, Any]) -> str:
    existing = str(row.get("ask_bucket") or "").strip()
    if existing:
        return existing
    ask = _safe_float(row.get("decision_ask"))
    if ask is None:
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


def _current_state_edge_bucket(row: Mapping[str, Any]) -> str:
    existing = str(row.get("shadow_current_state_edge_bucket") or "").strip()
    if existing:
        return existing
    edge = _safe_float(row.get("current_state_value_edge"))
    if edge is None:
        return "missing"
    if edge < 0.03:
        return "<0.03"
    if edge < 0.08:
        return "0.03-0.08"
    return ">=0.08"


def _phantom_confirmation_bucket(row: Mapping[str, Any]) -> str:
    family = _family(row)
    if family == NO_SCORE_DRIFT:
        trigger = str(row.get("shadow_no_score_drift_trigger") or "unknown_trigger")
        return f"no_score_drift|{trigger}"

    phantom = (
        str(row.get("shadow_phantom_risk_bucket") or "").strip()
        or str(row.get("shadow_phantom_risk_band") or "").strip()
        or str(row.get("phantom_risk_band") or "").strip()
        or "phantom_missing"
    )
    if _score_confirmed_60s_label(row) == 1:
        confirmation = "score_confirmed_60s"
    elif _no_score_60s_label(row) == 1:
        confirmation = "no_score_change_60s"
    elif _boolish(row.get("label_score_confirmation_available")):
        confirmation = "score_confirmation_other"
    else:
        confirmation = "score_confirmation_missing"
    return f"{phantom}|{confirmation}"


def _profit_unit(row: Mapping[str, Any], key: str) -> Optional[float]:
    value = _safe_float(row.get(key))
    if value is not None:
        return value
    label = _final_label(row)
    if label is None:
        return None
    price_key = "decision_ask" if key == "target_taker_profit_units" else "hypothetical_limit_price"
    price = _clip_prob(row.get(price_key))
    if price is None:
        return None
    return (1.0 / price - 1.0) if label == 1 else -1.0


def _roi_summary(rows: Sequence[Mapping[str, Any]], *, profit_key: str) -> Dict[str, Any]:
    profits: List[float] = []
    wins = 0
    losses = 0
    for row in rows:
        profit = _profit_unit(row, profit_key)
        if profit is None:
            continue
        label = _final_label(row)
        if label == 1:
            wins += 1
        elif label == 0:
            losses += 1
        profits.append(profit)
    count = len(profits)
    total_profit = sum(profits)
    return {
        "rows": count,
        "wins": wins,
        "losses": losses,
        "profit_units": _round(total_profit),
        "roi_per_1_usdc": _round(total_profit / count if count else None),
        "win_rate": _round(wins / count if count else None),
    }


def _roi_by_bucket(
    rows: Sequence[Mapping[str, Any]],
    *,
    bucket_fn: Callable[[Mapping[str, Any]], str],
    profit_key: str,
) -> Dict[str, Any]:
    groups: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[bucket_fn(row)].append(row)
    return {
        key: _roi_summary(group, profit_key=profit_key)
        for key, group in sorted(groups.items(), key=lambda item: item[0])
    }


def _counts_for_family(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    final_labels = [_final_label(row) for row in rows]
    final_labels = [label for label in final_labels if label is not None]
    score_labels = [_score_confirmed_60s_label(row) for row in rows]
    score_labels = [label for label in score_labels if label is not None]
    no_score_labels = [_no_score_60s_label(row) for row in rows]
    no_score_labels = [label for label in no_score_labels if label is not None]
    dates = sorted({_session_date(row) for row in rows if _session_date(row)})
    return {
        "rows": len(rows),
        "dates": len(dates),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "final_label_rows": len(final_labels),
        "positive_final_labels": sum(1 for y in final_labels if y == 1),
        "negative_final_labels": sum(1 for y in final_labels if y == 0),
        "score_confirmation_rows": len(score_labels),
        "score_confirmed_60s_labels": sum(1 for y in score_labels if y == 1),
        "no_score_change_60s_labels": sum(1 for y in no_score_labels if y == 1),
    }


def _coverage_ratio(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def _coverage_summary(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    total = len(rows)
    under_pair_available = sum(1 for row in rows if _boolish(row.get("under_pair_available")))
    under_pair_book = sum(
        1
        for row in rows
        if _has_value(row, "under_best_bid") and _has_value(row, "under_best_ask")
    )
    no_vig_market = sum(
        1
        for row in rows
        if _valid_probability(row, "over_mid_no_vig")
        and _valid_probability(row, "under_mid_no_vig")
        and _valid_probability(row, "decision_market_mid_no_vig")
    )
    run_count_panel = sum(
        1
        for row in rows
        if _has_value(row, "inference_panel_runs_considered")
        and _has_value(row, "inference_panel_selected_runs")
    )
    run_count_panel_full_3run = sum(
        1
        for row in rows
        if all(
            _has_value(row, f"inference_run{runs}_base_poisson")
            for runs in (1, 2, 3)
        )
    )
    run_count_empirical = sum(
        1
        for row in rows
        if any(
            _has_value(row, f"inference_run{runs}_base_empirical")
            for runs in (1, 2, 3)
        )
    )
    current_inferred_empirical = sum(
        1
        for row in rows
        if _has_value(row, "inferred_state_base_empirical")
        and _has_value(row, "inferred_state_base_poisson")
    )
    stage1_support = sum(
        1
        for row in rows
        if _has_value(row, "inferred_state_effective_n_proxy")
        and _has_value(row, "inferred_state_stage1_trust_weight")
    )
    current_state_support = sum(
        1
        for row in rows
        if _has_value(row, "current_state_value_effective_n_proxy")
        and _has_value(row, "current_state_value_stage1_trust_weight")
    )
    return {
        "rows": total,
        "under_pair_available_rows": under_pair_available,
        "under_pair_available_rate": _coverage_ratio(under_pair_available, total),
        "under_pair_book_rows": under_pair_book,
        "under_pair_book_rate": _coverage_ratio(under_pair_book, total),
        "no_vig_market_rows": no_vig_market,
        "no_vig_market_rate": _coverage_ratio(no_vig_market, total),
        "run_count_panel_rows": run_count_panel,
        "run_count_panel_rate": _coverage_ratio(run_count_panel, total),
        "run_count_panel_full_3run_rows": run_count_panel_full_3run,
        "run_count_panel_full_3run_rate": _coverage_ratio(run_count_panel_full_3run, total),
        "run_count_empirical_rows": run_count_empirical,
        "run_count_empirical_rate": _coverage_ratio(run_count_empirical, total),
        "inferred_poisson_empirical_rows": current_inferred_empirical,
        "inferred_poisson_empirical_rate": _coverage_ratio(current_inferred_empirical, total),
        "stage1_support_rows": stage1_support,
        "stage1_support_rate": _coverage_ratio(stage1_support, total),
        "current_state_support_rows": current_state_support,
        "current_state_support_rate": _coverage_ratio(current_state_support, total),
    }


def _coverage_report(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    by_family: Dict[str, Dict[str, Any]] = {}
    rows_by_family: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_family[_family(row)].append(row)
    for family in KNOWN_FAMILIES:
        rows_by_family.setdefault(family, [])
    for family, family_rows in sorted(rows_by_family.items()):
        by_family[family] = _coverage_summary(family_rows)
    return {
        "thresholds": dict(COVERAGE_MIN_WARN),
        "overall": _coverage_summary(rows),
        "by_family": by_family,
    }


def _coverage_warnings(coverage: Mapping[str, Any]) -> List[str]:
    warnings: List[str] = []
    by_family = coverage.get("by_family") or {}
    for family, payload in by_family.items():
        rows = int((payload or {}).get("rows") or 0)
        if rows <= 0:
            continue
        under_rate = _safe_float((payload or {}).get("under_pair_available_rate"))
        no_vig_rate = _safe_float((payload or {}).get("no_vig_market_rate"))
        run_panel_rate = _safe_float((payload or {}).get("run_count_panel_rate"))
        support_rate = _safe_float((payload or {}).get("stage1_support_rate"))
        if under_rate is not None and under_rate < COVERAGE_MIN_WARN["under_pair_available_rate"]:
            warnings.append(
                f"{family}: Under-pair coverage is low ({under_rate:.0%}); "
                "side-neutral/no-vig analysis may be incomplete."
            )
        if no_vig_rate is not None and no_vig_rate < COVERAGE_MIN_WARN["no_vig_market_rate"]:
            warnings.append(
                f"{family}: no-vig market coverage is low ({no_vig_rate:.0%}); "
                "market-anchored alpha diagnostics may fall back to ask."
            )
        if family == SCORE_EVENT_TRANSITION and run_panel_rate is not None and run_panel_rate < COVERAGE_MIN_WARN["run_count_panel_rate"]:
            warnings.append(
                f"{family}: run-count inference panel coverage is low ({run_panel_rate:.0%}); "
                "FV gap decomposition cannot fully explain inferred-run selection."
            )
        if family == SCORE_EVENT_TRANSITION and support_rate is not None and support_rate < COVERAGE_MIN_WARN["stage1_support_rate"]:
            warnings.append(
                f"{family}: Stage-1 support diagnostic coverage is low ({support_rate:.0%}); "
                "effective_n/trust-weight analysis may be incomplete."
            )
    return warnings


def _status_from_thresholds(
    *,
    family: str,
    counts: Mapping[str, Any],
    metrics: Mapping[str, Any],
    roi: Mapping[str, Any],
    thresholds: Mapping[str, Any],
) -> Dict[str, Any]:
    reasons: List[str] = []
    min_final = int(
        thresholds.get("min_no_score_drift_final_labels")
        if family == NO_SCORE_DRIFT
        else thresholds.get("min_family_final_labels")
    )
    if int(counts.get("dates") or 0) < int(thresholds["min_family_dates"]):
        reasons.append(
            f"not enough dates: {counts.get('dates')} < {thresholds['min_family_dates']}"
        )
    if int(counts.get("final_label_rows") or 0) < min_final:
        reasons.append(f"not enough settled final labels: {counts.get('final_label_rows')} < {min_final}")
    if int(counts.get("positive_final_labels") or 0) < int(thresholds["min_family_positive_labels"]):
        reasons.append(
            "not enough positive final labels: "
            f"{counts.get('positive_final_labels')} < {thresholds['min_family_positive_labels']}"
        )
    if int(counts.get("negative_final_labels") or 0) < int(thresholds["min_family_negative_labels"]):
        reasons.append(
            "not enough negative final labels: "
            f"{counts.get('negative_final_labels')} < {thresholds['min_family_negative_labels']}"
        )
    if family == SCORE_EVENT_TRANSITION:
        if int(counts.get("score_confirmation_rows") or 0) < int(thresholds["min_score_confirmation_labels"]):
            reasons.append(
                "not enough score-confirmation labels: "
                f"{counts.get('score_confirmation_rows')} < {thresholds['min_score_confirmation_labels']}"
            )
        if int(counts.get("score_confirmed_60s_labels") or 0) < int(thresholds["min_score_confirmed_labels"]):
            reasons.append(
                "not enough confirmed-score labels: "
                f"{counts.get('score_confirmed_60s_labels')} < {thresholds['min_score_confirmed_labels']}"
            )
        if int(counts.get("no_score_change_60s_labels") or 0) < int(thresholds["min_no_score_confirmed_labels"]):
            reasons.append(
                "not enough no-score confirmation labels: "
                f"{counts.get('no_score_change_60s_labels')} < {thresholds['min_no_score_confirmed_labels']}"
            )

    if reasons:
        return {
            "status": "not_enough_data",
            "promotable": False,
            "reasons": reasons,
        }

    market = metrics.get("market_probability") or {}
    raw_fv = metrics.get("raw_fv_probability") or {}
    metric_reasons: List[str] = []
    market_logloss = _safe_float(market.get("logloss"))
    raw_logloss = _safe_float(raw_fv.get("logloss"))
    if market_logloss is not None and raw_logloss is not None and raw_logloss >= market_logloss:
        metric_reasons.append(
            f"raw FV logloss is not better than market: {raw_logloss:.4f} >= {market_logloss:.4f}"
        )
    slope = _safe_float(raw_fv.get("calibration_slope"))
    if slope is None or abs(slope - 1.0) > float(thresholds["max_calibration_slope_abs_error"]):
        metric_reasons.append(
            "raw FV calibration slope outside tolerance: "
            f"{slope} vs 1.0 +/- {thresholds['max_calibration_slope_abs_error']}"
        )
    intercept = _safe_float(raw_fv.get("calibration_intercept"))
    if intercept is None or abs(intercept) > float(thresholds["max_calibration_intercept_abs"]):
        metric_reasons.append(
            "raw FV calibration intercept outside tolerance: "
            f"{intercept} vs +/- {thresholds['max_calibration_intercept_abs']}"
        )
    if thresholds.get("require_positive_roi"):
        overall_roi = ((roi.get("overall") or {}).get("taker") or {}).get("roi_per_1_usdc")
        overall_roi_f = _safe_float(overall_roi)
        if overall_roi_f is None or overall_roi_f <= 0:
            metric_reasons.append(f"overall taker ROI is not positive: {overall_roi}")

    if metric_reasons:
        return {
            "status": "blocked_metric_quality",
            "promotable": False,
            "reasons": metric_reasons,
        }
    return {
        "status": "promotable_candidate",
        "promotable": True,
        "reasons": [],
    }


def _family_report(
    family: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    thresholds: Mapping[str, Any],
) -> Dict[str, Any]:
    counts = _counts_for_family(rows)
    metrics = {
        "market_probability": _probability_metrics(
            rows,
            label_fn=_final_label,
            probability_key="decision_ask",
        ),
        "raw_fv_probability": _probability_metrics(
            rows,
            label_fn=_final_label,
            probability_key="fair_value_raw",
        ),
        "fallback_fv_probability": _probability_metrics(
            rows,
            label_fn=_final_label,
            probability_key="fair_value",
        ),
        "calibrated_fv_probability": _probability_metrics(
            rows,
            label_fn=_final_label,
            probability_key="fair_value_calibrated",
        ),
        # `shadow_p_score_event_proxy` is the score-event predictor; pairing it
        # with `target_score_confirmed_60s` is the only well-typed combination.
        # The previous version of this metric paired `fair_value_raw` (an
        # over-game outcome probability, ~0.91 mean) against the same label
        # (~11% base rate) and produced AUC < 0.5 by construction, because
        # phantom-driven signals fire with the highest FV but no real run.
        "score_confirmed_60s_proxy": _probability_metrics(
            rows,
            label_fn=_score_confirmed_60s_label,
            probability_key="shadow_p_score_event_proxy",
        ),
    }
    roi = {
        "overall": {
            "taker": _roi_summary(rows, profit_key="target_taker_profit_units"),
            "limit": _roi_summary(rows, profit_key="target_limit_profit_units"),
        },
        "by_ask_bucket": {
            "taker": _roi_by_bucket(
                rows,
                bucket_fn=_ask_bucket,
                profit_key="target_taker_profit_units",
            )
        },
        "by_current_state_edge_bucket": {
            "taker": _roi_by_bucket(
                rows,
                bucket_fn=_current_state_edge_bucket,
                profit_key="target_taker_profit_units",
            )
        },
        "by_phantom_confirmation_bucket": {
            "taker": _roi_by_bucket(
                rows,
                bucket_fn=_phantom_confirmation_bucket,
                profit_key="target_taker_profit_units",
            )
        },
    }
    readiness = _status_from_thresholds(
        family=family,
        counts=counts,
        metrics=metrics,
        roi=roi,
        thresholds=thresholds,
    )
    return {
        "family": family,
        "counts": counts,
        "metrics": metrics,
        "roi": roi,
        "artifact_promotability": {
            "probability_calibration": readiness,
            "ev_policy": readiness if family == SCORE_EVENT_TRANSITION else {
                "status": "not_applicable",
                "promotable": False,
                "reasons": ["EV policy runtime currently applies to score-event transition trades only."],
            },
            "no_score_drift_policy": readiness if family == NO_SCORE_DRIFT else {
                "status": "not_applicable",
                "promotable": False,
                "reasons": ["No-score drift policy is evaluated from no_score_drift family rows only."],
            },
        },
    }


def build_report(
    *,
    table_path: Path = DEFAULT_TABLE_PATH,
    mode: str = "live",
    min_date: str = "",
    max_date: str = "",
    min_family_dates: int = 10,
    min_family_final_labels: int = 300,
    min_no_score_drift_final_labels: int = 200,
    min_family_positive_labels: int = 75,
    min_family_negative_labels: int = 75,
    min_score_confirmation_labels: int = 300,
    min_score_confirmed_labels: int = 50,
    min_no_score_confirmed_labels: int = 50,
    max_calibration_slope_abs_error: float = 0.35,
    max_calibration_intercept_abs: float = 0.35,
    require_positive_roi: bool = True,
) -> Dict[str, Any]:
    source_rows = _load_jsonl(table_path)
    rows = _filter_rows(source_rows, mode=mode, min_date=min_date, max_date=max_date)
    dates = sorted({_session_date(row) for row in rows if _session_date(row)})
    thresholds = {
        "min_family_dates": int(min_family_dates),
        "min_family_final_labels": int(min_family_final_labels),
        "min_no_score_drift_final_labels": int(min_no_score_drift_final_labels),
        "min_family_positive_labels": int(min_family_positive_labels),
        "min_family_negative_labels": int(min_family_negative_labels),
        "min_score_confirmation_labels": int(min_score_confirmation_labels),
        "min_score_confirmed_labels": int(min_score_confirmed_labels),
        "min_no_score_confirmed_labels": int(min_no_score_confirmed_labels),
        "max_calibration_slope_abs_error": float(max_calibration_slope_abs_error),
        "max_calibration_intercept_abs": float(max_calibration_intercept_abs),
        "require_positive_roi": bool(require_positive_roi),
    }

    rows_by_family: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_family[_family(row)].append(row)
    for family in KNOWN_FAMILIES:
        rows_by_family.setdefault(family, [])

    family_reports = {
        family: _family_report(family, family_rows, thresholds=thresholds)
        for family, family_rows in sorted(rows_by_family.items())
    }
    coverage_checks = _coverage_report(rows)
    artifact_items: Dict[str, Dict[str, Any]] = {}
    any_promotable = False
    for family, family_payload in family_reports.items():
        for artifact, status in (family_payload.get("artifact_promotability") or {}).items():
            key = f"{family}:{artifact}"
            artifact_items[key] = status
            any_promotable = any_promotable or bool(status.get("promotable"))

    warnings: List[str] = []
    if not source_rows:
        warnings.append(f"No calibration-opportunity training rows found at {table_path}.")
    if not rows and source_rows:
        warnings.append("Rows were loaded, but filters removed all rows.")
    if not any_promotable:
        warnings.append("No artifact is currently promotable under the configured maturity thresholds.")
    warnings.extend(_coverage_warnings(coverage_checks))

    return {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "mode": mode,
        "source": {
            "table_path": str(table_path),
            "source_rows": len(source_rows),
            "filtered_rows": len(rows),
            "min_date": min_date or None,
            "max_date": max_date or None,
            "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
        },
        "thresholds": thresholds,
        "counts": {
            "rows_by_family": {family: len(family_rows) for family, family_rows in sorted(rows_by_family.items())},
            "settled_labels_by_family": {
                family: (payload.get("counts") or {}).get("final_label_rows")
                for family, payload in family_reports.items()
            },
        },
        "coverage_checks": coverage_checks,
        "families": family_reports,
        "artifact_promotability": {
            "any_promotable": any_promotable,
            "items": artifact_items,
        },
        "overall_status": "ready_for_review" if any_promotable else "not_enough_data",
        "warnings": warnings,
    }


def _fmt_metric(value: Any, digits: int = 3) -> str:
    value_f = _safe_float(value)
    if value_f is None:
        return "n/a"
    return f"{value_f:.{digits}f}"


def _fmt_pct(value: Any) -> str:
    value_f = _safe_float(value)
    if value_f is None:
        return "n/a"
    return f"{value_f * 100:.1f}%"


def _markdown_table(headers: List[str], rows: List[List[Any]]) -> List[str]:
    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        out.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return out


def _roi_bucket_rows(bucket_map: Mapping[str, Any]) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for bucket, payload in bucket_map.items():
        rows.append([
            bucket,
            int(payload.get("rows") or 0),
            int(payload.get("wins") or 0),
            int(payload.get("losses") or 0),
            _fmt_pct(payload.get("win_rate")),
            _fmt_metric(payload.get("profit_units")),
            _fmt_pct(payload.get("roi_per_1_usdc")),
        ])
    return rows or [["none", 0, 0, 0, "n/a", "n/a", "n/a"]]


def _coverage_rows(coverage_map: Mapping[str, Any]) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for family, payload in coverage_map.items():
        rows.append([
            family,
            int((payload or {}).get("rows") or 0),
            _fmt_pct((payload or {}).get("under_pair_available_rate")),
            _fmt_pct((payload or {}).get("no_vig_market_rate")),
            _fmt_pct((payload or {}).get("run_count_panel_rate")),
            _fmt_pct((payload or {}).get("run_count_panel_full_3run_rate")),
            _fmt_pct((payload or {}).get("inferred_poisson_empirical_rate")),
            _fmt_pct((payload or {}).get("stage1_support_rate")),
            _fmt_pct((payload or {}).get("current_state_support_rate")),
        ])
    return rows or [["none", 0, "n/a", "n/a", "n/a", "n/a", "n/a", "n/a", "n/a"]]


def render_markdown(report: Mapping[str, Any]) -> str:
    lines: List[str] = [
        "# MLB Polymarket Model Maturity Report",
        "",
        f"- Generated: {report.get('generated_at_utc')}",
        f"- Mode: {report.get('mode')}",
        f"- Overall status: {report.get('overall_status')}",
        f"- Source rows: {(report.get('source') or {}).get('filtered_rows')} filtered / {(report.get('source') or {}).get('source_rows')} loaded",
        f"- Date range: {(report.get('source') or {}).get('first_date')} -> {(report.get('source') or {}).get('last_date')}",
        "",
        "## Family Readiness",
    ]

    readiness_rows: List[List[Any]] = []
    for family, payload in (report.get("families") or {}).items():
        counts = payload.get("counts") or {}
        promo = ((payload.get("artifact_promotability") or {}).get("probability_calibration") or {})
        readiness_rows.append([
            family,
            counts.get("rows", 0),
            counts.get("dates", 0),
            counts.get("final_label_rows", 0),
            counts.get("positive_final_labels", 0),
            counts.get("negative_final_labels", 0),
            counts.get("score_confirmation_rows", 0),
            promo.get("status", "unknown"),
        ])
    lines.extend(_markdown_table(
        ["Family", "Rows", "Dates", "Settled", "Wins", "Losses", "Score Conf.", "Status"],
        readiness_rows,
    ))

    coverage = report.get("coverage_checks") or {}
    lines.extend(["", "## Coverage Checks"])
    lines.extend(_markdown_table(
        [
            "Family",
            "Rows",
            "Under Pair",
            "No-Vig Market",
            "Run Panel",
            "Full 3-Run Panel",
            "Poisson+Empirical",
            "Stage-1 Support",
            "Current Support",
        ],
        _coverage_rows((coverage.get("by_family") or {})),
    ))

    for family, payload in (report.get("families") or {}).items():
        lines.extend(["", f"## {family}", "", "### Probability Metrics"])
        metric_rows: List[List[Any]] = []
        for metric_name, metric in (payload.get("metrics") or {}).items():
            metric_rows.append([
                metric_name,
                metric.get("n", 0),
                _fmt_metric(metric.get("brier")),
                _fmt_metric(metric.get("logloss")),
                _fmt_metric(metric.get("auc")),
                _fmt_metric(metric.get("calibration_intercept")),
                _fmt_metric(metric.get("calibration_slope")),
                metric.get("calibration_status"),
            ])
        lines.extend(_markdown_table(
            ["Metric", "N", "Brier", "Logloss", "AUC", "Cal Int.", "Cal Slope", "Cal Status"],
            metric_rows,
        ))

        roi = payload.get("roi") or {}
        overall_taker = ((roi.get("overall") or {}).get("taker") or {})
        lines.extend([
            "",
            "### ROI",
            f"- Overall taker ROI: {_fmt_pct(overall_taker.get('roi_per_1_usdc'))} "
            f"({overall_taker.get('rows', 0)} settled rows, profit units {_fmt_metric(overall_taker.get('profit_units'))})",
            "",
            "Ask bucket:",
        ])
        ask = (((roi.get("by_ask_bucket") or {}).get("taker") or {}))
        lines.extend(_markdown_table(
            ["Bucket", "Rows", "Wins", "Losses", "Win Rate", "Profit Units", "ROI"],
            _roi_bucket_rows(ask),
        ))
        lines.extend(["", "Current-state-edge bucket:"])
        current_edge = (((roi.get("by_current_state_edge_bucket") or {}).get("taker") or {}))
        lines.extend(_markdown_table(
            ["Bucket", "Rows", "Wins", "Losses", "Win Rate", "Profit Units", "ROI"],
            _roi_bucket_rows(current_edge),
        ))
        lines.extend(["", "Phantom/confirmation bucket:"])
        phantom = (((roi.get("by_phantom_confirmation_bucket") or {}).get("taker") or {}))
        lines.extend(_markdown_table(
            ["Bucket", "Rows", "Wins", "Losses", "Win Rate", "Profit Units", "ROI"],
            _roi_bucket_rows(phantom),
        ))

        lines.extend(["", "### Promotion"])
        for artifact, status in (payload.get("artifact_promotability") or {}).items():
            lines.append(f"- {artifact}: {status.get('status')} (promotable={bool(status.get('promotable'))})")
            for reason in status.get("reasons") or []:
                lines.append(f"  - {reason}")

    lines.extend(["", "## Warnings"])
    warnings = report.get("warnings") or []
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- None.")
    return "\n".join(lines) + "\n"


def write_report(
    report: Mapping[str, Any],
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    *,
    output_stem: str = DEFAULT_OUTPUT_STEM,
) -> Tuple[Path, Path]:
    output_root.mkdir(parents=True, exist_ok=True)
    json_path = output_root / f"{output_stem}.json"
    md_path = output_root / f"{output_stem}.md"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    md_path.write_text(render_markdown(report), encoding="utf-8")

    as_of_date = ((report.get("source") or {}).get("last_date") or "").strip()
    if as_of_date:
        dated_json = output_root / f"{as_of_date}_{output_stem}.json"
        dated_md = output_root / f"{as_of_date}_{output_stem}.md"
        with dated_json.open("w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        dated_md.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build model maturity/readiness report.")
    p.add_argument("--table-path", type=Path, default=DEFAULT_TABLE_PATH)
    p.add_argument("--mode", choices=["live", "paper", "both"], default="live")
    p.add_argument("--min-date", type=str, default="")
    p.add_argument("--max-date", type=str, default="")
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--output-stem", type=str, default=DEFAULT_OUTPUT_STEM)
    p.add_argument("--min-family-dates", type=int, default=10)
    p.add_argument("--min-family-final-labels", type=int, default=300)
    p.add_argument("--min-no-score-drift-final-labels", type=int, default=200)
    p.add_argument("--min-family-positive-labels", type=int, default=75)
    p.add_argument("--min-family-negative-labels", type=int, default=75)
    p.add_argument("--min-score-confirmation-labels", type=int, default=300)
    p.add_argument("--min-score-confirmed-labels", type=int, default=50)
    p.add_argument("--min-no-score-confirmed-labels", type=int, default=50)
    p.add_argument("--max-calibration-slope-abs-error", type=float, default=0.35)
    p.add_argument("--max-calibration-intercept-abs", type=float, default=0.35)
    p.add_argument(
        "--allow-nonpositive-roi",
        action="store_true",
        help="Do not block promotion candidates solely for non-positive historical ROI.",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.min_date:
        datetime.strptime(args.min_date, "%Y-%m-%d")
    if args.max_date:
        datetime.strptime(args.max_date, "%Y-%m-%d")
    if args.min_date and args.max_date and args.min_date > args.max_date:
        raise SystemExit("--min-date must be <= --max-date")

    report = build_report(
        table_path=args.table_path,
        mode=args.mode,
        min_date=args.min_date,
        max_date=args.max_date,
        min_family_dates=args.min_family_dates,
        min_family_final_labels=args.min_family_final_labels,
        min_no_score_drift_final_labels=args.min_no_score_drift_final_labels,
        min_family_positive_labels=args.min_family_positive_labels,
        min_family_negative_labels=args.min_family_negative_labels,
        min_score_confirmation_labels=args.min_score_confirmation_labels,
        min_score_confirmed_labels=args.min_score_confirmed_labels,
        min_no_score_confirmed_labels=args.min_no_score_confirmed_labels,
        max_calibration_slope_abs_error=args.max_calibration_slope_abs_error,
        max_calibration_intercept_abs=args.max_calibration_intercept_abs,
        require_positive_roi=not bool(args.allow_nonpositive_roi),
    )
    json_path, md_path = write_report(report, args.output_root, output_stem=args.output_stem)
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
