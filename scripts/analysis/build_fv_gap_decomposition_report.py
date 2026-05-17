#!/usr/bin/env python3
"""
Build an FV gap decomposition report from calibration-opportunity rows.

This is the "why is FV above market?" companion to the stage ablation report.
It keeps the market as the reference point and decomposes the final model gap
into decision-time components:

  market ask / no-vig mid
  current-state Stage-1 Poisson and empirical
  inferred-state Stage-1 Poisson and empirical
  Stage-2 weather/park delta
  Stage-3 team-offense delta
  final runtime FV

The report is analysis-only. It does not write live runtime artifacts.

Outputs:
  data/analysis_output/fv_gap_decomposition/
    fv_gap_decomposition_report.json
    fv_gap_decomposition_report.md
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
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "fv_gap_decomposition"
DEFAULT_OUTPUT_STEM = "fv_gap_decomposition_report"

SCORE_EVENT_TRANSITION = "score_event_transition"
NO_SCORE_DRIFT = "no_score_drift"
ALL = "__all__"

PROBABILITY_FIELDS = {
    "market_ask": "prob_market_ask",
    "market_mid_no_vig": "prob_market_mid_no_vig",
    "current_state_poisson": "prob_current_state_poisson",
    "current_state_empirical": "prob_current_state_empirical",
    "current_state_after_stage23": "prob_current_state_after_stage23",
    "inferred_state_poisson": "prob_inferred_state_poisson",
    "inferred_state_empirical": "prob_inferred_state_empirical",
    "stage3_raw": "prob_stage3_raw",
    "final_runtime_fv": "prob_final_runtime_fv",
}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build FV gap decomposition report.")
    p.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--output-stem", type=str, default=DEFAULT_OUTPUT_STEM)
    p.add_argument("--mode", choices=["live", "paper", "both"], default="live")
    p.add_argument("--min-date", type=str, default="")
    p.add_argument("--max-date", type=str, default="")
    p.add_argument("--min-group-rows", type=int, default=5)
    p.add_argument("--strict", action="store_true")
    return p.parse_args(argv)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
    except Exception:
        return None
    if not math.isfinite(out):
        return None
    return out


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def _label(row: Mapping[str, Any]) -> Optional[int]:
    value = row.get("target_over_win")
    if value is None or value == "":
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


def _clip_prob(value: Any) -> Optional[float]:
    prob = _safe_float(value)
    if prob is None or not 0.0 < prob < 1.0:
        return None
    return min(max(prob, 1e-6), 1.0 - 1e-6)


def _logloss(labels: Sequence[int], probs: Sequence[float]) -> Optional[float]:
    if not labels:
        return None
    total = 0.0
    for y, p in zip(labels, probs):
        p = min(max(float(p), 1e-6), 1.0 - 1e-6)
        total += -(y * math.log(p) + (1 - y) * math.log(1.0 - p))
    return total / len(labels)


def _brier(labels: Sequence[int], probs: Sequence[float]) -> Optional[float]:
    if not labels:
        return None
    return sum((p - y) ** 2 for y, p in zip(labels, probs)) / len(labels)


def _auc(labels: Sequence[int], probs: Sequence[float]) -> Optional[float]:
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


def load_rows(path: Path) -> List[Dict[str, Any]]:
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


def filter_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    mode: str,
    min_date: str,
    max_date: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        row_mode = str(row.get("mode") or "").strip()
        if mode != "both" and row_mode and row_mode != mode:
            continue
        date = _date_value(row)
        if min_date and date and date < min_date:
            continue
        if max_date and date and date > max_date:
            continue
        if _label(row) is None:
            continue
        out.append(row)
    return out


def _metrics_for_probability(rows: Sequence[Mapping[str, Any]], field: str) -> Dict[str, Any]:
    labels: List[int] = []
    probs: List[float] = []
    for row in rows:
        label = _label(row)
        prob = _clip_prob(row.get(field))
        if label is None or prob is None:
            continue
        labels.append(label)
        probs.append(prob)
    wins = sum(labels)
    return {
        "field": field,
        "n": len(labels),
        "wins": wins,
        "losses": len(labels) - wins,
        "empirical_rate": _round(wins / len(labels), 6) if labels else None,
        "avg_prob": _round(sum(probs) / len(probs), 6) if probs else None,
        "brier": _round(_brier(labels, probs)),
        "logloss": _round(_logloss(labels, probs)),
        "auc": _round(_auc(labels, probs)),
    }


def _values(rows: Sequence[Mapping[str, Any]], key: str) -> List[float]:
    vals = [_safe_float(r.get(key)) for r in rows]
    return [v for v in vals if v is not None]


def _quantile(vals: Sequence[float], q: float) -> Optional[float]:
    if not vals:
        return None
    ordered = sorted(vals)
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _gap_summary(rows: Sequence[Mapping[str, Any]], gap_key: str) -> Dict[str, Any]:
    vals = _values(rows, gap_key)
    return {
        "gap": gap_key,
        "n": len(vals),
        "avg": _round(sum(vals) / len(vals) if vals else None),
        "p10": _round(_quantile(vals, 0.10)),
        "p50": _round(_quantile(vals, 0.50)),
        "p90": _round(_quantile(vals, 0.90)),
        "positive_share": _round(sum(1 for v in vals if v > 0) / len(vals) if vals else None),
        "large_positive_share_ge_5pp": _round(sum(1 for v in vals if v >= 0.05) / len(vals) if vals else None),
        "huge_positive_share_ge_15pp": _round(sum(1 for v in vals if v >= 0.15) / len(vals) if vals else None),
    }


def _ask_bucket(value: Any) -> str:
    p = _safe_float(value)
    if p is None:
        return "missing"
    if p < 0.60:
        return "<0.60"
    if p < 0.70:
        return "0.60-0.69"
    if p < 0.80:
        return "0.70-0.79"
    if p < 0.90:
        return "0.80-0.89"
    return ">=0.90"


def _gap_bucket(value: Any) -> str:
    gap = _safe_float(value)
    if gap is None:
        return "missing"
    if gap < -0.05:
        return "<-0.05"
    if gap < 0.00:
        return "-0.05-0.00"
    if gap < 0.05:
        return "0.00-0.05"
    if gap < 0.10:
        return "0.05-0.10"
    if gap < 0.15:
        return "0.10-0.15"
    return ">=0.15"


def _support_bucket(value: Any) -> str:
    n = _safe_float(value)
    if n is None:
        return "missing"
    if n < 25:
        return "<25"
    if n < 75:
        return "25-74"
    if n < 150:
        return "75-149"
    return ">=150"


def _line_bucket(row: Mapping[str, Any]) -> str:
    line = _safe_float(row.get("line"))
    return f"O{line:.1f}" if line is not None else "missing"


def _group_rows(rows: Sequence[Dict[str, Any]], group_name: str) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if group_name == "family":
            key = _family(row)
        elif group_name == "ask_bucket":
            key = _ask_bucket(row.get("decision_ask"))
        elif group_name == "line":
            key = _line_bucket(row)
        elif group_name == "inferred_runs":
            key = str(row.get("inferred_runs") if row.get("inferred_runs") not in (None, "") else "missing")
        elif group_name == "poisson_minus_market_bucket":
            key = _gap_bucket(row.get("gap_inferred_poisson_minus_market"))
        elif group_name == "poisson_minus_empirical_bucket":
            key = _gap_bucket(row.get("gap_poisson_minus_empirical"))
        elif group_name == "empirical_support_bucket":
            key = _support_bucket(
                row.get("inferred_state_effective_n_proxy")
                or row.get("inferred_state_effective_n")
                or row.get("inferred_state_n")
            )
        elif group_name == "stage1_trust_bucket":
            trust = _safe_float(row.get("inferred_state_stage1_trust_weight"))
            if trust is None:
                key = "missing"
            elif trust < 0.25:
                key = "<0.25"
            elif trust < 0.50:
                key = "0.25-0.50"
            elif trust < 0.75:
                key = "0.50-0.75"
            else:
                key = ">=0.75"
        elif group_name == "fallback_level":
            key = str(row.get("inferred_state_fallback_level") if row.get("inferred_state_fallback_level") not in (None, "") else "missing")
        elif group_name == "current_state_edge_bucket":
            key = str(row.get("shadow_current_state_edge_bucket") or "missing")
        elif group_name == "phantom_risk_band":
            key = str(row.get("shadow_phantom_risk_band") or row.get("phantom_risk_band") or "missing")
        elif group_name == "decision_reason":
            key = str(row.get("decision_reason") or "missing")
        elif group_name == "no_score_trigger":
            key = str(row.get("shadow_no_score_drift_trigger") or "missing")
        else:
            key = "unknown"
        groups[key].append(row)
    return dict(groups)


def _profit_units(label: int, ask: float) -> float:
    return (1.0 / ask - 1.0) if label == 1 else -1.0


def _summarize_rows(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    labeled = [r for r in rows if _label(r) is not None]
    wins = sum(int(_label(r) or 0) for r in labeled)
    profit = 0.0
    stake = 0
    for row in labeled:
        ask = _safe_float(row.get("decision_ask"))
        label = _label(row)
        if ask is None or label is None or ask <= 0:
            continue
        profit += _profit_units(label, ask)
        stake += 1
    return {
        "rows": len(rows),
        "labeled_rows": len(labeled),
        "wins": wins,
        "losses": len(labeled) - wins,
        "hit_rate": _round(wins / len(labeled), 6) if labeled else None,
        "avg_ask": _round(sum(_values(labeled, "decision_ask")) / len(_values(labeled, "decision_ask")) if _values(labeled, "decision_ask") else None),
        "avg_final_minus_market": _round(sum(_values(labeled, "gap_final_minus_market")) / len(_values(labeled, "gap_final_minus_market")) if _values(labeled, "gap_final_minus_market") else None),
        "avg_poisson_minus_empirical": _round(sum(_values(labeled, "gap_poisson_minus_empirical")) / len(_values(labeled, "gap_poisson_minus_empirical")) if _values(labeled, "gap_poisson_minus_empirical") else None),
        "taker_profit_units": _round(profit),
        "taker_roi_units": _round(profit / stake if stake else None),
    }


def _probability_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    return {
        name: _metrics_for_probability(rows, field)
        for name, field in PROBABILITY_FIELDS.items()
    }


def _attach_gap_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    market = _clip_prob(out.get("decision_ask"))
    market_nv = _clip_prob(out.get("decision_market_mid_no_vig"))
    inferred_po = _clip_prob(out.get("inferred_state_base_poisson") or out.get("base_fair_value"))
    inferred_emp = _clip_prob(out.get("inferred_state_base_empirical"))
    current_po = _clip_prob(out.get("current_state_value_base_poisson"))
    current_emp = _clip_prob(out.get("current_state_value_base_empirical"))
    current_stage23 = _clip_prob(out.get("current_state_value_fv_raw"))
    final = _clip_prob(out.get("fair_value"))
    raw = _clip_prob(out.get("fair_value_raw"))
    out["prob_market_ask"] = market
    out["prob_market_mid_no_vig"] = market_nv
    out["prob_current_state_poisson"] = current_po
    out["prob_current_state_empirical"] = current_emp
    out["prob_current_state_after_stage23"] = current_stage23
    out["prob_inferred_state_poisson"] = inferred_po
    out["prob_inferred_state_empirical"] = inferred_emp
    out["prob_stage3_raw"] = raw
    out["prob_final_runtime_fv"] = final
    out["gap_inferred_poisson_minus_market"] = (
        inferred_po - market if inferred_po is not None and market is not None else None
    )
    out["gap_inferred_empirical_minus_market"] = (
        inferred_emp - market if inferred_emp is not None and market is not None else None
    )
    out["gap_poisson_minus_empirical"] = (
        inferred_po - inferred_emp if inferred_po is not None and inferred_emp is not None else None
    )
    out["gap_current_poisson_minus_market"] = (
        current_po - market if current_po is not None and market is not None else None
    )
    out["gap_stage3_raw_minus_inferred_poisson"] = (
        raw - inferred_po if raw is not None and inferred_po is not None else None
    )
    out["gap_final_minus_market"] = (
        final - market if final is not None and market is not None else None
    )
    out["gap_final_minus_market_mid_no_vig"] = (
        final - market_nv if final is not None and market_nv is not None else None
    )
    return out


def _run_panel_summary(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for runs in (1, 2, 3):
        prefix = f"inference_run{runs}"
        available = [r for r in rows if _clip_prob(r.get(f"{prefix}_base_poisson")) is not None]
        selected = [r for r in available if str(r.get(f"{prefix}_selected")).lower() in {"1", "true", "yes"}]
        out[str(runs)] = {
            "available_rows": len(available),
            "selected_rows": len(selected),
            "selected_share": _round(len(selected) / len(available) if available else None),
            "avg_poisson": _round(sum(_values(available, f"{prefix}_base_poisson")) / len(_values(available, f"{prefix}_base_poisson")) if _values(available, f"{prefix}_base_poisson") else None),
            "avg_empirical": _round(sum(_values(available, f"{prefix}_base_empirical")) / len(_values(available, f"{prefix}_base_empirical")) if _values(available, f"{prefix}_base_empirical") else None),
            "avg_distance_to_ask": _round(sum(_values(available, f"{prefix}_distance_to_ask")) / len(_values(available, f"{prefix}_distance_to_ask")) if _values(available, f"{prefix}_distance_to_ask") else None),
            "avg_poisson_minus_empirical": _round(sum(_values(available, f"{prefix}_poisson_minus_empirical")) / len(_values(available, f"{prefix}_poisson_minus_empirical")) if _values(available, f"{prefix}_poisson_minus_empirical") else None),
        }
    return out


def build_report(
    rows: Sequence[Dict[str, Any]],
    *,
    min_group_rows: int,
    config: Mapping[str, Any],
) -> Dict[str, Any]:
    enriched = [_attach_gap_fields(row) for row in rows]
    families = defaultdict(list)
    families[ALL] = list(enriched)
    for row in enriched:
        families[_family(row)].append(row)

    by_family = {}
    for family, family_rows in sorted(families.items()):
        by_family[family] = {
            "summary": _summarize_rows(family_rows),
            "probability_metrics": _probability_metrics(family_rows),
            "gap_summaries": {
                key: _gap_summary(family_rows, key)
                for key in (
                    "gap_inferred_poisson_minus_market",
                    "gap_inferred_empirical_minus_market",
                    "gap_poisson_minus_empirical",
                    "gap_stage3_raw_minus_inferred_poisson",
                    "gap_final_minus_market",
                    "gap_final_minus_market_mid_no_vig",
                )
            },
            "run_count_panel": _run_panel_summary(family_rows),
        }

    group_sections: Dict[str, Dict[str, Any]] = {}
    for group_name in (
        "family",
        "decision_reason",
        "ask_bucket",
        "line",
        "inferred_runs",
        "poisson_minus_market_bucket",
        "poisson_minus_empirical_bucket",
        "empirical_support_bucket",
        "stage1_trust_bucket",
        "fallback_level",
        "current_state_edge_bucket",
        "phantom_risk_band",
        "no_score_trigger",
    ):
        group_rows = _group_rows(enriched, group_name)
        group_sections[group_name] = {
            key: _summarize_rows(vals)
            for key, vals in sorted(group_rows.items())
            if len(vals) >= min_group_rows
        }

    return {
        "generated_at_utc": _now_iso(),
        "description": (
            "FV gap decomposition from calibration opportunities. Market ask/no-vig "
            "are baselines; model probabilities are measured as deviations from market."
        ),
        "config": dict(config),
        "counts": {
            "rows": len(enriched),
            "labeled_rows": sum(1 for r in enriched if _label(r) is not None),
            "dates": sorted({d for d in (_date_value(r) for r in enriched) if d}),
            "families": {k: len(v) for k, v in sorted(families.items()) if k != ALL},
        },
        "by_family": by_family,
        "groups": group_sections,
        "warnings": [
            "Rows are calibration opportunities, not independent baseball games.",
            "No-vig midpoint requires paired Under book; missing pairs are reported as null.",
            "Run-count panel is observational and uses the runtime's ask-distance selection rule.",
        ],
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _write_markdown(path: Path, report: Mapping[str, Any]) -> None:
    lines: List[str] = []
    lines.append("# FV Gap Decomposition Report")
    lines.append("")
    lines.append(f"Generated: {report.get('generated_at_utc')}")
    lines.append("")
    counts = report.get("counts", {})
    lines.append(
        f"Rows: {counts.get('rows')}  labeled: {counts.get('labeled_rows')}  "
        f"dates: {', '.join(counts.get('dates') or [])}"
    )
    lines.append("")
    lines.append("## Family Summary")
    lines.append("")
    lines.append("| family | rows | hit_rate | avg_ask | avg_final_gap | avg_po_minus_emp | ROI units |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for family, payload in (report.get("by_family") or {}).items():
        summary = payload.get("summary") or {}
        lines.append(
            "| "
            + " | ".join(
                [
                    str(family),
                    _fmt(summary.get("labeled_rows")),
                    _fmt(summary.get("hit_rate")),
                    _fmt(summary.get("avg_ask")),
                    _fmt(summary.get("avg_final_minus_market")),
                    _fmt(summary.get("avg_poisson_minus_empirical")),
                    _fmt(summary.get("taker_roi_units")),
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append("## Probability Metrics")
    for family, payload in (report.get("by_family") or {}).items():
        lines.append("")
        lines.append(f"### {family}")
        lines.append("| probability | n | avg_prob | empirical_rate | brier | logloss | auc |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for name, metrics in (payload.get("probability_metrics") or {}).items():
            lines.append(
                "| "
                + " | ".join(
                    [
                        name,
                        _fmt(metrics.get("n")),
                        _fmt(metrics.get("avg_prob")),
                        _fmt(metrics.get("empirical_rate")),
                        _fmt(metrics.get("brier")),
                        _fmt(metrics.get("logloss")),
                        _fmt(metrics.get("auc")),
                    ]
                )
                + " |"
            )
    lines.append("")
    lines.append("## Group Diagnostics")
    for group_name, groups in (report.get("groups") or {}).items():
        lines.append("")
        lines.append(f"### {group_name}")
        lines.append("| bucket | rows | hit_rate | avg_ask | avg_final_gap | avg_po_minus_emp | ROI units |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for bucket, summary in groups.items():
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(bucket),
                        _fmt(summary.get("labeled_rows")),
                        _fmt(summary.get("hit_rate")),
                        _fmt(summary.get("avg_ask")),
                        _fmt(summary.get("avg_final_minus_market")),
                        _fmt(summary.get("avg_poisson_minus_empirical")),
                        _fmt(summary.get("taker_roi_units")),
                    ]
                )
                + " |"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    if args.min_date:
        datetime.strptime(args.min_date, "%Y-%m-%d")
    if args.max_date:
        datetime.strptime(args.max_date, "%Y-%m-%d")
    if args.min_date and args.max_date and args.min_date > args.max_date:
        raise SystemExit("--min-date must be <= --max-date")
    source_rows = load_rows(args.input_path)
    rows = filter_rows(
        source_rows,
        mode=args.mode,
        min_date=args.min_date,
        max_date=args.max_date,
    )
    if args.strict and not rows:
        raise SystemExit("Strict mode failed: no labeled calibration-opportunity rows.")
    report = build_report(
        rows,
        min_group_rows=args.min_group_rows,
        config={
            "input_path": str(args.input_path),
            "output_root": str(args.output_root),
            "output_stem": args.output_stem,
            "mode": args.mode,
            "min_date": args.min_date or None,
            "max_date": args.max_date or None,
            "min_group_rows": args.min_group_rows,
        },
    )
    args.output_root.mkdir(parents=True, exist_ok=True)
    json_path = args.output_root / f"{args.output_stem}.json"
    md_path = args.output_root / f"{args.output_stem}.md"
    _write_json(json_path, report)
    _write_markdown(md_path, report)
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
