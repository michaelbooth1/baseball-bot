#!/usr/bin/env python3
"""
Audit score-event Stage-1 Poisson vs empirical probabilities.

The live score-event path uses the Stage-1 cache's Poisson `poXX` probability
as the base FV after inferred scoring. This report reconstructs/logs the
matching empirical `oXX` sibling from the same inferred cache cell, including
support counts and line fallback details, so we can test whether the Poisson
prior is a source of FV overconfidence.

Outputs:
  data/analysis_output/stage1_inferred_empirical_audit/
    stage1_inferred_empirical_audit.json
    stage1_inferred_empirical_audit.md
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.analysis.analyze_polymarket_overreactions import OUCache  # noqa: E402
from scripts.trading.model_families import SCORE_EVENT_TRANSITION  # noqa: E402
from scripts.trading.stage1_cache_audit import resolve_cell_line_probability  # noqa: E402
from scripts.trading.stage1_support import stage1_support_diagnostics_from_values  # noqa: E402


DEFAULT_CANDIDATE_ROOT = PROJECT_DIR / "data" / "live_trading" / "candidate_universe"
DEFAULT_CACHE_PATH = PROJECT_DIR / "cache" / "mlb_ou_cache.json"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "stage1_inferred_empirical_audit"
DEFAULT_OUTPUT_STEM = "stage1_inferred_empirical_audit"


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


def _clip_prob(value: Any) -> Optional[float]:
    prob = _safe_float(value)
    if prob is None or not 0.0 < prob < 1.0:
        return None
    return min(max(prob, 1e-6), 1.0 - 1e-6)


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except Exception:
        return None


def _date_from_path(path: Path) -> str:
    name = path.name
    return name[:10] if len(name) >= 10 else ""


def _date_from_row(row: Mapping[str, Any], fallback: str = "") -> str:
    for key in ("session_date", "date"):
        value = str(row.get(key) or "").strip()
        if len(value) >= 10:
            return value[:10]
    for key in ("ts", "recorded_at"):
        value = str(row.get(key) or "").strip()
        if len(value) >= 10:
            return value[:10]
    return fallback


def _in_range(date: str, min_date: str, max_date: str) -> bool:
    if min_date and date and date < min_date:
        return False
    if max_date and date and date > max_date:
        return False
    return True


def _load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL: {exc}") from exc
            if isinstance(obj, dict):
                yield obj


def load_outcomes(
    candidate_root: Path,
    *,
    min_date: str = "",
    max_date: str = "",
    mode: str = "live",
) -> Dict[Tuple[str, int, str], int]:
    outcomes: Dict[Tuple[str, int, str], int] = {}
    for path in sorted(candidate_root.glob("*_outcomes.jsonl")):
        date = _date_from_path(path)
        if not _in_range(date, min_date, max_date):
            continue
        for row in _load_jsonl(path):
            if mode and str(row.get("mode") or mode) != mode:
                continue
            game_pk = _safe_int(row.get("game_pk"))
            line = str(row.get("line") or "").strip()
            over_hit = row.get("over_hit")
            if game_pk is None or not line or over_hit is None:
                continue
            outcomes[(date, game_pk, line)] = 1 if bool(over_hit) else 0
    return outcomes


def _row_family(row: Mapping[str, Any]) -> str:
    return str(row.get("signal_model_family") or row.get("state_value_strategy") or "").strip()


def _row_has_inferred_fv(row: Mapping[str, Any]) -> bool:
    return _clip_prob(row.get("base_fair_value")) is not None and _safe_int(row.get("inferred_runs")) is not None


def _after_inferred_scores(row: Mapping[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    away = _safe_int(row.get("away_score_before"))
    home = _safe_int(row.get("home_score_before"))
    runs = _safe_int(row.get("inferred_runs"))
    if away is None or home is None or runs is None:
        return None, None
    half = str(row.get("inning_state") or "").strip().lower()
    if half.startswith("bot") or half == "b":
        home += runs
    else:
        away += runs
    return away, home


def _cache_cell_for_row(
    row: Mapping[str, Any],
    cache: Optional[OUCache],
) -> Tuple[Optional[Mapping[str, Any]], Dict[str, Any]]:
    meta: Dict[str, Any] = {}
    cell_key = (
        row.get("inferred_state_cell_key")
        or row.get("inference_state_cell_key")
        or row.get("current_state_value_state_cell_key")
    )
    if cache is not None and cell_key:
        cell = cache.cells.get(str(cell_key))
        if isinstance(cell, dict):
            meta["state_cell_key"] = str(cell_key)
            meta["state_fallback_level"] = row.get("inferred_state_fallback_level", row.get("inference_state_fallback_level"))
            meta["state_fallback_label"] = row.get("inferred_state_fallback_label", row.get("inference_state_fallback_label"))
            return cell, meta

    if cache is None:
        return None, meta

    away_after, home_after = _after_inferred_scores(row)
    inning = _safe_int(row.get("inning"))
    outs = _safe_int(row.get("outs"))
    line = str(row.get("line") or "").strip()
    if away_after is None or home_after is None or inning is None or outs is None or not line:
        return None, meta
    try:
        _prob, lookup_meta = cache.lookup_with_meta(
            away_score=away_after,
            home_score=home_after,
            inning=inning,
            inning_state=str(row.get("inning_state") or ""),
            outs=outs,
            line=line,
            runners_on=_safe_int(row.get("runners_on")) or 0,
        )
    except Exception:
        return None, meta

    if not isinstance(lookup_meta, dict):
        return None, meta
    cell_key = lookup_meta.get("state_cell_key")
    cell = cache.cells.get(str(cell_key)) if cell_key is not None else None
    if not isinstance(cell, dict):
        return None, lookup_meta
    return cell, lookup_meta


def enrich_row(row: Mapping[str, Any], *, date: str, label: int, cache: Optional[OUCache]) -> Optional[Dict[str, Any]]:
    ask = _clip_prob(row.get("decision_ask"))
    poisson = _clip_prob(row.get("inferred_state_base_poisson"))
    if poisson is None:
        poisson = _clip_prob(row.get("base_fair_value"))
    if ask is None or poisson is None:
        return None

    cell, lookup_meta = _cache_cell_for_row(row, cache)
    empirical = _clip_prob(row.get("inferred_state_base_empirical"))
    empirical_meta: Dict[str, Any] = {}
    if empirical is None and cell is not None:
        empirical, empirical_meta = resolve_cell_line_probability(
            cell,
            requested_line=row.get("line"),
            prefix="o",
        )

    support_n = _safe_float(row.get("inferred_state_n"))
    support_samples = _safe_float(row.get("inferred_state_n_samples"))
    weighted_n = _safe_float(row.get("inferred_state_weighted_n"))
    effective_n = _safe_float(row.get("inferred_state_effective_n"))
    if cell is not None:
        support_n = support_n if support_n is not None else _safe_float(cell.get("n"))
        support_samples = support_samples if support_samples is not None else _safe_float(cell.get("n_samples"))
        weighted_n = weighted_n if weighted_n is not None else _safe_float(cell.get("weighted_n"))
        effective_n = effective_n if effective_n is not None else _safe_float(cell.get("effective_n"))

    fallback_level = _safe_int(row.get("inferred_state_fallback_level"))
    if fallback_level is None:
        fallback_level = _safe_int(row.get("inference_state_fallback_level"))
    if fallback_level is None:
        fallback_level = _safe_int(lookup_meta.get("state_fallback_level"))

    fallback_label = (
        row.get("inferred_state_fallback_label")
        or row.get("inference_state_fallback_label")
        or lookup_meta.get("state_fallback_label")
    )
    poisson_line_mode = (
        row.get("inferred_state_line_fallback_mode")
        or row.get("inference_line_fallback_mode")
        or lookup_meta.get("line_fallback_mode")
    )
    empirical_line_mode = (
        row.get("inferred_state_empirical_line_fallback_mode")
        or empirical_meta.get("line_fallback_mode")
    )
    support_diag = stage1_support_diagnostics_from_values(
        support_mass=effective_n if effective_n is not None else (weighted_n if weighted_n is not None else support_n),
        empirical_sample_support=support_samples,
        state_fallback_level=fallback_level,
        poisson_line_fallback_mode=poisson_line_mode,
        empirical_line_fallback_mode=empirical_line_mode,
    )
    gap = poisson - empirical if empirical is not None else None
    empirical_edge = empirical - ask if empirical is not None else None

    away_after, home_after = _after_inferred_scores(row)
    return {
        "date": date,
        "candidate_id": row.get("candidate_id"),
        "game_pk": _safe_int(row.get("game_pk")),
        "away_abbrev": row.get("away_abbrev"),
        "home_abbrev": row.get("home_abbrev"),
        "line": str(row.get("line") or ""),
        "inning": _safe_int(row.get("inning")),
        "inning_state": row.get("inning_state"),
        "outs": _safe_int(row.get("outs")),
        "runners_on": _safe_int(row.get("runners_on")),
        "away_score_before": _safe_int(row.get("away_score_before")),
        "home_score_before": _safe_int(row.get("home_score_before")),
        "inferred_runs": _safe_int(row.get("inferred_runs")),
        "inferred_away_after": away_after,
        "inferred_home_after": home_after,
        "decision": row.get("decision"),
        "decision_reason": row.get("decision_reason"),
        "ask": ask,
        "label": label,
        "poisson": poisson,
        "empirical": empirical,
        "poisson_minus_empirical": gap,
        "poisson_edge": poisson - ask,
        "empirical_edge": empirical_edge,
        "support_n": support_n,
        "support_n_samples": support_samples,
        "weighted_n": weighted_n,
        "effective_n": effective_n,
        "effective_n_proxy": row.get("inferred_state_effective_n_proxy") or support_diag.get("effective_n_proxy"),
        "stage1_trust_weight": row.get("inferred_state_stage1_trust_weight") or support_diag.get("stage1_trust_weight"),
        "stage1_support_bucket": row.get("inferred_state_stage1_support_bucket") or support_diag.get("stage1_support_bucket"),
        "empirical_sample_support": row.get("inferred_state_empirical_sample_support") or support_diag.get("empirical_sample_support"),
        "empirical_sample_bucket": row.get("inferred_state_empirical_sample_bucket") or support_diag.get("empirical_sample_bucket"),
        "state_fallback_level": fallback_level,
        "state_fallback_label": str(fallback_label or ""),
        "state_cell_key": (
            row.get("inferred_state_cell_key")
            or row.get("inference_state_cell_key")
            or lookup_meta.get("state_cell_key")
        ),
        "poisson_line_fallback_mode": str(poisson_line_mode or ""),
        "poisson_line_source_key": (
            row.get("inferred_state_line_key_poisson")
            or row.get("inference_line_source_key")
            or lookup_meta.get("line_source_key")
        ),
        "empirical_line_fallback_mode": str(empirical_line_mode or ""),
        "empirical_line_source_key": (
            row.get("inferred_state_empirical_line_source_key")
            or empirical_meta.get("line_source_key")
        ),
        "shadow_phantom_risk_band": row.get("shadow_phantom_risk_band") or row.get("phantom_risk_band") or "",
        "current_state_value_edge": _safe_float(row.get("current_state_value_edge")),
        "base_fair_value": _safe_float(row.get("base_fair_value")),
        "fair_value": _safe_float(row.get("fair_value")),
    }


def load_audit_rows(
    candidate_root: Path,
    *,
    cache: Optional[OUCache],
    min_date: str = "",
    max_date: str = "",
    mode: str = "live",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    outcomes = load_outcomes(candidate_root, min_date=min_date, max_date=max_date, mode=mode)
    rows: List[Dict[str, Any]] = []
    counters = defaultdict(int)
    for path in sorted(candidate_root.glob("*_candidates.jsonl")):
        file_date = _date_from_path(path)
        if not _in_range(file_date, min_date, max_date):
            continue
        for row in _load_jsonl(path):
            counters["candidate_rows_seen"] += 1
            if mode and str(row.get("mode") or mode) != mode:
                counters["wrong_mode"] += 1
                continue
            if _row_family(row) != SCORE_EVENT_TRANSITION:
                counters["non_score_event"] += 1
                continue
            if not _row_has_inferred_fv(row):
                counters["no_inferred_fv"] += 1
                continue
            date = _date_from_row(row, fallback=file_date)
            game_pk = _safe_int(row.get("game_pk"))
            line = str(row.get("line") or "").strip()
            label = outcomes.get((date, game_pk, line)) if game_pk is not None and line else None
            if label is None:
                counters["missing_label"] += 1
                continue
            enriched = enrich_row(row, date=date, label=label, cache=cache)
            if enriched is None:
                counters["enrichment_failed"] += 1
                continue
            if enriched.get("empirical") is None:
                counters["missing_empirical"] += 1
            rows.append(enriched)
    return rows, dict(counters)


def _state_dedupe_key(row: Mapping[str, Any]) -> Tuple[Any, ...]:
    return (
        row.get("date"),
        row.get("game_pk"),
        row.get("line"),
        row.get("inning"),
        row.get("inning_state"),
        row.get("outs"),
        row.get("runners_on"),
        row.get("away_score_before"),
        row.get("home_score_before"),
        row.get("inferred_runs"),
    )


def dedupe_state_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for row in rows:
        key = _state_dedupe_key(row)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


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


def _profit_per_share(label: int, ask: float) -> float:
    return (1.0 - ask) if label == 1 else -ask


def _model_metrics(rows: Sequence[Mapping[str, Any]], key: str) -> Dict[str, Any]:
    labels: List[int] = []
    probs: List[float] = []
    for row in rows:
        prob = _clip_prob(row.get(key))
        if prob is None:
            continue
        labels.append(int(row["label"]))
        probs.append(prob)
    wins = sum(labels)
    return {
        "n": len(labels),
        "wins": wins,
        "losses": len(labels) - wins,
        "empirical_rate": _round(wins / len(labels) if labels else None),
        "avg_prob": _round(sum(probs) / len(probs) if probs else None),
        "brier": _round(_brier(labels, probs)),
        "logloss": _round(_logloss(labels, probs)),
        "auc": _round(_auc_pairwise(labels, probs)),
    }


def _summarize_rows(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    labels = [int(r["label"]) for r in rows]
    asks = [float(r["ask"]) for r in rows if r.get("ask") is not None]
    gaps = [float(r["poisson_minus_empirical"]) for r in rows if r.get("poisson_minus_empirical") is not None]
    profits = [_profit_per_share(int(r["label"]), float(r["ask"])) for r in rows if r.get("ask") is not None]
    empirical_rows = [r for r in rows if r.get("empirical") is not None]
    return {
        "n_rows": len(rows),
        "n_empirical_available": len(empirical_rows),
        "wins": sum(labels),
        "losses": len(labels) - sum(labels),
        "hit_rate": _round(sum(labels) / len(labels) if labels else None),
        "avg_ask": _round(sum(asks) / len(asks) if asks else None),
        "avg_profit_per_share_at_ask": _round(sum(profits) / len(profits) if profits else None),
        "market_ask": _model_metrics(rows, "ask"),
        "poisson": _model_metrics(rows, "poisson"),
        "empirical": _model_metrics(rows, "empirical"),
        "avg_poisson_minus_empirical": _round(sum(gaps) / len(gaps) if gaps else None),
        "positive_gap_share": _round(sum(1 for gap in gaps if gap > 0) / len(gaps) if gaps else None),
        "large_positive_gap_share": _round(sum(1 for gap in gaps if gap >= 0.05) / len(gaps) if gaps else None),
    }


def _gap_bucket(value: Optional[float]) -> str:
    if value is None:
        return "missing"
    if value < -0.05:
        return "<-0.05"
    if value < 0.00:
        return "-0.05-0"
    if value < 0.03:
        return "0-0.03"
    if value < 0.05:
        return "0.03-0.05"
    if value < 0.10:
        return "0.05-0.10"
    return ">=0.10"


def _support_bucket(value: Optional[float]) -> str:
    if value is None:
        return "missing"
    if value < 20:
        return "<20"
    if value < 50:
        return "20-49"
    if value < 100:
        return "50-99"
    if value < 250:
        return "100-249"
    return ">=250"


def _ask_bucket(value: Optional[float]) -> str:
    if value is None:
        return "missing"
    if value < 0.60:
        return "<0.60"
    if value < 0.70:
        return "0.60-0.69"
    if value < 0.80:
        return "0.70-0.79"
    if value < 0.90:
        return "0.80-0.89"
    return ">=0.90"


def _inning_bucket(value: Optional[int]) -> str:
    if value is None:
        return "missing"
    if value <= 4:
        return "<=4"
    if value <= 6:
        return "5-6"
    return "7+"


def _bucket_summary(rows: Sequence[Dict[str, Any]], key_fn) -> Dict[str, Any]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(key_fn(row))].append(row)
    return {
        bucket: _summarize_rows(bucket_rows)
        for bucket, bucket_rows in sorted(groups.items(), key=lambda item: item[0])
    }


def build_report(
    rows: Sequence[Dict[str, Any]],
    *,
    raw_rows: Sequence[Dict[str, Any]],
    counters: Mapping[str, Any],
    min_date: str,
    max_date: str,
    candidate_root: Path,
    cache_path: Path,
) -> Dict[str, Any]:
    summary = _summarize_rows(rows)
    poisson_brier = summary["poisson"].get("brier")
    empirical_brier = summary["empirical"].get("brier")
    warning = None
    if empirical_brier is not None and poisson_brier is not None and empirical_brier < poisson_brier:
        warning = "empirical_stage1_beats_poisson_on_brier"
    elif summary.get("avg_poisson_minus_empirical") is not None and summary["avg_poisson_minus_empirical"] > 0.03:
        warning = "poisson_materially_above_empirical"

    return {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "scope": {
            "candidate_root": str(candidate_root),
            "cache_path": str(cache_path),
            "min_date": min_date,
            "max_date": max_date,
            "family": SCORE_EVENT_TRANSITION,
            "primary_row_unit": "first row per game-line-inning-half-outs-runners-score-before-inferred-runs",
        },
        "counters": dict(counters),
        "raw_row_count": len(raw_rows),
        "deduped_state_row_count": len(rows),
        "primary_summary": summary,
        "raw_summary": _summarize_rows(raw_rows),
        "by_gap_bucket": _bucket_summary(rows, lambda r: _gap_bucket(_safe_float(r.get("poisson_minus_empirical")))),
        "by_support_n_bucket": _bucket_summary(rows, lambda r: _support_bucket(_safe_float(r.get("support_n")))),
        "by_state_fallback_level": _bucket_summary(rows, lambda r: r.get("state_fallback_level") if r.get("state_fallback_level") is not None else "missing"),
        "by_poisson_line_mode": _bucket_summary(rows, lambda r: r.get("poisson_line_fallback_mode") or "missing"),
        "by_empirical_line_mode": _bucket_summary(rows, lambda r: r.get("empirical_line_fallback_mode") or "missing"),
        "by_ask_bucket": _bucket_summary(rows, lambda r: _ask_bucket(_safe_float(r.get("ask")))),
        "by_inning_bucket": _bucket_summary(rows, lambda r: _inning_bucket(_safe_int(r.get("inning")))),
        "by_decision_reason": _bucket_summary(rows, lambda r: r.get("decision_reason") or "missing"),
        "by_line": _bucket_summary(rows, lambda r: f"O{r.get('line')}" if r.get("line") else "missing"),
        "diagnostic_warning": warning,
        "interpretation": (
            "This report is diagnostic only. It does not recommend changing live FV/gate logic without "
            "walk-forward confirmation, but large positive Poisson-minus-empirical gaps are direct evidence "
            "to test market-anchored or empirical-blended Stage-1 variants."
        ),
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    summary = report.get("primary_summary", {})
    raw_summary = report.get("raw_summary", {})
    lines = [
        "# Stage-1 Inferred Empirical Audit",
        "",
        f"Generated: `{report.get('generated_at')}`",
        f"Scope: `{report.get('scope', {}).get('min_date') or 'start'}` to `{report.get('scope', {}).get('max_date') or 'latest'}`",
        "",
        "## Primary Deduped Summary",
        "",
        f"- Rows: {summary.get('n_rows')} deduped states ({report.get('raw_row_count')} raw rows)",
        f"- Hit rate: {summary.get('hit_rate')}",
        f"- Avg ask: {summary.get('avg_ask')}",
        f"- Avg Poisson minus empirical: {summary.get('avg_poisson_minus_empirical')}",
        f"- Positive gap share: {summary.get('positive_gap_share')}",
        f"- Large gap share (>= 5pp): {summary.get('large_positive_gap_share')}",
        f"- Diagnostic warning: `{report.get('diagnostic_warning') or 'none'}`",
        "",
        "## Model Calibration Snapshot",
        "",
        "| Model | n | avg prob | Brier | Logloss | AUC |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for key, label in (("market_ask", "Market ask"), ("poisson", "Stage-1 Poisson"), ("empirical", "Stage-1 empirical")):
        metric = summary.get(key, {})
        lines.append(
            f"| {label} | {metric.get('n')} | {metric.get('avg_prob')} | "
            f"{metric.get('brier')} | {metric.get('logloss')} | {metric.get('auc')} |"
        )

    lines.extend(
        [
            "",
            "## Raw Row Check",
            "",
            f"- Raw hit rate: {raw_summary.get('hit_rate')}",
            f"- Raw avg Poisson minus empirical: {raw_summary.get('avg_poisson_minus_empirical')}",
            "",
            "## Gap Buckets",
            "",
            "| Gap bucket | rows | hit rate | avg ask | avg gap | Poisson Brier | Empirical Brier |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for bucket, bucket_summary in report.get("by_gap_bucket", {}).items():
        lines.append(
            f"| {bucket} | {bucket_summary.get('n_rows')} | {bucket_summary.get('hit_rate')} | "
            f"{bucket_summary.get('avg_ask')} | {bucket_summary.get('avg_poisson_minus_empirical')} | "
            f"{bucket_summary.get('poisson', {}).get('brier')} | {bucket_summary.get('empirical', {}).get('brier')} |"
        )

    lines.extend(
        [
            "",
            "## Support Buckets",
            "",
            "| support_n | rows | hit rate | avg gap | Poisson Brier | Empirical Brier |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for bucket, bucket_summary in report.get("by_support_n_bucket", {}).items():
        lines.append(
            f"| {bucket} | {bucket_summary.get('n_rows')} | {bucket_summary.get('hit_rate')} | "
            f"{bucket_summary.get('avg_poisson_minus_empirical')} | "
            f"{bucket_summary.get('poisson', {}).get('brier')} | {bucket_summary.get('empirical', {}).get('brier')} |"
        )

    lines.extend(
        [
            "",
            "## Decision Reasons",
            "",
            "| reason | rows | hit rate | avg ask | avg gap | Poisson Brier | Empirical Brier |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for bucket, bucket_summary in report.get("by_decision_reason", {}).items():
        lines.append(
            f"| {bucket} | {bucket_summary.get('n_rows')} | {bucket_summary.get('hit_rate')} | "
            f"{bucket_summary.get('avg_ask')} | {bucket_summary.get('avg_poisson_minus_empirical')} | "
            f"{bucket_summary.get('poisson', {}).get('brier')} | {bucket_summary.get('empirical', {}).get('brier')} |"
        )

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Primary rows are deduped by game, line, state, and inferred run count so repeated polling ticks do not masquerade as independent evidence.",
            "- The runtime still uses Poisson Stage-1 as live base FV; empirical fields are audit/modeling inputs only.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(report: Mapping[str, Any], *, output_root: Path, output_stem: str) -> Dict[str, str]:
    output_root.mkdir(parents=True, exist_ok=True)
    as_of = str(report.get("scope", {}).get("max_date") or datetime.now(timezone.utc).date())
    json_text = json.dumps(report, indent=2, sort_keys=True)
    md_text = render_markdown(report)

    json_path = output_root / f"{output_stem}.json"
    md_path = output_root / f"{output_stem}.md"
    dated_json_path = output_root / f"{as_of}_{output_stem}.json"
    dated_md_path = output_root / f"{as_of}_{output_stem}.md"
    json_path.write_text(json_text, encoding="utf-8")
    dated_json_path.write_text(json_text, encoding="utf-8")
    md_path.write_text(md_text, encoding="utf-8")
    dated_md_path.write_text(md_text, encoding="utf-8")
    return {
        "json": str(json_path),
        "markdown": str(md_path),
        "dated_json": str(dated_json_path),
        "dated_markdown": str(dated_md_path),
    }


def write_row_outputs(
    rows: Sequence[Mapping[str, Any]],
    *,
    output_root: Path,
    output_stem: str,
    as_of: str,
) -> Dict[str, str]:
    output_root.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_root / f"{output_stem}_rows.jsonl"
    csv_path = output_root / f"{output_stem}_rows.csv"
    dated_jsonl_path = output_root / f"{as_of}_{output_stem}_rows.jsonl"
    dated_csv_path = output_root / f"{as_of}_{output_stem}_rows.csv"
    columns: List[str] = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                columns.append(str(key))

    jsonl_text = "".join(json.dumps(dict(row), sort_keys=True) + "\n" for row in rows)
    jsonl_path.write_text(jsonl_text, encoding="utf-8")
    dated_jsonl_path.write_text(jsonl_text, encoding="utf-8")
    for path in (csv_path, dated_csv_path):
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))
    return {
        "rows_jsonl": str(jsonl_path),
        "rows_csv": str(csv_path),
        "dated_rows_jsonl": str(dated_jsonl_path),
        "dated_rows_csv": str(dated_csv_path),
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit inferred score-event Stage-1 Poisson vs empirical cache probabilities.")
    parser.add_argument("--candidate-root", type=Path, default=DEFAULT_CANDIDATE_ROOT)
    parser.add_argument("--cache-path", type=Path, default=DEFAULT_CACHE_PATH)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--output-stem", default=DEFAULT_OUTPUT_STEM)
    parser.add_argument("--min-date", default="")
    parser.add_argument("--max-date", default="")
    parser.add_argument("--mode", default="live")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cache = OUCache(args.cache_path) if args.cache_path.exists() else None
    raw_rows, counters = load_audit_rows(
        args.candidate_root,
        cache=cache,
        min_date=args.min_date,
        max_date=args.max_date,
        mode=args.mode,
    )
    rows = dedupe_state_rows(raw_rows)
    report = build_report(
        rows,
        raw_rows=raw_rows,
        counters=counters,
        min_date=args.min_date,
        max_date=args.max_date,
        candidate_root=args.candidate_root,
        cache_path=args.cache_path,
    )
    paths = write_outputs(report, output_root=args.output_root, output_stem=args.output_stem)
    as_of = str(report.get("scope", {}).get("max_date") or datetime.now(timezone.utc).date())
    row_paths = write_row_outputs(rows, output_root=args.output_root, output_stem=args.output_stem, as_of=as_of)
    print(f"Wrote {paths['json']}")
    print(f"Wrote {paths['markdown']}")
    print(f"Wrote {row_paths['rows_jsonl']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
