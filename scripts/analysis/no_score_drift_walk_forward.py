#!/usr/bin/env python3
"""
no_score_drift_walk_forward.py -- Deduped no-score drift training + WF harness.

This is the separate model-family research lane for `no_score_drift`.
It intentionally works from the shadow candidate universe, not live placed
orders, because no-score drift is not live-enforced yet.

Row unit:
  one first-eligible shadow no-score candidate per game-line / same-score
  segment. This avoids counting repeated ticks from one quiet segment as
  independent evidence.

Outputs:
  data/analysis_output/no_score_drift_walk_forward/
    no_score_drift_training_rows.jsonl
    no_score_drift_training_rows.csv
    summary.json
    per_window_results.jsonl
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_no_score_drift_paper_ledger as ledger  # noqa: E402
import evaluate_no_score_drift_policy as nsd  # noqa: E402
import train_baseline_models as tbm  # noqa: E402
from scripts.trading.scoring_path_features import (  # noqa: E402
    SCORING_PATH_FIELD_KEYS,
    SCORING_PATH_MODEL_FIELD_KEYS,
)
from scripts.trading.weather_client import (  # noqa: E402
    WEATHER_FEATURE_FIELD_KEYS,
    WEATHER_MODEL_FEATURE_FIELD_KEYS,
)


LOGGER = logging.getLogger("no_score_drift_walk_forward")

DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "no_score_drift_walk_forward"
DEFAULT_TRAIN_DAYS = 14
DEFAULT_VAL_DAYS = 3
DEFAULT_TEST_DAYS = 1
DEFAULT_MIN_TRAIN_DATES = 3
DEFAULT_MIN_TRAIN_ROWS = 12
DEFAULT_MODEL_EDGE_THRESHOLDS = "0.00,0.02,0.04,0.06,0.08,0.10"

TRAINING_COLUMNS = [
    "row_id",
    "policy_row_id",
    "dedup_key",
    "duplicate_candidate_rows",
    "signal_model_family",
    "mode",
    "session_date",
    "ts",
    "candidate_id",
    "game_pk",
    "away_abbrev",
    "home_abbrev",
    "line",
    "inning",
    "inning_state",
    "outs",
    "runners_on",
    "away_score_before",
    "home_score_before",
    "current_total",
    "home_leading_late",
    "batting_team_is_home",
    "bottom9_available_if_needed",
    "expected_remaining_half_innings",
    "expected_remaining_pa_bucket",
    "home_skip_bottom9_risk",
    *SCORING_PATH_FIELD_KEYS,
    *WEATHER_FEATURE_FIELD_KEYS,
    "shadow_bottom9_home_lead_context",
    "shadow_home_skip_bottom9_risk_bucket",
    "score_segment_key",
    "score_segment_age_secs",
    "score_segment_ticks",
    "score_segment_drawdown",
    "decision_ask",
    "best_bid",
    "spread",
    "fair_value",
    "edge",
    "current_state_value_base_poisson",
    "current_state_value_base_empirical",
    "current_state_value_fv_raw",
    "current_state_value_edge",
    "current_state_value_empirical_edge",
    "current_state_value_used_fallback",
    "current_state_value_state_fallback_level",
    "current_state_value_state_fallback_label",
    "current_state_value_line_fallback_mode",
    "shadow_inning_runs_needed_bucket",
    "shadow_current_phantom_combo_bucket",
    "support_regime",
    "poisson_edge_pass",
    "empirical_edge_pass",
    "shadow_no_score_drift_trigger",
    "baseline_ask",
    "ask_jump",
    "lookback_ticks",
    "outcome_available",
    "target_win",
    "target_taker_profit_units",
    "final_total",
    "final_away",
    "final_home",
    "paper_decision",
    "paper_skip_reason",
    "paper_filled",
    "paper_fill_price",
    "paper_profit_usdc",
    "paper_roi",
    "paper_stake_usdc",
]

MODEL_FEATURE_COLUMNS = [
    "decision_ask",
    "best_bid",
    "spread",
    "fair_value",
    "edge",
    "current_state_value_base_poisson",
    "current_state_value_base_empirical",
    "current_state_value_fv_raw",
    "current_state_value_edge",
    "current_state_value_empirical_edge",
    "current_state_value_used_fallback",
    "current_state_value_state_fallback_level",
    "current_state_value_line_fallback_mode",
    "support_regime",
    "shadow_no_score_drift_trigger",
    "inning",
    "inning_state",
    "outs",
    "runners_on",
    "current_total",
    "home_leading_late",
    "batting_team_is_home",
    "bottom9_available_if_needed",
    "expected_remaining_half_innings",
    "expected_remaining_pa_bucket",
    "home_skip_bottom9_risk",
    *SCORING_PATH_MODEL_FIELD_KEYS,
    *WEATHER_MODEL_FEATURE_FIELD_KEYS,
    "shadow_bottom9_home_lead_context",
    "shadow_home_skip_bottom9_risk_bucket",
    "shadow_inning_runs_needed_bucket",
    "shadow_current_phantom_combo_bucket",
    "line",
    "score_segment_age_secs",
    "score_segment_ticks",
    "score_segment_drawdown",
    "baseline_ask",
    "ask_jump",
]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Deduped no-score drift walk-forward harness.")
    p.add_argument("--mode", choices=["live", "paper", "both"], default="live")
    p.add_argument("--min-date", type=str, default="", help="Inclusive source date.")
    p.add_argument("--max-date", type=str, default="", help="Inclusive source/test date.")
    p.add_argument("--start-date", type=str, default="", help="First walk-forward test date.")
    p.add_argument("--end-date", type=str, default="", help="Last walk-forward test date.")
    p.add_argument("--train-days", type=int, default=DEFAULT_TRAIN_DAYS)
    p.add_argument("--val-days", type=int, default=DEFAULT_VAL_DAYS)
    p.add_argument("--test-days", type=int, default=DEFAULT_TEST_DAYS)
    p.add_argument("--min-train-dates", type=int, default=DEFAULT_MIN_TRAIN_DATES)
    p.add_argument("--min-train-rows", type=int, default=DEFAULT_MIN_TRAIN_ROWS)
    p.add_argument("--model-edge-thresholds", type=str, default=DEFAULT_MODEL_EDGE_THRESHOLDS)
    p.add_argument("--stake", type=float, default=ledger.DEFAULT_STAKE_USDC)
    p.add_argument("--daily-budget", type=float, default=ledger.DEFAULT_DAILY_BUDGET_USDC)
    p.add_argument("--per-game-budget-fraction", type=float, default=ledger.DEFAULT_PER_GAME_BUDGET_FRACTION)
    p.add_argument("--max-orders-per-game", type=int, default=ledger.DEFAULT_MAX_ORDERS_PER_GAME)
    p.add_argument("--max-orders-per-game-line", type=int, default=ledger.DEFAULT_MAX_ORDERS_PER_GAME_LINE)
    p.add_argument("--support-regimes", type=str, default=",".join(ledger.DEFAULT_SUPPORT_REGIMES))
    p.add_argument("--price-policy", choices=["taker", "ask_minus_cents", "bid_plus_cents"], default="taker")
    p.add_argument("--fill-assumption", choices=["immediate", "touch_within_segment"], default="immediate")
    p.add_argument("--price-offset-cents", type=float, default=ledger.DEFAULT_PRICE_OFFSET_CENTS)
    p.add_argument("--touch-window-seconds", type=float, default=ledger.DEFAULT_TOUCH_WINDOW_SECONDS)
    p.add_argument("--min-poisson-edge", type=float, default=nsd.DEFAULT_MIN_POISSON_EDGE)
    p.add_argument("--min-empirical-edge", type=float, default=nsd.DEFAULT_MIN_EMPIRICAL_EDGE)
    p.add_argument("--include-unsettled", action="store_true")
    p.add_argument("--plan-only", action="store_true")
    p.add_argument("--build-only", action="store_true")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_date(raw: str) -> dt.date:
    return dt.datetime.strptime(raw, "%Y-%m-%d").date()


def _date_str(value: dt.date) -> str:
    return value.strftime("%Y-%m-%d")


def _date_range(start: dt.date, end: dt.date) -> List[dt.date]:
    out: List[dt.date] = []
    value = start
    while value <= end:
        out.append(value)
        value += dt.timedelta(days=1)
    return out


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        if math.isnan(out) or not math.isfinite(out):
            return None
        return out
    except Exception:
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def _bool_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if int(value) == 1 else 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return 1
    if text in {"0", "false", "no", "n"}:
        return 0
    return None


def _clip_prob(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    if value <= 0.0 or value >= 1.0:
        return None
    return min(max(float(value), 1e-8), 1.0 - 1e-8)


def _parse_float_csv(raw: str) -> List[float]:
    out: List[float] = []
    for token in str(raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        out.append(float(token))
    uniq = sorted(set(out))
    if not uniq:
        raise SystemExit("Expected at least one model edge threshold.")
    return uniq


def _allowed_support_regimes(raw: str) -> Tuple[str, ...]:
    return ledger._allowed_support_regimes(raw)


def _training_sort_key(row: Dict[str, Any]) -> Tuple[str, str, str]:
    return (
        str(row.get("session_date") or ""),
        str(row.get("ts") or ""),
        str(row.get("row_id") or ""),
    )


def _ledger_key(row: Dict[str, Any]) -> Tuple[str, str]:
    return str(row.get("dedup_key") or ""), str(row.get("candidate_id") or "")


def build_training_rows(
    policy_rows: Sequence[Dict[str, Any]],
    ledger_rows: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    ledger_by_key = {_ledger_key(row): row for row in ledger_rows}
    out: List[Dict[str, Any]] = []
    for idx, row in enumerate(sorted(policy_rows, key=lambda r: (str(r.get("ts") or ""), str(r.get("candidate_id") or ""))), start=1):
        lrow = ledger_by_key.get(_ledger_key(row), {})
        target_win = _bool_int(row.get("over_hit")) if row.get("outcome_available") else None
        train_row: Dict[str, Any] = {col: None for col in TRAINING_COLUMNS}
        train_row.update({
            "row_id": f"nsdwf_{idx:06d}",
            "policy_row_id": row.get("policy_row_id"),
            "dedup_key": row.get("dedup_key"),
            "duplicate_candidate_rows": row.get("duplicate_candidate_rows"),
            "signal_model_family": row.get("signal_model_family") or "no_score_drift",
            "mode": row.get("mode"),
            "session_date": row.get("session_date"),
            "ts": row.get("ts"),
            "candidate_id": row.get("candidate_id"),
            "game_pk": row.get("game_pk"),
            "away_abbrev": row.get("away_abbrev"),
            "home_abbrev": row.get("home_abbrev"),
            "line": row.get("line"),
            "inning": row.get("inning"),
            "inning_state": row.get("inning_state"),
            "outs": row.get("outs"),
            "runners_on": row.get("runners_on"),
            "away_score_before": row.get("away_score_before"),
            "home_score_before": row.get("home_score_before"),
            "current_total": row.get("current_total"),
            "home_leading_late": row.get("home_leading_late"),
            "batting_team_is_home": row.get("batting_team_is_home"),
            "bottom9_available_if_needed": row.get("bottom9_available_if_needed"),
            "expected_remaining_half_innings": row.get("expected_remaining_half_innings"),
            "expected_remaining_pa_bucket": row.get("expected_remaining_pa_bucket"),
            "home_skip_bottom9_risk": row.get("home_skip_bottom9_risk"),
            **{key: row.get(key) for key in SCORING_PATH_FIELD_KEYS},
            **{key: row.get(key) for key in WEATHER_FEATURE_FIELD_KEYS},
            "shadow_bottom9_home_lead_context": row.get("shadow_bottom9_home_lead_context"),
            "shadow_home_skip_bottom9_risk_bucket": row.get("shadow_home_skip_bottom9_risk_bucket"),
            "score_segment_key": row.get("score_segment_key"),
            "score_segment_age_secs": row.get("score_segment_age_secs"),
            "score_segment_ticks": row.get("score_segment_ticks"),
            "score_segment_drawdown": row.get("score_segment_drawdown"),
            "decision_ask": row.get("decision_ask"),
            "best_bid": row.get("best_bid"),
            "spread": row.get("spread"),
            "fair_value": row.get("fair_value"),
            "edge": row.get("edge"),
            "current_state_value_base_poisson": row.get("current_state_value_base_poisson"),
            "current_state_value_base_empirical": row.get("current_state_value_base_empirical"),
            "current_state_value_fv_raw": row.get("current_state_value_fv_raw"),
            "current_state_value_edge": row.get("current_state_value_edge"),
            "current_state_value_empirical_edge": row.get("current_state_value_empirical_edge"),
            "current_state_value_used_fallback": row.get("current_state_value_used_fallback"),
            "current_state_value_state_fallback_level": row.get("current_state_value_state_fallback_level"),
            "current_state_value_state_fallback_label": row.get("current_state_value_state_fallback_label"),
            "current_state_value_line_fallback_mode": row.get("current_state_value_line_fallback_mode"),
            "shadow_inning_runs_needed_bucket": row.get("shadow_inning_runs_needed_bucket"),
            "shadow_current_phantom_combo_bucket": row.get("shadow_current_phantom_combo_bucket"),
            "support_regime": row.get("support_regime"),
            "poisson_edge_pass": row.get("poisson_edge_pass"),
            "empirical_edge_pass": row.get("empirical_edge_pass"),
            "shadow_no_score_drift_trigger": row.get("shadow_no_score_drift_trigger"),
            "baseline_ask": row.get("baseline_ask"),
            "ask_jump": row.get("ask_jump"),
            "lookback_ticks": row.get("lookback_ticks"),
            "outcome_available": bool(row.get("outcome_available")),
            "target_win": target_win,
            "target_taker_profit_units": row.get("taker_profit_units"),
            "final_total": row.get("final_total"),
            "final_away": row.get("final_away"),
            "final_home": row.get("final_home"),
            "paper_decision": lrow.get("decision"),
            "paper_skip_reason": lrow.get("skip_reason"),
            "paper_filled": lrow.get("filled"),
            "paper_fill_price": lrow.get("fill_price"),
            "paper_profit_usdc": lrow.get("profit_usdc"),
            "paper_roi": lrow.get("roi"),
            "paper_stake_usdc": lrow.get("stake_usdc"),
        })
        out.append(train_row)
    out.sort(key=_training_sort_key)
    return out


def load_no_score_training_source(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    support_regimes = _allowed_support_regimes(args.support_regimes)
    candidates, outcomes = nsd.load_rows(args.mode, args.min_date, args.max_date)
    policy_rows, policy_counts = nsd.build_policy_rows(
        candidates,
        outcomes,
        min_poisson_edge=args.min_poisson_edge,
        min_empirical_edge=args.min_empirical_edge,
    )
    ledger_rows = ledger.build_ledger_rows(
        policy_rows,
        raw_candidates=candidates,
        allowed_support_regimes=support_regimes,
        stake_usdc=args.stake,
        daily_budget_usdc=args.daily_budget,
        per_game_budget_fraction=args.per_game_budget_fraction,
        max_orders_per_game=args.max_orders_per_game,
        max_orders_per_game_line=args.max_orders_per_game_line,
        price_policy=args.price_policy,
        fill_assumption=args.fill_assumption,
        price_offset_cents=args.price_offset_cents,
        touch_window_seconds=args.touch_window_seconds,
        include_unsettled=args.include_unsettled,
    )
    training_rows = build_training_rows(policy_rows, ledger_rows)
    counts = {
        **policy_counts,
        "training_rows": len(training_rows),
        "ledger_rows": len(ledger_rows),
        "rows_with_outcome": sum(1 for row in training_rows if row.get("target_win") is not None),
    }
    return candidates, policy_rows, training_rows, counts


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
    if not available_dates:
        return []
    available_set = set(available_dates)
    earliest = _parse_date(min(available_dates))
    latest = _parse_date(max(available_dates))
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
        val_start = val_end - dt.timedelta(days=val_days - 1)
        train_end = val_start - dt.timedelta(days=1)
        train_start = train_end - dt.timedelta(days=train_days - 1)
        train_dates = [_date_str(d) for d in _date_range(train_start, train_end) if _date_str(d) in available_set]
        val_dates = [_date_str(d) for d in _date_range(val_start, val_end) if _date_str(d) in available_set]
        test_dates = [_date_str(d) for d in _date_range(test_start, test_end) if _date_str(d) in available_set]
        windows.append({
            "test_start": _date_str(test_start),
            "test_end": _date_str(test_end),
            "train_start": _date_str(train_start),
            "train_end": _date_str(train_end),
            "val_start": _date_str(val_start),
            "val_end": _date_str(val_end),
            "train_dates_with_data": train_dates,
            "val_dates_with_data": val_dates,
            "test_dates_with_data": test_dates,
            "skip_reason": (
                "no_test_data" if not test_dates
                else "insufficient_train_history" if len(train_dates) < min_train_dates
                else None
            ),
        })
        test_start += dt.timedelta(days=test_days)
    return windows


def _metrics_for_probs(rows: Sequence[Dict[str, Any]], prob_field: str) -> Dict[str, Any]:
    y: List[int] = []
    probs: List[float] = []
    missing = 0
    for row in rows:
        label = _bool_int(row.get("target_win"))
        prob = _clip_prob(_safe_float(row.get(prob_field)))
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


def _market_edge(row: Dict[str, Any], prob_field: str) -> Optional[float]:
    prob = _clip_prob(_safe_float(row.get(prob_field)))
    ask = _clip_prob(_safe_float(row.get("decision_ask")))
    if prob is None or ask is None:
        return None
    return prob - ask


def _paper_summary(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    submitted = [r for r in rows if str(r.get("paper_decision") or "").startswith("submitted")]
    filled = [r for r in submitted if r.get("paper_filled") is True]
    wins = [r for r in filled if _bool_int(r.get("target_win")) == 1]
    profits = [_safe_float(r.get("paper_profit_usdc")) for r in filled]
    profits = [p for p in profits if p is not None]
    costs = [_safe_float(r.get("paper_stake_usdc")) for r in filled]
    costs = [c for c in costs if c is not None]
    return {
        "rows": len(rows),
        "submitted": len(submitted),
        "filled": len(filled),
        "wins": len(wins),
        "fill_rate": round(len(filled) / len(submitted), 6) if submitted else None,
        "win_rate": round(len(wins) / len(filled), 6) if filled else None,
        "profit_usdc": round(sum(profits), 4) if profits else 0.0,
        "stake_filled_usdc": round(sum(costs), 4) if costs else 0.0,
        "roi": round(sum(profits) / sum(costs), 6) if costs and sum(costs) > 0 else None,
    }


def _group_summary(rows: Sequence[Dict[str, Any]], field: str) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "missing")].append(row)
    return {key: _paper_summary(group) for key, group in sorted(groups.items())}


def _selected_by_model_edge(rows: Sequence[Dict[str, Any]], probs_by_row_id: Dict[str, float], threshold: float) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    for row in rows:
        prob = probs_by_row_id.get(str(row.get("row_id") or ""))
        ask = _clip_prob(_safe_float(row.get("decision_ask")))
        if prob is None or ask is None:
            continue
        if prob - ask >= threshold:
            selected.append(row)
    return selected


def _prediction_map(pred_rows: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for row in pred_rows:
        row_id = str(row.get("bet_id") or "")
        prob = _clip_prob(_safe_float(row.get("prob")))
        if row_id and prob is not None:
            out[row_id] = prob
    return out


def _fit_model(window_rows: List[Dict[str, Any]], *, strict: bool) -> Tuple[Optional[Dict[str, Any]], Dict[str, float], Optional[str]]:
    labeled_train = [
        row for row in window_rows
        if row.get("split") == "train" and row.get("target_win") is not None
    ]
    if len(set(_bool_int(row.get("target_win")) for row in labeled_train)) < 2:
        return None, {}, "train_has_one_class"
    try:
        summary, _payload, pred_rows = tbm.train_task(
            all_rows=window_rows,
            task_name="no_score_drift_win",
            label_col="target_win",
            feature_cols=MODEL_FEATURE_COLUMNS,
            strict=strict,
        )
        return summary, _prediction_map(pred_rows), None
    except SystemExit as exc:
        if strict:
            raise
        return None, {}, str(exc)


def _choose_threshold(
    validation_rows: List[Dict[str, Any]],
    probs_by_row_id: Dict[str, float],
    thresholds: Sequence[float],
) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    for threshold in thresholds:
        selected = _selected_by_model_edge(validation_rows, probs_by_row_id, threshold)
        summary = _paper_summary(selected)
        candidates.append({
            "threshold": threshold,
            "summary": summary,
        })

    def rank_key(row: Dict[str, Any]) -> Tuple[float, float, int]:
        summary = row["summary"]
        return (
            float(summary.get("profit_usdc") or 0.0),
            float(summary.get("roi") or -999.0),
            int(summary.get("filled") or 0),
        )

    best = dict(sorted(candidates, key=rank_key, reverse=True)[0])
    best["grid"] = candidates
    return best


def _assign_splits(rows: Sequence[Dict[str, Any]], window: Dict[str, Any]) -> List[Dict[str, Any]]:
    split_by_date: Dict[str, str] = {}
    for date in window["train_dates_with_data"]:
        split_by_date[date] = "train"
    for date in window["val_dates_with_data"]:
        split_by_date[date] = "validation"
    for date in window["test_dates_with_data"]:
        split_by_date[date] = "test"
    out: List[Dict[str, Any]] = []
    for row in rows:
        split = split_by_date.get(str(row.get("session_date") or ""))
        if split is None:
            continue
        new_row = dict(row)
        new_row["split"] = split
        # train_task prediction rows use bet_id as the stable key.
        new_row["bet_id"] = new_row.get("row_id")
        out.append(new_row)
    return out


def run_window(
    window: Dict[str, Any],
    rows: Sequence[Dict[str, Any]],
    *,
    thresholds: Sequence[float],
    min_train_rows: int,
    strict: bool,
) -> Dict[str, Any]:
    out = dict(window)
    out["completed"] = False
    out["error"] = None
    if window.get("skip_reason"):
        out["error"] = window["skip_reason"]
        return out

    window_rows = _assign_splits(rows, window)
    train_rows = [r for r in window_rows if r["split"] == "train" and r.get("target_win") is not None]
    validation_rows = [r for r in window_rows if r["split"] == "validation"]
    test_rows = [r for r in window_rows if r["split"] == "test"]
    out["row_counts"] = {
        "train": len([r for r in window_rows if r["split"] == "train"]),
        "validation": len(validation_rows),
        "test": len(test_rows),
        "train_labeled": len(train_rows),
        "test_labeled": len([r for r in test_rows if r.get("target_win") is not None]),
    }
    if len(train_rows) < min_train_rows:
        out["error"] = f"insufficient_train_rows ({len(train_rows)} < {min_train_rows})"
        return out

    model_summary, probs_by_row_id, model_error = _fit_model(window_rows, strict=strict)
    if model_error and strict:
        raise SystemExit(model_error)

    chosen = _choose_threshold(validation_rows, probs_by_row_id, thresholds) if probs_by_row_id else {
        "threshold": None,
        "summary": _paper_summary([]),
        "grid": [],
    }
    selected_test = (
        _selected_by_model_edge(test_rows, probs_by_row_id, float(chosen["threshold"]))
        if chosen["threshold"] is not None
        else []
    )

    model_test_probs: List[float] = []
    model_test_labels: List[int] = []
    for row in test_rows:
        label = _bool_int(row.get("target_win"))
        prob = probs_by_row_id.get(str(row.get("row_id") or ""))
        if label is None or prob is None:
            continue
        model_test_labels.append(label)
        model_test_probs.append(prob)

    out.update({
        "baseline_paper_policy": _paper_summary(test_rows),
        "baseline_by_support_regime": _group_summary(test_rows, "support_regime"),
        "probability_metrics": {
            "poisson_fv": _metrics_for_probs(test_rows, "current_state_value_fv_raw"),
            "empirical_fv": _metrics_for_probs(test_rows, "current_state_value_base_empirical"),
            "market_ask": _metrics_for_probs(test_rows, "decision_ask"),
            "model": tbm.metric_summary(model_test_labels, model_test_probs) if model_test_labels else {
                "rows": 0,
                "positive_rate": None,
                "logloss": None,
                "brier": None,
                "auc": None,
                "accuracy_0p5": None,
            },
        },
        "model": {
            "status": "trained" if model_summary is not None else "not_trained",
            "error": model_error,
            "summary": model_summary,
        },
        "model_policy": {
            "selected_validation_threshold": chosen["threshold"],
            "validation_summary_at_threshold": chosen["summary"],
            "threshold_grid": chosen["grid"],
            "test_summary": _paper_summary(selected_test),
            "selected_test_rows": len(selected_test),
        },
    })
    out["completed"] = True
    return out


def aggregate_results(window_results: Sequence[Dict[str, Any]], rows: Sequence[Dict[str, Any]], counts: Dict[str, Any]) -> Dict[str, Any]:
    completed = [row for row in window_results if row.get("completed")]
    skipped = [row for row in window_results if not row.get("completed")]

    def collect(path: Sequence[str]) -> List[float]:
        values: List[float] = []
        for window in completed:
            cur: Any = window
            for key in path:
                if not isinstance(cur, dict):
                    cur = None
                    break
                cur = cur.get(key)
            val = _safe_float(cur)
            if val is not None:
                values.append(val)
        return values

    def stat(values: Sequence[float], op: str) -> Optional[float]:
        vals = [float(v) for v in values if math.isfinite(float(v))]
        if not vals:
            return None
        if op == "mean":
            return round(statistics.mean(vals), 6)
        if op == "median":
            return round(statistics.median(vals), 6)
        if op == "stdev":
            return round(statistics.stdev(vals), 6) if len(vals) >= 2 else 0.0
        return None

    summary = {
        "generated_at_utc": _now_iso(),
        "description": "Deduped no-score drift walk-forward harness; no live trading.",
        "counts": counts,
        "overall_training_table": {
            "rows": len(rows),
            "rows_with_outcome": sum(1 for row in rows if row.get("target_win") is not None),
            "by_support_regime": _group_summary(rows, "support_regime"),
            "baseline_paper_policy": _paper_summary(rows),
        },
        "walk_forward": {
            "windows_planned": len(window_results),
            "windows_completed": len(completed),
            "windows_skipped": len(skipped),
            "skipped_reasons": _count_by(skipped, lambda w: w.get("error") or "unknown"),
            "baseline_paper_profit_sum": round(sum(collect(["baseline_paper_policy", "profit_usdc"])), 4),
            "model_policy_profit_sum": round(sum(collect(["model_policy", "test_summary", "profit_usdc"])), 4),
            "baseline_roi_mean": stat(collect(["baseline_paper_policy", "roi"]), "mean"),
            "model_policy_roi_mean": stat(collect(["model_policy", "test_summary", "roi"]), "mean"),
            "poisson_brier_mean": stat(collect(["probability_metrics", "poisson_fv", "brier"]), "mean"),
            "empirical_brier_mean": stat(collect(["probability_metrics", "empirical_fv", "brier"]), "mean"),
            "market_brier_mean": stat(collect(["probability_metrics", "market_ask", "brier"]), "mean"),
            "model_brier_mean": stat(collect(["probability_metrics", "model", "brier"]), "mean"),
        },
        "warnings": [
            {
                "code": "shadow_policy_not_live",
                "message": "No-score drift remains shadow/paper. Walk-forward output is research evidence, not live enforcement.",
            }
        ],
    }
    return summary


def _count_by(items: Iterable[Any], key_fn) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for item in items:
        key = str(key_fn(item))
        out[key] = out.get(key, 0) + 1
    return out


def write_outputs(
    output_root: Path,
    training_rows: Sequence[Dict[str, Any]],
    window_results: Sequence[Dict[str, Any]],
    summary: Dict[str, Any],
) -> Dict[str, str]:
    output_root.mkdir(parents=True, exist_ok=True)
    rows_jsonl = output_root / "no_score_drift_training_rows.jsonl"
    rows_csv = output_root / "no_score_drift_training_rows.csv"
    per_window = output_root / "per_window_results.jsonl"
    summary_path = output_root / "summary.json"

    with rows_jsonl.open("w", encoding="utf-8") as f:
        for row in training_rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    with rows_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRAINING_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in training_rows:
            writer.writerow(row)
    with per_window.open("w", encoding="utf-8") as f:
        for row in window_results:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    return {
        "training_jsonl": str(rows_jsonl),
        "training_csv": str(rows_csv),
        "per_window": str(per_window),
        "summary": str(summary_path),
    }


def _validate_args(args: argparse.Namespace) -> None:
    for attr in ("min_date", "max_date", "start_date", "end_date"):
        raw = str(getattr(args, attr) or "")
        if raw:
            _parse_date(raw)
    min_date = str(args.min_date or "")
    max_date = str(args.max_date or "")
    if min_date and max_date and min_date > max_date:
        raise SystemExit("--min-date must be <= --max-date")
    if args.train_days <= 0 or args.val_days < 0 or args.test_days <= 0:
        raise SystemExit("--train-days and --test-days must be > 0; --val-days must be >= 0")
    if args.min_train_rows < 1:
        raise SystemExit("--min-train-rows must be >= 1")
    if args.stake <= 0 or args.daily_budget <= 0:
        raise SystemExit("--stake and --daily-budget must be > 0")


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    _validate_args(args)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    thresholds = _parse_float_csv(args.model_edge_thresholds)

    LOGGER.info("Loading no-score drift source rows")
    _raw_candidates, _policy_rows, training_rows, counts = load_no_score_training_source(args)
    available_dates = sorted({str(row.get("session_date") or "") for row in training_rows if row.get("session_date")})
    start_date = _parse_date(args.start_date) if args.start_date else None
    end_date = _parse_date(args.end_date or args.max_date) if (args.end_date or args.max_date) else None
    windows = plan_windows(
        available_dates,
        start_date=start_date,
        end_date=end_date,
        train_days=args.train_days,
        val_days=args.val_days,
        test_days=args.test_days,
        min_train_dates=args.min_train_dates,
    )

    if args.plan_only:
        for window in windows:
            print(json.dumps(window, sort_keys=True))
        return

    window_results: List[Dict[str, Any]] = []
    if not args.build_only:
        for window in windows:
            result = run_window(
                window,
                training_rows,
                thresholds=thresholds,
                min_train_rows=args.min_train_rows,
                strict=args.strict,
            )
            if args.strict and result.get("error"):
                raise SystemExit(f"Strict mode failed for {window['test_start']}: {result['error']}")
            window_results.append(result)
            LOGGER.info(
                "Window %s completed=%s error=%s",
                window["test_start"],
                result.get("completed"),
                result.get("error"),
            )

    summary = aggregate_results(window_results, training_rows, counts)
    summary["config"] = {
        "mode": args.mode,
        "min_date": args.min_date or None,
        "max_date": args.max_date or None,
        "start_date": args.start_date or None,
        "end_date": args.end_date or None,
        "train_days": args.train_days,
        "val_days": args.val_days,
        "test_days": args.test_days,
        "min_train_dates": args.min_train_dates,
        "min_train_rows": args.min_train_rows,
        "model_edge_thresholds": thresholds,
        "stake": args.stake,
        "daily_budget": args.daily_budget,
        "per_game_budget_fraction": args.per_game_budget_fraction,
        "max_orders_per_game": args.max_orders_per_game,
        "max_orders_per_game_line": args.max_orders_per_game_line,
        "support_regimes": list(_allowed_support_regimes(args.support_regimes)),
        "price_policy": args.price_policy,
        "fill_assumption": args.fill_assumption,
        "price_offset_cents": args.price_offset_cents,
        "touch_window_seconds": args.touch_window_seconds,
        "min_poisson_edge": args.min_poisson_edge,
        "min_empirical_edge": args.min_empirical_edge,
        "include_unsettled": bool(args.include_unsettled),
    }
    paths = write_outputs(args.output_root, training_rows, window_results, summary)
    LOGGER.info("Wrote %s", paths["training_jsonl"])
    LOGGER.info("Wrote %s", paths["summary"])
    print(f"Wrote {paths['summary']}")
    print(f"Wrote {paths['training_jsonl']}")


if __name__ == "__main__":
    main()
