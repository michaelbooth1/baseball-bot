#!/usr/bin/env python3
"""
Build closing/late-price value diagnostics for candidates and orders.

CLV is the fastest sanity check that a quoted edge is real before final outcome
sample size matures. This report is deliberately conservative about the word
"close": Polymarket in-game captures are usually short post-signal windows, not
true market close. The canonical CLV mark is therefore named `late_mid` and is
the last captured midpoint available for that signal/order, with fixed-horizon
marks included when present.

Inputs:
  data/analysis_output/unified_signals/signals_master.jsonl
  data/analysis_output/unified_signals/signal_book_snapshots.jsonl
  data/analysis_output/analysis_safe_trades/analysis_safe_trades.jsonl
  data/analysis_output/calibration_opportunity_training/
      calibration_opportunity_training_table.jsonl

Outputs:
  data/analysis_output/clv/
    clv_rows.jsonl
    clv_rows.csv
    clv_summary.json
    clv_summary.md
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_UNIFIED_ROOT = PROJECT_DIR / "data" / "analysis_output" / "unified_signals"
DEFAULT_SIGNALS_MASTER = DEFAULT_UNIFIED_ROOT / "signals_master.jsonl"
DEFAULT_SNAPSHOTS = DEFAULT_UNIFIED_ROOT / "signal_book_snapshots.jsonl"
DEFAULT_ANALYSIS_SAFE_TRADES = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "analysis_safe_trades"
    / "analysis_safe_trades.jsonl"
)
DEFAULT_CALIBRATION_TABLE = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "calibration_opportunity_training"
    / "calibration_opportunity_training_table.jsonl"
)
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "clv"
DEFAULT_HORIZONS = "30,60,120"

OUTPUT_COLUMNS = [
    "schema_version",
    "row_type",
    "row_id",
    "mode",
    "session_date",
    "bet_id",
    "candidate_id",
    "game_pk",
    "away_abbrev",
    "home_abbrev",
    "line",
    "side",
    "signal_model_family",
    "state_value_strategy",
    "decision",
    "decision_reason",
    "gate_or_reason",
    "order_status_final",
    "is_filled",
    "is_live_money",
    "is_paper_fallback",
    "inning",
    "inning_state",
    "outs",
    "runners_on",
    "current_total",
    "runs_needed",
    "entry_price",
    "entry_price_source",
    "decision_ask",
    "entry_ask",
    "limit_price",
    "actual_fill_price",
    "execution_price",
    "execution_price_source",
    "entry_mid",
    "entry_spread",
    "t0_mid",
    "t0_best_bid",
    "t0_best_ask",
    "late_mid",
    "late_bid",
    "late_ask",
    "late_elapsed_s",
    "late_ts",
    "late_mark_source",
    "clv_mid_vs_entry",
    "clv_mid_vs_execution",
    "clv_mid_vs_entry_cents",
    "clv_mid_vs_execution_cents",
    "clv_positive_vs_entry",
    "clv_positive_vs_execution",
    "snapshot_count",
    "capture_window_seconds",
    "has_late_price",
    "mid_30s",
    "mid_60s",
    "mid_120s",
    "clv_mid_30s_vs_entry",
    "clv_mid_60s_vs_entry",
    "clv_mid_120s_vs_entry",
    "fair_value",
    "fair_value_calibrated",
    "base_fair_value",
    "edge",
    "current_state_value_edge",
    "shadow_phantom_risk_band",
    "shadow_phantom_risk_score",
    "shadow_current_phantom_combo_bucket",
    "ask_bucket",
    "edge_bucket",
    "inning_bucket",
    "runs_needed_bucket",
    "phantom_risk_bucket",
    "realized_profit_usdc",
    "fill_cost_usdc",
    "realized_roi",
    "won",
    "final_total",
]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build CLV / late-price diagnostics.")
    parser.add_argument("--signals-master", type=Path, default=DEFAULT_SIGNALS_MASTER)
    parser.add_argument("--snapshots", type=Path, default=DEFAULT_SNAPSHOTS)
    parser.add_argument("--analysis-safe-trades", type=Path, default=DEFAULT_ANALYSIS_SAFE_TRADES)
    parser.add_argument("--calibration-table", type=Path, default=DEFAULT_CALIBRATION_TABLE)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--mode", choices=["live", "paper", "both"], default="live")
    parser.add_argument("--min-date", type=str, default="")
    parser.add_argument("--max-date", type=str, default="")
    parser.add_argument("--horizons", type=str, default=DEFAULT_HORIZONS)
    parser.add_argument(
        "--no-candidate-coverage-rows",
        action="store_true",
        help="Only emit signal/order rows that can potentially join snapshots.",
    )
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args(argv)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _safe_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "win", "won"}:
        return True
    if text in {"0", "false", "no", "n", "loss", "lost"}:
        return False
    return None


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _date_in_range(date_str: str, min_date: str, max_date: str) -> bool:
    if min_date and date_str and date_str < min_date:
        return False
    if max_date and date_str and date_str > max_date:
        return False
    return True


def _read_jsonl(path: Path, warnings: List[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        warnings.append(f"missing input path: {path}")
        return rows
    with path.open(encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            text = raw.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                warnings.append(f"bad JSON {path}:{line_no}: {exc}")
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _parse_horizons(raw: str) -> List[int]:
    out: List[int] = []
    for token in str(raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        value = int(float(token))
        if value <= 0:
            raise SystemExit("--horizons values must be positive seconds.")
        if value not in out:
            out.append(value)
    return sorted(out)


def _snapshot_mid(row: Mapping[str, Any]) -> Optional[float]:
    mid = _safe_float(row.get("mid"))
    if mid is not None:
        return mid
    bid = _safe_float(row.get("best_bid"))
    ask = _safe_float(row.get("best_ask"))
    if bid is not None and ask is not None and 0.0 <= bid <= 1.0 and 0.0 <= ask <= 1.0:
        return (bid + ask) / 2.0
    return None


def _build_snapshot_map(snapshot_rows: Sequence[Mapping[str, Any]]) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in snapshot_rows:
        mode = str(row.get("mode") or "")
        bet_id = str(row.get("bet_id") or "")
        if not mode or not bet_id:
            continue
        grouped[(mode, bet_id)].append(dict(row))
    for rows in grouped.values():
        rows.sort(key=lambda r: (_safe_float(r.get("elapsed_s")) or 0.0, int(r.get("seq") or 0), str(r.get("ts") or "")))
    return dict(grouped)


def _nearest_snapshot_at_or_before(snapshots: Sequence[Mapping[str, Any]], horizon: float) -> Optional[Mapping[str, Any]]:
    candidates: List[Mapping[str, Any]] = []
    for row in snapshots:
        elapsed = _safe_float(row.get("elapsed_s"))
        if elapsed is not None and elapsed <= horizon and _snapshot_mid(row) is not None:
            candidates.append(row)
    if candidates:
        return candidates[-1]
    best: Optional[Mapping[str, Any]] = None
    best_diff: Optional[float] = None
    for row in snapshots:
        elapsed = _safe_float(row.get("elapsed_s"))
        if elapsed is None or _snapshot_mid(row) is None:
            continue
        diff = abs(elapsed - horizon)
        if best_diff is None or diff < best_diff:
            best = row
            best_diff = diff
    return best


def _last_valid_snapshot(snapshots: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    for row in reversed(list(snapshots)):
        if _snapshot_mid(row) is not None:
            return row
    return None


def _bucket_price(value: Optional[float]) -> str:
    if value is None:
        return "missing"
    if value < 0.40:
        return "<0.40"
    if value < 0.55:
        return "0.40-0.55"
    if value < 0.70:
        return "0.55-0.70"
    if value < 0.85:
        return "0.70-0.85"
    return ">=0.85"


def _bucket_edge(value: Optional[float]) -> str:
    if value is None:
        return "missing"
    if value < 0:
        return "<0.00"
    if value < 0.05:
        return "0.00-0.05"
    if value < 0.10:
        return "0.05-0.10"
    if value < 0.15:
        return "0.10-0.15"
    if value < 0.20:
        return "0.15-0.20"
    return ">=0.20"


def _bucket_inning(value: Any) -> str:
    inning = _safe_float(value)
    if inning is None:
        return "missing"
    if inning <= 4:
        return "<=4"
    if inning <= 6:
        return "5-6"
    if inning <= 8:
        return "7-8"
    return "9+"


def _bucket_runs_needed(value: Optional[float]) -> str:
    if value is None:
        return "missing"
    if value <= 1.5:
        return "<=1.5"
    if value <= 2.5:
        return "1.5-2.5"
    if value <= 3.5:
        return "2.5-3.5"
    return ">3.5"


def _gate_or_reason(row: Mapping[str, Any]) -> str:
    decision_reason = str(row.get("decision_reason") or "").strip()
    if decision_reason:
        return decision_reason
    decision = str(row.get("decision") or "").strip()
    if decision:
        return decision
    if row.get("source_has_ledger_events") or row.get("source_has_session_bet"):
        return "placed_order"
    status = str(row.get("order_status_final") or "").strip()
    return status or "unknown"


def _analysis_safe_trade_map(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    out: Dict[str, Mapping[str, Any]] = {}
    for row in rows:
        bet_id = str(row.get("bet_id") or "")
        if bet_id:
            out[bet_id] = row
    return out


def _execution_price(signal_row: Mapping[str, Any], trade_row: Optional[Mapping[str, Any]]) -> Tuple[Optional[float], str]:
    if trade_row:
        fill_price = _safe_float(trade_row.get("actual_fill_price") or trade_row.get("fill_price"))
        if fill_price is not None:
            return fill_price, "analysis_safe_fill_price"
    for key in ("actual_fill_price", "fill_price", "limit_price", "posted_limit", "decision_ask", "entry_ask"):
        value = _safe_float(signal_row.get(key))
        if value is not None:
            return value, key
    return None, "missing"


def _realized_fields(signal_row: Mapping[str, Any], trade_row: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if trade_row and _safe_bool(trade_row.get("is_filled")):
        profit = _safe_float(trade_row.get("realized_profit_usdc"))
        cost = _safe_float(trade_row.get("fill_cost_usdc"))
        roi = profit / cost if profit is not None and cost and cost > 0 else _safe_float(trade_row.get("roi_on_cost"))
        return {
            "realized_profit_usdc": _round(profit),
            "fill_cost_usdc": _round(cost),
            "realized_roi": _round(roi),
            "won": _safe_bool(trade_row.get("won")),
            "is_filled": True,
            "is_live_money": _safe_bool(trade_row.get("is_live_money")),
            "is_paper_fallback": _safe_bool(trade_row.get("is_paper_fallback")),
        }
    realized_executed = _safe_bool(signal_row.get("realized_executed"))
    profit = _safe_float(signal_row.get("realized_profit")) if realized_executed else None
    stake = _safe_float(signal_row.get("stake"))
    roi = profit / stake if profit is not None and stake and stake > 0 else None
    return {
        "realized_profit_usdc": _round(profit),
        "fill_cost_usdc": _round(stake if realized_executed else None),
        "realized_roi": _round(roi),
        "won": _safe_bool(_coalesce(signal_row.get("realized_win"), signal_row.get("won_counterfactual"))),
        "is_filled": bool(realized_executed),
        "is_live_money": None,
        "is_paper_fallback": None,
    }


def _horizon_marks(snapshots: Sequence[Mapping[str, Any]], horizons: Sequence[int]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for horizon in horizons:
        snap = _nearest_snapshot_at_or_before(snapshots, float(horizon))
        mid = _snapshot_mid(snap) if snap else None
        out[f"mid_{horizon}s"] = _round(mid)
    return out


def _build_signal_clv_row(
    signal_row: Mapping[str, Any],
    snapshots: Sequence[Mapping[str, Any]],
    trade_row: Optional[Mapping[str, Any]],
    horizons: Sequence[int],
) -> Dict[str, Any]:
    decision_ask = _safe_float(signal_row.get("decision_ask"))
    entry_ask = _safe_float(signal_row.get("entry_ask"))
    t0_mid = _safe_float(signal_row.get("t0_mid"))
    entry_mid = _safe_float(_coalesce(signal_row.get("decision_mid"), signal_row.get("over_mid"), t0_mid))
    entry_price = _safe_float(_coalesce(decision_ask, entry_ask, signal_row.get("t0_best_ask")))
    entry_price_source = (
        "decision_ask"
        if decision_ask is not None
        else "entry_ask"
        if entry_ask is not None
        else "t0_best_ask"
        if _safe_float(signal_row.get("t0_best_ask")) is not None
        else "missing"
    )
    execution_price, execution_price_source = _execution_price(signal_row, trade_row)
    late = _last_valid_snapshot(snapshots)
    late_mid = _snapshot_mid(late) if late else None
    late_elapsed = _safe_float(late.get("elapsed_s")) if late else None
    clv_entry = late_mid - entry_price if late_mid is not None and entry_price is not None else None
    clv_execution = late_mid - execution_price if late_mid is not None and execution_price is not None else None
    fair_value = _safe_float(signal_row.get("fair_value"))
    edge = _safe_float(_coalesce(signal_row.get("edge"), signal_row.get("edge_at_ask")))
    runs_needed = _safe_float(signal_row.get("runs_needed"))
    realized = _realized_fields(signal_row, trade_row)
    marks = _horizon_marks(snapshots, horizons)
    row: Dict[str, Any] = {
        "schema_version": 1,
        "row_type": "order_or_captured_signal",
        "row_id": f"signal:{signal_row.get('mode')}:{signal_row.get('bet_id')}",
        "mode": signal_row.get("mode"),
        "session_date": signal_row.get("session_date"),
        "bet_id": signal_row.get("bet_id"),
        "candidate_id": signal_row.get("candidate_id"),
        "game_pk": signal_row.get("game_pk"),
        "away_abbrev": signal_row.get("away_abbrev"),
        "home_abbrev": signal_row.get("home_abbrev"),
        "line": signal_row.get("line"),
        "side": signal_row.get("side") or "over",
        "signal_model_family": signal_row.get("signal_model_family") or signal_row.get("state_value_strategy") or "unknown",
        "state_value_strategy": signal_row.get("state_value_strategy"),
        "decision": signal_row.get("decision"),
        "decision_reason": signal_row.get("decision_reason"),
        "gate_or_reason": _gate_or_reason(signal_row),
        "order_status_final": signal_row.get("order_status_final"),
        "inning": signal_row.get("inning"),
        "inning_state": signal_row.get("inning_state"),
        "outs": signal_row.get("outs"),
        "runners_on": signal_row.get("runners_on"),
        "current_total": signal_row.get("current_total"),
        "runs_needed": _round(runs_needed),
        "entry_price": _round(entry_price),
        "entry_price_source": entry_price_source,
        "decision_ask": _round(decision_ask),
        "entry_ask": _round(entry_ask),
        "limit_price": _round(_safe_float(signal_row.get("limit_price"))),
        "actual_fill_price": _round(_safe_float(signal_row.get("actual_fill_price"))),
        "execution_price": _round(execution_price),
        "execution_price_source": execution_price_source,
        "entry_mid": _round(entry_mid),
        "entry_spread": _round(_safe_float(_coalesce(signal_row.get("spread"), signal_row.get("t0_spread")))),
        "t0_mid": _round(t0_mid),
        "t0_best_bid": _round(_safe_float(signal_row.get("t0_best_bid"))),
        "t0_best_ask": _round(_safe_float(signal_row.get("t0_best_ask"))),
        "late_mid": _round(late_mid),
        "late_bid": _round(_safe_float(late.get("best_bid")) if late else None),
        "late_ask": _round(_safe_float(late.get("best_ask")) if late else None),
        "late_elapsed_s": _round(late_elapsed, 3),
        "late_ts": late.get("ts") if late else None,
        "late_mark_source": "last_captured_mid" if late_mid is not None else "missing",
        "clv_mid_vs_entry": _round(clv_entry),
        "clv_mid_vs_execution": _round(clv_execution),
        "clv_mid_vs_entry_cents": _round(clv_entry * 100.0 if clv_entry is not None else None, 3),
        "clv_mid_vs_execution_cents": _round(clv_execution * 100.0 if clv_execution is not None else None, 3),
        "clv_positive_vs_entry": clv_entry > 0 if clv_entry is not None else None,
        "clv_positive_vs_execution": clv_execution > 0 if clv_execution is not None else None,
        "snapshot_count": len(snapshots),
        "capture_window_seconds": _round(max((_safe_float(s.get("elapsed_s")) or 0.0 for s in snapshots), default=0.0), 3),
        "has_late_price": late_mid is not None,
        "fair_value": _round(fair_value),
        "fair_value_calibrated": _round(_safe_float(signal_row.get("fair_value_calibrated"))),
        "base_fair_value": _round(_safe_float(signal_row.get("base_fair_value"))),
        "edge": _round(edge),
        "current_state_value_edge": _round(_safe_float(signal_row.get("current_state_value_edge"))),
        "shadow_phantom_risk_band": signal_row.get("shadow_phantom_risk_band"),
        "shadow_phantom_risk_score": _round(_safe_float(signal_row.get("shadow_phantom_risk_score"))),
        "shadow_current_phantom_combo_bucket": signal_row.get("shadow_current_phantom_combo_bucket"),
        "ask_bucket": _bucket_price(entry_price),
        "edge_bucket": _bucket_edge(edge),
        "inning_bucket": _bucket_inning(signal_row.get("inning")),
        "runs_needed_bucket": _bucket_runs_needed(runs_needed),
        "phantom_risk_bucket": signal_row.get("shadow_phantom_risk_bucket") or signal_row.get("shadow_phantom_risk_band") or "missing",
        "final_total": signal_row.get("final_total"),
    }
    row.update(marks)
    for horizon in horizons:
        mid = _safe_float(row.get(f"mid_{horizon}s"))
        row[f"clv_mid_{horizon}s_vs_entry"] = _round(mid - entry_price if mid is not None and entry_price is not None else None)
    row.update(realized)
    return row


def _build_candidate_coverage_row(candidate_row: Mapping[str, Any]) -> Dict[str, Any]:
    decision_ask = _safe_float(candidate_row.get("decision_ask"))
    edge = _safe_float(_coalesce(candidate_row.get("edge"), candidate_row.get("raw_model_edge_to_ask")))
    runs_needed = _safe_float(candidate_row.get("runs_needed"))
    return {
        "schema_version": 1,
        "row_type": "candidate_no_late_price",
        "row_id": f"candidate:{candidate_row.get('mode')}:{candidate_row.get('candidate_id')}",
        "mode": candidate_row.get("mode"),
        "session_date": candidate_row.get("session_date"),
        "bet_id": candidate_row.get("bet_id"),
        "candidate_id": candidate_row.get("candidate_id"),
        "game_pk": candidate_row.get("game_pk"),
        "away_abbrev": candidate_row.get("away_abbrev"),
        "home_abbrev": candidate_row.get("home_abbrev"),
        "line": candidate_row.get("line"),
        "side": candidate_row.get("side") or "over",
        "signal_model_family": candidate_row.get("signal_model_family") or candidate_row.get("state_value_strategy") or "unknown",
        "state_value_strategy": candidate_row.get("state_value_strategy"),
        "decision": candidate_row.get("decision"),
        "decision_reason": candidate_row.get("decision_reason"),
        "gate_or_reason": _gate_or_reason(candidate_row),
        "inning": candidate_row.get("inning"),
        "inning_state": candidate_row.get("inning_state"),
        "outs": candidate_row.get("outs"),
        "runners_on": candidate_row.get("runners_on"),
        "current_total": candidate_row.get("current_total"),
        "runs_needed": _round(runs_needed),
        "entry_price": _round(decision_ask),
        "entry_price_source": "decision_ask" if decision_ask is not None else "missing",
        "decision_ask": _round(decision_ask),
        "has_late_price": False,
        "late_mark_source": "not_captured",
        "snapshot_count": 0,
        "fair_value": _round(_safe_float(candidate_row.get("fair_value"))),
        "fair_value_calibrated": _round(_safe_float(candidate_row.get("fair_value_calibrated"))),
        "base_fair_value": _round(_safe_float(candidate_row.get("base_fair_value"))),
        "edge": _round(edge),
        "current_state_value_edge": _round(_safe_float(candidate_row.get("current_state_value_edge"))),
        "shadow_phantom_risk_band": candidate_row.get("shadow_phantom_risk_band") or candidate_row.get("phantom_risk_band"),
        "shadow_phantom_risk_score": _round(_safe_float(candidate_row.get("shadow_phantom_risk_score"))),
        "shadow_current_phantom_combo_bucket": candidate_row.get("shadow_current_phantom_combo_bucket"),
        "ask_bucket": candidate_row.get("ask_bucket") or _bucket_price(decision_ask),
        "edge_bucket": candidate_row.get("edge_bucket") or _bucket_edge(edge),
        "inning_bucket": candidate_row.get("shadow_inning_bucket") or _bucket_inning(candidate_row.get("inning")),
        "runs_needed_bucket": candidate_row.get("runs_needed_bucket") or _bucket_runs_needed(runs_needed),
        "phantom_risk_bucket": candidate_row.get("shadow_phantom_risk_bucket") or candidate_row.get("phantom_risk_band") or "missing",
        "won": _safe_bool(candidate_row.get("target_over_win")),
        "final_total": candidate_row.get("final_total"),
    }


def build_clv_rows(
    *,
    signal_rows: Sequence[Mapping[str, Any]],
    snapshot_rows: Sequence[Mapping[str, Any]],
    trade_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    mode: str,
    min_date: str,
    max_date: str,
    horizons: Sequence[int],
    include_candidate_coverage_rows: bool,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    snapshot_map = _build_snapshot_map(snapshot_rows)
    trades_by_bet = _analysis_safe_trade_map(trade_rows)
    out: List[Dict[str, Any]] = []
    signal_bet_ids: set[str] = set()
    for row in signal_rows:
        row_mode = str(row.get("mode") or "")
        if mode != "both" and row_mode != mode:
            continue
        session_date = str(row.get("session_date") or "")
        if not _date_in_range(session_date, min_date, max_date):
            continue
        bet_id = str(row.get("bet_id") or "")
        if not bet_id:
            continue
        signal_bet_ids.add(bet_id)
        out.append(
            _build_signal_clv_row(
                row,
                snapshot_map.get((row_mode, bet_id), []),
                trades_by_bet.get(bet_id),
                horizons,
            )
        )

    if include_candidate_coverage_rows:
        for row in candidate_rows:
            row_mode = str(row.get("mode") or "")
            if mode != "both" and row_mode and row_mode != mode:
                continue
            session_date = str(row.get("session_date") or "")
            if not _date_in_range(session_date, min_date, max_date):
                continue
            bet_id = str(row.get("bet_id") or "")
            if bet_id and bet_id in signal_bet_ids:
                continue
            out.append(_build_candidate_coverage_row(row))

    out.sort(key=lambda r: (str(r.get("session_date") or ""), str(r.get("row_type") or ""), str(r.get("row_id") or "")))
    stats = {
        "snapshot_groups": len(snapshot_map),
        "trade_rows_loaded": len(trade_rows),
        "signal_rows_loaded": len(signal_rows),
        "candidate_rows_loaded": len(candidate_rows),
    }
    return out, stats


def _mean(values: Sequence[float]) -> Optional[float]:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    return statistics.mean(vals) if vals else None


def _median(values: Sequence[float]) -> Optional[float]:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    return statistics.median(vals) if vals else None


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys) if math.isfinite(float(x)) and math.isfinite(float(y))]
    if len(pairs) < 2:
        return None
    xvals = [p[0] for p in pairs]
    yvals = [p[1] for p in pairs]
    xmean = statistics.mean(xvals)
    ymean = statistics.mean(yvals)
    num = sum((x - xmean) * (y - ymean) for x, y in pairs)
    den_x = math.sqrt(sum((x - xmean) ** 2 for x in xvals))
    den_y = math.sqrt(sum((y - ymean) ** 2 for y in yvals))
    if den_x <= 0 or den_y <= 0:
        return None
    return num / (den_x * den_y)


def _summary_for_rows(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    clv = [_safe_float(r.get("clv_mid_vs_entry")) for r in rows]
    clv = [v for v in clv if v is not None]
    clv_exec = [_safe_float(r.get("clv_mid_vs_execution")) for r in rows]
    clv_exec = [v for v in clv_exec if v is not None]
    roi_pairs = [
        (_safe_float(r.get("clv_mid_vs_execution")), _safe_float(r.get("realized_roi")))
        for r in rows
        if _safe_float(r.get("clv_mid_vs_execution")) is not None and _safe_float(r.get("realized_roi")) is not None
    ]
    return {
        "rows": len(rows),
        "rows_with_late_price": sum(1 for r in rows if r.get("has_late_price")),
        "rows_with_realized_roi": sum(1 for r in rows if _safe_float(r.get("realized_roi")) is not None),
        "mean_clv_mid_vs_entry": _round(_mean(clv)),
        "median_clv_mid_vs_entry": _round(_median(clv)),
        "mean_clv_mid_vs_execution": _round(_mean(clv_exec)),
        "median_clv_mid_vs_execution": _round(_median(clv_exec)),
        "positive_clv_rate_vs_entry": _round(sum(1 for v in clv if v > 0) / len(clv) if clv else None),
        "positive_clv_rate_vs_execution": _round(sum(1 for v in clv_exec if v > 0) / len(clv_exec) if clv_exec else None),
        "realized_roi_correlation_with_clv_execution": _round(
            _pearson([p[0] for p in roi_pairs], [p[1] for p in roi_pairs])
            if roi_pairs
            else None
        ),
    }


def _group(rows: Sequence[Mapping[str, Any]], field: str, *, min_rows: int = 1) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "missing")].append(row)
    out: Dict[str, Dict[str, Any]] = {}
    for key, group_rows in sorted(groups.items()):
        if len(group_rows) >= min_rows:
            out[key] = _summary_for_rows(group_rows)
    return out


def _clv_bucket(value: Optional[float]) -> str:
    if value is None:
        return "missing"
    if value < -0.05:
        return "<-5c"
    if value < -0.02:
        return "-5c..-2c"
    if value < 0:
        return "-2c..0c"
    if value < 0.02:
        return "0c..2c"
    if value < 0.05:
        return "2c..5c"
    return ">=5c"


def _roi_by_clv_bucket(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        clv = _safe_float(row.get("clv_mid_vs_execution"))
        roi = _safe_float(row.get("realized_roi"))
        if clv is None or roi is None:
            continue
        groups[_clv_bucket(clv)].append(row)
    out: Dict[str, Dict[str, Any]] = {}
    for bucket, group_rows in sorted(groups.items()):
        rois = [_safe_float(r.get("realized_roi")) for r in group_rows]
        rois = [v for v in rois if v is not None]
        profits = [_safe_float(r.get("realized_profit_usdc")) for r in group_rows]
        profits = [v for v in profits if v is not None]
        costs = [_safe_float(r.get("fill_cost_usdc")) for r in group_rows]
        costs = [v for v in costs if v is not None]
        out[bucket] = {
            "rows": len(group_rows),
            "mean_roi": _round(_mean(rois)),
            "profit_usdc": _round(sum(profits)),
            "cost_usdc": _round(sum(costs)),
            "roi_on_cost": _round(sum(profits) / sum(costs) if costs and sum(costs) > 0 else None),
        }
    return out


def build_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    config: Mapping[str, Any],
    load_stats: Mapping[str, Any],
    warnings: Sequence[str],
) -> Dict[str, Any]:
    measurable = [r for r in rows if r.get("has_late_price")]
    filled = [r for r in rows if _safe_float(r.get("realized_roi")) is not None]
    by_family_gate: Dict[str, Dict[str, Any]] = {}
    nested: Dict[Tuple[str, str], List[Mapping[str, Any]]] = defaultdict(list)
    for row in measurable:
        nested[(str(row.get("signal_model_family") or "unknown"), str(row.get("gate_or_reason") or "unknown"))].append(row)
    for (family, gate), group_rows in sorted(nested.items()):
        by_family_gate[f"{family}|{gate}"] = _summary_for_rows(group_rows)

    return {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "description": "CLV/late-price diagnostics. late_mid is last captured midpoint, not guaranteed true market close.",
        "config": dict(config),
        "load_stats": dict(load_stats),
        "row_counts": {
            "total_rows": len(rows),
            "order_or_captured_signal_rows": sum(1 for r in rows if r.get("row_type") == "order_or_captured_signal"),
            "candidate_coverage_rows": sum(1 for r in rows if r.get("row_type") == "candidate_no_late_price"),
            "rows_with_late_price": len(measurable),
            "filled_rows_with_roi": len(filled),
        },
        "overall": _summary_for_rows(rows),
        "measurable_only": _summary_for_rows(measurable),
        "clv_by_family": _group(measurable, "signal_model_family"),
        "clv_by_gate_or_reason": _group(measurable, "gate_or_reason"),
        "clv_by_family_gate": by_family_gate,
        "clv_by_ask_bucket": _group(measurable, "ask_bucket"),
        "clv_by_edge_bucket": _group(measurable, "edge_bucket"),
        "clv_by_inning_bucket": _group(measurable, "inning_bucket"),
        "clv_by_runs_needed_bucket": _group(measurable, "runs_needed_bucket"),
        "clv_by_phantom_risk_bucket": _group(measurable, "phantom_risk_bucket"),
        "clv_vs_realized_roi": {
            "filled_rows": _summary_for_rows(filled),
            "roi_by_clv_execution_bucket": _roi_by_clv_bucket(filled),
        },
        "coverage_by_family": _group(rows, "signal_model_family"),
        "warnings": list(warnings)[:200],
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(dict(row), sort_keys=True) + "\n")


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_markdown(path: Path, summary: Mapping[str, Any]) -> None:
    lines = [
        "# CLV / Late-Price Report",
        "",
        f"Generated: {summary.get('generated_at_utc')}",
        "",
        "Note: `late_mid` is the last captured post-signal midpoint, not guaranteed true market close.",
        "",
        "## Overall",
        "",
    ]
    overall = summary.get("measurable_only") or {}
    lines.extend(
        [
            f"- Rows with late price: `{overall.get('rows_with_late_price')}`",
            f"- Mean CLV vs entry: `{overall.get('mean_clv_mid_vs_entry')}`",
            f"- Positive CLV rate vs entry: `{overall.get('positive_clv_rate_vs_entry')}`",
            f"- Mean CLV vs execution: `{overall.get('mean_clv_mid_vs_execution')}`",
            f"- CLV/realized ROI correlation: `{overall.get('realized_roi_correlation_with_clv_execution')}`",
            "",
            "## By Family",
            "",
            "| family | rows | mean CLV entry | positive rate | ROI corr |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for family, payload in (summary.get("clv_by_family") or {}).items():
        lines.append(
            f"| {family} | {payload.get('rows_with_late_price')} | "
            f"{payload.get('mean_clv_mid_vs_entry')} | "
            f"{payload.get('positive_clv_rate_vs_entry')} | "
            f"{payload.get('realized_roi_correlation_with_clv_execution')} |"
        )
    lines.extend(["", "## CLV vs Realized ROI", "", "| CLV bucket | rows | ROI on cost | profit |", "|---|---:|---:|---:|"])
    buckets = ((summary.get("clv_vs_realized_roi") or {}).get("roi_by_clv_execution_bucket") or {})
    for bucket, payload in buckets.items():
        lines.append(
            f"| {bucket} | {payload.get('rows')} | {payload.get('roi_on_cost')} | {payload.get('profit_usdc')} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    warnings: List[str] = []
    horizons = _parse_horizons(args.horizons)
    signal_rows = _read_jsonl(args.signals_master, warnings)
    snapshot_rows = _read_jsonl(args.snapshots, warnings)
    trade_rows = _read_jsonl(args.analysis_safe_trades, warnings)
    candidate_rows = _read_jsonl(args.calibration_table, warnings)
    rows, load_stats = build_clv_rows(
        signal_rows=signal_rows,
        snapshot_rows=snapshot_rows,
        trade_rows=trade_rows,
        candidate_rows=candidate_rows,
        mode=args.mode,
        min_date=args.min_date,
        max_date=args.max_date,
        horizons=horizons,
        include_candidate_coverage_rows=not bool(args.no_candidate_coverage_rows),
    )
    config = {
        "signals_master": str(args.signals_master),
        "snapshots": str(args.snapshots),
        "analysis_safe_trades": str(args.analysis_safe_trades),
        "calibration_table": str(args.calibration_table),
        "mode": args.mode,
        "min_date": args.min_date or None,
        "max_date": args.max_date or None,
        "horizons": horizons,
        "candidate_coverage_rows": not bool(args.no_candidate_coverage_rows),
    }
    summary = build_summary(rows, config=config, load_stats=load_stats, warnings=warnings)
    if args.strict and not summary["row_counts"]["rows_with_late_price"]:
        raise SystemExit("Strict mode failed: no rows with late price.")

    args.output_root.mkdir(parents=True, exist_ok=True)
    rows_jsonl = args.output_root / "clv_rows.jsonl"
    rows_csv = args.output_root / "clv_rows.csv"
    summary_json = args.output_root / "clv_summary.json"
    summary_md = args.output_root / "clv_summary.md"
    _write_jsonl(rows_jsonl, rows)
    _write_csv(rows_csv, rows)
    _write_json(summary_json, summary)
    _write_markdown(summary_md, summary)
    print(f"Wrote {summary_json}")
    print(f"Wrote {rows_jsonl}")


if __name__ == "__main__":
    main()
