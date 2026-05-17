#!/usr/bin/env python3
"""
Build a fair-value stage ablation report.

This report answers a narrow question: where does the FV stack improve or
damage calibration relative to the market and to the prior stage?

Default input is the broad calibration-opportunity table, not just placed
orders, because placed bets are sparse and selection-biased.

Outputs:
  data/analysis_output/fair_value_stage_ablation/
    fair_value_stage_ablation_report.json
    fair_value_stage_ablation_report.md
    <as_of_date>_fair_value_stage_ablation_report.json
    <as_of_date>_fair_value_stage_ablation_report.md
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:  # CLI execution from scripts/analysis
    from analyze_polymarket_overreactions import OUCache  # type: ignore
except ImportError:  # Package import in tests
    from scripts.analysis.analyze_polymarket_overreactions import OUCache  # type: ignore


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_INPUT_PATH = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "calibration_opportunity_training"
    / "calibration_opportunity_training_table.jsonl"
)
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "fair_value_stage_ablation"
DEFAULT_OUTPUT_STEM = "fair_value_stage_ablation_report"

SCORE_EVENT_TRANSITION = "score_event_transition"
NO_SCORE_DRIFT = "no_score_drift"
ALL_FAMILIES = "__all__"

STAGE_LABELS = {
    "market_ask_baseline": "Market ask baseline",
    "current_state_stage1_poisson": "Current-state Stage-1 Poisson",
    "current_state_stage1_empirical": "Current-state Stage-1 empirical",
    "current_state_after_stage23": "Current-state after Stage-2/3",
    "stage1_after_score_event_inference": "Stage-1 after score-event inference",
    "stage2_after_run_env": "Stage-2 park/weather run-env",
    "stage3_after_team_offense": "Stage-3 team offense",
    "final_runtime_fv": "Final runtime FV",
}

LADDERS = {
    SCORE_EVENT_TRANSITION: [
        "market_ask_baseline",
        "current_state_stage1_poisson",
        "current_state_after_stage23",
        "stage1_after_score_event_inference",
        "stage2_after_run_env",
        "stage3_after_team_offense",
        "final_runtime_fv",
    ],
    NO_SCORE_DRIFT: [
        "market_ask_baseline",
        "stage1_after_score_event_inference",
        "stage3_after_team_offense",
        "final_runtime_fv",
    ],
    ALL_FAMILIES: [
        "market_ask_baseline",
        "stage1_after_score_event_inference",
        "stage2_after_run_env",
        "stage3_after_team_offense",
        "final_runtime_fv",
    ],
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


def _clip_prob(value: Any) -> Optional[float]:
    prob = _safe_float(value)
    if prob is None:
        return None
    if not 0.0 < prob < 1.0:
        return None
    return min(max(prob, 1e-6), 1.0 - 1e-6)


def _logit(prob: float) -> float:
    prob = min(max(prob, 1e-6), 1.0 - 1e-6)
    return math.log(prob / (1.0 - prob))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _apply_logit_delta(prob: Optional[float], delta: Any) -> Optional[float]:
    if prob is None:
        return None
    d = _safe_float(delta)
    if d is None:
        return None
    return min(max(_sigmoid(_logit(prob) + d), 1e-6), 1.0 - 1e-6)


def _apply_optional_logit_delta(prob: Optional[float], delta: Any) -> Optional[float]:
    if prob is None:
        return None
    d = _safe_float(delta)
    if d is None:
        return prob
    return min(max(_sigmoid(_logit(prob) + d), 1e-6), 1.0 - 1e-6)


def _first_prob(row: Mapping[str, Any], keys: Sequence[str]) -> Optional[float]:
    for key in keys:
        prob = _clip_prob(row.get(key))
        if prob is not None:
            return prob
    return None


def _line_emp_key(line: Any) -> Optional[str]:
    try:
        return "o" + str(float(line)).replace(".", "")
    except Exception:
        text = str(line or "").strip()
        return ("o" + text.replace(".", "")) if text else None


def _cache_lookup_with_empirical(
    cache: Optional[OUCache],
    row: Mapping[str, Any],
    *,
    after_inferred_score: bool = False,
) -> Tuple[Optional[float], Optional[float]]:
    if cache is None:
        return None, None
    away = _safe_int(row.get("away_score_before"))
    home = _safe_int(row.get("home_score_before"))
    inning = _safe_int(row.get("inning"))
    outs = _safe_int(row.get("outs"))
    line = row.get("line")
    if away is None or home is None or inning is None or outs is None or line in (None, ""):
        return None, None
    inning_state = str(row.get("inning_state") or "")
    runners_on = _safe_int(row.get("runners_on")) or 0
    if after_inferred_score:
        inferred_runs = _safe_int(row.get("inferred_runs")) or 0
        if inferred_runs > 0:
            if inning_state.lower().startswith("bot"):
                home += inferred_runs
            else:
                away += inferred_runs
    try:
        poisson_prob, meta = cache.lookup_with_meta(
            away_score=away,
            home_score=home,
            inning=inning,
            inning_state=inning_state,
            outs=outs,
            line=str(line),
            runners_on=runners_on,
        )
    except Exception:
        return None, None

    empirical_prob = None
    state_key = meta.get("state_cell_key") if isinstance(meta, dict) else None
    emp_key = _line_emp_key(line)
    if state_key and emp_key:
        cell = cache.cells.get(str(state_key))
        if isinstance(cell, dict):
            empirical_prob = _clip_prob(cell.get(emp_key))
    return _clip_prob(poisson_prob), empirical_prob


def _label(row: Mapping[str, Any]) -> Optional[int]:
    for key in ("target_over_win", "target_win", "outcome_win"):
        value = row.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, (int, float)) and value in (0, 1):
            return int(value)
        if isinstance(value, str):
            norm = value.strip().lower()
            if norm in {"1", "true", "yes", "y", "win", "won"}:
                return 1
            if norm in {"0", "false", "no", "n", "loss", "lost"}:
                return 0
    return None


def _family(row: Mapping[str, Any]) -> str:
    value = str(row.get("signal_model_family") or row.get("state_value_strategy") or "").strip()
    return value or "unknown"


def _date_value(row: Mapping[str, Any]) -> str:
    for key in ("session_date", "date"):
        value = str(row.get(key) or "").strip()
        if len(value) >= 10:
            return value[:10]
    ts = str(row.get("ts") or row.get("recorded_at") or "").strip()
    return ts[:10] if len(ts) >= 10 else ""


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


def _calibration_slope_intercept(labels: Sequence[int], probs: Sequence[float]) -> Dict[str, Any]:
    positives = sum(1 for y in labels if y == 1)
    negatives = sum(1 for y in labels if y == 0)
    if len(labels) < 3 or positives == 0 or negatives == 0:
        return {"status": "not_enough_class_balance", "intercept": None, "slope": None}

    x = [_logit(p) for p in probs]
    beta0 = 0.0
    beta1 = 1.0
    status = "ok"
    for _ in range(50):
        g0 = 0.0
        g1 = 0.0
        h00 = 1e-6
        h01 = 0.0
        h11 = 1e-6
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
        return {"status": "unstable_or_separated", "intercept": None, "slope": None}
    return {"status": status, "intercept": round(beta0, 6), "slope": round(beta1, 6)}


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except Exception:
        return None


def load_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as f:
            rows.extend(dict(row) for row in csv.DictReader(f))
        return rows
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL row: {exc}") from exc
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def filter_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    mode: str = "live",
    min_date: str = "",
    max_date: str = "",
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        if mode and str(row.get("mode") or "").strip() and str(row.get("mode")).strip() != mode:
            continue
        date = _date_value(row)
        if min_date and date and date < min_date:
            continue
        if max_date and date and date > max_date:
            continue
        out.append(row)
    return out


def stage_predictions(
    row: Mapping[str, Any],
    *,
    stage1_cache: Optional[OUCache] = None,
) -> Dict[str, Optional[float]]:
    cache_current_poisson, cache_current_empirical = _cache_lookup_with_empirical(
        stage1_cache,
        row,
        after_inferred_score=False,
    )
    cache_after_inference, _cache_after_empirical = _cache_lookup_with_empirical(
        stage1_cache,
        row,
        after_inferred_score=_family(row) == SCORE_EVENT_TRANSITION,
    )

    base = cache_after_inference
    if base is None:
        base = _first_prob(row, ("base_fair_value", "current_state_value_base_poisson"))
    if stage1_cache is not None:
        # In cache-swap mode, carry the recomputed Stage-1 value forward when
        # the later logged delta is unavailable. That keeps cache comparisons
        # from silently reverting to logged runtime probabilities.
        stage2_delta = row.get("stage2_run_env_delta")
        if _safe_float(stage2_delta) is None:
            stage2_delta = row.get("current_state_value_stage2_run_env_delta")
        stage2 = _apply_optional_logit_delta(base, stage2_delta)
    else:
        stage2 = _apply_logit_delta(base, row.get("stage2_run_env_delta"))
        if stage2 is None:
            # No-score drift currently logs the current-state Stage-2 delta under
            # the state-value namespace.
            stage2 = _apply_logit_delta(base, row.get("current_state_value_stage2_run_env_delta"))

    if stage1_cache is None:
        stage3 = _first_prob(row, ("fair_value_raw",))
    else:
        stage3 = _apply_optional_logit_delta(stage2 if stage2 is not None else base, row.get("team_offense_delta"))
    if stage3 is None:
        stage3 = _apply_logit_delta(stage2 if stage2 is not None else base, row.get("team_offense_delta"))
    if stage3 is None and stage1_cache is None:
        stage3 = _first_prob(row, ("current_state_value_fv_raw", "shadow_fv_after_inferred_score"))

    current_state_stage23 = None
    if cache_current_poisson is not None:
        current_state_stage2 = _apply_optional_logit_delta(
            cache_current_poisson,
            row.get("current_state_value_stage2_run_env_delta", row.get("stage2_run_env_delta")),
        )
        current_state_stage23 = _apply_optional_logit_delta(
            current_state_stage2 if current_state_stage2 is not None else cache_current_poisson,
            row.get("current_state_value_team_offense_delta", row.get("team_offense_delta")),
        )
    if current_state_stage23 is None:
        current_state_stage23 = _first_prob(
            row,
            ("current_state_value_fv_raw", "shadow_fv_current_state"),
        )

    return {
        "market_ask_baseline": _first_prob(row, ("decision_ask", "execution_ask")),
        "current_state_stage1_poisson": (
            cache_current_poisson
            if cache_current_poisson is not None
            else _first_prob(row, ("current_state_value_base_poisson",))
        ),
        "current_state_stage1_empirical": (
            cache_current_empirical
            if cache_current_empirical is not None
            else _first_prob(row, ("current_state_value_base_empirical",))
        ),
        "current_state_after_stage23": current_state_stage23,
        "stage1_after_score_event_inference": base,
        "stage2_after_run_env": stage2,
        "stage3_after_team_offense": stage3,
        "final_runtime_fv": (
            stage3
            if stage1_cache is not None
            else _first_prob(row, ("fair_value", "fair_value_calibrated", "fair_value_raw"))
        ),
    }


def _stage_dataset(
    rows: Sequence[Mapping[str, Any]],
    stage: str,
    *,
    stage1_cache: Optional[OUCache] = None,
) -> Tuple[List[int], List[float], List[float]]:
    labels: List[int] = []
    probs: List[float] = []
    markets: List[float] = []
    for row in rows:
        y = _label(row)
        if y is None:
            continue
        preds = stage_predictions(row, stage1_cache=stage1_cache)
        pred = preds.get(stage)
        if pred is None:
            continue
        market = preds.get("market_ask_baseline")
        labels.append(y)
        probs.append(pred)
        markets.append(market if market is not None else float("nan"))
    return labels, probs, markets


def summarize_stage(
    rows: Sequence[Mapping[str, Any]],
    stage: str,
    *,
    stage1_cache: Optional[OUCache] = None,
) -> Dict[str, Any]:
    labels, probs, markets = _stage_dataset(rows, stage, stage1_cache=stage1_cache)
    positives = sum(labels)
    market_edges = [p - m for p, m in zip(probs, markets) if math.isfinite(m)]
    brier = _brier(labels, probs)
    logloss = _logloss(labels, probs)
    cal = _calibration_slope_intercept(labels, probs)
    return {
        "stage": stage,
        "label": STAGE_LABELS.get(stage, stage),
        "n": len(labels),
        "wins": positives,
        "losses": len(labels) - positives,
        "empirical_rate": _round(positives / len(labels), 6) if labels else None,
        "avg_prob": _round(sum(probs) / len(probs), 6) if probs else None,
        "avg_market_edge": _round(sum(market_edges) / len(market_edges), 6) if market_edges else None,
        "brier": _round(brier),
        "logloss": _round(logloss),
        "auc": _round(_auc_pairwise(labels, probs)),
        "calibration_intercept": cal.get("intercept"),
        "calibration_slope": cal.get("slope"),
        "calibration_status": cal.get("status"),
    }


def compare_stages(
    rows: Sequence[Mapping[str, Any]],
    previous_stage: str,
    next_stage: str,
    *,
    stage1_cache: Optional[OUCache] = None,
) -> Dict[str, Any]:
    labels: List[int] = []
    prev_probs: List[float] = []
    next_probs: List[float] = []
    abs_moves: List[float] = []
    for row in rows:
        y = _label(row)
        if y is None:
            continue
        preds = stage_predictions(row, stage1_cache=stage1_cache)
        prev = preds.get(previous_stage)
        nxt = preds.get(next_stage)
        if prev is None or nxt is None:
            continue
        labels.append(y)
        prev_probs.append(prev)
        next_probs.append(nxt)
        abs_moves.append(abs(nxt - prev))

    prev_brier = _brier(labels, prev_probs)
    next_brier = _brier(labels, next_probs)
    prev_logloss = _logloss(labels, prev_probs)
    next_logloss = _logloss(labels, next_probs)
    return {
        "previous_stage": previous_stage,
        "next_stage": next_stage,
        "previous_label": STAGE_LABELS.get(previous_stage, previous_stage),
        "next_label": STAGE_LABELS.get(next_stage, next_stage),
        "n_overlap": len(labels),
        "previous_brier": _round(prev_brier),
        "next_brier": _round(next_brier),
        "delta_brier_next_minus_previous": _round(
            None if prev_brier is None or next_brier is None else next_brier - prev_brier
        ),
        "previous_logloss": _round(prev_logloss),
        "next_logloss": _round(next_logloss),
        "delta_logloss_next_minus_previous": _round(
            None if prev_logloss is None or next_logloss is None else next_logloss - prev_logloss
        ),
        "avg_probability_move": _round(
            (sum(next_probs) - sum(prev_probs)) / len(labels) if labels else None
        ),
        "avg_abs_probability_move": _round(sum(abs_moves) / len(abs_moves) if abs_moves else None),
        "interpretation": (
            "improved"
            if prev_brier is not None and next_brier is not None and next_brier < prev_brier
            else "damaged"
            if prev_brier is not None and next_brier is not None and next_brier > prev_brier
            else "unknown"
        ),
    }


def _group_by_family(rows: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    groups[ALL_FAMILIES] = list(rows)
    for row in rows:
        groups[_family(row)].append(row)
    return dict(groups)


def build_weather_ablation(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    return build_weather_ablation_with_cache(rows, stage1_cache=None)


def build_weather_ablation_with_cache(
    rows: Sequence[Dict[str, Any]],
    *,
    stage1_cache: Optional[OUCache] = None,
) -> Dict[str, Any]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if stage_predictions(row, stage1_cache=stage1_cache).get("stage2_after_run_env") is None:
            continue
        usable = "usable" if _boolish(row.get("stage2_weather_model_usable", row.get("weather_model_usable"))) else "not_usable"
        exposure = str(row.get("stadium_weather_exposure") or "unknown")
        status = str(row.get("weather_source_status") or "unknown")
        groups[f"weather_model_{usable}"].append(row)
        groups[f"exposure:{exposure}"].append(row)
        groups[f"source_status:{status}"].append(row)

    out: Dict[str, Any] = {}
    for key, group_rows in sorted(groups.items()):
        deltas = [
            _safe_float(r.get("stage2_run_env_delta"))
            if _safe_float(r.get("stage2_run_env_delta")) is not None
            else _safe_float(r.get("current_state_value_stage2_run_env_delta"))
            for r in group_rows
        ]
        deltas = [d for d in deltas if d is not None]
        cmp_ = compare_stages(
            group_rows,
            "stage1_after_score_event_inference",
            "stage2_after_run_env",
            stage1_cache=stage1_cache,
        )
        out[key] = {
            "n_rows": len(group_rows),
            "n_delta": len(deltas),
            "avg_stage2_logit_delta": _round(sum(deltas) / len(deltas) if deltas else None),
            "avg_abs_stage2_logit_delta": _round(sum(abs(d) for d in deltas) / len(deltas) if deltas else None),
            "stage1_to_stage2": cmp_,
        }
    return out


def build_inference_ablation(
    rows: Sequence[Dict[str, Any]],
    *,
    stage1_cache: Optional[OUCache] = None,
) -> Dict[str, Any]:
    score_rows = [r for r in rows if _family(r) == SCORE_EVENT_TRANSITION]
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in score_rows:
        if stage_predictions(row, stage1_cache=stage1_cache).get("current_state_after_stage23") is None:
            continue
        inferred = _safe_int(row.get("inferred_runs"))
        risk = str(row.get("shadow_phantom_risk_band") or row.get("phantom_risk_band") or "unknown")
        groups[f"inferred_runs:{inferred if inferred is not None else 'unknown'}"].append(row)
        groups[f"phantom_risk:{risk}"].append(row)
    return {
        key: compare_stages(
            group_rows,
            "current_state_after_stage23",
            "stage1_after_score_event_inference",
            stage1_cache=stage1_cache,
        )
        for key, group_rows in sorted(groups.items())
    }


def build_market_anchoring(
    rows: Sequence[Dict[str, Any]],
    *,
    stage1_cache: Optional[OUCache] = None,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for family, group_rows in sorted(_group_by_family(rows).items()):
        cmp_ = compare_stages(
            group_rows,
            "market_ask_baseline",
            "final_runtime_fv",
            stage1_cache=stage1_cache,
        )
        residuals: List[float] = []
        high_model_losses = 0
        high_model_rows = 0
        for row in group_rows:
            y = _label(row)
            preds = stage_predictions(row, stage1_cache=stage1_cache)
            market = preds.get("market_ask_baseline")
            final = preds.get("final_runtime_fv")
            if market is None or final is None:
                continue
            residual = final - market
            residuals.append(residual)
            if residual >= 0.15:
                high_model_rows += 1
                if y == 0:
                    high_model_losses += 1
        out[family] = {
            **cmp_,
            "avg_final_minus_market": _round(sum(residuals) / len(residuals) if residuals else None),
            "avg_abs_final_minus_market": _round(sum(abs(v) for v in residuals) / len(residuals) if residuals else None),
            "high_model_edge_rows": high_model_rows,
            "high_model_edge_losses": high_model_losses,
        }
    return out


def _ask_bucket(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if value < 0.60:
        return "<0.60"
    if value < 0.70:
        return "0.60-0.69"
    if value < 0.80:
        return "0.70-0.79"
    if value < 0.90:
        return "0.80-0.89"
    return ">=0.90"


def _edge_bucket(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if value < -0.05:
        return "<-0.05"
    if value < 0.00:
        return "-0.05--0.00"
    if value < 0.03:
        return "0.00-0.03"
    if value < 0.06:
        return "0.03-0.06"
    if value < 0.10:
        return "0.06-0.10"
    if value < 0.15:
        return "0.10-0.15"
    return ">=0.15"


def _line_bucket(row: Mapping[str, Any]) -> str:
    value = _safe_float(row.get("line"))
    return f"O{value:.1f}" if value is not None else "unknown"


def _post_2023_bucket(row: Mapping[str, Any]) -> str:
    date = _date_value(row)
    if not date:
        return "unknown"
    return "post_2023" if date >= "2023-01-01" else "pre_2023"


def _profit_units(label: int, ask: float) -> float:
    return (1.0 / ask - 1.0) if label else -1.0


def _policy_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    stage1_cache: Optional[OUCache] = None,
) -> Dict[str, Any]:
    labeled_rows = 0
    selected_rows = 0
    selected_wins = 0
    profit_units = 0.0
    final_probs: List[float] = []
    labels: List[int] = []
    for row in rows:
        label = _label(row)
        if label is None:
            continue
        preds = stage_predictions(row, stage1_cache=stage1_cache)
        ask = preds.get("market_ask_baseline")
        final = preds.get("final_runtime_fv")
        if ask is None or final is None:
            continue
        labeled_rows += 1
        labels.append(label)
        final_probs.append(final)
        if final - ask <= 0:
            continue
        selected_rows += 1
        selected_wins += label
        profit_units += _profit_units(label, ask)

    brier = _brier(labels, final_probs)
    logloss = _logloss(labels, final_probs)
    return {
        "rows": len(rows),
        "labeled_rows": labeled_rows,
        "selected_rows": selected_rows,
        "selected_wins": selected_wins,
        "selected_losses": selected_rows - selected_wins,
        "selected_win_rate": _round(selected_wins / selected_rows, 6) if selected_rows else None,
        "selected_profit_units": _round(profit_units),
        "selected_roi_per_cost_unit": _round(profit_units / selected_rows, 6) if selected_rows else None,
        "final_brier": _round(brier),
        "final_logloss": _round(logloss),
        "final_auc": _round(_auc_pairwise(labels, final_probs)),
    }


def _bucketed_policy_summaries(
    rows: Sequence[Dict[str, Any]],
    *,
    stage1_cache: Optional[OUCache] = None,
) -> Dict[str, Any]:
    groups: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        "by_signal_model_family": defaultdict(list),
        "by_line": defaultdict(list),
        "by_ask_bucket": defaultdict(list),
        "by_current_state_edge_bucket": defaultdict(list),
        "by_post_2023": defaultdict(list),
    }
    for row in rows:
        preds = stage_predictions(row, stage1_cache=stage1_cache)
        ask = preds.get("market_ask_baseline")
        current = preds.get("current_state_after_stage23") or preds.get("current_state_stage1_poisson")
        groups["by_signal_model_family"][_family(row)].append(row)
        groups["by_line"][_line_bucket(row)].append(row)
        groups["by_ask_bucket"][_ask_bucket(ask)].append(row)
        edge = None if ask is None or current is None else current - ask
        groups["by_current_state_edge_bucket"][_edge_bucket(edge)].append(row)
        groups["by_post_2023"][_post_2023_bucket(row)].append(row)

    out: Dict[str, Any] = {}
    for group_name, buckets in groups.items():
        out[group_name] = {
            bucket: _policy_summary(bucket_rows, stage1_cache=stage1_cache)
            for bucket, bucket_rows in sorted(buckets.items())
        }
    return out


def _load_stage1_cache(stage1_cache_path: Optional[Path]) -> Optional[OUCache]:
    if not stage1_cache_path:
        return None
    return OUCache(stage1_cache_path)


def build_report(
    rows: Sequence[Dict[str, Any]],
    *,
    input_path: Path,
    mode: str,
    min_date: str = "",
    max_date: str = "",
    min_rows: int = 20,
    stage1_cache: Optional[OUCache] = None,
    stage1_cache_path: Optional[Path] = None,
    stage1_cache_label: str = "logged_runtime",
) -> Dict[str, Any]:
    if stage1_cache is None and stage1_cache_path is not None:
        stage1_cache = _load_stage1_cache(stage1_cache_path)
    filtered = filter_rows(rows, mode=mode, min_date=min_date, max_date=max_date)
    labeled = [row for row in filtered if _label(row) is not None]
    as_of_date = max((_date_value(row) for row in filtered if _date_value(row)), default=max_date)
    groups = _group_by_family(labeled)

    stage_summary: Dict[str, Dict[str, Any]] = {}
    incremental: Dict[str, List[Dict[str, Any]]] = {}
    for family, group_rows in sorted(groups.items()):
        ladder = LADDERS.get(family, LADDERS[ALL_FAMILIES])
        stage_summary[family] = {
            stage: summarize_stage(group_rows, stage, stage1_cache=stage1_cache)
            for stage in ladder
        }
        incremental[family] = [
            compare_stages(group_rows, prev, nxt, stage1_cache=stage1_cache)
            for prev, nxt in zip(ladder, ladder[1:])
        ]

    missing_by_stage = {
        stage: sum(1 for row in labeled if stage_predictions(row, stage1_cache=stage1_cache).get(stage) is None)
        for stage in STAGE_LABELS
    }
    recommendations = _recommend(stage_summary, incremental, min_rows=min_rows)
    return {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "as_of_date": as_of_date,
        "mode": mode,
        "min_date": min_date,
        "max_date": max_date,
        "input_path": str(input_path),
        "stage1_cache": {
            "label": stage1_cache_label,
            "path": str(stage1_cache_path) if stage1_cache_path else "",
            "mode": "recomputed_from_cache_with_logged_stage2_stage3_deltas" if stage1_cache is not None else "logged_runtime_values",
        },
        "row_counts": {
            "input_rows": len(rows),
            "filtered_rows": len(filtered),
            "labeled_rows": len(labeled),
            "families": {family: len(group_rows) for family, group_rows in sorted(groups.items())},
        },
        "stage_summaries": stage_summary,
        "incremental_comparisons": incremental,
        "weather_ablation": build_weather_ablation_with_cache(labeled, stage1_cache=stage1_cache),
        "score_event_inference_ablation": build_inference_ablation(labeled, stage1_cache=stage1_cache),
        "market_anchoring": build_market_anchoring(labeled, stage1_cache=stage1_cache),
        "bucket_diagnostics": _bucketed_policy_summaries(labeled, stage1_cache=stage1_cache),
        "missing_labeled_predictions_by_stage": missing_by_stage,
        "recommendations": recommendations,
        "notes": [
            "delta_brier_next_minus_previous < 0 means the next stage improved Brier on overlapping rows.",
            "Market ask is treated as the no-alpha baseline for market anchoring.",
            "selected_profit_units assumes a $1 cost unit at market ask for rows where final FV exceeds ask; it is a diagnostic selection proxy, not a live order replay.",
            "Stage-2 includes park/weather/density/hr-factor run environment; weather_model_usable splits isolate rows with clean Weather v2 outdoor context.",
            "When stage1_cache.mode is recomputed_from_cache_with_logged_stage2_stage3_deltas, Stage-1 is recomputed from the supplied cache while later adjustments use logged runtime deltas.",
        ],
    }


def _recommend(
    stage_summary: Mapping[str, Mapping[str, Any]],
    incremental: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    min_rows: int,
) -> List[str]:
    recs: List[str] = []
    for family, comparisons in incremental.items():
        if family == ALL_FAMILIES:
            continue
        for cmp_ in comparisons:
            n = int(cmp_.get("n_overlap") or 0)
            delta = _safe_float(cmp_.get("delta_brier_next_minus_previous"))
            if n < min_rows or delta is None:
                continue
            if delta > 0:
                recs.append(
                    f"{family}: {cmp_.get('next_label')} damaged Brier vs {cmp_.get('previous_label')} "
                    f"on {n} rows (delta={delta:+.4f}); shadow-audit before promotion/tuning."
                )
            elif delta < -0.005:
                recs.append(
                    f"{family}: {cmp_.get('next_label')} improved Brier vs {cmp_.get('previous_label')} "
                    f"on {n} rows (delta={delta:+.4f}); candidate for deeper walk-forward review."
                )
    if not recs:
        recs.append(
            f"No stage has at least {min_rows} overlapping labeled rows with a decisive Brier signal yet."
        )
    return recs


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_markdown(report: Mapping[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# Fair Value Stage Ablation Report")
    lines.append("")
    lines.append(f"- Generated: `{report.get('generated_at_utc')}`")
    lines.append(f"- As of date: `{report.get('as_of_date') or ''}`")
    lines.append(f"- Input: `{report.get('input_path')}`")
    stage1_cache = report.get("stage1_cache", {}) or {}
    lines.append(
        f"- Stage-1 cache: `{stage1_cache.get('label') or 'logged_runtime'}` "
        f"({stage1_cache.get('mode') or 'logged_runtime_values'})"
    )
    counts = report.get("row_counts", {}) or {}
    lines.append(f"- Rows: input `{counts.get('input_rows')}`, filtered `{counts.get('filtered_rows')}`, labeled `{counts.get('labeled_rows')}`")
    lines.append("")

    lines.append("## Recommendations")
    for rec in report.get("recommendations", []) or []:
        lines.append(f"- {rec}")
    lines.append("")

    stage_summaries = report.get("stage_summaries", {}) or {}
    for family, summaries in stage_summaries.items():
        lines.append(f"## Stage Metrics: {family}")
        lines.append("| Stage | n | Emp Rate | Avg Prob | Brier | Logloss | AUC | Cal Slope |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for stage, stats in (summaries or {}).items():
            lines.append(
                "| {label} | {n} | {emp} | {avg} | {brier} | {logloss} | {auc} | {slope} |".format(
                    label=stats.get("label") or stage,
                    n=stats.get("n") or 0,
                    emp=_fmt(stats.get("empirical_rate")),
                    avg=_fmt(stats.get("avg_prob")),
                    brier=_fmt(stats.get("brier")),
                    logloss=_fmt(stats.get("logloss")),
                    auc=_fmt(stats.get("auc")),
                    slope=_fmt(stats.get("calibration_slope")),
                )
            )
        lines.append("")

    lines.append("## Incremental Deltas")
    for family, comparisons in (report.get("incremental_comparisons", {}) or {}).items():
        lines.append(f"### {family}")
        lines.append("| Previous -> Next | n | Delta Brier | Delta Logloss | Avg Abs Move | Read |")
        lines.append("|---|---:|---:|---:|---:|---|")
        for cmp_ in comparisons or []:
            lines.append(
                "| {prev} -> {nxt} | {n} | {db} | {dl} | {move} | {read} |".format(
                    prev=cmp_.get("previous_label"),
                    nxt=cmp_.get("next_label"),
                    n=cmp_.get("n_overlap") or 0,
                    db=_fmt(cmp_.get("delta_brier_next_minus_previous")),
                    dl=_fmt(cmp_.get("delta_logloss_next_minus_previous")),
                    move=_fmt(cmp_.get("avg_abs_probability_move")),
                    read=cmp_.get("interpretation"),
                )
            )
        lines.append("")

    lines.append("## Weather Ablation")
    lines.append("| Group | n | Avg S2 Delta | Avg Abs S2 Delta | Brier Delta |")
    lines.append("|---|---:|---:|---:|---:|")
    for key, stats in (report.get("weather_ablation", {}) or {}).items():
        cmp_ = stats.get("stage1_to_stage2", {}) or {}
        lines.append(
            f"| {key} | {stats.get('n_rows') or 0} | {_fmt(stats.get('avg_stage2_logit_delta'))} | "
            f"{_fmt(stats.get('avg_abs_stage2_logit_delta'))} | "
            f"{_fmt(cmp_.get('delta_brier_next_minus_previous'))} |"
        )
    lines.append("")

    bucket_diags = report.get("bucket_diagnostics", {}) or {}
    if bucket_diags:
        lines.append("## Policy Proxy Diagnostics")
        for group_name, buckets in bucket_diags.items():
            lines.append(f"### {group_name}")
            lines.append("| Bucket | Labeled | Selected | Win Rate | Profit Units | ROI/Cost | Brier | AUC |")
            lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
            for bucket, stats in (buckets or {}).items():
                lines.append(
                    "| {bucket} | {labeled} | {selected} | {wr} | {profit} | {roi} | {brier} | {auc} |".format(
                        bucket=bucket,
                        labeled=stats.get("labeled_rows") or 0,
                        selected=stats.get("selected_rows") or 0,
                        wr=_fmt(stats.get("selected_win_rate")),
                        profit=_fmt(stats.get("selected_profit_units")),
                        roi=_fmt(stats.get("selected_roi_per_cost_unit")),
                        brier=_fmt(stats.get("final_brier")),
                        auc=_fmt(stats.get("final_auc")),
                    )
                )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_outputs(report: Mapping[str, Any], *, output_root: Path, output_stem: str) -> Dict[str, str]:
    output_root.mkdir(parents=True, exist_ok=True)
    as_of = str(report.get("as_of_date") or "unknown")
    json_path = output_root / f"{output_stem}.json"
    md_path = output_root / f"{output_stem}.md"
    dated_json_path = output_root / f"{as_of}_{output_stem}.json"
    dated_md_path = output_root / f"{as_of}_{output_stem}.md"
    text = json.dumps(report, indent=2, sort_keys=True)
    json_path.write_text(text, encoding="utf-8")
    dated_json_path.write_text(text, encoding="utf-8")
    md = render_markdown(report)
    md_path.write_text(md, encoding="utf-8")
    dated_md_path.write_text(md, encoding="utf-8")
    return {
        "json": str(json_path),
        "markdown": str(md_path),
        "dated_json": str(dated_json_path),
        "dated_markdown": str(dated_md_path),
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build a fair-value stage ablation report.")
    p.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--output-stem", type=str, default=DEFAULT_OUTPUT_STEM)
    p.add_argument("--mode", type=str, default="live")
    p.add_argument("--min-date", type=str, default="")
    p.add_argument("--max-date", type=str, default="")
    p.add_argument("--min-rows", type=int, default=20)
    p.add_argument("--stage1-cache-path", type=Path, default=None)
    p.add_argument("--stage1-cache-label", type=str, default="logged_runtime")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    rows = load_rows(args.input_path)
    report = build_report(
        rows,
        input_path=args.input_path,
        mode=str(args.mode or ""),
        min_date=str(args.min_date or ""),
        max_date=str(args.max_date or ""),
        min_rows=int(args.min_rows),
        stage1_cache_path=args.stage1_cache_path,
        stage1_cache_label=str(args.stage1_cache_label or "logged_runtime"),
    )
    paths = write_outputs(report, output_root=args.output_root, output_stem=args.output_stem)
    print(json.dumps({"paths": paths, "row_counts": report.get("row_counts", {})}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
