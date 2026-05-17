#!/usr/bin/env python3
"""
under_walk_forward_runner.py -- Rolling walk-forward for UNDER signal_win.

Phase A4 of the Bidirectional-trading roadmap (2026-05-16). Sibling to
`walk_forward_runner.py` (which is OVER-side). Trains UNDER signal_win
on rolling windows by flipping the target label:

    under_target_win = 1 - target_win
    (where target_win is the OVER counterfactual win label)

Scope intentionally narrower than OVER walk-forward:

  - Trains ONLY signal_win. The OVER runner also trains execution_fill,
    but that's a model of whether the LIVE ENGINE'S Over order filled.
    UNDER orders haven't been posted yet (Phase A is offline-only;
    Phase C introduces two-sided quoting), so under_target_filled
    has no historical observations to learn from.

  - No model-policy P&L simulation, no observed-order replay. Those
    require fill predictions and historical Under orders, neither of
    which exists in Phase A. They land in Phase B/C as side-aware
    audit and side-aware live trading mature.

Outputs:
  data/analysis_output/under_walk_forward/summary.json
  data/analysis_output/under_walk_forward/per_window_results.jsonl
  data/analysis_output/under_walk_forward/calibration_drift.csv
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
from typing import Any, Dict, Iterable, List, Optional, Sequence


PROJECT_DIR = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

# Reuse the OVER runner's primitives so the UNDER pipeline doesn't drift
# from the OVER one on window planning, row filtering, or training-table
# construction.
from build_signal_training_table import (  # noqa: E402
    PRE_SIGNAL_COLUMNS,
    PRE_SIGNAL_MODEL_COLUMNS,
    POST_SIGNAL_STATIC_COLUMNS,
    _filter_rows,
    _infer_param_columns,
    _infer_post_signal_columns,
    _row_date,
    _read_jsonl,
    build_training_rows,
)
from train_baseline_models import train_task  # noqa: E402
from walk_forward_runner import (  # noqa: E402
    _date_str,
    _parse_date,
    _split_map_for_window,
    plan_windows,
    _stat,
)

LOGGER = logging.getLogger("under_walk_forward_runner")

DEFAULT_INPUT_PATH = (
    PROJECT_DIR / "data" / "analysis_output" / "unified_signals" / "signals_master.jsonl"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_DIR / "data" / "analysis_output" / "under_walk_forward"
)

DEFAULT_TRAIN_DAYS = 14
DEFAULT_VAL_DAYS = 3
DEFAULT_TEST_DAYS = 1
DEFAULT_MIN_TRAIN_DATES = 5
DEFAULT_MIN_TRAIN_LABELS = 10


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="UNDER-side rolling walk-forward (signal_win only)."
    )
    p.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--mode", choices=["live", "paper", "both"], default="live")
    p.add_argument("--start-date", type=str, default="")
    p.add_argument("--end-date", type=str, default="")
    p.add_argument("--train-days", type=int, default=DEFAULT_TRAIN_DAYS)
    p.add_argument("--val-days", type=int, default=DEFAULT_VAL_DAYS)
    p.add_argument("--test-days", type=int, default=DEFAULT_TEST_DAYS)
    p.add_argument("--min-train-dates", type=int, default=DEFAULT_MIN_TRAIN_DATES)
    p.add_argument("--min-train-labels", type=int, default=DEFAULT_MIN_TRAIN_LABELS)
    p.add_argument("--plan-only", action="store_true")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--drop-unsettled", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _flip_target_win(row: Dict[str, Any]) -> Dict[str, Any]:
    """Returns the training row with target_win flipped to under_target_win.

    Mutates a shallow copy so the source training row stays intact for
    any downstream OVER consumer (the runner does not share rows across
    windows, but defensive copy keeps the function pure).
    """
    out = dict(row)
    over_label = row.get("target_win")
    if over_label is None:
        out["target_win"] = None
    else:
        try:
            out["target_win"] = 1 - int(over_label)
        except (TypeError, ValueError):
            out["target_win"] = None
    return out


def _run_one_window(
    window: Dict[str, Any],
    rows: List[Dict[str, Any]],
    drop_unsettled: bool,
    min_train_labels: int,
    strict: bool,
) -> Dict[str, Any]:
    """Train UNDER signal_win for one walk-forward window.

    Mirrors the OVER `_run_one_window` flow but flips target_win and
    skips the execution_fill task entirely. Per-window metrics are
    returned in the same shape so the aggregator can reuse OVER
    primitives.
    """
    out = dict(window)
    out["completed"] = False
    out["error"] = None
    out["side"] = "under"

    if window.get("skip_reason"):
        out["error"] = window["skip_reason"]
        return out

    split_map = _split_map_for_window(
        window["train_dates_with_data"],
        window["val_dates_with_data"],
        window["test_dates_with_data"],
    )
    window_dates = set(split_map.keys())
    win_rows = [r for r in rows if str(r.get("session_date") or "") in window_dates]

    sorted_dates = sorted(window_dates)
    date_rank = {d: i for i, d in enumerate(sorted_dates)}

    param_cols = _infer_param_columns(win_rows)
    post_cols = _infer_post_signal_columns(win_rows)
    pre_cols_output = list(PRE_SIGNAL_COLUMNS)
    pre_cols = list(PRE_SIGNAL_MODEL_COLUMNS)
    feature_cols_signal = pre_cols

    over_training_rows = build_training_rows(
        rows=win_rows,
        pre_signal_columns=pre_cols_output,
        post_signal_columns=POST_SIGNAL_STATIC_COLUMNS + post_cols,
        param_columns=param_cols,
        split_map=split_map,
        date_rank=date_rank,
        drop_unsettled=drop_unsettled,
    )
    # The under-flip step. target_filled and target_profit stay as-is:
    # they describe what the LIVE engine's OVER order did, not what an
    # UNDER order would have done. We do not train execution_fill on
    # under-side data because there is no under-side fill history.
    training_rows = [_flip_target_win(r) for r in over_training_rows]

    train_subset = [r for r in training_rows if r.get("split") == "train"]
    train_labels_win = sum(1 for r in train_subset if r.get("target_win") is not None)
    out["row_counts"] = {
        "train": sum(1 for r in training_rows if r.get("split") == "train"),
        "validation": sum(1 for r in training_rows if r.get("split") == "validation"),
        "test": sum(1 for r in training_rows if r.get("split") == "test"),
        "train_labels_under_win": train_labels_win,
    }
    if train_labels_win < min_train_labels:
        out["error"] = (
            f"insufficient_train_labels (under_win={train_labels_win}, "
            f"need>={min_train_labels})"
        )
        return out

    metrics_by_task: Dict[str, Any] = {}
    try:
        summary, model_payload, _pred_rows = train_task(
            all_rows=training_rows,
            task_name="signal_win",
            label_col="target_win",  # already flipped to under via _flip_target_win
            feature_cols=feature_cols_signal,
            strict=strict,
        )
        metrics_by_task["signal_win"] = {
            "train": summary.get("metrics", {}).get("train") if isinstance(summary, dict) else None,
            "validation": summary.get("metrics", {}).get("validation") if isinstance(summary, dict) else None,
            "test": summary.get("metrics", {}).get("test") if isinstance(summary, dict) else None,
            "selected_hparams": (
                model_payload.get("search_selected") if isinstance(model_payload, dict) else None
            ),
        }
    except SystemExit as exc:
        metrics_by_task["signal_win"] = {"error": str(exc)}
        if strict:
            out["error"] = f"task_failed:signal_win:{exc}"
            return out
    except Exception as exc:
        metrics_by_task["signal_win"] = {"error": repr(exc)}
        if strict:
            out["error"] = f"task_failed:signal_win:{exc!r}"
            return out

    out["task_metrics"] = metrics_by_task
    out["completed"] = True
    out["error"] = None
    return out


def aggregate(window_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    completed = [w for w in window_results if w.get("completed")]
    skipped = [w for w in window_results if not w.get("completed")]

    def collect(metric: str) -> List[float]:
        vals = []
        for w in completed:
            tm = (w.get("task_metrics") or {}).get("signal_win")
            if not isinstance(tm, dict):
                continue
            test = tm.get("test") if isinstance(tm.get("test"), dict) else None
            if test and test.get(metric) is not None:
                vals.append(float(test[metric]))
        return vals

    brier_vals = collect("brier")
    logloss_vals = collect("logloss")
    auc_vals = collect("auc")

    skipped_reasons: Dict[str, int] = {}
    for w in skipped:
        skipped_reasons[str(w.get("error") or "unknown")] = (
            skipped_reasons.get(str(w.get("error") or "unknown"), 0) + 1
        )

    return {
        "generated_at_utc": _now_iso(),
        "side": "under",
        "n_windows_planned": len(window_results),
        "n_windows_completed": len(completed),
        "n_windows_skipped": len(skipped),
        "skipped_reasons": skipped_reasons,
        "tasks": {
            "signal_win": {
                "n_windows_with_test_metrics": len(brier_vals),
                "test_brier": {
                    "mean": _stat(brier_vals, "mean"),
                    "median": _stat(brier_vals, "median"),
                    "stdev": _stat(brier_vals, "stdev"),
                },
                "test_logloss": {
                    "mean": _stat(logloss_vals, "mean"),
                    "median": _stat(logloss_vals, "median"),
                    "stdev": _stat(logloss_vals, "stdev"),
                },
                "test_auc": {
                    "mean": _stat(auc_vals, "mean"),
                    "median": _stat(auc_vals, "median"),
                    "stdev": _stat(auc_vals, "stdev"),
                },
            },
        },
        "warnings": [
            {
                "code": "phase_a_offline_only",
                "message": (
                    "Phase A4: UNDER walk-forward trains a signal_win "
                    "predictor on flipped labels for offline research "
                    "only. UNDER trading does NOT promote to live until "
                    "Phase B (side-aware infrastructure) and Phase C "
                    "(two-sided quote engine) ship."
                ),
            },
        ],
    }


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

    rows: List[Dict[str, Any]] = []
    for w in window_results:
        tasks = w.get("task_metrics") or {}
        for task_name, tm in tasks.items():
            train_brier = (tm.get("train") or {}).get("brier") if isinstance(tm.get("train"), dict) else None
            test_brier = (tm.get("test") or {}).get("brier") if isinstance(tm.get("test"), dict) else None
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
                "n_test": (w.get("row_counts") or {}).get("test"),
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


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    if not args.input_path.exists():
        raise SystemExit(f"Missing input path: {args.input_path}")

    raw_rows = _read_jsonl(args.input_path)
    rows = _filter_rows(
        raw_rows, mode=args.mode, min_date=None,
        max_date=args.end_date or None,
    )

    available_dates = sorted({_row_date(r) for r in rows if _row_date(r)})
    if not available_dates:
        raise SystemExit("No dated rows in input -- cannot run UNDER walk-forward.")

    start_d = _parse_date(args.start_date) if args.start_date else None
    end_d = _parse_date(args.end_date) if args.end_date else None

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
        "Planned %d UNDER windows (train=%dd, val=%dd, test=%dd) over %d dates [%s..%s]",
        len(windows), args.train_days, args.val_days, args.test_days,
        len(available_dates), available_dates[0], available_dates[-1],
    )

    if args.plan_only:
        for w in windows:
            print(json.dumps({
                "test_start": w["test_start"],
                "test_end": w["test_end"],
                "n_train_dates_with_data": len(w["train_dates_with_data"]),
                "n_val_dates_with_data": len(w["val_dates_with_data"]),
                "n_test_dates_with_data": len(w["test_dates_with_data"]),
                "skip_reason": w.get("skip_reason"),
            }))
        return

    results: List[Dict[str, Any]] = []
    for i, w in enumerate(windows, start=1):
        LOGGER.info(
            "[%d/%d] test=%s..%s", i, len(windows), w["test_start"], w["test_end"]
        )
        result = _run_one_window(
            window=w, rows=rows, drop_unsettled=args.drop_unsettled,
            min_train_labels=args.min_train_labels, strict=args.strict,
        )
        if not result.get("completed"):
            LOGGER.info("  skipped: %s", result.get("error"))
            if args.strict:
                raise SystemExit(
                    f"Strict mode failed for test={w['test_start']}..{w['test_end']}: "
                    f"{result.get('error') or 'unknown'}"
                )
        else:
            sw = (result.get("task_metrics", {}).get("signal_win") or {}).get("test") or {}
            LOGGER.info(
                "  completed: n_train=%s n_test=%s under_signal_win.test_brier=%s",
                result["row_counts"]["train"], result["row_counts"]["test"],
                sw.get("brier"),
            )
        results.append(result)

    summary = aggregate(results)
    write_outputs(args.output_root, summary, results)
    LOGGER.info(
        "UNDER walk-forward complete: %d/%d windows completed",
        summary["n_windows_completed"], summary["n_windows_planned"],
    )


if __name__ == "__main__":
    main()
