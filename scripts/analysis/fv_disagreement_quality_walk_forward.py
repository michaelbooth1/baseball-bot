#!/usr/bin/env python3
"""
Walk-forward validation for FV-vs-market disagreement buckets.

The descriptive FV disagreement report tells us which buckets looked good in
sample. This script asks the harder question:

    If we had trusted only buckets that looked good using prior data, would
    those trusted FV disagreements have improved calibration, CLV, or ROI out
    of sample?

Each window:
  1. Builds FV-disagreement quality rows from calibration opportunities.
  2. Uses train dates to find candidate bucket keys with positive FV-vs-market
     calibration gain.
  3. Requires those same bucket keys to survive a validation window.
  4. Marks matching test-date rows as trusted without looking at test labels.

Outputs:
  data/analysis_output/fv_disagreement_quality_walk_forward/
    summary.json
    summary.md
    per_window_results.jsonl
    predictions.jsonl
    selected_buckets.jsonl
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_fv_disagreement_quality_report as dq  # noqa: E402


DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "fv_disagreement_quality_walk_forward"
DEFAULT_TRAIN_DAYS = 7
DEFAULT_VAL_DAYS = 2
DEFAULT_TEST_DAYS = 1
DEFAULT_MIN_TRAIN_DATES = 3
DEFAULT_MIN_TRAIN_ROWS = 30
DEFAULT_MIN_TRAIN_BUCKET_ROWS = 8
DEFAULT_MIN_VAL_BUCKET_ROWS = 3
DEFAULT_MIN_TRAIN_BRIER_GAIN = 0.0
DEFAULT_MIN_VAL_BRIER_GAIN = 0.0
DEFAULT_MAX_SELECTED_BUCKETS = 25

BUCKET_DIMENSIONS = (
    "fv_gap_bucket",
    "disagreement_direction",
    "ask_bucket_x_gap",
    "support_trust_x_gap",
    "support_n_x_gap",
    "current_state_edge_x_phantom",
    "inning_x_runs_needed",
    "home_skip_bottom9_x_gap",
    "decision_reason_x_gap",
)

PREDICTION_COLUMNS = [
    "schema_version",
    "family",
    "market_anchor",
    "window_id",
    "session_date",
    "test_date",
    "row_id",
    "candidate_id",
    "bet_id",
    "game_pk",
    "line",
    "decision_reason",
    "market_probability",
    "fair_value",
    "fv_minus_market",
    "fv_gap_bucket",
    "is_disagreement",
    "trusted_disagreement",
    "matched_bucket_count",
    "matched_bucket_keys",
    "label_over_win",
    "market_brier",
    "fv_brier",
    "brier_gain_vs_market",
    "market_logloss",
    "fv_logloss",
    "logloss_gain_vs_market",
    "clv_mid_vs_entry",
    "realized_roi",
    "realized_profit_usdc",
    "fill_cost_usdc",
    "taker_profit_units",
    "limit_profit_units",
    "stage1_trust_weight",
    "stage1_effective_n",
]

WINDOW_COLUMNS = [
    "schema_version",
    "window_id",
    "family",
    "market_anchor",
    "train_start",
    "train_end",
    "val_start",
    "val_end",
    "test_start",
    "test_end",
    "completed",
    "skip_reason",
    "train_rows",
    "val_rows",
    "test_rows",
    "selected_bucket_count",
    "test_disagreement_rows",
    "trusted_test_rows",
    "trusted_mean_brier_gain_vs_market",
    "trusted_mean_logloss_gain_vs_market",
    "trusted_mean_clv_mid_vs_entry",
    "trusted_roi_on_cost",
    "all_test_mean_brier_gain_vs_market",
    "all_test_mean_clv_mid_vs_entry",
]

SELECTED_BUCKET_COLUMNS = [
    "schema_version",
    "window_id",
    "family",
    "market_anchor",
    "bucket_dimension",
    "bucket_value",
    "bucket_key",
    "train_rows",
    "train_brier_gain",
    "train_logloss_gain",
    "train_clv",
    "train_roi_on_cost",
    "val_rows",
    "val_brier_gain",
    "val_logloss_gain",
    "val_clv",
    "val_roi_on_cost",
    "selection_score",
]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Walk-forward FV disagreement quality validation.")
    p.add_argument("--calibration-table", type=Path, default=dq.DEFAULT_CALIBRATION_TABLE)
    p.add_argument("--clv-rows", type=Path, default=dq.DEFAULT_CLV_ROWS)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--mode", choices=["live", "paper", "both"], default="live")
    p.add_argument("--family", choices=["all", *dq.KNOWN_FAMILIES], default="all")
    p.add_argument(
        "--market-anchors",
        type=str,
        default="ask,mid_no_vig_or_ask",
        help="Comma-separated anchors: ask,mid_no_vig,mid_no_vig_or_ask.",
    )
    p.add_argument("--min-date", type=str, default="", help="Inclusive source date.")
    p.add_argument("--max-date", type=str, default="", help="Inclusive source/test date.")
    p.add_argument("--start-date", type=str, default="", help="First test date.")
    p.add_argument("--end-date", type=str, default="", help="Last test date.")
    p.add_argument("--train-days", type=int, default=DEFAULT_TRAIN_DAYS)
    p.add_argument("--val-days", type=int, default=DEFAULT_VAL_DAYS)
    p.add_argument("--test-days", type=int, default=DEFAULT_TEST_DAYS)
    p.add_argument("--min-train-dates", type=int, default=DEFAULT_MIN_TRAIN_DATES)
    p.add_argument("--min-train-rows", type=int, default=DEFAULT_MIN_TRAIN_ROWS)
    p.add_argument("--min-train-bucket-rows", type=int, default=DEFAULT_MIN_TRAIN_BUCKET_ROWS)
    p.add_argument("--min-val-bucket-rows", type=int, default=DEFAULT_MIN_VAL_BUCKET_ROWS)
    p.add_argument("--min-train-brier-gain", type=float, default=DEFAULT_MIN_TRAIN_BRIER_GAIN)
    p.add_argument("--min-val-brier-gain", type=float, default=DEFAULT_MIN_VAL_BRIER_GAIN)
    p.add_argument("--max-selected-buckets", type=int, default=DEFAULT_MAX_SELECTED_BUCKETS)
    p.add_argument("--min-abs-disagreement", type=float, default=0.03)
    p.add_argument("--plan-only", action="store_true")
    p.add_argument("--strict", action="store_true")
    return p.parse_args(argv)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_date(raw: str) -> dt.date:
    return dt.datetime.strptime(raw, "%Y-%m-%d").date()


def _date_str(value: dt.date) -> str:
    return value.strftime("%Y-%m-%d")


def _date_range(start: dt.date, end: dt.date) -> List[dt.date]:
    out: List[dt.date] = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur += dt.timedelta(days=1)
    return out


def _safe_float(value: Any) -> Optional[float]:
    return dq._safe_float(value)


def _safe_int(value: Any) -> Optional[int]:
    return dq._safe_int(value)


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _mean(values: Iterable[Any]) -> Optional[float]:
    vals = [_safe_float(v) for v in values]
    vals = [v for v in vals if v is not None]
    return statistics.mean(vals) if vals else None


def _parse_csv(raw: str, *, allowed: Sequence[str], name: str) -> List[str]:
    out: List[str] = []
    allowed_set = set(allowed)
    for token in str(raw or "").split(","):
        value = token.strip()
        if not value:
            continue
        if value not in allowed_set:
            raise SystemExit(f"Unsupported {name}: {value}. Expected one of {sorted(allowed_set)}.")
        if value not in out:
            out.append(value)
    if not out:
        raise SystemExit(f"Expected at least one {name}.")
    return out


def _validate_args(args: argparse.Namespace) -> None:
    for attr in ("min_date", "max_date", "start_date", "end_date"):
        raw = str(getattr(args, attr) or "")
        if raw:
            _parse_date(raw)
    if args.min_date and args.max_date and args.min_date > args.max_date:
        raise SystemExit("--min-date must be <= --max-date")
    if args.start_date and args.end_date and args.start_date > args.end_date:
        raise SystemExit("--start-date must be <= --end-date")
    if args.train_days <= 0 or args.val_days <= 0 or args.test_days <= 0:
        raise SystemExit("--train-days, --val-days, and --test-days must be > 0.")
    for attr in (
        "min_train_dates",
        "min_train_rows",
        "min_train_bucket_rows",
        "min_val_bucket_rows",
        "max_selected_buckets",
    ):
        if int(getattr(args, attr)) < 1:
            raise SystemExit(f"--{attr.replace('_', '-')} must be >= 1.")
    if args.min_abs_disagreement < 0:
        raise SystemExit("--min-abs-disagreement must be non-negative.")


def _row_date(row: Mapping[str, Any]) -> str:
    return str(row.get("session_date") or "")[:10]


def _row_between(row: Mapping[str, Any], start: dt.date, end: dt.date) -> bool:
    date_str = _row_date(row)
    if not date_str:
        return False
    date = _parse_date(date_str)
    return start <= date <= end


def plan_windows(
    available_dates: Sequence[str],
    *,
    start_date: Optional[dt.date],
    end_date: Optional[dt.date],
    train_days: int,
    val_days: int,
    test_days: int,
    min_train_dates: int,
) -> List[Dict[str, Any]]:
    dates = sorted(set(d for d in available_dates if d))
    if not dates:
        return []
    earliest = _parse_date(dates[0])
    latest = _parse_date(dates[-1])
    first_test = earliest + dt.timedelta(days=train_days + val_days)
    if start_date is not None:
        first_test = max(first_test, start_date)
    last_test = end_date if end_date is not None else latest
    if first_test > last_test:
        return []

    out: List[Dict[str, Any]] = []
    cur = first_test
    available_set = set(dates)
    while cur <= last_test:
        train_start = cur - dt.timedelta(days=train_days + val_days)
        train_end = cur - dt.timedelta(days=val_days + 1)
        val_start = cur - dt.timedelta(days=val_days)
        val_end = cur - dt.timedelta(days=1)
        test_end = min(cur + dt.timedelta(days=test_days - 1), last_test)
        train_dates = [_date_str(d) for d in _date_range(train_start, train_end) if _date_str(d) in available_set]
        val_dates = [_date_str(d) for d in _date_range(val_start, val_end) if _date_str(d) in available_set]
        test_dates = [_date_str(d) for d in _date_range(cur, test_end) if _date_str(d) in available_set]
        out.append(
            {
                "window_id": f"{_date_str(cur)}_{_date_str(test_end)}",
                "train_start": _date_str(train_start),
                "train_end": _date_str(train_end),
                "val_start": _date_str(val_start),
                "val_end": _date_str(val_end),
                "test_start": _date_str(cur),
                "test_end": _date_str(test_end),
                "train_dates": train_dates,
                "val_dates": val_dates,
                "test_dates": test_dates,
                "has_min_train_dates": len(train_dates) >= min_train_dates,
            }
        )
        cur = test_end + dt.timedelta(days=1)
    return out


def _compound(*values: Any) -> str:
    return "|".join(str(v if v not in (None, "") else "missing") for v in values)


def bucket_value(row: Mapping[str, Any], dimension: str) -> str:
    if dimension == "fv_gap_bucket":
        return str(row.get("fv_gap_bucket") or "missing")
    if dimension == "disagreement_direction":
        return str(row.get("disagreement_direction") or "missing")
    if dimension == "ask_bucket_x_gap":
        return _compound(row.get("ask_bucket"), row.get("fv_gap_bucket"))
    if dimension == "support_trust_x_gap":
        return _compound(row.get("stage1_trust_bucket"), row.get("fv_gap_bucket"))
    if dimension == "support_n_x_gap":
        return _compound(row.get("stage1_effective_n_bucket"), row.get("fv_gap_bucket"))
    if dimension == "current_state_edge_x_phantom":
        return _compound(row.get("current_state_edge_bucket"), row.get("shadow_phantom_risk_bucket"))
    if dimension == "inning_x_runs_needed":
        return _compound(row.get("inning_bucket"), row.get("runs_needed_bucket"))
    if dimension == "home_skip_bottom9_x_gap":
        return _compound(row.get("home_skip_bottom9_risk_bucket"), row.get("fv_gap_bucket"))
    if dimension == "decision_reason_x_gap":
        return _compound(row.get("decision_reason"), row.get("fv_gap_bucket"))
    raise KeyError(f"Unsupported bucket dimension: {dimension}")


def bucket_key(row: Mapping[str, Any], dimension: str) -> str:
    return f"{dimension}={bucket_value(row, dimension)}"


def _bucket_summary(rows: Sequence[Mapping[str, Any]], *, family: str, dimension: str, value: str) -> Dict[str, Any]:
    if not rows:
        return {
            "rows": 0,
            "labeled_rows": 0,
            "mean_brier_gain_vs_market": None,
            "mean_logloss_gain_vs_market": None,
            "mean_clv_mid_vs_entry": None,
            "roi_on_cost": None,
            "evidence_score": None,
        }
    return dq._bucket_summary(
        rows,
        bucket_scope="walk_forward",
        bucket_dimension=dimension,
        bucket_value=value,
        family=family,
    )


def _group_by_bucket(rows: Sequence[Mapping[str, Any]], dimension: str) -> Dict[str, List[Mapping[str, Any]]]:
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[bucket_value(row, dimension)].append(row)
    return grouped


def select_trusted_buckets(
    *,
    train_rows: Sequence[Mapping[str, Any]],
    val_rows: Sequence[Mapping[str, Any]],
    family: str,
    market_anchor: str,
    window_id: str,
    min_train_bucket_rows: int,
    min_val_bucket_rows: int,
    min_train_brier_gain: float,
    min_val_brier_gain: float,
    max_selected_buckets: int,
) -> List[Dict[str, Any]]:
    train_disagreements = [r for r in train_rows if bool(r.get("is_disagreement"))]
    val_disagreements = [r for r in val_rows if bool(r.get("is_disagreement"))]
    selected: List[Dict[str, Any]] = []
    for dimension in BUCKET_DIMENSIONS:
        train_groups = _group_by_bucket(train_disagreements, dimension)
        val_groups = _group_by_bucket(val_disagreements, dimension)
        for value, group_rows in sorted(train_groups.items()):
            train_summary = _bucket_summary(group_rows, family=family, dimension=dimension, value=value)
            train_n = int(train_summary.get("labeled_rows") or 0)
            train_gain = _safe_float(train_summary.get("mean_brier_gain_vs_market"))
            if train_n < min_train_bucket_rows or train_gain is None or train_gain < min_train_brier_gain:
                continue
            val_group = val_groups.get(value, [])
            val_summary = _bucket_summary(val_group, family=family, dimension=dimension, value=value)
            val_n = int(val_summary.get("labeled_rows") or 0)
            val_gain = _safe_float(val_summary.get("mean_brier_gain_vs_market"))
            if val_n < min_val_bucket_rows or val_gain is None or val_gain < min_val_brier_gain:
                continue
            train_logloss = _safe_float(train_summary.get("mean_logloss_gain_vs_market"))
            val_logloss = _safe_float(val_summary.get("mean_logloss_gain_vs_market"))
            train_clv = _safe_float(train_summary.get("mean_clv_mid_vs_entry"))
            val_clv = _safe_float(val_summary.get("mean_clv_mid_vs_entry"))
            train_roi = _safe_float(train_summary.get("roi_on_cost"))
            val_roi = _safe_float(val_summary.get("roi_on_cost"))
            sample_weight = math.sqrt((train_n + val_n) / (train_n + val_n + 25.0))
            selection_score = (0.35 * train_gain + 0.65 * val_gain) * sample_weight
            if val_clv is not None:
                selection_score += 0.10 * val_clv * sample_weight
            if val_roi is not None:
                selection_score += 0.03 * val_roi * sample_weight
            selected.append(
                {
                    "schema_version": 1,
                    "window_id": window_id,
                    "family": family,
                    "market_anchor": market_anchor,
                    "bucket_dimension": dimension,
                    "bucket_value": value,
                    "bucket_key": f"{dimension}={value}",
                    "train_rows": train_n,
                    "train_brier_gain": _round(train_gain),
                    "train_logloss_gain": _round(train_logloss),
                    "train_clv": _round(train_clv),
                    "train_roi_on_cost": _round(train_roi),
                    "val_rows": val_n,
                    "val_brier_gain": _round(val_gain),
                    "val_logloss_gain": _round(val_logloss),
                    "val_clv": _round(val_clv),
                    "val_roi_on_cost": _round(val_roi),
                    "selection_score": _round(selection_score),
                }
            )
    selected.sort(
        key=lambda row: (
            _safe_float(row.get("selection_score")) or -999.0,
            _safe_float(row.get("val_brier_gain")) or -999.0,
            int(row.get("val_rows") or 0),
        ),
        reverse=True,
    )
    return selected[:max_selected_buckets]


def _matched_keys(row: Mapping[str, Any], selected_keys: set[str]) -> List[str]:
    matches: List[str] = []
    for dimension in BUCKET_DIMENSIONS:
        key = bucket_key(row, dimension)
        if key in selected_keys:
            matches.append(key)
    return matches


def _prediction_row(
    row: Mapping[str, Any],
    *,
    family: str,
    market_anchor: str,
    window_id: str,
    test_date: str,
    matched: Sequence[str],
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "family": family,
        "market_anchor": market_anchor,
        "window_id": window_id,
        "session_date": test_date,
        "test_date": test_date,
        "row_id": row.get("row_id"),
        "candidate_id": row.get("candidate_id"),
        "bet_id": row.get("bet_id"),
        "game_pk": row.get("game_pk"),
        "line": row.get("line"),
        "decision_reason": row.get("decision_reason"),
        "market_probability": row.get("market_probability"),
        "fair_value": row.get("fair_value"),
        "fv_minus_market": row.get("fv_minus_market"),
        "fv_gap_bucket": row.get("fv_gap_bucket"),
        "is_disagreement": bool(row.get("is_disagreement")),
        "trusted_disagreement": bool(matched),
        "matched_bucket_count": len(matched),
        "matched_bucket_keys": ";".join(matched),
        "label_over_win": row.get("label_over_win"),
        "market_brier": row.get("market_brier"),
        "fv_brier": row.get("fv_brier"),
        "brier_gain_vs_market": row.get("brier_gain_vs_market"),
        "market_logloss": row.get("market_logloss"),
        "fv_logloss": row.get("fv_logloss"),
        "logloss_gain_vs_market": row.get("logloss_gain_vs_market"),
        "clv_mid_vs_entry": row.get("clv_mid_vs_entry"),
        "realized_roi": row.get("realized_roi"),
        "realized_profit_usdc": row.get("realized_profit_usdc"),
        "fill_cost_usdc": row.get("fill_cost_usdc"),
        "taker_profit_units": row.get("taker_profit_units"),
        "limit_profit_units": row.get("limit_profit_units"),
        "stage1_trust_weight": row.get("stage1_trust_weight"),
        "stage1_effective_n": row.get("stage1_effective_n"),
    }


def _summary_for_rows(rows: Sequence[Mapping[str, Any]], *, family: str, label: str) -> Dict[str, Any]:
    if not rows:
        return {
            "bucket_scope": label,
            "rows": 0,
            "labeled_rows": 0,
            "mean_brier_gain_vs_market": None,
            "mean_logloss_gain_vs_market": None,
            "mean_clv_mid_vs_entry": None,
            "roi_on_cost": None,
        }
    return dq._bucket_summary(
        rows,
        bucket_scope=label,
        bucket_dimension="selection",
        bucket_value=label,
        family=family,
    )


def evaluate_window(
    rows: Sequence[Mapping[str, Any]],
    *,
    family: str,
    market_anchor: str,
    window: Mapping[str, Any],
    min_train_bucket_rows: int,
    min_val_bucket_rows: int,
    min_train_brier_gain: float,
    min_val_brier_gain: float,
    max_selected_buckets: int,
    min_train_rows: int,
    min_train_dates: int,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    train_start = _parse_date(str(window["train_start"]))
    train_end = _parse_date(str(window["train_end"]))
    val_start = _parse_date(str(window["val_start"]))
    val_end = _parse_date(str(window["val_end"]))
    test_start = _parse_date(str(window["test_start"]))
    test_end = _parse_date(str(window["test_end"]))
    train_rows = [r for r in rows if _row_between(r, train_start, train_end)]
    val_rows = [r for r in rows if _row_between(r, val_start, val_end)]
    test_rows = [r for r in rows if _row_between(r, test_start, test_end)]
    skip_reason = ""
    if not bool(window.get("has_min_train_dates")):
        skip_reason = "insufficient_train_dates"
    elif len(train_rows) < min_train_rows:
        skip_reason = "insufficient_train_rows"
    elif not val_rows:
        skip_reason = "empty_validation_window"
    elif not test_rows:
        skip_reason = "empty_test_window"
    if skip_reason:
        return (
            {
                "schema_version": 1,
                "window_id": window["window_id"],
                "family": family,
                "market_anchor": market_anchor,
                "train_start": window["train_start"],
                "train_end": window["train_end"],
                "val_start": window["val_start"],
                "val_end": window["val_end"],
                "test_start": window["test_start"],
                "test_end": window["test_end"],
                "completed": False,
                "skip_reason": skip_reason,
                "train_rows": len(train_rows),
                "val_rows": len(val_rows),
                "test_rows": len(test_rows),
                "selected_bucket_count": 0,
            },
            [],
            [],
        )

    selected = select_trusted_buckets(
        train_rows=train_rows,
        val_rows=val_rows,
        family=family,
        market_anchor=market_anchor,
        window_id=str(window["window_id"]),
        min_train_bucket_rows=min_train_bucket_rows,
        min_val_bucket_rows=min_val_bucket_rows,
        min_train_brier_gain=min_train_brier_gain,
        min_val_brier_gain=min_val_brier_gain,
        max_selected_buckets=max_selected_buckets,
    )
    selected_keys = {str(row["bucket_key"]) for row in selected}
    test_disagreements = [r for r in test_rows if bool(r.get("is_disagreement"))]
    predictions: List[Dict[str, Any]] = []
    for row in test_disagreements:
        matched = _matched_keys(row, selected_keys)
        predictions.append(
            _prediction_row(
                row,
                family=family,
                market_anchor=market_anchor,
                window_id=str(window["window_id"]),
                test_date=_row_date(row),
                matched=matched,
            )
        )
    trusted = [r for r, pred in zip(test_disagreements, predictions) if pred["trusted_disagreement"]]
    untrusted = [r for r, pred in zip(test_disagreements, predictions) if not pred["trusted_disagreement"]]
    all_summary = _summary_for_rows(test_disagreements, family=family, label="all_test_disagreements")
    trusted_summary = _summary_for_rows(trusted, family=family, label="trusted_test_disagreements")
    untrusted_summary = _summary_for_rows(untrusted, family=family, label="untrusted_test_disagreements")
    result = {
        "schema_version": 1,
        "window_id": window["window_id"],
        "family": family,
        "market_anchor": market_anchor,
        "train_start": window["train_start"],
        "train_end": window["train_end"],
        "val_start": window["val_start"],
        "val_end": window["val_end"],
        "test_start": window["test_start"],
        "test_end": window["test_end"],
        "completed": True,
        "skip_reason": "",
        "train_rows": len(train_rows),
        "val_rows": len(val_rows),
        "test_rows": len(test_rows),
        "selected_bucket_count": len(selected),
        "test_disagreement_rows": len(test_disagreements),
        "trusted_test_rows": len(trusted),
        "all_test_disagreements": all_summary,
        "trusted_test_disagreements": trusted_summary,
        "untrusted_test_disagreements": untrusted_summary,
        "trusted_mean_brier_gain_vs_market": trusted_summary.get("mean_brier_gain_vs_market"),
        "trusted_mean_logloss_gain_vs_market": trusted_summary.get("mean_logloss_gain_vs_market"),
        "trusted_mean_clv_mid_vs_entry": trusted_summary.get("mean_clv_mid_vs_entry"),
        "trusted_roi_on_cost": trusted_summary.get("roi_on_cost"),
        "all_test_mean_brier_gain_vs_market": all_summary.get("mean_brier_gain_vs_market"),
        "all_test_mean_clv_mid_vs_entry": all_summary.get("mean_clv_mid_vs_entry"),
    }
    return result, predictions, selected


def _aggregate_predictions(rows: Sequence[Mapping[str, Any]], *, family: str, label: str) -> Dict[str, Any]:
    return _summary_for_rows(rows, family=family, label=label)


def _prediction_to_quality_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    # Prediction rows carry the same metric names needed by dq._bucket_summary.
    return dict(row)


def aggregate_summary(
    *,
    combos: Mapping[str, Dict[str, Any]],
    source_rows: int,
    config: Mapping[str, Any],
    load_warnings: Sequence[str],
) -> Dict[str, Any]:
    all_dates: List[str] = []
    for payload in combos.values():
        all_dates.extend(str(d) for d in (payload.get("rows_by_date") or {}).keys() if d)
    out: Dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "min_date": min(all_dates) if all_dates else None,
        "max_date": max(all_dates) if all_dates else None,
        "description": (
            "Walk-forward validation for FV-vs-market disagreement buckets. "
            "Selected buckets are learned from train/validation dates only."
        ),
        "source_rows": source_rows,
        "config": dict(config),
        "families": {},
        "warnings": [
            {
                "code": "research_only",
                "message": "This report is diagnostic-only and must not be used as a live gate without stable out-of-sample evidence.",
            },
            {
                "code": "bucket_selection_not_model_fit",
                "message": "This validates bucket trust rules, not a fitted probability model.",
            },
            *[{"code": "load_warning", "message": str(w)} for w in load_warnings[:50]],
        ],
    }
    for combo_key, payload in combos.items():
        window_results = payload["window_results"]
        predictions = payload["prediction_rows"]
        selected_predictions = [p for p in predictions if bool(p.get("trusted_disagreement"))]
        unselected_predictions = [p for p in predictions if not bool(p.get("trusted_disagreement"))]
        completed = [w for w in window_results if bool(w.get("completed"))]
        skipped = [w for w in window_results if not bool(w.get("completed"))]
        family = str(payload["family"])
        out["families"][combo_key] = {
            "family": family,
            "market_anchor": payload["market_anchor"],
            "quality_rows_loaded": payload["rows_loaded"],
            "rows_by_date": payload["rows_by_date"],
            "windows_planned": len(window_results),
            "windows_completed": len(completed),
            "windows_skipped": len(skipped),
            "skipped_reasons": dict(Counter(str(w.get("skip_reason") or "unknown") for w in skipped)),
            "selected_bucket_rows": len(payload["selected_bucket_rows"]),
            "out_of_sample_disagreement_rows": len(predictions),
            "trusted_out_of_sample_rows": len(selected_predictions),
            "untrusted_out_of_sample_rows": len(unselected_predictions),
            "all_test_disagreements": _aggregate_predictions(
                [_prediction_to_quality_row(p) for p in predictions],
                family=family,
                label="all_test_disagreements",
            ),
            "trusted_test_disagreements": _aggregate_predictions(
                [_prediction_to_quality_row(p) for p in selected_predictions],
                family=family,
                label="trusted_test_disagreements",
            ),
            "untrusted_test_disagreements": _aggregate_predictions(
                [_prediction_to_quality_row(p) for p in unselected_predictions],
                family=family,
                label="untrusted_test_disagreements",
            ),
            "mean_window_metrics": {
                "trusted_brier_gain": _round(
                    _mean(w.get("trusted_mean_brier_gain_vs_market") for w in completed)
                ),
                "all_brier_gain": _round(
                    _mean(w.get("all_test_mean_brier_gain_vs_market") for w in completed)
                ),
                "trusted_clv": _round(
                    _mean(w.get("trusted_mean_clv_mid_vs_entry") for w in completed)
                ),
                "all_clv": _round(
                    _mean(w.get("all_test_mean_clv_mid_vs_entry") for w in completed)
                ),
            },
        }
    return out


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


def _write_markdown(path: Path, summary: Mapping[str, Any]) -> None:
    lines = [
        "# FV Disagreement Quality Walk-Forward",
        "",
        f"Generated: {summary.get('generated_at_utc')}",
        "",
        "Selected buckets are learned from prior train/validation dates only, then applied to test dates.",
        "",
        "| family | anchor | windows | OOS rows | trusted rows | trusted Brier gain | all Brier gain | trusted CLV | trusted ROI |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for payload in (summary.get("families") or {}).values():
        trusted = payload.get("trusted_test_disagreements") or {}
        all_test = payload.get("all_test_disagreements") or {}
        lines.append(
            "| "
            + " | ".join(
                [
                    str(payload.get("family")),
                    str(payload.get("market_anchor")),
                    f"{payload.get('windows_completed')}/{payload.get('windows_planned')}",
                    str(payload.get("out_of_sample_disagreement_rows")),
                    str(payload.get("trusted_out_of_sample_rows")),
                    _fmt(trusted.get("mean_brier_gain_vs_market")),
                    _fmt(all_test.get("mean_brier_gain_vs_market")),
                    _fmt(trusted.get("mean_clv_mid_vs_entry")),
                    _fmt(trusted.get("roi_on_cost")),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "Interpretation: positive Brier/logloss gain means raw FV beat the selected market anchor on the test rows. "
            "Empty trusted rows means no prior bucket survived train+validation for that window.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_walk_forward(
    *,
    calibration_rows: Sequence[Mapping[str, Any]],
    clv_rows: Sequence[Mapping[str, Any]],
    mode: str,
    families: Sequence[str],
    market_anchors: Sequence[str],
    min_date: str,
    max_date: str,
    start_date: str,
    end_date: str,
    train_days: int,
    val_days: int,
    test_days: int,
    min_train_dates: int,
    min_train_rows: int,
    min_train_bucket_rows: int,
    min_val_bucket_rows: int,
    min_train_brier_gain: float,
    min_val_brier_gain: float,
    max_selected_buckets: int,
    min_abs_disagreement: float,
    plan_only: bool = False,
) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    combos: Dict[str, Dict[str, Any]] = {}
    plan_rows: List[Dict[str, Any]] = []
    for family in families:
        for anchor in market_anchors:
            quality_rows, load_stats = dq.build_quality_rows(
                calibration_rows=calibration_rows,
                clv_rows=clv_rows,
                mode=mode,
                family=family,
                min_date=min_date,
                max_date=max_date,
                market_anchor=anchor,
                min_abs_disagreement=min_abs_disagreement,
            )
            available_dates = sorted({_row_date(r) for r in quality_rows if _row_date(r)})
            windows = plan_windows(
                available_dates,
                start_date=_parse_date(start_date) if start_date else None,
                end_date=_parse_date(end_date) if end_date else None,
                train_days=train_days,
                val_days=val_days,
                test_days=test_days,
                min_train_dates=min_train_dates,
            )
            combo_key = f"{family}|{anchor}"
            rows_by_date = dict(Counter(_row_date(r) for r in quality_rows if _row_date(r)))
            for window in windows:
                plan_rows.append(
                    {
                        "family": family,
                        "market_anchor": anchor,
                        "window_id": window["window_id"],
                        "train_start": window["train_start"],
                        "train_end": window["train_end"],
                        "val_start": window["val_start"],
                        "val_end": window["val_end"],
                        "test_start": window["test_start"],
                        "test_end": window["test_end"],
                        "train_dates": window["train_dates"],
                        "val_dates": window["val_dates"],
                        "test_dates": window["test_dates"],
                    }
                )
            payload = {
                "family": family,
                "market_anchor": anchor,
                "rows_loaded": len(quality_rows),
                "load_stats": load_stats,
                "rows_by_date": rows_by_date,
                "window_results": [],
                "prediction_rows": [],
                "selected_bucket_rows": [],
                "windows_planned": len(windows),
            }
            if not plan_only:
                for window in windows:
                    result, predictions, selected = evaluate_window(
                        quality_rows,
                        family=family,
                        market_anchor=anchor,
                        window=window,
                        min_train_bucket_rows=min_train_bucket_rows,
                        min_val_bucket_rows=min_val_bucket_rows,
                        min_train_brier_gain=min_train_brier_gain,
                        min_val_brier_gain=min_val_brier_gain,
                        max_selected_buckets=max_selected_buckets,
                        min_train_rows=min_train_rows,
                        min_train_dates=min_train_dates,
                    )
                    payload["window_results"].append(result)
                    payload["prediction_rows"].extend(predictions)
                    payload["selected_bucket_rows"].extend(selected)
            else:
                payload["window_results"] = [
                    {
                        "schema_version": 1,
                        "window_id": window["window_id"],
                        "family": family,
                        "market_anchor": anchor,
                        "train_start": window["train_start"],
                        "train_end": window["train_end"],
                        "val_start": window["val_start"],
                        "val_end": window["val_end"],
                        "test_start": window["test_start"],
                        "test_end": window["test_end"],
                        "completed": False,
                        "skip_reason": "plan_only",
                    }
                    for window in windows
                ]
            combos[combo_key] = payload
    return combos, plan_rows


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    _validate_args(args)
    anchors = _parse_csv(
        args.market_anchors,
        allowed=("ask", "mid_no_vig", "mid_no_vig_or_ask"),
        name="market anchor",
    )
    families = list(dq.KNOWN_FAMILIES) if args.family == "all" else [args.family]
    warnings: List[str] = []
    calibration_rows = dq._read_table(args.calibration_table, warnings)
    clv_rows = dq._read_table(args.clv_rows, warnings)
    combos, plan_rows = run_walk_forward(
        calibration_rows=calibration_rows,
        clv_rows=clv_rows,
        mode=args.mode,
        families=families,
        market_anchors=anchors,
        min_date=args.min_date,
        max_date=args.max_date,
        start_date=args.start_date,
        end_date=args.end_date,
        train_days=args.train_days,
        val_days=args.val_days,
        test_days=args.test_days,
        min_train_dates=args.min_train_dates,
        min_train_rows=args.min_train_rows,
        min_train_bucket_rows=args.min_train_bucket_rows,
        min_val_bucket_rows=args.min_val_bucket_rows,
        min_train_brier_gain=args.min_train_brier_gain,
        min_val_brier_gain=args.min_val_brier_gain,
        max_selected_buckets=args.max_selected_buckets,
        min_abs_disagreement=args.min_abs_disagreement,
        plan_only=bool(args.plan_only),
    )
    config = {
        "calibration_table": str(args.calibration_table),
        "clv_rows": str(args.clv_rows),
        "output_root": str(args.output_root),
        "mode": args.mode,
        "family": args.family,
        "market_anchors": anchors,
        "min_date": args.min_date or None,
        "max_date": args.max_date or None,
        "start_date": args.start_date or None,
        "end_date": args.end_date or None,
        "train_days": args.train_days,
        "val_days": args.val_days,
        "test_days": args.test_days,
        "min_train_dates": args.min_train_dates,
        "min_train_rows": args.min_train_rows,
        "min_train_bucket_rows": args.min_train_bucket_rows,
        "min_val_bucket_rows": args.min_val_bucket_rows,
        "min_train_brier_gain": args.min_train_brier_gain,
        "min_val_brier_gain": args.min_val_brier_gain,
        "max_selected_buckets": args.max_selected_buckets,
        "min_abs_disagreement": args.min_abs_disagreement,
        "plan_only": bool(args.plan_only),
    }
    summary = aggregate_summary(
        combos=combos,
        source_rows=len(calibration_rows),
        config=config,
        load_warnings=warnings,
    )
    completed = [
        w
        for payload in combos.values()
        for w in payload["window_results"]
        if bool(w.get("completed"))
    ]
    if args.strict and not args.plan_only and not completed:
        raise SystemExit("Strict mode failed: no completed FV disagreement walk-forward windows.")

    args.output_root.mkdir(parents=True, exist_ok=True)
    window_rows = [w for payload in combos.values() for w in payload["window_results"]]
    prediction_rows = [p for payload in combos.values() for p in payload["prediction_rows"]]
    selected_rows = [b for payload in combos.values() for b in payload["selected_bucket_rows"]]
    _write_json(args.output_root / "summary.json", summary)
    _write_markdown(args.output_root / "summary.md", summary)
    _write_jsonl(args.output_root / "per_window_results.jsonl", window_rows)
    _write_jsonl(args.output_root / "predictions.jsonl", prediction_rows)
    _write_jsonl(args.output_root / "selected_buckets.jsonl", selected_rows)
    _write_csv(args.output_root / "per_window_results.csv", window_rows, WINDOW_COLUMNS)
    _write_csv(args.output_root / "predictions.csv", prediction_rows, PREDICTION_COLUMNS)
    _write_csv(args.output_root / "selected_buckets.csv", selected_rows, SELECTED_BUCKET_COLUMNS)
    _write_json(args.output_root / "window_plan.json", {"generated_at_utc": _now_iso(), "windows": plan_rows})
    print(f"Wrote {args.output_root / 'summary.json'}")
    print(f"Wrote {args.output_root / 'predictions.jsonl'}")


if __name__ == "__main__":
    main()
