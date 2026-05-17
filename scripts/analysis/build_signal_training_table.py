#!/usr/bin/env python3
"""
Build a leakage-aware modeling table from unified signal rows.

Inputs:
  data/analysis_output/unified_signals/signals_master.jsonl

Outputs:
  data/analysis_output/training_tables/
    signal_training_table.jsonl
    signal_training_table.csv
    signal_training_manifest.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.trading.model_families import infer_signal_model_family
from scripts.trading.scoring_path_features import SCORING_PATH_FIELD_KEYS, SCORING_PATH_MODEL_FIELD_KEYS
from scripts.trading.stage1_support import STAGE1_SUPPORT_SUFFIXES, stage1_support_field_names
from scripts.trading.weather_client import (
    WEATHER_FEATURE_FIELD_KEYS,
    WEATHER_MODEL_FEATURE_FIELD_KEYS,
)

DEFAULT_INPUT_PATH = PROJECT_DIR / "data" / "analysis_output" / "unified_signals" / "signals_master.jsonl"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "training_tables"
DEFAULT_OUTPUT_STEM = "signal_training_table"

ASK_BID_HORIZON_RE = re.compile(r"^(ask|bid)_(\d+)s$")
SIM_WINDOW_RE = re.compile(r"^sim_(fill_time|filled|cents_saved_vs_taker)_(\d+)s(?:_p([12])c)?$")

LOGGER = logging.getLogger("build_signal_training_table")

MARKET_COMPLEMENT_COLUMNS = [
    "under_token_id",
    "over_best_bid",
    "over_best_ask",
    "over_mid",
    "over_spread",
    "over_ltp",
    "over_book_source",
    "decision_mid",
    "under_pair_available",
    "under_best_bid",
    "under_best_ask",
    "under_mid",
    "under_spread",
    "under_ltp",
    "under_book_source",
    "over_under_ask_sum",
    "over_under_bid_sum",
    "over_under_mid_sum",
    "over_mid_no_vig",
    "under_mid_no_vig",
    "decision_market_mid_no_vig",
]

INFERENCE_PANEL_COLUMNS = [
    "inference_panel_runs_considered",
    "inference_panel_selected_rule",
    "inference_panel_selected_runs",
    *[
        f"inference_run{runs}_{suffix}"
        for runs in (1, 2, 3)
        for suffix in (
            "selected",
            "away_score",
            "home_score",
            "total",
            "base_poisson",
            "base_empirical",
            "poisson_minus_empirical",
            "distance_to_ask",
            "empirical_distance_to_ask",
            "n",
            "n_samples",
            "effective_n",
            *(f"support_{suffix}" for suffix in STAGE1_SUPPORT_SUFFIXES),
            "fallback_level",
            "fallback_label",
            "cell_key",
            "line_fallback_mode",
            "line_source_key",
            "empirical_line_fallback_mode",
            "empirical_line_source_key",
        )
    ],
]


IDENTITY_COLUMNS = [
    "mode",
    "bet_id",
    "session_date",
    "session_path",
    "placed_at",
    "signal_epoch_s",
    "game_pk",
    "line",
    "side",
    "away_abbrev",
    "home_abbrev",
]

PRE_SIGNAL_COLUMNS = [
    "signal_model_family",
    "source_has_session_bet",
    "source_has_ledger_events",
    "source_has_capture",
    "over_token_id",
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
    "lead_abs",
    "runs_needed",
    "inferred_runs",
    "inferred_away_after",
    "inferred_home_after",
    "entry_ask",
    "decision_ask",
    *MARKET_COMPLEMENT_COLUMNS,
    "fair_value",
    "base_fair_value",
    "inferred_state_base_poisson",
    "inferred_state_base_empirical",
    "inferred_state_poisson_minus_empirical",
    "inferred_state_empirical_edge",
    "inferred_state_n",
    "inferred_state_n_samples",
    "inferred_state_weighted_n",
    "inferred_state_effective_n",
    *stage1_support_field_names("inferred_state"),
    "inferred_state_fallback_level",
    "inferred_state_fallback_label",
    "inferred_state_cell_key",
    "inferred_state_line_key_poisson",
    "inferred_state_line_key_empirical",
    "inferred_state_line_fallback_mode",
    "inferred_state_empirical_line_fallback_mode",
    "inferred_state_empirical_line_source_key",
    "inferred_state_empirical_line_source_key_low",
    "inferred_state_empirical_line_source_key_high",
    "inferred_state_used_fallback",
    "inferred_state_base_source",
    *INFERENCE_PANEL_COLUMNS,
    "stage2_run_env_delta",
    "team_offense_delta",
    "edge_at_ask",
    "state_value_strategy",
    "current_state_value_base_poisson",
    "current_state_value_base_empirical",
    "current_state_value_line_key_poisson",
    "current_state_value_line_key_empirical",
    "current_state_value_used_fallback",
    "current_state_value_state_fallback_level",
    "current_state_value_state_fallback_label",
    "current_state_value_state_cell_key",
    "current_state_value_line_fallback_mode",
    "current_state_value_line_source_key",
    "current_state_value_empirical_line_fallback_mode",
    "current_state_value_empirical_line_source_key",
    *stage1_support_field_names("current_state_value"),
    "current_state_value_fv_raw",
    "current_state_value_stage2_run_env_delta",
    "current_state_value_team_offense_delta",
    "current_state_value_edge",
    "current_state_value_empirical_edge",
    "current_state_value_away_score",
    "current_state_value_home_score",
    "current_state_value_total",
    "shadow_fv_current_state",
    "shadow_fv_after_inferred_score",
    "shadow_fv_inferred_lift",
    "shadow_no_event_edge",
    "shadow_after_event_edge",
    "shadow_p_score_event_proxy",
    "shadow_phantom_risk_score",
    "shadow_phantom_risk_band",
    "shadow_transition_model",
    "shadow_transition_inferred_runs",
    "shadow_extreme_edge",
    "shadow_extreme_edge_threshold",
    "shadow_ltp_at_signal",
    "shadow_ltp_ask_gap",
    "shadow_ltp_ask_gap_threshold",
    "shadow_ltp_ask_gap_exceeded",
    "shadow_risk_tags",
    "shadow_low_ask_high_edge",
    "shadow_runs_needed_exact_3p5",
    "shadow_current_state_edge_bucket",
    "shadow_phantom_risk_bucket",
    "shadow_current_phantom_combo_bucket",
    "shadow_inning_bucket",
    "shadow_inning_runs_needed_bucket",
    "shadow_bottom9_home_lead_context",
    "shadow_home_skip_bottom9_risk_bucket",
    "shadow_post_tr20_extreme_020_pass",
    "shadow_post_tr20_ask_ramp_v2_pass",
    "shadow_post_tr20_gate6_relax_enforce_pass",
    "shadow_post_tr20_combined_pass",
    "limit_price",
    "posted_limit",
    "edge_at_limit",
    "stake",
    "stake_mode",
    "kelly_full_fraction",
    "kelly_fraction_used",
    "t0_best_bid",
    "t0_best_ask",
    "t0_spread",
    "t0_mid",
    "t0_ltp",
    "t0_latency_ms",
    "t0_total_bid_depth",
    "t0_total_ask_depth",
]

_WEATHER_FEATURE_FIELD_SET = set(WEATHER_FEATURE_FIELD_KEYS)
_SCORING_PATH_NON_MODEL_FIELD_SET = set(SCORING_PATH_FIELD_KEYS) - set(SCORING_PATH_MODEL_FIELD_KEYS)
PRE_SIGNAL_MODEL_COLUMNS = [
    c for c in PRE_SIGNAL_COLUMNS
    if c not in _WEATHER_FEATURE_FIELD_SET and c not in _SCORING_PATH_NON_MODEL_FIELD_SET
] + list(WEATHER_MODEL_FEATURE_FIELD_KEYS)

POST_SIGNAL_STATIC_COLUMNS = [
    "ask_move_2s",
    "ask_move_5s",
    "ask_velocity_5s_cents",
    "min_ask_30s",
    "max_ask_30s",
    "min_spread_30s",
    "max_spread_30s",
]

LABEL_COLUMNS = [
    "target_filled",
    "target_profit",
    "target_win",
]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build leakage-aware modeling table from unified signals.")
    p.add_argument(
        "--input-path",
        type=Path,
        default=DEFAULT_INPUT_PATH,
        help=f"Path to signals_master JSONL (default: {DEFAULT_INPUT_PATH}).",
    )
    p.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Output directory (default: {DEFAULT_OUTPUT_ROOT}).",
    )
    p.add_argument(
        "--output-stem",
        type=str,
        default=DEFAULT_OUTPUT_STEM,
        help=f"Output stem for table files (default: {DEFAULT_OUTPUT_STEM}).",
    )
    p.add_argument(
        "--mode",
        choices=["live", "paper", "both"],
        default="both",
        help="Filter rows by mode (default: both).",
    )
    p.add_argument("--min-date", type=str, default="", help="Inclusive lower date bound (YYYY-MM-DD).")
    p.add_argument("--max-date", type=str, default="", help="Inclusive upper date bound (YYYY-MM-DD).")
    p.add_argument(
        "--val-frac",
        type=float,
        default=0.15,
        help="Fraction of dates assigned to validation split (default: 0.15).",
    )
    p.add_argument(
        "--test-frac",
        type=float,
        default=0.15,
        help="Fraction of dates assigned to test split (default: 0.15).",
    )
    p.add_argument(
        "--drop-unsettled",
        action="store_true",
        help="Drop rows where settlement is not final (targets unavailable).",
    )
    p.add_argument("--strict", action="store_true", help="Fail when integrity checks fail.")
    p.add_argument("--verbose", action="store_true", help="Verbose logging.")
    return p.parse_args(argv)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            rows.append(json.loads(raw))
    return rows


def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _write_csv(path: Path, rows: Sequence[Dict[str, Any]], columns: List[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _dedupe_preserve_order(values: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for v in values:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _safe_bool_to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    return 1 if bool(v) else 0


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def _row_date(row: Dict[str, Any]) -> str:
    d = str(row.get("session_date") or "")
    if d:
        return d
    placed_at = str(row.get("placed_at") or "")
    if len(placed_at) >= 10:
        return placed_at[:10]
    bet_id = str(row.get("bet_id") or "")
    if len(bet_id) >= 10 and bet_id[4] == "-" and bet_id[7] == "-":
        return bet_id[:10]
    return ""


def _date_in_range(date_str: str, min_date: Optional[str], max_date: Optional[str]) -> bool:
    if not date_str:
        return True
    if min_date and date_str < min_date:
        return False
    if max_date and date_str > max_date:
        return False
    return True


def _filter_rows(
    rows: List[Dict[str, Any]],
    mode: str,
    min_date: Optional[str],
    max_date: Optional[str],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        row_mode = str(row.get("mode") or "")
        if mode != "both" and row_mode != mode:
            continue
        session_date = _row_date(row)
        if not _date_in_range(session_date, min_date, max_date):
            continue
        new_row = dict(row)
        new_row["session_date"] = session_date
        new_row["signal_model_family"] = infer_signal_model_family(new_row)
        out.append(new_row)
    return out


def _infer_param_columns(rows: List[Dict[str, Any]]) -> List[str]:
    cols = set()
    for row in rows:
        for k in row.keys():
            if k.startswith("param_"):
                cols.add(k)
    return sorted(cols)


def _ask_bid_sort_key(col: str) -> Tuple[int, int]:
    m = ASK_BID_HORIZON_RE.match(col)
    if not m:
        return (10**9, 10**9)
    side_rank = 0 if m.group(1) == "ask" else 1
    return int(m.group(2)), side_rank


def _sim_sort_key(col: str) -> Tuple[int, int, int]:
    m = SIM_WINDOW_RE.match(col)
    if not m:
        return (10**9, 10**9, 10**9)
    kind = m.group(1)
    price_rank = int(m.group(3)) if m.group(3) is not None else 0
    order = {"fill_time": 0, "filled": 1, "cents_saved_vs_taker": 2}
    return int(m.group(2)), price_rank, order.get(kind, 99)


def _infer_post_signal_columns(rows: List[Dict[str, Any]]) -> List[str]:
    ask_bid_cols = set()
    sim_cols = set()
    for row in rows:
        for k in row.keys():
            if ASK_BID_HORIZON_RE.match(k):
                ask_bid_cols.add(k)
            if SIM_WINDOW_RE.match(k):
                sim_cols.add(k)

    static_present = [c for c in POST_SIGNAL_STATIC_COLUMNS if any(c in r for r in rows)]
    return (
        sorted(ask_bid_cols, key=_ask_bid_sort_key)
        + static_present
        + sorted(sim_cols, key=_sim_sort_key)
    )


def _allocate_split_counts(num_dates: int, val_frac: float, test_frac: float) -> Tuple[int, int, int]:
    if num_dates <= 0:
        return 0, 0, 0
    if num_dates == 1:
        return 1, 0, 0
    if num_dates == 2:
        return 1, 0, 1

    val_n = max(1, int(round(num_dates * val_frac)))
    test_n = max(1, int(round(num_dates * test_frac)))
    train_n = num_dates - val_n - test_n

    while train_n < 1 and (val_n > 1 or test_n > 1):
        if val_n >= test_n and val_n > 1:
            val_n -= 1
        elif test_n > 1:
            test_n -= 1
        train_n = num_dates - val_n - test_n

    if train_n < 1:
        # Minimum viable fallback keeps temporal order and ensures train exists.
        train_n = 1
        remaining = num_dates - train_n
        val_n = 1 if remaining >= 2 else 0
        test_n = remaining - val_n

    return train_n, val_n, test_n


def _build_date_split_map(
    dates: List[str],
    val_frac: float,
    test_frac: float,
) -> Tuple[Dict[str, str], Dict[str, int], Dict[str, List[str]]]:
    unique_dates = sorted(set(d for d in dates if d))
    train_n, val_n, test_n = _allocate_split_counts(len(unique_dates), val_frac=val_frac, test_frac=test_frac)

    train_dates = unique_dates[:train_n]
    val_dates = unique_dates[train_n : train_n + val_n]
    test_dates = unique_dates[train_n + val_n : train_n + val_n + test_n]

    split_map: Dict[str, str] = {}
    for d in train_dates:
        split_map[d] = "train"
    for d in val_dates:
        split_map[d] = "validation"
    for d in test_dates:
        split_map[d] = "test"

    date_rank = {d: i for i, d in enumerate(unique_dates)}
    split_dates = {
        "train": train_dates,
        "validation": val_dates,
        "test": test_dates,
    }
    return split_map, date_rank, split_dates


def _rows_by_mode(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row.get("mode") or "unknown")] += 1
    return dict(counts)


def build_training_rows(
    rows: List[Dict[str, Any]],
    pre_signal_columns: List[str],
    post_signal_columns: List[str],
    param_columns: List[str],
    split_map: Dict[str, str],
    date_rank: Dict[str, int],
    drop_unsettled: bool,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        session_date = str(row.get("session_date") or "")
        split = split_map.get(session_date, "train")
        settled = bool(row.get("settled")) if row.get("settled") is not None else False
        if drop_unsettled and not settled:
            continue

        training_row: Dict[str, Any] = {}
        training_row.update({c: row.get(c) for c in IDENTITY_COLUMNS if c in row or c == "session_date"})
        training_row["split"] = split
        training_row["split_date_rank"] = date_rank.get(session_date)
        training_row["label_available"] = bool(settled)

        for c in pre_signal_columns:
            training_row[c] = row.get(c)
        for c in param_columns:
            training_row[c] = row.get(c)
        for c in post_signal_columns:
            training_row[c] = row.get(c)

        if settled:
            training_row["target_filled"] = _safe_bool_to_int(row.get("realized_executed"))
            training_row["target_profit"] = _safe_float(row.get("realized_profit"))
            training_row["target_win"] = _safe_bool_to_int(row.get("realized_win"))
        else:
            training_row["target_filled"] = None
            training_row["target_profit"] = None
            training_row["target_win"] = None

        out.append(training_row)

    out.sort(
        key=lambda r: (
            str(r.get("session_date") or ""),
            int(r.get("split_date_rank") or 0),
            str(r.get("mode") or ""),
            str(r.get("bet_id") or ""),
        )
    )
    return out


def _label_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "rows_total": len(rows),
        "rows_with_labels": 0,
        "target_filled_positive": 0,
        "target_win_positive": 0,
        "target_profit_sum": 0.0,
        "target_profit_nonnull": 0,
    }
    for row in rows:
        if row.get("label_available"):
            stats["rows_with_labels"] += 1
        if row.get("target_filled") == 1:
            stats["target_filled_positive"] += 1
        if row.get("target_win") == 1:
            stats["target_win_positive"] += 1
        profit = _safe_float(row.get("target_profit"))
        if profit is not None:
            stats["target_profit_sum"] += profit
            stats["target_profit_nonnull"] += 1
    return stats


def write_manifest(
    path: Path,
    args: argparse.Namespace,
    source_rows: List[Dict[str, Any]],
    filtered_rows: List[Dict[str, Any]],
    training_rows: List[Dict[str, Any]],
    split_dates: Dict[str, List[str]],
    pre_signal_columns: List[str],
    pre_signal_model_columns: List[str],
    post_signal_columns: List[str],
    param_columns: List[str],
    output_columns: List[str],
) -> None:
    rows_by_split: Dict[str, int] = defaultdict(int)
    labels_by_split: Dict[str, Dict[str, Any]] = defaultdict(dict)
    for split_name in ["train", "validation", "test"]:
        split_rows = [r for r in training_rows if r.get("split") == split_name]
        rows_by_split[split_name] = len(split_rows)
        labels_by_split[split_name] = _label_stats(split_rows)

    manifest = {
        "generated_at_utc": _now_iso(),
        "config": {
            "input_path": str(args.input_path),
            "output_root": str(args.output_root),
            "output_stem": args.output_stem,
            "mode": args.mode,
            "min_date": args.min_date or None,
            "max_date": args.max_date or None,
            "val_frac": args.val_frac,
            "test_frac": args.test_frac,
            "drop_unsettled": bool(args.drop_unsettled),
            "strict": bool(args.strict),
        },
        "counts": {
            "source_rows_total": len(source_rows),
            "filtered_rows_total": len(filtered_rows),
            "training_rows_total": len(training_rows),
            "filtered_rows_by_mode": _rows_by_mode(filtered_rows),
            "training_rows_by_mode": _rows_by_mode(training_rows),
            "rows_by_split": dict(rows_by_split),
            "dates_by_split": split_dates,
        },
        "column_groups": {
            "identity_columns": IDENTITY_COLUMNS + ["split", "split_date_rank", "label_available"],
            "pre_signal_features": pre_signal_model_columns + param_columns,
            "pre_signal_audit_columns": pre_signal_columns,
            "post_signal_execution_features": post_signal_columns,
            "label_columns": LABEL_COLUMNS,
            "all_output_columns": output_columns,
        },
        "label_stats": labels_by_split,
        "leakage_policy": {
            "train_validation_test_splits": "Contiguous non-overlapping date splits by session_date.",
            "feature_groups": "Use pre_signal_features for signal-time models; include post_signal_execution_features only for execution-policy analyses. pre_signal_audit_columns are preserved in the table but may include provenance/high-cardinality fields.",
        },
        "status": "ok",
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    if args.min_date:
        datetime.strptime(args.min_date, "%Y-%m-%d")
    if args.max_date:
        datetime.strptime(args.max_date, "%Y-%m-%d")
    if args.min_date and args.max_date and args.min_date > args.max_date:
        raise SystemExit("--min-date must be <= --max-date")
    if args.val_frac < 0 or args.test_frac < 0:
        raise SystemExit("--val-frac and --test-frac must be >= 0")
    if args.val_frac + args.test_frac >= 1.0:
        raise SystemExit("--val-frac + --test-frac must be < 1.0")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    if not args.input_path.exists():
        raise SystemExit(f"Input file not found: {args.input_path}")
    args.output_root.mkdir(parents=True, exist_ok=True)

    source_rows = _read_jsonl(args.input_path)
    filtered_rows = _filter_rows(
        source_rows,
        mode=args.mode,
        min_date=args.min_date or None,
        max_date=args.max_date or None,
    )

    pre_signal_columns = [c for c in PRE_SIGNAL_COLUMNS if any(c in r for r in filtered_rows)]
    pre_signal_model_columns = [
        c for c in PRE_SIGNAL_MODEL_COLUMNS if any(c in r for r in filtered_rows)
    ]
    param_columns = _infer_param_columns(filtered_rows)
    post_signal_columns = _infer_post_signal_columns(filtered_rows)

    split_map, date_rank, split_dates = _build_date_split_map(
        dates=[str(r.get("session_date") or "") for r in filtered_rows],
        val_frac=args.val_frac,
        test_frac=args.test_frac,
    )

    training_rows = build_training_rows(
        rows=filtered_rows,
        pre_signal_columns=pre_signal_columns,
        post_signal_columns=post_signal_columns,
        param_columns=param_columns,
        split_map=split_map,
        date_rank=date_rank,
        drop_unsettled=args.drop_unsettled,
    )

    output_columns = _dedupe_preserve_order(
        IDENTITY_COLUMNS
        + ["split", "split_date_rank", "label_available"]
        + pre_signal_columns
        + param_columns
        + post_signal_columns
        + LABEL_COLUMNS
    )

    if args.strict:
        if not training_rows:
            raise SystemExit("Strict mode failed: no training rows after filtering.")
        if not any(r.get("split") == "train" for r in training_rows):
            raise SystemExit("Strict mode failed: no train rows assigned.")
        if not any(r.get("label_available") for r in training_rows):
            raise SystemExit("Strict mode failed: no rows with labels available.")

    output_jsonl = args.output_root / f"{args.output_stem}.jsonl"
    output_csv = args.output_root / f"{args.output_stem}.csv"
    manifest_path = args.output_root / f"{args.output_stem}_manifest.json"

    _write_jsonl(output_jsonl, training_rows)
    _write_csv(output_csv, training_rows, output_columns)
    write_manifest(
        path=manifest_path,
        args=args,
        source_rows=source_rows,
        filtered_rows=filtered_rows,
        training_rows=training_rows,
        split_dates=split_dates,
        pre_signal_columns=pre_signal_columns,
        pre_signal_model_columns=pre_signal_model_columns,
        post_signal_columns=post_signal_columns,
        param_columns=param_columns,
        output_columns=output_columns,
    )

    LOGGER.info("Wrote %s", output_jsonl)
    LOGGER.info("Wrote %s", output_csv)
    LOGGER.info("Wrote %s", manifest_path)
    LOGGER.info(
        "Rows: source=%d filtered=%d training=%d",
        len(source_rows),
        len(filtered_rows),
        len(training_rows),
    )


if __name__ == "__main__":
    main()
