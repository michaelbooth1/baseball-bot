#!/usr/bin/env python3
"""
Build a per-trade execution diagnostics report.

This report joins unified signal rows with per-snapshot book captures and emits
one diagnostics row per trade with execution-focused fields:
  - limit_touch (did book ask ever touch posted limit?)
  - first_touch_seconds (first elapsed_s where ask <= limit)
  - shadow touches/fills at +1c/+2c repricer levels (metrics only)
  - cancel_reason
  - counterfactual outcome (would the signal win if filled?)

Inputs (from build_unified_signal_table.py):
  data/analysis_output/unified_signals/signals_master.jsonl
  data/analysis_output/unified_signals/signal_book_snapshots.jsonl

Outputs:
  data/analysis_output/execution_diagnostics/
    execution_diagnostics_master.jsonl
    execution_diagnostics_master.csv
    execution_diagnostics_summary.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_UNIFIED_ROOT = PROJECT_DIR / "data" / "analysis_output" / "unified_signals"
DEFAULT_SIGNALS_MASTER_PATH = DEFAULT_UNIFIED_ROOT / "signals_master.jsonl"
DEFAULT_SNAPSHOTS_PATH = DEFAULT_UNIFIED_ROOT / "signal_book_snapshots.jsonl"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "execution_diagnostics"

LOGGER = logging.getLogger("build_execution_diagnostics_report")


OUTPUT_COLUMNS = [
    "mode",
    "session_date",
    "bet_id",
    "game_pk",
    "away_abbrev",
    "home_abbrev",
    "line",
    "inning",
    "order_status_final",
    "cancel_reason",
    "settled",
    "counterfactual_outcome",
    "won_counterfactual",
    "realized_executed",
    "realized_win",
    "stake",
    "realized_profit",
    "entry_ask",
    "posted_limit",
    "limit_touch",
    "first_touch_seconds",
    "first_touch_ask",
    "limit_touch_p1c",
    "first_touch_seconds_p1c",
    "first_touch_ask_p1c",
    "limit_touch_p2c",
    "first_touch_seconds_p2c",
    "first_touch_ask_p2c",
    "capture_window_seconds",
    "touch_before_cancel",
    "touch_before_fill",
    "filled_at",
    "cancelled_at",
    "sim_filled_30s",
    "sim_fill_time_30s",
    "sim_filled_30s_p1c",
    "sim_fill_time_30s_p1c",
    "sim_filled_30s_p2c",
    "sim_fill_time_30s_p2c",
    "counterfactual_profit_if_filled_limit",
]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build per-trade execution diagnostics report.")
    p.add_argument(
        "--signals-master",
        type=Path,
        default=DEFAULT_SIGNALS_MASTER_PATH,
        help=f"Path to signals_master.jsonl (default: {DEFAULT_SIGNALS_MASTER_PATH}).",
    )
    p.add_argument(
        "--snapshots",
        type=Path,
        default=DEFAULT_SNAPSHOTS_PATH,
        help=f"Path to signal_book_snapshots.jsonl (default: {DEFAULT_SNAPSHOTS_PATH}).",
    )
    p.add_argument(
        "--mode",
        choices=["live", "paper", "both"],
        default="live",
        help="Which mode rows to include (default: live).",
    )
    p.add_argument("--min-date", type=str, default="", help="Inclusive lower date bound (YYYY-MM-DD).")
    p.add_argument("--max-date", type=str, default="", help="Inclusive upper date bound (YYYY-MM-DD).")
    p.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Output directory (default: {DEFAULT_OUTPUT_ROOT}).",
    )
    p.add_argument("--strict", action="store_true", help="Fail on hard check failures.")
    p.add_argument("--verbose", action="store_true", help="Verbose logging.")
    p.add_argument(
        "--console-report",
        dest="console_report",
        action="store_true",
        default=True,
        help="Print compact execution diagnostics report to stdout (default: enabled).",
    )
    p.add_argument(
        "--no-console-report",
        dest="console_report",
        action="store_false",
        help="Disable compact execution diagnostics stdout report.",
    )
    p.add_argument(
        "--report-top-n",
        type=int,
        default=8,
        help="Top-N rows to show in compact report sections (default: 8).",
    )
    return p.parse_args(argv)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        return int(v)
    except Exception:
        return None


def _safe_bool(v: Any) -> Optional[bool]:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "y"):
        return True
    if s in ("false", "0", "no", "n"):
        return False
    return None


def _coalesce(values: Iterable[Any]) -> Any:
    for v in values:
        if v is not None and v != "":
            return v
    return None


def _parse_iso_to_epoch(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _date_in_range(date_str: str, min_date: Optional[str], max_date: Optional[str]) -> bool:
    if not date_str:
        return True
    if min_date and date_str < min_date:
        return False
    if max_date and date_str > max_date:
        return False
    return True


def _infer_session_date(row: Dict[str, Any]) -> str:
    sd = str(row.get("session_date") or "")
    if len(sd) == 10:
        return sd
    placed_at = str(row.get("placed_at") or "")
    if len(placed_at) >= 10:
        return placed_at[:10]
    bet_id = str(row.get("bet_id") or "")
    if len(bet_id) >= 10 and bet_id[4] == "-" and bet_id[7] == "-":
        return bet_id[:10]
    return ""


def _read_jsonl(path: Path, warnings: List[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        warnings.append(f"path does not exist: {path}")
        return rows
    with open(path, encoding="utf-8") as f:
        for i, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception as exc:
                warnings.append(f"malformed JSON {path}:{i}: {exc}")
                continue
            if not isinstance(row, dict):
                warnings.append(f"non-dict JSON row {path}:{i}")
                continue
            rows.append(row)
    return rows


def _build_snapshot_map(snapshot_rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in snapshot_rows:
        mode = str(row.get("mode") or "")
        bet_id = str(row.get("bet_id") or "")
        if not mode or not bet_id:
            continue
        grouped[(mode, bet_id)].append(row)

    for key, rows in grouped.items():
        rows.sort(
            key=lambda r: (
                float(r.get("elapsed_s") or 0.0),
                int(r.get("seq") or 0),
                str(r.get("ts") or ""),
            )
        )
    return dict(grouped)


def _first_limit_touch(
    snapshots: List[Dict[str, Any]],
    posted_limit: Optional[float],
    eps: float = 1e-9,
) -> Tuple[Optional[bool], Optional[float], Optional[float], Optional[str], Optional[float]]:
    if posted_limit is None:
        return None, None, None, None, None
    if not snapshots:
        return None, None, None, None, None

    first_touch_seconds: Optional[float] = None
    first_touch_ask: Optional[float] = None
    first_touch_ts: Optional[str] = None
    capture_window_seconds = max(
        (_safe_float(s.get("elapsed_s")) or 0.0 for s in snapshots),
        default=0.0,
    )
    for s in snapshots:
        ask = _safe_float(s.get("best_ask"))
        if ask is None:
            continue
        if ask <= posted_limit + eps:
            first_touch_seconds = _safe_float(s.get("elapsed_s"))
            first_touch_ask = ask
            first_touch_ts = str(s.get("ts") or "")
            return True, first_touch_seconds, first_touch_ask, first_touch_ts, capture_window_seconds
    return False, None, None, None, capture_window_seconds


def _counterfactual_outcome(won_counterfactual: Optional[bool], settled: bool) -> str:
    if not settled or won_counterfactual is None:
        return "pending"
    return "win" if won_counterfactual else "loss"


def _counterfactual_profit_if_filled_limit(
    *,
    stake: Optional[float],
    posted_limit: Optional[float],
    settled: bool,
    won_counterfactual: Optional[bool],
) -> Optional[float]:
    if not settled or won_counterfactual is None:
        return None
    if stake is None or posted_limit is None or posted_limit <= 0:
        return None
    if won_counterfactual:
        return round((stake / posted_limit) - stake, 6)
    return round(-stake, 6)


def build_diagnostics_rows(
    master_rows: List[Dict[str, Any]],
    snapshot_map: Dict[Tuple[str, str], List[Dict[str, Any]]],
    *,
    mode: str,
    min_date: Optional[str],
    max_date: Optional[str],
    warnings: List[str],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in master_rows:
        row_mode = str(row.get("mode") or "")
        if mode != "both" and row_mode != mode:
            continue

        bet_id = str(row.get("bet_id") or "")
        if not bet_id:
            warnings.append("signals_master row missing bet_id")
            continue

        session_date = _infer_session_date(row)
        if not _date_in_range(session_date, min_date, max_date):
            continue

        posted_limit = _safe_float(_coalesce([row.get("posted_limit"), row.get("limit_price")]))
        snapshots = snapshot_map.get((row_mode, bet_id), [])
        limit_touch, first_touch_seconds, first_touch_ask, first_touch_ts, capture_window_seconds = _first_limit_touch(
            snapshots,
            posted_limit=posted_limit,
        )
        posted_limit_p1c = (
            round(posted_limit + 0.01, 6) if posted_limit is not None else None
        )
        posted_limit_p2c = (
            round(posted_limit + 0.02, 6) if posted_limit is not None else None
        )
        limit_touch_p1c, first_touch_seconds_p1c, first_touch_ask_p1c, _first_touch_ts_p1c, _capture_window_p1c = _first_limit_touch(
            snapshots,
            posted_limit=posted_limit_p1c,
        )
        limit_touch_p2c, first_touch_seconds_p2c, first_touch_ask_p2c, _first_touch_ts_p2c, _capture_window_p2c = _first_limit_touch(
            snapshots,
            posted_limit=posted_limit_p2c,
        )
        if capture_window_seconds is None:
            capture_window_seconds = _capture_window_p1c if _capture_window_p1c is not None else _capture_window_p2c

        settled = bool(_safe_bool(row.get("settled")))
        won_counterfactual = _safe_bool(row.get("won_counterfactual"))
        filled_at = str(row.get("filled_at") or "")
        cancelled_at = str(row.get("cancelled_at") or "")
        t_touch = _parse_iso_to_epoch(first_touch_ts)
        t_cancelled = _parse_iso_to_epoch(cancelled_at)
        t_filled = _parse_iso_to_epoch(filled_at)

        touch_before_cancel: Optional[bool] = None
        if t_touch is not None and t_cancelled is not None:
            touch_before_cancel = t_touch <= t_cancelled + 1e-9

        touch_before_fill: Optional[bool] = None
        if t_touch is not None and t_filled is not None:
            touch_before_fill = t_touch <= t_filled + 1e-9

        stake = _safe_float(row.get("stake"))
        realized_profit = _safe_float(row.get("realized_profit"))
        counterfactual_profit = _counterfactual_profit_if_filled_limit(
            stake=stake,
            posted_limit=posted_limit,
            settled=settled,
            won_counterfactual=won_counterfactual,
        )

        out_row = {
            "mode": row_mode,
            "session_date": session_date,
            "bet_id": bet_id,
            "game_pk": _safe_int(row.get("game_pk")),
            "away_abbrev": row.get("away_abbrev"),
            "home_abbrev": row.get("home_abbrev"),
            "line": row.get("line"),
            "inning": _safe_int(row.get("inning")),
            "order_status_final": row.get("order_status_final"),
            "cancel_reason": str(row.get("cancel_reason_final") or ""),
            "settled": settled,
            "counterfactual_outcome": _counterfactual_outcome(won_counterfactual, settled),
            "won_counterfactual": won_counterfactual,
            "realized_executed": bool(_safe_bool(row.get("realized_executed"))),
            "realized_win": _safe_bool(row.get("realized_win")),
            "stake": stake,
            "realized_profit": realized_profit,
            "entry_ask": _safe_float(row.get("entry_ask")),
            "posted_limit": posted_limit,
            "limit_touch": limit_touch,
            "first_touch_seconds": first_touch_seconds,
            "first_touch_ask": first_touch_ask,
            "limit_touch_p1c": limit_touch_p1c,
            "first_touch_seconds_p1c": first_touch_seconds_p1c,
            "first_touch_ask_p1c": first_touch_ask_p1c,
            "limit_touch_p2c": limit_touch_p2c,
            "first_touch_seconds_p2c": first_touch_seconds_p2c,
            "first_touch_ask_p2c": first_touch_ask_p2c,
            "capture_window_seconds": capture_window_seconds,
            "touch_before_cancel": touch_before_cancel,
            "touch_before_fill": touch_before_fill,
            "filled_at": filled_at or None,
            "cancelled_at": cancelled_at or None,
            "sim_filled_30s": _safe_bool(row.get("sim_filled_30s")),
            "sim_fill_time_30s": _safe_float(row.get("sim_fill_time_30s")),
            "sim_filled_30s_p1c": _safe_bool(row.get("sim_filled_30s_p1c")),
            "sim_fill_time_30s_p1c": _safe_float(row.get("sim_fill_time_30s_p1c")),
            "sim_filled_30s_p2c": _safe_bool(row.get("sim_filled_30s_p2c")),
            "sim_fill_time_30s_p2c": _safe_float(row.get("sim_fill_time_30s_p2c")),
            "counterfactual_profit_if_filled_limit": counterfactual_profit,
        }
        out.append(out_row)

    out.sort(
        key=lambda r: (
            str(r.get("session_date") or ""),
            str(r.get("mode") or ""),
            str(r.get("bet_id") or ""),
        )
    )
    return out


def _touch_rate(rows: List[Dict[str, Any]]) -> Optional[float]:
    vals = [r for r in rows if isinstance(r.get("limit_touch"), bool)]
    if not vals:
        return None
    touched = sum(1 for r in vals if r.get("limit_touch") is True)
    return round(touched / len(vals), 4)


def _bool_rate(rows: List[Dict[str, Any]], key: str) -> Optional[float]:
    vals = [r for r in rows if isinstance(r.get(key), bool)]
    if not vals:
        return None
    positives = sum(1 for r in vals if r.get(key) is True)
    return round(positives / len(vals), 4)


def build_summary(
    diagnostics_rows: List[Dict[str, Any]],
    *,
    config: Dict[str, Any],
    warnings: List[str],
    hard_errors: List[str],
) -> Dict[str, Any]:
    by_mode: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_status: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    cancel_reason_rows: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for row in diagnostics_rows:
        mode = str(row.get("mode") or "unknown")
        by_mode[mode].append(row)
        status = str(row.get("order_status_final") or "unknown")
        by_status[status].append(row)
        reason = str(row.get("cancel_reason") or "")
        if reason:
            cancel_reason_rows[reason].append(row)

    touched_known = [r for r in diagnostics_rows if isinstance(r.get("limit_touch"), bool)]
    touched = [r for r in touched_known if r.get("limit_touch") is True]
    filled = [r for r in diagnostics_rows if str(r.get("order_status_final") or "") == "filled"]
    cancelled = [r for r in diagnostics_rows if str(r.get("order_status_final") or "") == "cancelled"]
    cancelled_cf_win = [r for r in cancelled if r.get("counterfactual_outcome") == "win"]

    summary = {
        "generated_at_utc": _now_iso(),
        "config": config,
        "counts": {
            "rows_total": len(diagnostics_rows),
            "rows_by_mode": {k: len(v) for k, v in sorted(by_mode.items())},
            "rows_by_order_status": {k: len(v) for k, v in sorted(by_status.items())},
            "rows_with_touch_observed": len(touched_known),
            "rows_limit_touched": len(touched),
            "filled_rows": len(filled),
            "cancelled_rows": len(cancelled),
            "cancelled_counterfactual_wins": len(cancelled_cf_win),
        },
        "rates": {
            "overall_touch_rate": _touch_rate(diagnostics_rows),
            "filled_touch_rate": _touch_rate(filled),
            "cancelled_touch_rate": _touch_rate(cancelled),
            "overall_touch_rate_p1c": _bool_rate(diagnostics_rows, "limit_touch_p1c"),
            "overall_touch_rate_p2c": _bool_rate(diagnostics_rows, "limit_touch_p2c"),
            "sim_fill_rate_30s": _bool_rate(diagnostics_rows, "sim_filled_30s"),
            "sim_fill_rate_30s_p1c": _bool_rate(diagnostics_rows, "sim_filled_30s_p1c"),
            "sim_fill_rate_30s_p2c": _bool_rate(diagnostics_rows, "sim_filled_30s_p2c"),
            "cancelled_counterfactual_win_rate": round(len(cancelled_cf_win) / len(cancelled), 4)
            if cancelled
            else None,
        },
        "cancel_reason_breakdown": {
            reason: {
                "count": len(rows),
                "touch_rate": _touch_rate(rows),
                "counterfactual_wins": sum(1 for r in rows if r.get("counterfactual_outcome") == "win"),
                "counterfactual_losses": sum(1 for r in rows if r.get("counterfactual_outcome") == "loss"),
                "counterfactual_pending": sum(1 for r in rows if r.get("counterfactual_outcome") == "pending"),
                "counterfactual_profit_if_filled_limit": round(
                    sum(_safe_float(r.get("counterfactual_profit_if_filled_limit")) or 0.0 for r in rows),
                    4,
                ),
                "counterfactual_stake_sum": round(
                    sum(_safe_float(r.get("stake")) or 0.0 for r in rows if r.get("counterfactual_profit_if_filled_limit") is not None),
                    4,
                ),
            }
            for reason, rows in sorted(cancel_reason_rows.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        },
        "warnings_count": len(warnings),
        "hard_errors_count": len(hard_errors),
        "warnings": warnings[:300],
        "hard_errors": hard_errors[:300],
        "status": "failed" if hard_errors else "ok",
    }
    return summary


def _fmt_pct_from_ratio(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:.1f}%"


def _fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"${x:+.2f}"


def print_compact_console_report(
    diagnostics_rows: List[Dict[str, Any]],
    summary: Dict[str, Any],
    *,
    top_n: int,
) -> None:
    if not diagnostics_rows:
        print("\nExecution diagnostics: no rows to report.\n")
        return

    top_n = max(1, int(top_n))
    print("\n" + "=" * 88)
    print("Execution Diagnostics (Compact)")
    print("=" * 88)

    counts = summary.get("counts", {}) or {}
    rates = summary.get("rates", {}) or {}
    print(
        "Rows={rows}  Filled={filled}  Cancelled={cancelled}  "
        "TouchRate={touch}  FilledTouchRate={filled_touch}  CancelledCFWinRate={cf_win}".format(
            rows=counts.get("rows_total", 0),
            filled=counts.get("filled_rows", 0),
            cancelled=counts.get("cancelled_rows", 0),
            touch=_fmt_pct_from_ratio(rates.get("overall_touch_rate")),
            filled_touch=_fmt_pct_from_ratio(rates.get("filled_touch_rate")),
            cf_win=_fmt_pct_from_ratio(rates.get("cancelled_counterfactual_win_rate")),
        )
    )
    print(
        "Shadow repricer (30s): base_fill={base}  +1c_fill={p1}  +2c_fill={p2}  "
        "base_touch={t0}  +1c_touch={t1}  +2c_touch={t2}".format(
            base=_fmt_pct_from_ratio(rates.get("sim_fill_rate_30s")),
            p1=_fmt_pct_from_ratio(rates.get("sim_fill_rate_30s_p1c")),
            p2=_fmt_pct_from_ratio(rates.get("sim_fill_rate_30s_p2c")),
            t0=_fmt_pct_from_ratio(rates.get("overall_touch_rate")),
            t1=_fmt_pct_from_ratio(rates.get("overall_touch_rate_p1c")),
            t2=_fmt_pct_from_ratio(rates.get("overall_touch_rate_p2c")),
        )
    )

    # Touch-vs-fill gap
    touch_known = [r for r in diagnostics_rows if isinstance(r.get("limit_touch"), bool)]
    touched = [r for r in touch_known if r.get("limit_touch") is True]
    touched_filled = [r for r in touched if bool(r.get("realized_executed"))]
    touched_not_filled = [r for r in touched if not bool(r.get("realized_executed"))]
    not_touched_filled = [
        r for r in touch_known if (r.get("limit_touch") is False and bool(r.get("realized_executed")))
    ]
    touched_fill_rate = (len(touched_filled) / len(touched)) if touched else None
    print("\nTouch-vs-fill gap:")
    print(
        "  touch_observed={obs}  touched={touched}  touched_not_filled={tnf}  "
        "touch_to_fill={t2f}  not_touched_but_filled={ntf}".format(
            obs=len(touch_known),
            touched=len(touched),
            tnf=len(touched_not_filled),
            t2f=_fmt_pct_from_ratio(touched_fill_rate),
            ntf=len(not_touched_filled),
        )
    )

    # Cancel-reason opportunity impact
    print("\nCancel-reason ROI impact (counterfactual at posted limit):")
    print("  {:<18} {:>5} {:>8} {:>11} {:>10} {:>9}".format(
        "Reason", "N", "Touch%", "Stake", "CF Profit", "CF ROI"
    ))
    cancel_rows = [r for r in diagnostics_rows if str(r.get("order_status_final") or "") == "cancelled"]
    by_reason: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in cancel_rows:
        reason = str(r.get("cancel_reason") or "unknown")
        by_reason[reason].append(r)

    def _reason_key(item: Tuple[str, List[Dict[str, Any]]]) -> Tuple[float, int]:
        _reason, rows = item
        prof = sum(_safe_float(r.get("counterfactual_profit_if_filled_limit")) or 0.0 for r in rows)
        return (prof, len(rows))

    for reason, rows in sorted(by_reason.items(), key=_reason_key, reverse=True)[:top_n]:
        stake_sum = sum(
            _safe_float(r.get("stake")) or 0.0
            for r in rows
            if r.get("counterfactual_profit_if_filled_limit") is not None
        )
        cf_profit = sum(_safe_float(r.get("counterfactual_profit_if_filled_limit")) or 0.0 for r in rows)
        cf_roi = (cf_profit / stake_sum) if stake_sum > 0 else None
        touch_rate = _touch_rate(rows)
        print(
            "  {:<18} {:>5} {:>8} {:>11} {:>10} {:>9}".format(
                reason[:18],
                len(rows),
                _fmt_pct_from_ratio(touch_rate),
                f"${stake_sum:.2f}",
                _fmt_money(cf_profit),
                _fmt_pct_from_ratio(cf_roi),
            )
        )

    # Top missed fills
    print("\nTop missed fills (counterfactual wins, not executed):")
    missed = [
        r for r in diagnostics_rows
        if (not bool(r.get("realized_executed"))) and (str(r.get("counterfactual_outcome") or "") == "win")
    ]
    if not missed:
        print("  none")
    else:
        missed.sort(
            key=lambda r: (
                -(_safe_float(r.get("counterfactual_profit_if_filled_limit")) or float("-inf")),
                (_safe_float(r.get("first_touch_seconds")) if _safe_float(r.get("first_touch_seconds")) is not None else 1e9),
                str(r.get("bet_id") or ""),
            )
        )
        print("  {:<24} {:<11} {:>7} {:>8} {:>10} {:>8} {:>8} {:>10} {:<14}".format(
            "BetID", "Game", "Line", "Touched", "TouchSec", "+1cSec", "+2cSec", "CF Profit", "CancelReason"
        ))
        for r in missed[:top_n]:
            game = f"{str(r.get('away_abbrev') or '')}@{str(r.get('home_abbrev') or '')}"
            touch = r.get("limit_touch")
            touch_str = "yes" if touch is True else ("no" if touch is False else "n/a")
            first_touch = _safe_float(r.get("first_touch_seconds"))
            first_touch_str = f"{first_touch:.1f}" if first_touch is not None else "-"
            shadow_fill_p1 = _safe_float(r.get("sim_fill_time_30s_p1c"))
            shadow_fill_p2 = _safe_float(r.get("sim_fill_time_30s_p2c"))
            shadow_fill_p1_str = f"{shadow_fill_p1:.1f}" if shadow_fill_p1 is not None else "-"
            shadow_fill_p2_str = f"{shadow_fill_p2:.1f}" if shadow_fill_p2 is not None else "-"
            cf_profit = _safe_float(r.get("counterfactual_profit_if_filled_limit"))
            print(
                "  {:<24} {:<11} {:>7} {:>8} {:>10} {:>8} {:>8} {:>10} {:<14}".format(
                    str(r.get("bet_id") or "")[:24],
                    game[:11],
                    str(r.get("line") or ""),
                    touch_str,
                    first_touch_str,
                    shadow_fill_p1_str,
                    shadow_fill_p2_str,
                    _fmt_money(cf_profit),
                    str(r.get("cancel_reason") or "")[:14],
                )
            )
    print("=" * 88 + "\n")


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def write_csv(path: Path, rows: List[Dict[str, Any]], columns: List[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    if args.min_date:
        datetime.strptime(args.min_date, "%Y-%m-%d")
    if args.max_date:
        datetime.strptime(args.max_date, "%Y-%m-%d")
    if args.min_date and args.max_date and args.min_date > args.max_date:
        raise SystemExit("--min-date must be <= --max-date")
    if args.report_top_n <= 0:
        raise SystemExit("--report-top-n must be > 0")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    warnings: List[str] = []
    hard_errors: List[str] = []

    master_rows = _read_jsonl(args.signals_master, warnings)
    snapshot_rows = _read_jsonl(args.snapshots, warnings)
    snapshot_map = _build_snapshot_map(snapshot_rows)

    diagnostics_rows = build_diagnostics_rows(
        master_rows=master_rows,
        snapshot_map=snapshot_map,
        mode=args.mode,
        min_date=args.min_date or None,
        max_date=args.max_date or None,
        warnings=warnings,
    )
    if args.strict and not diagnostics_rows:
        hard_errors.append("strict mode failed: no diagnostics rows produced")

    args.output_root.mkdir(parents=True, exist_ok=True)
    out_jsonl = args.output_root / "execution_diagnostics_master.jsonl"
    out_csv = args.output_root / "execution_diagnostics_master.csv"
    out_summary = args.output_root / "execution_diagnostics_summary.json"

    write_jsonl(out_jsonl, diagnostics_rows)
    write_csv(out_csv, diagnostics_rows, OUTPUT_COLUMNS)

    summary = build_summary(
        diagnostics_rows=diagnostics_rows,
        config={
            "mode": args.mode,
            "min_date": args.min_date or None,
            "max_date": args.max_date or None,
            "signals_master": str(args.signals_master),
            "snapshots": str(args.snapshots),
            "output_root": str(args.output_root),
            "strict": args.strict,
            "console_report": bool(args.console_report),
            "report_top_n": int(args.report_top_n),
        },
        warnings=warnings,
        hard_errors=hard_errors,
    )
    with open(out_summary, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    LOGGER.info("Wrote %s", out_jsonl)
    LOGGER.info("Wrote %s", out_csv)
    LOGGER.info("Wrote %s", out_summary)
    LOGGER.info(
        "rows=%d  warnings=%d  hard_errors=%d",
        len(diagnostics_rows),
        len(warnings),
        len(hard_errors),
    )
    if args.console_report:
        print_compact_console_report(
            diagnostics_rows=diagnostics_rows,
            summary=summary,
            top_n=args.report_top_n,
        )

    if args.strict and hard_errors:
        raise SystemExit("Strict mode failed. See execution_diagnostics_summary.json.")


if __name__ == "__main__":
    main()
