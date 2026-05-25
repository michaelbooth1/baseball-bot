#!/usr/bin/env python3
"""
walk_forward_runner.py -- Rolling walk-forward backtest harness.

Promoted to the top of the roadmap on 2026-04-30. Without this, no claim
about gate or model effectiveness — including the entire TR1->TR15
evolution narrative — is statistically defensible. Every other roadmap
item (calibration, fill model, EV policy, learned execution) ultimately
needs walk-forward evidence before it can be promoted from shadow to
enforced.

For each rolling test date D in [start, end]:
    train window:  [D - train_days - val_days, D - val_days)
    val window:    [D - val_days, D)
    test window:   [D, D + test_days)

For each window we:
    - reuse build_signal_training_table.build_training_rows() to build
      the labeled rows with explicit train/val/test split assignments
    - reuse train_baseline_models.train_task() to train signal_win and
      execution_fill models, with hyperparameter selection on val
    - capture per-window test-set metrics (brier, logloss, AUC, accuracy)
      and baseline live-engine trade metrics (n_trades, fill_rate, win_rate,
      realized cumulative profit on the test date)

Aggregations we report:
    - mean / median test-window brier per task (calibration drift)
    - mean / median test-window AUC per task (discrimination)
    - cumulative baseline live-engine realized profit across test dates
      (and max drawdown)
    - observed-order model policy P&L across historical live orders only
    - mean test-window ROI on filled trades
    - trade-frequency stability (per-window n_trades stdev / mean)

Usage:

    # Default: walk forward across all available live dates, 14d train + 3d val
    python scripts/analysis/walk_forward_runner.py --mode live

    # Custom windows + date range
    python scripts/analysis/walk_forward_runner.py --mode live \\
        --train-days 21 --val-days 5 \\
        --start-date 2026-04-15 --end-date 2026-04-29

    # Plan-only (print planned windows, no training)
    python scripts/analysis/walk_forward_runner.py --mode live --plan-only

    # Strict (fail on any window error rather than skipping)
    python scripts/analysis/walk_forward_runner.py --mode live --strict

Outputs:
    data/analysis_output/walk_forward/summary.json
    data/analysis_output/walk_forward/per_window_results.jsonl
    data/analysis_output/walk_forward/calibration_drift.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import logging
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

PROJECT_DIR = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"

# Reuse existing analysis tooling rather than re-implementing models.
sys.path.insert(0, str(ANALYSIS_DIR))
from build_signal_training_table import (  # noqa: E402
    IDENTITY_COLUMNS,
    PRE_SIGNAL_COLUMNS,
    PRE_SIGNAL_MODEL_COLUMNS,
    POST_SIGNAL_STATIC_COLUMNS,
    _date_in_range,
    _filter_rows,
    _infer_param_columns,
    _infer_post_signal_columns,
    _row_date,
    _read_jsonl,
    build_training_rows,
)
from train_baseline_models import train_task  # noqa: E402

LOGGER = logging.getLogger("walk_forward_runner")

DEFAULT_INPUT_PATH  = PROJECT_DIR / "data" / "analysis_output" / "unified_signals" / "signals_master.jsonl"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "walk_forward"

DEFAULT_TRAIN_DAYS = 14
DEFAULT_VAL_DAYS = 3
DEFAULT_TEST_DAYS = 1
DEFAULT_MIN_TRAIN_DATES = 5     # require at least this many distinct train dates per window
DEFAULT_MIN_TRAIN_LABELS = 10   # require at least this many labeled train rows per task per window


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Rolling walk-forward backtest harness — top roadmap item.",
    )
    p.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH,
                   help=f"signals_master.jsonl source (default: {DEFAULT_INPUT_PATH}).")
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT,
                   help=f"Output directory (default: {DEFAULT_OUTPUT_ROOT}).")
    p.add_argument("--mode", choices=["live", "paper", "both"], default="live",
                   help="Filter rows by mode (default: live).")
    p.add_argument("--config-label-filter", type=str, default="",
                   help="Optional config_label filter for parallel engine comparisons.")
    p.add_argument("--start-date", type=str, default="",
                   help="First test date (YYYY-MM-DD). Default: earliest date with enough train history.")
    p.add_argument("--end-date", type=str, default="",
                   help="Last test date (YYYY-MM-DD). Default: latest date with data.")
    p.add_argument("--train-days", type=int, default=DEFAULT_TRAIN_DAYS,
                   help=f"Calendar days in rolling train window (default: {DEFAULT_TRAIN_DAYS}).")
    p.add_argument("--val-days", type=int, default=DEFAULT_VAL_DAYS,
                   help=f"Calendar days in rolling validation window (default: {DEFAULT_VAL_DAYS}).")
    p.add_argument("--test-days", type=int, default=DEFAULT_TEST_DAYS,
                   help=f"Calendar days in rolling test window (default: {DEFAULT_TEST_DAYS}).")
    p.add_argument("--min-train-dates", type=int, default=DEFAULT_MIN_TRAIN_DATES,
                   help=f"Skip windows with fewer than this many distinct train dates (default: {DEFAULT_MIN_TRAIN_DATES}).")
    p.add_argument("--min-train-labels", type=int, default=DEFAULT_MIN_TRAIN_LABELS,
                   help=f"Skip windows with fewer than this many labeled train rows (default: {DEFAULT_MIN_TRAIN_LABELS}).")
    p.add_argument("--plan-only", action="store_true",
                   help="Print planned windows without training. Useful for debugging window definitions.")
    p.add_argument("--strict", action="store_true",
                   help="Fail on any per-window training error instead of skipping the window.")
    p.add_argument("--drop-unsettled", action="store_true",
                   help="Drop rows with no settled outcome (no targets available).")
    p.add_argument("--verbose", action="store_true", help="DEBUG-level logging.")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_date(s: str) -> _dt.date:
    return _dt.datetime.strptime(s, "%Y-%m-%d").date()


def _date_str(d: _dt.date) -> str:
    return d.strftime("%Y-%m-%d")


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _date_range(start: _dt.date, end: _dt.date) -> List[_dt.date]:
    out = []
    d = start
    while d <= end:
        out.append(d)
        d += _dt.timedelta(days=1)
    return out


def _prediction_map(pred_rows: Iterable[Dict[str, Any]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for row in pred_rows:
        if str(row.get("split") or "") != "test":
            continue
        bet_id = str(row.get("bet_id") or "")
        prob = _safe_float(row.get("prob"))
        if bet_id and prob is not None:
            out[bet_id] = prob
    return out


def _execution_price(row: Dict[str, Any]) -> Optional[float]:
    for key in ("limit_price", "posted_limit", "decision_ask", "entry_ask"):
        price = _safe_float(row.get(key))
        if price is not None and 0.0 < price < 1.0:
            return price
    return None


def _observed_order_model_policy_results(
    placed_rows: List[Dict[str, Any]],
    predictions_by_task: Dict[str, Dict[str, float]],
    min_ev_per_stake: float = 0.0,
) -> Dict[str, Any]:
    """Simulate model skip/select only across orders the live engine placed.

    This does not invent fills for orders we never posted. It answers a narrower
    but useful question: among historical live orders, would the walk-forward
    models have skipped enough bad orders to improve observed realized P&L?
    """

    win_preds = predictions_by_task.get("signal_win") or {}
    fill_preds = predictions_by_task.get("execution_fill") or {}
    scored: List[Dict[str, Any]] = []
    selected: List[Dict[str, Any]] = []
    missing_predictions = 0
    invalid_price = 0

    for row in placed_rows:
        bet_id = str(row.get("bet_id") or "")
        price = _execution_price(row)
        p_win = win_preds.get(bet_id)
        p_fill = fill_preds.get(bet_id)
        if p_win is None or p_fill is None:
            missing_predictions += 1
            continue
        if price is None:
            invalid_price += 1
            continue
        ev_if_filled_per_stake = (p_win / price) - 1.0
        ev_realized_per_stake = p_fill * ev_if_filled_per_stake
        scored_row = {
            "row": row,
            "p_win_if_filled": p_win,
            "p_fill": p_fill,
            "execution_price": price,
            "ev_if_filled_per_stake": ev_if_filled_per_stake,
            "ev_realized_per_stake": ev_realized_per_stake,
        }
        scored.append(scored_row)
        if ev_realized_per_stake > min_ev_per_stake:
            selected.append(scored_row)

    selected_rows = [item["row"] for item in selected]
    filled_rows = [r for r in selected_rows if r.get("target_filled") == 1]
    won_rows = [r for r in filled_rows if r.get("target_win") == 1]
    selected_profit = sum(_safe_float(r.get("target_profit")) or 0.0 for r in filled_rows)
    selected_ev = [float(item["ev_realized_per_stake"]) for item in selected]
    scored_ev = [float(item["ev_realized_per_stake"]) for item in scored]

    return {
        "status": "simulated_observed_orders" if scored else "not_simulated",
        "scope": "historical_live_orders_only",
        "selection_rule": "p_fill * (p_win_if_filled / execution_price - 1) > min_ev_per_stake",
        "min_ev_per_stake": min_ev_per_stake,
        "n_observed_orders": len(placed_rows),
        "n_scored_orders": len(scored),
        "n_missing_predictions": missing_predictions,
        "n_invalid_price": invalid_price,
        "n_selected": len(selected),
        "selection_rate": round(len(selected) / len(scored), 6) if scored else None,
        "n_filled_observed": len(filled_rows),
        "n_won_observed": len(won_rows),
        "observed_fill_rate": round(len(filled_rows) / len(selected), 6) if selected else None,
        "observed_win_rate_filled": round(len(won_rows) / len(filled_rows), 6) if filled_rows else None,
        "observed_realized_profit": round(selected_profit, 4),
        "mean_ev_realized_per_stake_selected": _stat(selected_ev, "mean"),
        "mean_ev_realized_per_stake_all_scored": _stat(scored_ev, "mean"),
        "limitations": (
            "Selection is evaluated only on orders historically placed by the live engine; "
            "it cannot estimate fills or profit for candidates the live engine did not post."
        ),
    }


# ---------------------------------------------------------------------------
# Window planning
# ---------------------------------------------------------------------------

def plan_windows(
    available_dates: Sequence[str],
    start_date: Optional[_dt.date],
    end_date: Optional[_dt.date],
    train_days: int,
    val_days: int,
    test_days: int,
    min_train_dates: int,
) -> List[Dict[str, Any]]:
    """Return a list of window dicts: {test_start, test_end, train_dates, val_dates}.

    Iterates daily starting at the earliest date that has train_days + val_days
    of prior history available (or `start_date` if given) and stops at end_date.
    """
    if not available_dates:
        return []
    available_set = set(available_dates)
    earliest = _parse_date(min(available_dates))
    latest = _parse_date(max(available_dates))

    # Earliest possible test_start = first date that has full train + val history before it
    earliest_test = earliest + _dt.timedelta(days=train_days + val_days)
    if start_date is not None:
        earliest_test = max(earliest_test, start_date)
    final_test = end_date if end_date is not None else latest
    if earliest_test > final_test:
        return []

    windows: List[Dict[str, Any]] = []
    test_d = earliest_test
    while test_d <= final_test:
        test_start = test_d
        test_end = min(test_d + _dt.timedelta(days=test_days - 1), final_test)
        val_end = test_start - _dt.timedelta(days=1)
        val_start = val_end - _dt.timedelta(days=val_days - 1)
        train_end = val_start - _dt.timedelta(days=1)
        train_start = train_end - _dt.timedelta(days=train_days - 1)

        train_dates = [_date_str(d) for d in _date_range(train_start, train_end) if _date_str(d) in available_set]
        val_dates   = [_date_str(d) for d in _date_range(val_start, val_end)     if _date_str(d) in available_set]
        test_dates_ = [_date_str(d) for d in _date_range(test_start, test_end)   if _date_str(d) in available_set]

        windows.append({
            "test_start": _date_str(test_start),
            "test_end": _date_str(test_end),
            "train_start": _date_str(train_start),
            "train_end": _date_str(train_end),
            "val_start": _date_str(val_start),
            "val_end": _date_str(val_end),
            "train_dates_with_data": train_dates,
            "val_dates_with_data": val_dates,
            "test_dates_with_data": test_dates_,
            "skip_reason": (
                "no_test_data"          if not test_dates_
                else "insufficient_train_history" if len(train_dates) < min_train_dates
                else None
            ),
        })
        # Roll forward one test_days step to keep a strict walk-forward cadence.
        test_d = test_d + _dt.timedelta(days=test_days)
    return windows


# ---------------------------------------------------------------------------
# Per-window training
# ---------------------------------------------------------------------------

def _split_map_for_window(
    train_dates: Iterable[str],
    val_dates: Iterable[str],
    test_dates: Iterable[str],
) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for d in train_dates:
        out[d] = "train"
    for d in val_dates:
        out[d] = "validation"
    for d in test_dates:
        out[d] = "test"
    return out


def _run_one_window(
    window: Dict[str, Any],
    rows: List[Dict[str, Any]],
    drop_unsettled: bool,
    min_train_labels: int,
    strict: bool,
) -> Dict[str, Any]:
    """Train + evaluate models for one walk-forward window. Returns metrics dict."""
    out = dict(window)
    out["completed"] = False
    out["error"] = None

    if window.get("skip_reason"):
        out["error"] = window["skip_reason"]
        return out

    split_map = _split_map_for_window(
        window["train_dates_with_data"],
        window["val_dates_with_data"],
        window["test_dates_with_data"],
    )
    # Restrict rows to dates participating in this window — keeps the row set small.
    window_dates = set(split_map.keys())
    win_rows = [r for r in rows if str(r.get("session_date") or "") in window_dates]

    # Date-rank for sort stability inside build_training_rows.
    sorted_dates = sorted(window_dates)
    date_rank = {d: i for i, d in enumerate(sorted_dates)}

    param_cols = _infer_param_columns(win_rows)
    post_cols  = _infer_post_signal_columns(win_rows)
    pre_cols_output = list(PRE_SIGNAL_COLUMNS)
    pre_cols   = list(PRE_SIGNAL_MODEL_COLUMNS)
    feature_cols_signal = pre_cols
    feature_cols_fill   = pre_cols + POST_SIGNAL_STATIC_COLUMNS + post_cols

    training_rows = build_training_rows(
        rows=win_rows,
        pre_signal_columns=pre_cols_output,
        post_signal_columns=POST_SIGNAL_STATIC_COLUMNS + post_cols,
        param_columns=param_cols,
        split_map=split_map,
        date_rank=date_rank,
        drop_unsettled=drop_unsettled,
    )

    # Quick label-count sanity per task before training.
    train_subset = [r for r in training_rows if r.get("split") == "train"]
    train_labels_win  = sum(1 for r in train_subset if r.get("target_win") is not None)
    train_labels_fill = sum(1 for r in train_subset if r.get("target_filled") is not None)
    out["row_counts"] = {
        "train": sum(1 for r in training_rows if r.get("split") == "train"),
        "validation": sum(1 for r in training_rows if r.get("split") == "validation"),
        "test": sum(1 for r in training_rows if r.get("split") == "test"),
        "train_labels_win": train_labels_win,
        "train_labels_fill": train_labels_fill,
    }
    if train_labels_win < min_train_labels and train_labels_fill < min_train_labels:
        out["error"] = f"insufficient_train_labels (win={train_labels_win}, fill={train_labels_fill}, need>={min_train_labels})"
        return out

    tasks = []
    if train_labels_win >= min_train_labels:
        tasks.append(("signal_win", "target_win", feature_cols_signal))
    if train_labels_fill >= min_train_labels:
        tasks.append(("execution_fill", "target_filled", feature_cols_fill))

    metrics_by_task: Dict[str, Any] = {}
    predictions_by_task: Dict[str, Dict[str, float]] = {}
    for task_name, label_col, feature_cols in tasks:
        try:
            summary, model_payload, pred_rows = train_task(
                all_rows=training_rows,
                task_name=task_name,
                label_col=label_col,
                feature_cols=feature_cols,
                strict=strict,
            )
            predictions_by_task[task_name] = _prediction_map(pred_rows)
            metrics_by_task[task_name] = {
                "train": summary.get("metrics", {}).get("train") if isinstance(summary, dict) else None,
                "validation": summary.get("metrics", {}).get("validation") if isinstance(summary, dict) else None,
                "test": summary.get("metrics", {}).get("test") if isinstance(summary, dict) else None,
                "selected_hparams": (
                    model_payload.get("search_selected") if isinstance(model_payload, dict) else None
                ),
            }
        except SystemExit as exc:
            metrics_by_task[task_name] = {"error": str(exc)}
            if strict:
                out["error"] = f"task_failed:{task_name}:{exc}"
                return out
        except Exception as exc:
            metrics_by_task[task_name] = {"error": repr(exc)}
            if strict:
                out["error"] = f"task_failed:{task_name}:{exc!r}"
                return out

    # Baseline live-engine metrics on the test split: realized profit from rows
    # that actually placed orders, regardless of model prediction.
    test_subset = [r for r in training_rows if r.get("split") == "test"]
    placed_rows = [r for r in test_subset if r.get("target_filled") is not None]
    filled_rows = [r for r in placed_rows if r.get("target_filled") == 1]
    won_rows    = [r for r in filled_rows if r.get("target_win") == 1]
    realized_profit = sum(_safe_float(r.get("target_profit")) or 0.0 for r in filled_rows)
    out["baseline_live_engine_results"] = {
        "n_test_rows": len(test_subset),
        "n_placed": len(placed_rows),
        "n_filled": len(filled_rows),
        "n_won": len(won_rows),
        "fill_rate": (len(filled_rows) / len(placed_rows)) if placed_rows else None,
        "win_rate_filled": (len(won_rows) / len(filled_rows)) if filled_rows else None,
        "baseline_realized_profit": round(realized_profit, 4),
    }
    out["model_policy_results"] = _observed_order_model_policy_results(
        placed_rows=placed_rows,
        predictions_by_task=predictions_by_task,
    )
    out["task_metrics"] = metrics_by_task
    out["completed"] = True
    out["error"] = None
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _stat(values: Sequence[float], op: str) -> Optional[float]:
    vals = [v for v in values if v is not None and isinstance(v, (int, float)) and math.isfinite(v)]
    if not vals:
        return None
    if op == "mean":
        return round(statistics.mean(vals), 6)
    if op == "median":
        return round(statistics.median(vals), 6)
    if op == "stdev":
        return round(statistics.stdev(vals), 6) if len(vals) >= 2 else 0.0
    raise ValueError(op)


def _max_drawdown(profits_in_order: Sequence[float]) -> float:
    """Maximum drawdown of a cumulative-profit series."""
    if not profits_in_order:
        return 0.0
    peak = cum = 0.0
    max_dd = 0.0
    for p in profits_in_order:
        cum += float(p or 0.0)
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
    return round(max_dd, 4)


def aggregate(window_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    completed = [w for w in window_results if w.get("completed")]
    skipped = [w for w in window_results if not w.get("completed")]

    # Per-task test metrics
    def collect(task: str, metric: str) -> List[float]:
        vals = []
        for w in completed:
            tm = (w.get("task_metrics") or {}).get(task)
            if not isinstance(tm, dict):
                continue
            test = tm.get("test") if isinstance(tm.get("test"), dict) else None
            if test and test.get(metric) is not None:
                vals.append(float(test[metric]))
        return vals

    summary: Dict[str, Any] = {
        "generated_at_utc": _now_iso(),
        "n_windows_planned": len(window_results),
        "n_windows_completed": len(completed),
        "n_windows_skipped": len(skipped),
        "skipped_reasons": _count_by(skipped, lambda w: w.get("error") or "unknown"),
        "tasks": {},
        "baseline_live_engine_results": {},
        "model_policy_results": {},
        "warnings": [],
    }

    for task in ("signal_win", "execution_fill"):
        bri = collect(task, "brier")
        ll  = collect(task, "logloss")
        auc = collect(task, "auc")
        acc = collect(task, "accuracy_0p5")
        summary["tasks"][task] = {
            "n_windows_with_test_metrics": len(bri),
            "test_brier":   {"mean": _stat(bri, "mean"), "median": _stat(bri, "median"), "stdev": _stat(bri, "stdev")},
            "test_logloss": {"mean": _stat(ll, "mean"),  "median": _stat(ll, "median"),  "stdev": _stat(ll, "stdev")},
            "test_auc":     {"mean": _stat(auc, "mean"), "median": _stat(auc, "median"), "stdev": _stat(auc, "stdev")},
            "test_accuracy_0p5":{"mean": _stat(acc, "mean"), "median": _stat(acc, "median"), "stdev": _stat(acc, "stdev")},
        }

    # Baseline live-engine rollup across test windows.
    profits  = [
        (w.get("baseline_live_engine_results") or {}).get("baseline_realized_profit")
        for w in completed
    ]
    profits  = [p for p in profits if p is not None]
    n_filled = sum((w.get("baseline_live_engine_results") or {}).get("n_filled") or 0 for w in completed)
    n_won    = sum((w.get("baseline_live_engine_results") or {}).get("n_won") or 0 for w in completed)
    n_placed = sum((w.get("baseline_live_engine_results") or {}).get("n_placed") or 0 for w in completed)
    fill_rates = [(w.get("baseline_live_engine_results") or {}).get("fill_rate") for w in completed]
    win_rates  = [(w.get("baseline_live_engine_results") or {}).get("win_rate_filled") for w in completed]
    n_per_window = [(w.get("baseline_live_engine_results") or {}).get("n_placed") or 0 for w in completed]

    summary["baseline_live_engine_results"] = {
        "total_n_placed_across_test_windows": n_placed,
        "total_n_filled_across_test_windows": n_filled,
        "total_n_won_across_test_windows": n_won,
        "cumulative_baseline_realized_profit": round(sum(profits), 4),
        "max_baseline_drawdown_across_test_windows": _max_drawdown(profits),
        "mean_per_window_baseline_realized_profit": _stat(profits, "mean"),
        "median_per_window_baseline_realized_profit": _stat(profits, "median"),
        "mean_fill_rate_per_window": _stat(fill_rates, "mean"),
        "mean_win_rate_filled_per_window": _stat(win_rates, "mean"),
        "trade_frequency_stability": {
            "mean_n_placed_per_window": _stat(n_per_window, "mean"),
            "stdev_n_placed_per_window": _stat(n_per_window, "stdev"),
            "coeff_var": (
                round(_stat(n_per_window, "stdev") / _stat(n_per_window, "mean"), 4)
                if _stat(n_per_window, "mean") and _stat(n_per_window, "mean") > 0 else None
            ),
        },
    }

    model_rows = [w.get("model_policy_results") or {} for w in completed]
    model_profits = [r.get("observed_realized_profit") for r in model_rows]
    model_profits = [p for p in model_profits if p is not None]
    model_selected = sum(r.get("n_selected") or 0 for r in model_rows)
    model_scored = sum(r.get("n_scored_orders") or 0 for r in model_rows)
    model_filled = sum(r.get("n_filled_observed") or 0 for r in model_rows)
    model_won = sum(r.get("n_won_observed") or 0 for r in model_rows)
    model_selection_rates = [r.get("selection_rate") for r in model_rows]
    model_fill_rates = [r.get("observed_fill_rate") for r in model_rows]
    model_win_rates = [r.get("observed_win_rate_filled") for r in model_rows]
    model_status = "simulated_observed_orders" if model_scored else "not_simulated"
    model_total_profit = round(sum(model_profits), 4)
    baseline_total_profit = summary["baseline_live_engine_results"]["cumulative_baseline_realized_profit"]

    summary["model_policy_results"] = {
        "status": model_status,
        "scope": "historical_live_orders_only",
        "selection_rule": "p_fill * (p_win_if_filled / execution_price - 1) > 0",
        "total_n_scored_orders": model_scored,
        "total_n_selected": model_selected,
        "total_n_filled_observed": model_filled,
        "total_n_won_observed": model_won,
        "selection_rate": round(model_selected / model_scored, 6) if model_scored else None,
        "mean_selection_rate_per_window": _stat(model_selection_rates, "mean"),
        "mean_observed_fill_rate_per_window": _stat(model_fill_rates, "mean"),
        "mean_observed_win_rate_filled_per_window": _stat(model_win_rates, "mean"),
        "cumulative_observed_realized_profit": model_total_profit,
        "max_observed_drawdown_across_test_windows": _max_drawdown(model_profits),
        "incremental_profit_vs_baseline": (
            round(model_total_profit - baseline_total_profit, 4)
            if baseline_total_profit is not None else None
        ),
        "limitations": (
            "This is a skip/select simulation on historical live orders only. "
            "It cannot estimate fills or profit for candidates the live engine did not post."
        ),
    }

    execution_fill_auc = summary["tasks"]["execution_fill"]["test_auc"]["mean"]
    if execution_fill_auc is not None and (
        summary["tasks"]["execution_fill"]["n_windows_with_test_metrics"] < 10
        or n_placed < 50
    ):
        summary["warnings"].append({
            "code": "small_sample_execution_fill_auc",
            "message": (
                "Execution-fill AUC is a small-sample warning only. Treat weak "
                "walk-forward AUC here as 'do not enforce yet', not as proof the "
                "fill model is permanently bad."
            ),
            "execution_fill_test_auc_mean": execution_fill_auc,
            "n_windows_with_test_metrics": summary["tasks"]["execution_fill"]["n_windows_with_test_metrics"],
            "baseline_total_n_placed": n_placed,
        })
    return summary


def _count_by(items: Iterable[Any], key) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for it in items:
        k = str(key(it))
        out[k] = out.get(k, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_outputs(
    output_root: Path,
    summary: Dict[str, Any],
    window_results: List[Dict[str, Any]],
) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    summary_path = output_root / "summary.json"
    per_window_path = output_root / "per_window_results.jsonl"
    drift_csv_path = output_root / "calibration_drift.csv"

    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    with open(per_window_path, "w", encoding="utf-8") as f:
        for w in window_results:
            f.write(json.dumps(w) + "\n")

    # Calibration-drift CSV: one row per (window, task) with train/test brier
    rows: List[Dict[str, Any]] = []
    for w in window_results:
        tasks = w.get("task_metrics") or {}
        for task_name, tm in tasks.items():
            train_brier = (tm.get("train") or {}).get("brier") if isinstance(tm.get("train"), dict) else None
            test_brier  = (tm.get("test") or {}).get("brier")  if isinstance(tm.get("test"), dict)  else None
            rows.append({
                "test_start": w.get("test_start"),
                "test_end": w.get("test_end"),
                "task": task_name,
                "train_brier": train_brier,
                "test_brier": test_brier,
                "drift": (
                    round(test_brier - train_brier, 6)
                    if test_brier is not None and train_brier is not None else ""
                ),
                "n_train": (w.get("row_counts") or {}).get("train"),
                "n_test":  (w.get("row_counts") or {}).get("test"),
            })
    if rows:
        with open(drift_csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    LOGGER.info("Wrote %s", summary_path)
    LOGGER.info("Wrote %s", per_window_path)
    if rows:
        LOGGER.info("Wrote %s (%d rows)", drift_csv_path, len(rows))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    if not args.input_path.exists():
        raise SystemExit(f"Missing input path: {args.input_path}")

    LOGGER.info("Loading signals_master rows from %s", args.input_path)
    raw_rows = _read_jsonl(args.input_path)
    LOGGER.info("Loaded %d signal rows", len(raw_rows))

    rows = _filter_rows(
        raw_rows,
        mode=args.mode,
        min_date=None,  # --start-date is the first test date; keep earlier train/val history.
        max_date=args.end_date or None,
    )
    if args.config_label_filter:
        rows = [
            r for r in rows
            if str(r.get("config_label") or "default") == str(args.config_label_filter)
        ]
    LOGGER.info(
        "After mode/end-date/config-label filter (%s/%s): %d rows",
        args.mode,
        args.config_label_filter or "all",
        len(rows),
    )

    available_dates = sorted({_row_date(r) for r in rows if _row_date(r)})
    if not available_dates:
        raise SystemExit("No dated rows in input — cannot run walk-forward.")

    start_d = _parse_date(args.start_date) if args.start_date else None
    end_d   = _parse_date(args.end_date)   if args.end_date   else None

    windows = plan_windows(
        available_dates=available_dates,
        start_date=start_d,
        end_date=end_d,
        train_days=args.train_days,
        val_days=args.val_days,
        test_days=args.test_days,
        min_train_dates=args.min_train_dates,
    )

    LOGGER.info(
        "Planned %d windows (train=%dd, val=%dd, test=%dd) over %d available dates [%s..%s]",
        len(windows), args.train_days, args.val_days, args.test_days,
        len(available_dates), available_dates[0], available_dates[-1],
    )

    if args.plan_only:
        for w in windows:
            print(json.dumps({
                "test_start": w["test_start"],
                "test_end": w["test_end"],
                "train_start": w["train_start"],
                "train_end": w["train_end"],
                "val_start": w["val_start"],
                "val_end": w["val_end"],
                "n_train_dates_with_data": len(w["train_dates_with_data"]),
                "n_val_dates_with_data": len(w["val_dates_with_data"]),
                "n_test_dates_with_data": len(w["test_dates_with_data"]),
                "skip_reason": w.get("skip_reason"),
            }))
        return

    results: List[Dict[str, Any]] = []
    for i, w in enumerate(windows, start=1):
        LOGGER.info(
            "[%d/%d] test=%s..%s  train=%s..%s  val=%s..%s",
            i, len(windows), w["test_start"], w["test_end"],
            w["train_start"], w["train_end"],
            w["val_start"], w["val_end"],
        )
        result = _run_one_window(
            window=w,
            rows=rows,
            drop_unsettled=args.drop_unsettled,
            min_train_labels=args.min_train_labels,
            strict=args.strict,
        )
        if not result.get("completed"):
            LOGGER.info("  skipped: %s", result.get("error"))
            if args.strict:
                raise SystemExit(
                    f"Strict mode failed for test={w['test_start']}..{w['test_end']}: "
                    f"{result.get('error') or 'unknown'}"
                )
        else:
            t = result.get("baseline_live_engine_results", {})
            mp = result.get("model_policy_results", {})
            sw = (result.get("task_metrics", {}).get("signal_win") or {}).get("test") or {}
            LOGGER.info(
                "  completed: n_train=%s n_test=%s placed=%s filled=%s won=%s "
                "baseline_profit=%.2f model_selected=%s model_profit=%.2f "
                "signal_win.test_brier=%s",
                result["row_counts"]["train"], result["row_counts"]["test"],
                t.get("n_placed"), t.get("n_filled"), t.get("n_won"),
                t.get("baseline_realized_profit") or 0.0,
                mp.get("n_selected"),
                mp.get("observed_realized_profit") or 0.0,
                sw.get("brier"),
            )
        results.append(result)

    summary = aggregate(results)
    summary["config_label_filter"] = args.config_label_filter or None
    write_outputs(args.output_root, summary, results)
    LOGGER.info(
        "Walk-forward complete: %d/%d windows completed, baseline_cumulative_profit=%s, baseline_max_dd=%s",
        summary["n_windows_completed"], summary["n_windows_planned"],
        summary["baseline_live_engine_results"]["cumulative_baseline_realized_profit"],
        summary["baseline_live_engine_results"]["max_baseline_drawdown_across_test_windows"],
    )


if __name__ == "__main__":
    main()
