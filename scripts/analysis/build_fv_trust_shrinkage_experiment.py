#!/usr/bin/env python3
"""
Build an FV trust/shrinkage experiment from calibration opportunities.

This report is analysis-only. It compares raw runtime fair value to market-
anchored variants that shrink model disagreement toward ask or no-vig market
midpoint when Stage-1 support is weak.

Inputs:
  data/analysis_output/calibration_opportunity_training/
    calibration_opportunity_training_table.jsonl

Outputs:
  data/analysis_output/fv_trust_shrinkage/
    fv_trust_shrinkage_report.json
    fv_trust_shrinkage_report.md
    fv_trust_shrinkage_predictions.jsonl
    <as_of_date>_fv_trust_shrinkage_report.json
    <as_of_date>_fv_trust_shrinkage_report.md
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


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_INPUT_PATH = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "calibration_opportunity_training"
    / "calibration_opportunity_training_table.jsonl"
)
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "fv_trust_shrinkage"
DEFAULT_OUTPUT_STEM = "fv_trust_shrinkage"

SCORE_EVENT_TRANSITION = "score_event_transition"
NO_SCORE_DRIFT = "no_score_drift"
FAMILIES = (SCORE_EVENT_TRANSITION, NO_SCORE_DRIFT)
ALL_FAMILIES = "__all__"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build FV trust/shrinkage experiment.")
    p.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--output-stem", type=str, default=DEFAULT_OUTPUT_STEM)
    p.add_argument("--mode", choices=["live", "paper", "both"], default="live")
    p.add_argument("--family", choices=["all", *FAMILIES], default="all")
    p.add_argument("--min-date", type=str, default="")
    p.add_argument("--max-date", type=str, default="")
    p.add_argument(
        "--taus",
        type=str,
        default="40,80,160",
        help="Comma-separated effective-n half-life values for trust weights.",
    )
    p.add_argument(
        "--edge-thresholds",
        type=str,
        default="0.03,0.05,0.08",
        help="Comma-separated p-ask thresholds for paper selection summaries.",
    )
    p.add_argument("--val-frac", type=float, default=0.20)
    p.add_argument("--test-frac", type=float, default=0.20)
    p.add_argument("--min-family-rows", type=int, default=20)
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
    if not math.isfinite(out):
        return None
    return out


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _clip_prob(value: Any) -> Optional[float]:
    prob = _safe_float(value)
    if prob is None or not 0.0 < prob < 1.0:
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


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def _date_value(row: Mapping[str, Any]) -> str:
    value = str(row.get("session_date") or row.get("date") or "").strip()
    if len(value) >= 10:
        return value[:10]
    ts = str(row.get("ts") or row.get("recorded_at") or "").strip()
    return ts[:10] if len(ts) >= 10 else ""


def _family(row: Mapping[str, Any]) -> str:
    value = str(row.get("signal_model_family") or row.get("state_value_strategy") or "").strip()
    return value or "unknown"


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


def _read_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
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
                raise ValueError(f"{path}:{line_no}: invalid JSONL row: {exc}") from exc
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _parse_float_csv(raw: str, *, name: str, positive: bool = False) -> List[float]:
    values: List[float] = []
    for part in str(raw or "").split(","):
        text = part.strip()
        if not text:
            continue
        try:
            value = float(text)
        except ValueError as exc:
            raise SystemExit(f"Invalid --{name} value '{text}'") from exc
        if positive and value <= 0:
            raise SystemExit(f"--{name} values must be positive")
        values.append(value)
    if not values:
        raise SystemExit(f"--{name} must include at least one value")
    return sorted(set(values))


def filter_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    mode: str,
    family: str,
    min_date: str,
    max_date: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        row_mode = str(row.get("mode") or "").strip()
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
        if _label(row) is None:
            continue
        if _clip_prob(row.get("decision_ask")) is None:
            continue
        if _raw_fv(row) is None:
            continue
        out.append(dict(row))
    return out


def _raw_fv(row: Mapping[str, Any]) -> Optional[float]:
    for key in (
        "fair_value",
        "fair_value_calibrated",
        "fair_value_raw",
        "current_state_value_fv_raw",
        "inferred_state_base_poisson",
    ):
        prob = _clip_prob(row.get(key))
        if prob is not None:
            return prob
    return None


def _anchor_ask(row: Mapping[str, Any]) -> Optional[float]:
    return _clip_prob(row.get("decision_ask"))


def _anchor_mid_no_vig(row: Mapping[str, Any], *, fallback_to_ask: bool) -> Optional[float]:
    mid = _clip_prob(row.get("decision_market_mid_no_vig"))
    if mid is not None:
        return mid
    return _anchor_ask(row) if fallback_to_ask else None


def _support_primary(row: Mapping[str, Any]) -> Optional[float]:
    family = _family(row)
    if family == SCORE_EVENT_TRANSITION:
        return _safe_float(row.get("inferred_state_effective_n_proxy"))
    if family == NO_SCORE_DRIFT:
        return _safe_float(row.get("current_state_value_effective_n_proxy"))
    return (
        _safe_float(row.get("inferred_state_effective_n_proxy"))
        or _safe_float(row.get("current_state_value_effective_n_proxy"))
    )


def _support_fallback(row: Mapping[str, Any]) -> Optional[float]:
    family = _family(row)
    if family == SCORE_EVENT_TRANSITION:
        return (
            _safe_float(row.get("inferred_state_effective_n_proxy"))
            or _safe_float(row.get("current_state_value_effective_n_proxy"))
        )
    if family == NO_SCORE_DRIFT:
        return (
            _safe_float(row.get("current_state_value_effective_n_proxy"))
            or _safe_float(row.get("inferred_state_effective_n_proxy"))
        )
    return _support_primary(row)


def _logged_trust(row: Mapping[str, Any]) -> Optional[float]:
    family = _family(row)
    if family == SCORE_EVENT_TRANSITION:
        trust = _safe_float(row.get("inferred_state_stage1_trust_weight"))
        if trust is not None:
            return trust
        return _safe_float(row.get("current_state_value_stage1_trust_weight"))
    if family == NO_SCORE_DRIFT:
        trust = _safe_float(row.get("current_state_value_stage1_trust_weight"))
        if trust is not None:
            return trust
        return _safe_float(row.get("inferred_state_stage1_trust_weight"))
    return _safe_float(row.get("inferred_state_stage1_trust_weight"))


def _weight_from_support(support: Optional[float], tau: float) -> float:
    if support is None:
        return 0.0
    return max(0.0, min(1.0, 1.0 - math.exp(-max(0.0, support) / tau)))


def _shrink_logit(raw: float, anchor: float, weight: float) -> float:
    weight = max(0.0, min(1.0, float(weight)))
    return min(max(_sigmoid(_logit(anchor) + weight * (_logit(raw) - _logit(anchor))), 1e-6), 1.0 - 1e-6)


def _profit_units(row: Mapping[str, Any]) -> Optional[float]:
    existing = _safe_float(row.get("target_taker_profit_units"))
    if existing is not None:
        return existing
    ask = _anchor_ask(row)
    label = _label(row)
    if ask is None or label is None:
        return None
    return (1.0 / ask - 1.0) if label == 1 else -1.0


def _build_date_split_map(rows: Sequence[Mapping[str, Any]], *, val_frac: float, test_frac: float) -> Dict[str, str]:
    dates = sorted({d for d in (_date_value(row) for row in rows) if d})
    if not dates:
        return {}
    n = len(dates)
    n_test = int(round(n * test_frac)) if test_frac > 0 else 0
    n_val = int(round(n * val_frac)) if val_frac > 0 else 0
    if n >= 3:
        n_test = max(1, min(n_test, n - 2))
        n_val = max(1, min(n_val, n - n_test - 1))
    else:
        n_test = 0
        n_val = 0
    train_cut = n - n_val - n_test
    val_cut = n - n_test
    out: Dict[str, str] = {}
    for idx, date in enumerate(dates):
        if idx < train_cut:
            split = "train"
        elif idx < val_cut:
            split = "validation"
        else:
            split = "test"
        out[date] = split
    return out


def build_variant_probabilities(
    row: Mapping[str, Any],
    *,
    taus: Sequence[float],
) -> Dict[str, float]:
    raw = _raw_fv(row)
    ask = _anchor_ask(row)
    if raw is None or ask is None:
        return {}
    mid = _anchor_mid_no_vig(row, fallback_to_ask=False)
    mid_or_ask = _anchor_mid_no_vig(row, fallback_to_ask=True)

    probs: Dict[str, float] = {
        "market_ask": ask,
        "raw_fv": raw,
    }
    if mid is not None:
        probs["market_mid_no_vig"] = mid
    if mid_or_ask is not None:
        probs["market_mid_no_vig_or_ask"] = mid_or_ask

    logged = _logged_trust(row)
    if logged is not None:
        probs["shrink_logged_trust_to_ask"] = _shrink_logit(raw, ask, logged)
        if mid_or_ask is not None:
            probs["shrink_logged_trust_to_mid_no_vig_or_ask"] = _shrink_logit(raw, mid_or_ask, logged)

    primary_support = _support_primary(row)
    fallback_support = _support_fallback(row)
    for tau in taus:
        tau_label = f"tau{int(tau) if float(tau).is_integer() else str(tau).replace('.', 'p')}"
        for source_name, support in (
            ("primary_support", primary_support),
            ("fallback_support", fallback_support),
        ):
            weight = _weight_from_support(support, tau)
            probs[f"shrink_{source_name}_{tau_label}_to_ask"] = _shrink_logit(raw, ask, weight)
            if mid_or_ask is not None:
                probs[f"shrink_{source_name}_{tau_label}_to_mid_no_vig_or_ask"] = _shrink_logit(
                    raw,
                    mid_or_ask,
                    weight,
                )
    return probs


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
        return {"intercept": None, "slope": None, "status": "not_enough_class_balance"}

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
        if abs(beta0) > 100 or abs(beta1) > 100:
            status = "unstable_or_separated"
            break
        if abs(delta0) + abs(delta1) < 1e-7:
            break
    if abs(beta0) > 50 or abs(beta1) > 50:
        return {"intercept": None, "slope": None, "status": "unstable_or_separated"}
    return {"intercept": _round(beta0), "slope": _round(beta1), "status": status}


def _probability_metrics(rows: Sequence[Mapping[str, Any]], variant: str) -> Dict[str, Any]:
    labels: List[int] = []
    probs: List[float] = []
    for row in rows:
        label = _label(row)
        prob = _clip_prob((row.get("_variant_probs") or {}).get(variant))
        if label is None or prob is None:
            continue
        labels.append(label)
        probs.append(prob)
    calib = _calibration_slope_intercept(labels, probs)
    return {
        "variant": variant,
        "n": len(labels),
        "positive_labels": sum(1 for y in labels if y == 1),
        "negative_labels": sum(1 for y in labels if y == 0),
        "empirical_rate": _round(sum(labels) / len(labels)) if labels else None,
        "avg_prob": _round(sum(probs) / len(probs)) if probs else None,
        "brier": _round(_brier(labels, probs)),
        "logloss": _round(_logloss(labels, probs)),
        "auc": _round(_auc_pairwise(labels, probs)),
        "calibration_intercept": calib.get("intercept"),
        "calibration_slope": calib.get("slope"),
        "calibration_status": calib.get("status"),
    }


def _policy_metrics(
    rows: Sequence[Mapping[str, Any]],
    variant: str,
    *,
    threshold: float,
) -> Dict[str, Any]:
    selected: List[Tuple[Mapping[str, Any], float, float]] = []
    for row in rows:
        probs = row.get("_variant_probs") or {}
        prob = _clip_prob(probs.get(variant))
        ask = _anchor_ask(row)
        profit = _profit_units(row)
        if prob is None or ask is None or profit is None:
            continue
        edge = prob - ask
        if edge >= threshold:
            selected.append((row, edge, profit))
    labels = [_label(row) for row, _, _ in selected]
    labels_i = [int(x) for x in labels if x is not None]
    profits = [profit for _, _, profit in selected]
    asks = [_anchor_ask(row) for row, _, _ in selected]
    edges = [edge for _, edge, _ in selected]
    return {
        "threshold": threshold,
        "selected": len(selected),
        "win_rate": _round(sum(labels_i) / len(labels_i)) if labels_i else None,
        "profit_units": _round(sum(profits)) if profits else 0.0,
        "roi_per_1usd_stake": _round(sum(profits) / len(profits)) if profits else None,
        "avg_edge": _round(sum(edges) / len(edges)) if edges else None,
        "avg_ask": _round(sum(a for a in asks if a is not None) / len([a for a in asks if a is not None]))
        if any(a is not None for a in asks)
        else None,
    }


def _support_coverage(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    total = len(rows)

    def rate(count: int) -> Optional[float]:
        return _round(count / total) if total else None

    primary = sum(1 for row in rows if _support_primary(row) is not None)
    fallback = sum(1 for row in rows if _support_fallback(row) is not None)
    logged = sum(1 for row in rows if _logged_trust(row) is not None)
    mid = sum(1 for row in rows if _anchor_mid_no_vig(row, fallback_to_ask=False) is not None)
    return {
        "rows": total,
        "primary_support_rows": primary,
        "primary_support_rate": rate(primary),
        "fallback_support_rows": fallback,
        "fallback_support_rate": rate(fallback),
        "logged_trust_rows": logged,
        "logged_trust_rate": rate(logged),
        "mid_no_vig_rows": mid,
        "mid_no_vig_rate": rate(mid),
    }


def _variant_names(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    names = set()
    for row in rows:
        names.update((row.get("_variant_probs") or {}).keys())
    preferred = [
        "market_ask",
        "market_mid_no_vig",
        "market_mid_no_vig_or_ask",
        "raw_fv",
        "shrink_logged_trust_to_ask",
        "shrink_logged_trust_to_mid_no_vig_or_ask",
    ]
    return [x for x in preferred if x in names] + sorted(names - set(preferred))


def build_report(
    rows: Sequence[Dict[str, Any]],
    *,
    taus: Sequence[float],
    edge_thresholds: Sequence[float],
    val_frac: float,
    test_frac: float,
    config: Mapping[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    split_map = _build_date_split_map(rows, val_frac=val_frac, test_frac=test_frac)
    enriched: List[Dict[str, Any]] = []
    predictions: List[Dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        date = _date_value(r)
        split = split_map.get(date, "train")
        r["_split"] = split
        probs = build_variant_probabilities(r, taus=taus)
        r["_variant_probs"] = probs
        enriched.append(r)
        ask = _anchor_ask(r)
        predictions.append(
            {
                "session_date": date,
                "split": split,
                "mode": r.get("mode"),
                "candidate_id": r.get("candidate_id"),
                "game_pk": r.get("game_pk"),
                "line": r.get("line"),
                "family": _family(r),
                "label": _label(r),
                "decision_ask": ask,
                "market_mid_no_vig": _anchor_mid_no_vig(r, fallback_to_ask=False),
                "raw_fv": _raw_fv(r),
                "primary_support": _support_primary(r),
                "fallback_support": _support_fallback(r),
                "logged_trust": _logged_trust(r),
                "variant_probs": probs,
                "variant_edges_to_ask": {
                    key: _round(prob - ask) if ask is not None else None
                    for key, prob in probs.items()
                },
            }
        )

    by_family: Dict[str, Any] = {}
    family_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in enriched:
        family_groups[_family(row)].append(row)
        family_groups[ALL_FAMILIES].append(row)
    for family in FAMILIES:
        family_groups.setdefault(family, [])

    for family, family_rows in sorted(family_groups.items()):
        variants = _variant_names(family_rows)
        split_payload: Dict[str, Any] = {}
        for split in ("overall", "train", "validation", "test"):
            split_rows = family_rows if split == "overall" else [r for r in family_rows if r.get("_split") == split]
            split_payload[split] = {
                "rows": len(split_rows),
                "probability_metrics": {
                    variant: _probability_metrics(split_rows, variant)
                    for variant in variants
                },
                "threshold_policies": {
                    variant: {
                        f"{threshold:.3f}": _policy_metrics(split_rows, variant, threshold=threshold)
                        for threshold in edge_thresholds
                    }
                    for variant in variants
                },
            }

        def brier_for(variant: str, split: str = "test") -> float:
            metric = (split_payload.get(split) or {}).get("probability_metrics", {}).get(variant) or {}
            brier = _safe_float(metric.get("brier"))
            return brier if brier is not None else float("inf")

        rank_split = "test" if split_payload.get("test", {}).get("rows", 0) else "overall"
        ranked = sorted(variants, key=lambda variant: brier_for(variant, rank_split))
        by_family[family] = {
            "rows": len(family_rows),
            "support_coverage": _support_coverage(family_rows),
            "rank_split": rank_split,
            "best_by_brier": ranked[:8],
            "splits": split_payload,
        }

    return {
        "generated_at_utc": _now_iso(),
        "description": (
            "Analysis-only FV trust/shrinkage experiment. Raw runtime FV is "
            "shrunk toward market ask or no-vig midpoint using Stage-1 support."
        ),
        "config": dict(config),
        "counts": {
            "input_rows": len(rows),
            "split_dates": {
                split: sorted(date for date, assigned in split_map.items() if assigned == split)
                for split in ("train", "validation", "test")
            },
            "rows_by_family": {
                family: len([row for row in enriched if _family(row) == family])
                for family in sorted({_family(row) for row in enriched})
            },
        },
        "variant_notes": {
            "raw_fv": "Runtime fair_value from the calibration opportunity row.",
            "market_ask": "Decision ask baseline.",
            "market_mid_no_vig": "Over no-vig midpoint when paired Under book is available.",
            "market_mid_no_vig_or_ask": "No-vig midpoint with ask fallback.",
            "shrink_*": "logit(anchor) + trust_weight * (logit(raw_fv) - logit(anchor)).",
            "primary_support": "score_event_transition uses inferred-state support; no_score_drift uses current-state support.",
            "fallback_support": "Uses primary support, then the other state support if primary is missing.",
        },
        "by_family": by_family,
    }, predictions


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> List[str]:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(_fmt(value) for value in row) + " |")
    return out


def render_markdown(report: Mapping[str, Any]) -> str:
    lines: List[str] = [
        "# FV Trust Shrinkage Experiment",
        "",
        f"- Generated: {report.get('generated_at_utc')}",
        f"- Rows: {(report.get('counts') or {}).get('input_rows')}",
        "- Behavior: analysis-only; no live FV/gate logic changed.",
        "",
        "## Design",
        "",
        "Each shrinkage variant uses `logit(anchor) + w * (logit(raw_fv) - logit(anchor))`.",
        "`w` comes from Stage-1 support, so thin/fallback-heavy states are pulled toward market.",
        "",
    ]
    for family, payload in (report.get("by_family") or {}).items():
        lines.extend(["", f"## {family}", ""])
        coverage = payload.get("support_coverage") or {}
        lines.extend(_markdown_table(
            ["Rows", "Primary Support", "Fallback Support", "Logged Trust", "No-Vig Mid"],
            [[
                coverage.get("rows"),
                coverage.get("primary_support_rate"),
                coverage.get("fallback_support_rate"),
                coverage.get("logged_trust_rate"),
                coverage.get("mid_no_vig_rate"),
            ]],
        ))
        rank_split = payload.get("rank_split") or "overall"
        metrics = (((payload.get("splits") or {}).get(rank_split) or {}).get("probability_metrics") or {})
        best = payload.get("best_by_brier") or []
        metric_rows = []
        for variant in best[:8]:
            metric = metrics.get(variant) or {}
            metric_rows.append([
                variant,
                metric.get("n"),
                metric.get("empirical_rate"),
                metric.get("avg_prob"),
                metric.get("brier"),
                metric.get("logloss"),
                metric.get("auc"),
                metric.get("calibration_slope"),
            ])
        lines.extend(["", f"### Best Brier Variants ({rank_split})"])
        lines.extend(_markdown_table(
            ["Variant", "N", "Emp Rate", "Avg Prob", "Brier", "Logloss", "AUC", "Cal Slope"],
            metric_rows or [["none", 0, None, None, None, None, None, None]],
        ))
        policy_rows = []
        threshold_policies = (((payload.get("splits") or {}).get(rank_split) or {}).get("threshold_policies") or {})
        for variant in best[:5]:
            threshold_map = threshold_policies.get(variant) or {}
            for threshold in ("0.030", "0.050", "0.080"):
                pol = threshold_map.get(threshold)
                if not pol:
                    continue
                policy_rows.append([
                    variant,
                    threshold,
                    pol.get("selected"),
                    pol.get("win_rate"),
                    pol.get("profit_units"),
                    pol.get("roi_per_1usd_stake"),
                    pol.get("avg_edge"),
                ])
        lines.extend(["", f"### Paper Selection At Ask ({rank_split})"])
        lines.extend(_markdown_table(
            ["Variant", "Edge >=", "Selected", "Win Rate", "Profit Units", "ROI / $1", "Avg Edge"],
            policy_rows or [["none", "n/a", 0, None, 0, None, None]],
        ))
    lines.extend([
        "",
        "## Notes",
        "",
        "- Best variant ranking is descriptive, not promotable. Use walk-forward before any runtime change.",
        "- Score-event and no-score drift are evaluated separately because their support source and adverse-selection profile differ.",
    ])
    return "\n".join(lines) + "\n"


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(dict(row), sort_keys=True) + "\n")


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    if args.min_date:
        datetime.strptime(args.min_date, "%Y-%m-%d")
    if args.max_date:
        datetime.strptime(args.max_date, "%Y-%m-%d")
    if args.min_date and args.max_date and args.min_date > args.max_date:
        raise SystemExit("--min-date must be <= --max-date")
    if args.val_frac < 0 or args.test_frac < 0 or args.val_frac + args.test_frac >= 1.0:
        raise SystemExit("--val-frac and --test-frac must be >= 0 and sum to < 1")

    taus = _parse_float_csv(args.taus, name="taus", positive=True)
    edge_thresholds = _parse_float_csv(args.edge_thresholds, name="edge-thresholds", positive=False)
    rows = filter_rows(
        _read_rows(args.input_path),
        mode=args.mode,
        family=args.family,
        min_date=args.min_date,
        max_date=args.max_date,
    )
    if args.strict and len(rows) < args.min_family_rows:
        raise SystemExit(f"Strict mode failed: only {len(rows)} usable rows.")

    report, predictions = build_report(
        rows,
        taus=taus,
        edge_thresholds=edge_thresholds,
        val_frac=args.val_frac,
        test_frac=args.test_frac,
        config={
            "input_path": str(args.input_path),
            "output_root": str(args.output_root),
            "mode": args.mode,
            "family": args.family,
            "min_date": args.min_date or None,
            "max_date": args.max_date or None,
            "taus": taus,
            "edge_thresholds": edge_thresholds,
            "val_frac": args.val_frac,
            "test_frac": args.test_frac,
            "min_family_rows": args.min_family_rows,
        },
    )
    if args.strict:
        for family in FAMILIES if args.family == "all" else (args.family,):
            rows_for_family = ((report.get("by_family") or {}).get(family) or {}).get("rows", 0)
            if int(rows_for_family or 0) < args.min_family_rows:
                raise SystemExit(f"Strict mode failed: {family} has only {rows_for_family} rows.")

    args.output_root.mkdir(parents=True, exist_ok=True)
    as_of = args.max_date or max((_date_value(row) for row in rows), default=datetime.now().date().isoformat())
    report_path = args.output_root / f"{args.output_stem}_report.json"
    md_path = args.output_root / f"{args.output_stem}_report.md"
    pred_path = args.output_root / f"{args.output_stem}_predictions.jsonl"
    dated_report_path = args.output_root / f"{as_of}_{args.output_stem}_report.json"
    dated_md_path = args.output_root / f"{as_of}_{args.output_stem}_report.md"
    _write_json(report_path, report)
    _write_json(dated_report_path, report)
    md = render_markdown(report)
    md_path.write_text(md, encoding="utf-8")
    dated_md_path.write_text(md, encoding="utf-8")
    _write_jsonl(pred_path, predictions)
    print(f"Wrote {report_path}")
    print(f"Wrote {md_path}")
    print(f"Wrote {pred_path}")


if __name__ == "__main__":
    main()
