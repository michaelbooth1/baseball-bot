#!/usr/bin/env python3
"""
Rolling walk-forward lane for calibration market-anchored alpha models.

The daily alpha trainer is useful for producing runtime-refit research
artifacts, but it uses a simple chronological split. This script makes the
promotion question explicit:

    Does residual alpha beat market ask / no-vig baselines out of sample,
    separately for score-event transition and no-score drift?

For each family + anchor price, every test row is scored only by a model fit on
prior train dates with hyperparameters selected on the validation window.
Policy P&L is evaluated at executable ask, not at no-vig mid, and confidence
intervals are clustered by game/date to avoid treating repeated opportunities
from one game state as independent evidence.

Outputs:
  data/analysis_output/calibration_market_anchored_alpha_walk_forward/
    summary.json
    summary.md
    per_window_results.jsonl
    predictions.jsonl
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import random
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

import train_baseline_models as tbm  # noqa: E402
import train_calibration_market_anchored_alpha as cal_alpha  # noqa: E402
import train_market_anchored_alpha as maa  # noqa: E402


LOGGER = logging.getLogger("calibration_market_anchored_alpha_walk_forward")

DEFAULT_OUTPUT_ROOT = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "calibration_market_anchored_alpha_walk_forward"
)
DEFAULT_TRAIN_DAYS = 7
DEFAULT_VAL_DAYS = 2
DEFAULT_TEST_DAYS = 1
DEFAULT_MIN_TRAIN_DATES = 4
DEFAULT_MIN_TRAIN_ROWS = 30
DEFAULT_THRESHOLDS = "0.00,0.02,0.04,0.06,0.08,0.10"
DEFAULT_BOOTSTRAP_REPS = 500


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rolling walk-forward evaluation for family-separated "
            "market-anchored alpha models."
        )
    )
    parser.add_argument("--table-path", type=Path, default=cal_alpha.DEFAULT_TABLE_PATH)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--mode", choices=["live", "paper", "both"], default="live")
    parser.add_argument("--family", choices=["all", *cal_alpha.FAMILIES], default="all")
    parser.add_argument(
        "--anchor-prices",
        type=str,
        default="ask,mid_no_vig",
        help="Comma-separated anchor prices to evaluate: ask,mid_no_vig.",
    )
    parser.add_argument("--min-date", type=str, default="", help="Inclusive source date.")
    parser.add_argument("--max-date", type=str, default="", help="Inclusive source/test date.")
    parser.add_argument("--start-date", type=str, default="", help="First test date.")
    parser.add_argument("--end-date", type=str, default="", help="Last test date.")
    parser.add_argument("--train-days", type=int, default=DEFAULT_TRAIN_DAYS)
    parser.add_argument("--val-days", type=int, default=DEFAULT_VAL_DAYS)
    parser.add_argument("--test-days", type=int, default=DEFAULT_TEST_DAYS)
    parser.add_argument("--min-train-dates", type=int, default=DEFAULT_MIN_TRAIN_DATES)
    parser.add_argument("--min-train-rows", type=int, default=DEFAULT_MIN_TRAIN_ROWS)
    parser.add_argument("--policy-thresholds", type=str, default=DEFAULT_THRESHOLDS)
    parser.add_argument("--max-iter", type=int, default=cal_alpha.DEFAULT_MAX_ITER if hasattr(cal_alpha, "DEFAULT_MAX_ITER") else maa.DEFAULT_MAX_ITER)
    parser.add_argument("--bootstrap-reps", type=int, default=DEFAULT_BOOTSTRAP_REPS)
    parser.add_argument("--bootstrap-seed", type=int, default=20260515)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


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
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _safe_prob(value: Any) -> Optional[float]:
    out = _safe_float(value)
    if out is None or out <= 0.0 or out >= 1.0:
        return None
    return min(max(out, 1e-8), 1.0 - 1e-8)


def _label_int(value: Any) -> Optional[int]:
    return maa._label_int(value)


def _parse_csv_choices(raw: str, *, allowed: Sequence[str], name: str) -> List[str]:
    out: List[str] = []
    allowed_set = set(allowed)
    for token in str(raw or "").split(","):
        value = token.strip()
        if not value:
            continue
        if value not in allowed_set:
            raise SystemExit(f"Unsupported {name}: {value}. Expected one of {sorted(allowed_set)}")
        if value not in out:
            out.append(value)
    if not out:
        raise SystemExit(f"Expected at least one {name}.")
    return out


def _parse_thresholds(raw: str) -> List[float]:
    out: List[float] = []
    for token in str(raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        out.append(float(token))
    uniq = sorted(set(out))
    if not uniq:
        raise SystemExit("Expected at least one policy threshold.")
    return uniq


def _row_family(row: Mapping[str, Any]) -> str:
    return cal_alpha._family(row)


def _row_date(row: Mapping[str, Any]) -> str:
    return cal_alpha._date_value(row)


def load_alpha_rows(
    source_rows: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    family: str,
    anchor_price: str,
    min_date: str,
    max_date: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for row in source_rows:
        row_mode = str(row.get("mode") or "").strip()
        if mode != "both" and row_mode and row_mode != mode:
            continue
        if family != "all" and _row_family(row) != family:
            continue
        date = _row_date(row)
        if not cal_alpha._date_in_range(date, min_date, max_date):
            continue
        if anchor_price == "mid_no_vig" and _safe_prob(row.get("decision_market_mid_no_vig")) is None:
            continue
        alpha_row = cal_alpha._calibration_alpha_row(
            row,
            split="unsplit",
            anchor_price=anchor_price,
        )
        if alpha_row is None:
            continue
        alpha_row["family"] = _row_family(row)
        alpha_row["anchor_price"] = anchor_price
        alpha_row["decision_ask"] = row.get("decision_ask", alpha_row.get("market_price"))
        alpha_row["decision_market_mid_no_vig"] = row.get("decision_market_mid_no_vig")
        alpha_row["cluster_id"] = _cluster_id(alpha_row)
        rows.append(alpha_row)
    rows.sort(key=lambda r: (str(r.get("session_date")), str(r.get("side_row_id"))))
    return rows


def _cluster_id(row: Mapping[str, Any]) -> str:
    game = str(row.get("game_pk") or "").strip()
    if not game:
        game = "|".join(
            [
                str(row.get("session_date") or ""),
                str(row.get("away_abbrev") or ""),
                str(row.get("home_abbrev") or ""),
            ]
        )
    line = str(row.get("line") or "")
    date = str(row.get("session_date") or "")
    return f"{date}|{game}|{line}"


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
    available_set = set(dates)
    earliest = _parse_date(dates[0])
    latest = _parse_date(dates[-1])
    first_test = earliest + dt.timedelta(days=train_days + val_days)
    if start_date is not None:
        first_test = max(first_test, start_date)
    last_test = end_date if end_date is not None else latest
    if first_test > last_test:
        return []

    windows: List[Dict[str, Any]] = []
    test_start = first_test
    while test_start <= last_test:
        test_end = min(test_start + dt.timedelta(days=test_days - 1), last_test)
        val_end = test_start - dt.timedelta(days=1)
        val_start = val_end - dt.timedelta(days=val_days - 1) if val_days > 0 else test_start
        train_end = val_start - dt.timedelta(days=1)
        train_start = train_end - dt.timedelta(days=train_days - 1)
        train_dates = [_date_str(d) for d in _date_range(train_start, train_end) if _date_str(d) in available_set]
        val_dates = [_date_str(d) for d in _date_range(val_start, val_end) if val_days > 0 and _date_str(d) in available_set]
        test_dates = [_date_str(d) for d in _date_range(test_start, test_end) if _date_str(d) in available_set]
        windows.append(
            {
                "test_start": _date_str(test_start),
                "test_end": _date_str(test_end),
                "train_start": _date_str(train_start),
                "train_end": _date_str(train_end),
                "val_start": _date_str(val_start) if val_days > 0 else None,
                "val_end": _date_str(val_end) if val_days > 0 else None,
                "train_dates_with_data": train_dates,
                "val_dates_with_data": val_dates,
                "test_dates_with_data": test_dates,
                "skip_reason": (
                    "no_test_data"
                    if not test_dates
                    else "insufficient_train_history"
                    if len(train_dates) < min_train_dates
                    else None
                ),
            }
        )
        test_start += dt.timedelta(days=test_days)
    return windows


def _assign_splits(rows: Sequence[Dict[str, Any]], window: Mapping[str, Any]) -> List[Dict[str, Any]]:
    split_by_date: Dict[str, str] = {}
    for date in window.get("train_dates_with_data") or []:
        split_by_date[str(date)] = "train"
    for date in window.get("val_dates_with_data") or []:
        split_by_date[str(date)] = "validation"
    for date in window.get("test_dates_with_data") or []:
        split_by_date[str(date)] = "test"
    out: List[Dict[str, Any]] = []
    for row in rows:
        split = split_by_date.get(str(row.get("session_date") or ""))
        if split is None:
            continue
        new_row = dict(row)
        new_row["split"] = split
        out.append(new_row)
    return out


def _prob_metrics(rows: Sequence[Mapping[str, Any]], field: str) -> Dict[str, Any]:
    y: List[int] = []
    probs: List[float] = []
    missing = 0
    for row in rows:
        label = _label_int(row.get("target_win"))
        prob = _safe_prob(row.get(field))
        if label is None:
            continue
        if prob is None:
            missing += 1
            continue
        y.append(label)
        probs.append(prob)
    out = tbm.metric_summary(y, probs)
    out["missing_prob_rows"] = missing
    return out


def _profit_for_row(row: Mapping[str, Any], price_field: str = "decision_ask") -> Optional[float]:
    price = _safe_prob(row.get(price_field))
    label = _label_int(row.get("target_win"))
    if price is None or label is None:
        return None
    return (1.0 / price) - 1.0 if label == 1 else -1.0


def _policy_selected(
    rows: Sequence[Mapping[str, Any]],
    *,
    prob_field: str,
    threshold: float,
    price_field: str = "decision_ask",
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    for row in rows:
        prob = _safe_prob(row.get(prob_field))
        price = _safe_prob(row.get(price_field))
        if prob is None or price is None:
            continue
        if prob - price >= threshold:
            selected.append(dict(row))
    return selected


def _policy_summary(rows: Sequence[Mapping[str, Any]], *, bootstrap_reps: int, seed: int) -> Dict[str, Any]:
    profits: List[float] = []
    cluster_profit: Dict[str, float] = defaultdict(float)
    cluster_stake: Dict[str, float] = defaultdict(float)
    wins = 0
    losses = 0
    missing = 0
    for row in rows:
        profit = _profit_for_row(row)
        if profit is None:
            missing += 1
            continue
        cluster = str(row.get("cluster_id") or _cluster_id(row))
        cluster_profit[cluster] += profit
        cluster_stake[cluster] += 1.0
        profits.append(profit)
        if _label_int(row.get("target_win")) == 1:
            wins += 1
        else:
            losses += 1
    stake = float(len(profits))
    profit_sum = sum(profits)
    roi = profit_sum / stake if stake else None
    return {
        "selected_rows": len(rows),
        "settled_rows": len(profits),
        "missing_profit_rows": missing,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / stake, 6) if stake else None,
        "profit_units": round(profit_sum, 6),
        "stake_units": round(stake, 6),
        "roi": round(roi, 6) if roi is not None else None,
        "clusters": len(cluster_profit),
        "cluster_bootstrap_ci": _cluster_bootstrap_ci(
            cluster_profit,
            cluster_stake,
            reps=bootstrap_reps,
            seed=seed,
        ),
    }


def _quantile(values: Sequence[float], q: float) -> Optional[float]:
    vals = sorted(float(v) for v in values if math.isfinite(float(v)))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def _cluster_bootstrap_ci(
    cluster_profit: Mapping[str, float],
    cluster_stake: Mapping[str, float],
    *,
    reps: int,
    seed: int,
) -> Dict[str, Any]:
    clusters = sorted(cluster_profit)
    if not clusters:
        return {
            "method": "cluster_bootstrap_by_game_date_line",
            "reps": reps,
            "profit_units_p025": None,
            "profit_units_p975": None,
            "roi_p025": None,
            "roi_p975": None,
        }
    if len(clusters) == 1 or reps <= 0:
        profit = sum(cluster_profit.values())
        stake = sum(cluster_stake.values())
        roi = profit / stake if stake else None
        return {
            "method": "cluster_bootstrap_by_game_date_line",
            "reps": 0,
            "profit_units_p025": round(profit, 6),
            "profit_units_p975": round(profit, 6),
            "roi_p025": round(roi, 6) if roi is not None else None,
            "roi_p975": round(roi, 6) if roi is not None else None,
        }
    rng = random.Random(seed)
    profit_draws: List[float] = []
    roi_draws: List[float] = []
    n = len(clusters)
    for _ in range(reps):
        profit = 0.0
        stake = 0.0
        for _j in range(n):
            cluster = clusters[rng.randrange(n)]
            profit += float(cluster_profit[cluster])
            stake += float(cluster_stake[cluster])
        profit_draws.append(profit)
        if stake > 0:
            roi_draws.append(profit / stake)
    p025 = _quantile(profit_draws, 0.025)
    p975 = _quantile(profit_draws, 0.975)
    r025 = _quantile(roi_draws, 0.025)
    r975 = _quantile(roi_draws, 0.975)
    return {
        "method": "cluster_bootstrap_by_game_date_line",
        "reps": reps,
        "profit_units_p025": round(p025, 6) if p025 is not None else None,
        "profit_units_p975": round(p975, 6) if p975 is not None else None,
        "roi_p025": round(r025, 6) if r025 is not None else None,
        "roi_p975": round(r975, 6) if r975 is not None else None,
    }


def _policy_grid(
    rows: Sequence[Mapping[str, Any]],
    *,
    thresholds: Sequence[float],
    prob_field: str,
    bootstrap_reps: int,
    seed: int,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for threshold in thresholds:
        selected = _policy_selected(rows, prob_field=prob_field, threshold=threshold)
        summary = _policy_summary(selected, bootstrap_reps=bootstrap_reps, seed=seed)
        summary["threshold"] = threshold
        summary["prob_field"] = prob_field
        out.append(summary)
    return out


def _selected_by_row_threshold(rows: Sequence[Mapping[str, Any]], *, threshold_field: str) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    for row in rows:
        threshold = _safe_float(row.get(threshold_field))
        if threshold is None:
            continue
        prob = _safe_prob(row.get("alpha_probability"))
        ask = _safe_prob(row.get("decision_ask"))
        if prob is None or ask is None:
            continue
        if prob - ask >= threshold:
            selected.append(dict(row))
    return selected


def _rank_policy(summary: Mapping[str, Any]) -> Tuple[float, float, int]:
    return (
        float(summary.get("profit_units") or 0.0),
        float(summary.get("roi") if summary.get("roi") is not None else -999.0),
        int(summary.get("settled_rows") or 0),
    )


def _choose_threshold(
    validation_rows: Sequence[Mapping[str, Any]],
    *,
    thresholds: Sequence[float],
    bootstrap_reps: int,
    seed: int,
) -> Dict[str, Any]:
    grid = _policy_grid(
        validation_rows,
        thresholds=thresholds,
        prob_field="alpha_probability",
        bootstrap_reps=bootstrap_reps,
        seed=seed,
    )
    best = dict(sorted(grid, key=_rank_policy, reverse=True)[0]) if grid else {}
    return {"selected_threshold": best.get("threshold"), "validation_grid": grid, "selected_validation_summary": best}


def _prediction_map(pred_rows: Sequence[Mapping[str, Any]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for row in pred_rows:
        if str(row.get("split") or "") != "test":
            continue
        row_id = str(row.get("side_row_id") or "")
        prob = _safe_prob(row.get("alpha_probability"))
        if row_id and prob is not None:
            out[row_id] = prob
    return out


def _with_alpha_predictions(
    rows: Sequence[Mapping[str, Any]],
    probs_by_side_row_id: Mapping[str, float],
    *,
    window_id: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        side_row_id = str(row.get("side_row_id") or "")
        prob = probs_by_side_row_id.get(side_row_id)
        if prob is None:
            continue
        new_row = dict(row)
        market = _safe_prob(new_row.get("decision_ask"))
        new_row["alpha_probability"] = prob
        new_row["alpha_edge_to_ask"] = prob - market if market is not None else None
        new_row["window_id"] = window_id
        out.append(new_row)
    return out


def run_window(
    window: Mapping[str, Any],
    rows: Sequence[Dict[str, Any]],
    *,
    family: str,
    anchor_price: str,
    thresholds: Sequence[float],
    min_train_rows: int,
    max_iter: int,
    bootstrap_reps: int,
    bootstrap_seed: int,
    strict: bool,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    out: Dict[str, Any] = {
        **dict(window),
        "family": family,
        "anchor_price": anchor_price,
        "completed": False,
        "error": None,
    }
    if window.get("skip_reason"):
        out["error"] = window["skip_reason"]
        return out, []

    window_rows = _assign_splits(rows, window)
    train_rows = [r for r in window_rows if r.get("split") == "train" and _label_int(r.get("target_win")) is not None]
    validation_rows = [r for r in window_rows if r.get("split") == "validation" and _label_int(r.get("target_win")) is not None]
    test_rows = [r for r in window_rows if r.get("split") == "test" and _label_int(r.get("target_win")) is not None]
    out["row_counts"] = {
        "train": len([r for r in window_rows if r.get("split") == "train"]),
        "validation": len([r for r in window_rows if r.get("split") == "validation"]),
        "test": len([r for r in window_rows if r.get("split") == "test"]),
        "train_labeled": len(train_rows),
        "validation_labeled": len(validation_rows),
        "test_labeled": len(test_rows),
    }
    if len(train_rows) < min_train_rows:
        out["error"] = f"insufficient_train_rows ({len(train_rows)} < {min_train_rows})"
        return out, []
    if len(set(int(r.get("target_win")) for r in train_rows)) < 2:
        out["error"] = "train_has_one_class"
        return out, []
    if not test_rows:
        out["error"] = "no_test_labels"
        return out, []

    try:
        model_report, _model_payload, pred_rows = maa.train_alpha_model(
            window_rows,
            feature_cols=cal_alpha.CALIBRATION_ALPHA_FEATURE_COLUMNS,
            min_train_rows=min_train_rows,
            max_iter=max_iter,
            strict=strict,
        )
    except SystemExit as exc:
        if strict:
            raise
        out["error"] = str(exc)
        return out, []

    probs_by_id = _prediction_map(pred_rows)
    window_id = f"{family}|{anchor_price}|{window.get('test_start')}"
    scored_test_rows = _with_alpha_predictions(test_rows, probs_by_id, window_id=window_id)
    if not scored_test_rows:
        out["error"] = "no_scored_test_rows"
        return out, []

    # maa.train_alpha_model returns validation predictions separately. Attach
    # them so threshold selection is truly validation-only.
    validation_probs = {
        str(r.get("side_row_id") or ""): _safe_prob(r.get("alpha_probability"))
        for r in pred_rows
        if str(r.get("split") or "") == "validation"
    }
    validation_scored = _with_alpha_predictions(
        [r for r in window_rows if r.get("split") == "validation"],
        {k: v for k, v in validation_probs.items() if v is not None},
        window_id=f"{family}|{anchor_price}|{window.get('test_start')}|validation",
    )
    chosen = _choose_threshold(
        validation_scored,
        thresholds=thresholds,
        bootstrap_reps=bootstrap_reps,
        seed=bootstrap_seed,
    )
    selected_threshold = chosen.get("selected_threshold")
    for row in scored_test_rows:
        row["validation_selected_alpha_threshold"] = selected_threshold
    selected_test = (
        _policy_selected(scored_test_rows, prob_field="alpha_probability", threshold=float(selected_threshold))
        if selected_threshold is not None
        else []
    )
    raw_grid = _policy_grid(
        scored_test_rows,
        thresholds=thresholds,
        prob_field="raw_model_probability",
        bootstrap_reps=bootstrap_reps,
        seed=bootstrap_seed + 11,
    )
    no_vig_grid = _policy_grid(
        scored_test_rows,
        thresholds=thresholds,
        prob_field="decision_market_mid_no_vig",
        bootstrap_reps=bootstrap_reps,
        seed=bootstrap_seed + 17,
    )
    alpha_grid = _policy_grid(
        scored_test_rows,
        thresholds=thresholds,
        prob_field="alpha_probability",
        bootstrap_reps=bootstrap_reps,
        seed=bootstrap_seed + 23,
    )
    out.update(
        {
            "completed": True,
            "model": {
                "search_selected": model_report.get("search_selected"),
                "feature_count_total": model_report.get("feature_count_total"),
                "metrics": model_report.get("metrics"),
            },
            "probability_metrics_test": {
                "market_ask": _prob_metrics(scored_test_rows, "decision_ask"),
                "market_mid_no_vig": _prob_metrics(scored_test_rows, "decision_market_mid_no_vig"),
                "raw_model": _prob_metrics(scored_test_rows, "raw_model_probability"),
                "market_anchored_alpha": _prob_metrics(scored_test_rows, "alpha_probability"),
            },
            "validation_policy_selection": chosen,
            "policy_pnl_test": {
                "all_at_ask": _policy_summary(scored_test_rows, bootstrap_reps=bootstrap_reps, seed=bootstrap_seed + 29),
                "alpha_selected": _policy_summary(selected_test, bootstrap_reps=bootstrap_reps, seed=bootstrap_seed + 31),
                "alpha_grid": alpha_grid,
                "raw_model_grid": raw_grid,
                "no_vig_grid": no_vig_grid,
            },
        }
    )
    return out, scored_test_rows


def _aggregate_policy_grid(
    rows: Sequence[Mapping[str, Any]],
    *,
    thresholds: Sequence[float],
    bootstrap_reps: int,
    seed: int,
) -> Dict[str, Any]:
    alpha_grid = _policy_grid(
        rows,
        thresholds=thresholds,
        prob_field="alpha_probability",
        bootstrap_reps=bootstrap_reps,
        seed=seed,
    )
    raw_grid = _policy_grid(
        rows,
        thresholds=thresholds,
        prob_field="raw_model_probability",
        bootstrap_reps=bootstrap_reps,
        seed=seed + 101,
    )
    no_vig_grid = _policy_grid(
        rows,
        thresholds=thresholds,
        prob_field="decision_market_mid_no_vig",
        bootstrap_reps=bootstrap_reps,
        seed=seed + 202,
    )
    validation_selected_alpha = _selected_by_row_threshold(
        rows,
        threshold_field="validation_selected_alpha_threshold",
    )
    return {
        "all_at_ask": _policy_summary(rows, bootstrap_reps=bootstrap_reps, seed=seed + 303),
        "validation_selected_alpha": _policy_summary(
            validation_selected_alpha,
            bootstrap_reps=bootstrap_reps,
            seed=seed + 404,
        ),
        "alpha_grid": alpha_grid,
        "raw_model_grid": raw_grid,
        "no_vig_grid": no_vig_grid,
        "best_alpha_by_test_profit": sorted(alpha_grid, key=_rank_policy, reverse=True)[0] if alpha_grid else None,
        "best_alpha_by_test_profit_hindsight": sorted(alpha_grid, key=_rank_policy, reverse=True)[0] if alpha_grid else None,
        "best_raw_by_test_profit": sorted(raw_grid, key=_rank_policy, reverse=True)[0] if raw_grid else None,
        "best_no_vig_by_test_profit": sorted(no_vig_grid, key=_rank_policy, reverse=True)[0] if no_vig_grid else None,
    }


def _mean_metric(window_results: Sequence[Mapping[str, Any]], path: Sequence[str]) -> Optional[float]:
    values: List[float] = []
    for window in window_results:
        cur: Any = window
        for key in path:
            if not isinstance(cur, Mapping):
                cur = None
                break
            cur = cur.get(key)
        val = _safe_float(cur)
        if val is not None:
            values.append(val)
    if not values:
        return None
    return round(statistics.mean(values), 6)


def aggregate_summary(
    *,
    combos: Mapping[str, Dict[str, Any]],
    source_rows: int,
    config: Mapping[str, Any],
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "generated_at_utc": _now_iso(),
        "description": (
            "Rolling walk-forward market-anchored alpha lane. Families and "
            "anchor prices are evaluated separately; P&L uses executable ask."
        ),
        "source_rows": source_rows,
        "config": dict(config),
        "families": {},
        "warnings": [
            {
                "code": "research_only",
                "message": "This report is not a live-trading artifact. Promotion requires stable family-specific walk-forward evidence.",
            },
            {
                "code": "clustered_ci_small_sample",
                "message": "Cluster bootstrap intervals can be wide or degenerate when completed windows or game clusters are sparse.",
            },
        ],
    }
    for combo_key, payload in combos.items():
        window_results = payload["window_results"]
        completed = [w for w in window_results if w.get("completed")]
        skipped = [w for w in window_results if not w.get("completed")]
        prediction_rows = payload["prediction_rows"]
        family_summary = {
            "family": payload["family"],
            "anchor_price": payload["anchor_price"],
            "rows_loaded": payload["rows_loaded"],
            "rows_by_date": payload["rows_by_date"],
            "windows_planned": len(window_results),
            "windows_completed": len(completed),
            "windows_skipped": len(skipped),
            "skipped_reasons": dict(Counter(str(w.get("error") or "unknown") for w in skipped)),
            "out_of_sample_rows": len(prediction_rows),
            "out_of_sample_clusters": len({str(r.get("cluster_id") or "") for r in prediction_rows}),
            "probability_metrics_all_test": {
                "market_ask": _prob_metrics(prediction_rows, "decision_ask"),
                "market_mid_no_vig": _prob_metrics(prediction_rows, "decision_market_mid_no_vig"),
                "raw_model": _prob_metrics(prediction_rows, "raw_model_probability"),
                "market_anchored_alpha": _prob_metrics(prediction_rows, "alpha_probability"),
            },
            "mean_window_brier": {
                "market_ask": _mean_metric(completed, ["probability_metrics_test", "market_ask", "brier"]),
                "market_mid_no_vig": _mean_metric(completed, ["probability_metrics_test", "market_mid_no_vig", "brier"]),
                "raw_model": _mean_metric(completed, ["probability_metrics_test", "raw_model", "brier"]),
                "market_anchored_alpha": _mean_metric(completed, ["probability_metrics_test", "market_anchored_alpha", "brier"]),
            },
            "policy_pnl_all_test": _aggregate_policy_grid(
                prediction_rows,
                thresholds=config["policy_thresholds"],
                bootstrap_reps=int(config["bootstrap_reps"]),
                seed=int(config["bootstrap_seed"]),
            ),
        }
        summary["families"][combo_key] = family_summary
    return summary


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(dict(row), sort_keys=True) + "\n")


def _write_markdown(path: Path, summary: Mapping[str, Any]) -> None:
    lines = [
        "# Calibration Market-Anchored Alpha Walk-Forward",
        "",
        f"Generated: {summary.get('generated_at_utc')}",
        "",
        "| family | anchor | windows | OOS rows | alpha Brier | ask Brier | no-vig Brier | val-selected ROI | val-selected profit | CI profit |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for payload in (summary.get("families") or {}).values():
        metrics = payload.get("probability_metrics_all_test") or {}
        alpha = metrics.get("market_anchored_alpha") or {}
        ask = metrics.get("market_ask") or {}
        no_vig = metrics.get("market_mid_no_vig") or {}
        best = ((payload.get("policy_pnl_all_test") or {}).get("validation_selected_alpha") or {})
        ci = best.get("cluster_bootstrap_ci") or {}
        ci_text = ""
        if ci.get("profit_units_p025") is not None:
            ci_text = f"[{ci.get('profit_units_p025'):.2f}, {ci.get('profit_units_p975'):.2f}]"
        lines.append(
            "| "
            + " | ".join(
                [
                    str(payload.get("family")),
                    str(payload.get("anchor_price")),
                    f"{payload.get('windows_completed')}/{payload.get('windows_planned')}",
                    str(payload.get("out_of_sample_rows")),
                    "" if alpha.get("brier") is None else f"{alpha.get('brier'):.4f}",
                    "" if ask.get("brier") is None else f"{ask.get('brier'):.4f}",
                    "" if no_vig.get("brier") is None else f"{no_vig.get('brier'):.4f}",
                    "" if best.get("roi") is None else f"{best.get('roi'):.3f}",
                    f"{float(best.get('profit_units') or 0.0):.2f}",
                    ci_text,
                ]
            )
            + " |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _validate_args(args: argparse.Namespace) -> None:
    for attr in ("min_date", "max_date", "start_date", "end_date"):
        raw = str(getattr(args, attr) or "")
        if raw:
            _parse_date(raw)
    if args.min_date and args.max_date and args.min_date > args.max_date:
        raise SystemExit("--min-date must be <= --max-date")
    if args.start_date and args.end_date and args.start_date > args.end_date:
        raise SystemExit("--start-date must be <= --end-date")
    if args.train_days <= 0 or args.val_days < 0 or args.test_days <= 0:
        raise SystemExit("--train-days and --test-days must be > 0; --val-days must be >= 0")
    if args.min_train_dates < 1 or args.min_train_rows < 1:
        raise SystemExit("--min-train-dates and --min-train-rows must be >= 1")
    if args.bootstrap_reps < 0:
        raise SystemExit("--bootstrap-reps must be >= 0")


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    _validate_args(args)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    if not args.table_path.exists():
        raise SystemExit(f"Missing table path: {args.table_path}")

    families = list(cal_alpha.FAMILIES) if args.family == "all" else [args.family]
    anchors = _parse_csv_choices(
        args.anchor_prices,
        allowed=("ask", "mid_no_vig"),
        name="anchor price",
    )
    thresholds = _parse_thresholds(args.policy_thresholds)
    source_rows = cal_alpha._read_jsonl(args.table_path)
    end_date = _parse_date(args.end_date or args.max_date) if (args.end_date or args.max_date) else None
    start_date = _parse_date(args.start_date) if args.start_date else None
    config = {
        "table_path": str(args.table_path),
        "output_root": str(args.output_root),
        "mode": args.mode,
        "family": args.family,
        "anchor_prices": anchors,
        "min_date": args.min_date or None,
        "max_date": args.max_date or None,
        "start_date": args.start_date or None,
        "end_date": args.end_date or None,
        "train_days": args.train_days,
        "val_days": args.val_days,
        "test_days": args.test_days,
        "min_train_dates": args.min_train_dates,
        "min_train_rows": args.min_train_rows,
        "policy_thresholds": thresholds,
        "max_iter": args.max_iter,
        "bootstrap_reps": args.bootstrap_reps,
        "bootstrap_seed": args.bootstrap_seed,
    }

    combos: Dict[str, Dict[str, Any]] = {}
    all_windows: List[Dict[str, Any]] = []
    all_predictions: List[Dict[str, Any]] = []
    for family in families:
        for anchor_price in anchors:
            rows = load_alpha_rows(
                source_rows,
                mode=args.mode,
                family=family,
                anchor_price=anchor_price,
                min_date=args.min_date,
                max_date=args.max_date,
            )
            available_dates = sorted({str(r.get("session_date") or "") for r in rows if r.get("session_date")})
            windows = plan_windows(
                available_dates,
                start_date=start_date,
                end_date=end_date,
                train_days=args.train_days,
                val_days=args.val_days,
                test_days=args.test_days,
                min_train_dates=args.min_train_dates,
            )
            combo_key = f"{family}|{anchor_price}"
            combos[combo_key] = {
                "family": family,
                "anchor_price": anchor_price,
                "rows_loaded": len(rows),
                "rows_by_date": dict(Counter(str(r.get("session_date") or "") for r in rows)),
                "window_results": [],
                "prediction_rows": [],
            }
            if args.plan_only:
                for window in windows:
                    print(json.dumps({"family": family, "anchor_price": anchor_price, **window}, sort_keys=True))
                continue
            for idx, window in enumerate(windows):
                result, pred_rows = run_window(
                    window,
                    rows,
                    family=family,
                    anchor_price=anchor_price,
                    thresholds=thresholds,
                    min_train_rows=args.min_train_rows,
                    max_iter=args.max_iter,
                    bootstrap_reps=args.bootstrap_reps,
                    bootstrap_seed=args.bootstrap_seed + idx,
                    strict=args.strict,
                )
                if args.strict and result.get("error"):
                    raise SystemExit(
                        f"Strict mode failed for {family}/{anchor_price}/{window['test_start']}: {result['error']}"
                    )
                combos[combo_key]["window_results"].append(result)
                combos[combo_key]["prediction_rows"].extend(pred_rows)
                all_windows.append(result)
                all_predictions.extend(pred_rows)
                LOGGER.info(
                    "Window family=%s anchor=%s test=%s completed=%s error=%s",
                    family,
                    anchor_price,
                    window.get("test_start"),
                    result.get("completed"),
                    result.get("error"),
                )

    if args.plan_only:
        return

    summary = aggregate_summary(
        combos=combos,
        source_rows=len(source_rows),
        config=config,
    )
    args.output_root.mkdir(parents=True, exist_ok=True)
    summary_path = args.output_root / "summary.json"
    md_path = args.output_root / "summary.md"
    per_window_path = args.output_root / "per_window_results.jsonl"
    predictions_path = args.output_root / "predictions.jsonl"
    _write_json(summary_path, summary)
    _write_markdown(md_path, summary)
    _write_jsonl(per_window_path, all_windows)
    _write_jsonl(predictions_path, all_predictions)
    LOGGER.info("Wrote %s", summary_path)
    LOGGER.info("Wrote %s", md_path)
    LOGGER.info("Wrote %s", per_window_path)
    LOGGER.info("Wrote %s", predictions_path)
    print(f"Wrote {summary_path}")


if __name__ == "__main__":
    main()
