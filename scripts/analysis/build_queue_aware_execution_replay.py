#!/usr/bin/env python3
"""
Build a queue-aware execution price replay.

This is an offline/background research tool. It does not change live order
placement. The goal is to compare execution price policies by realized value,
not fill rate alone:

  - current_limit: the posted limit used by the live engine
  - limit_p1c: posted limit + 1 cent
  - limit_p2c: posted limit + 2 cents
  - taker_like: immediate buy at decision ask

Queue note:
  Top-of-book captures do not reveal our exact queue position or level-specific
  size. The replay therefore reports both:
    touch_fill          -- first best_ask <= target limit
    queue_adjusted_fill -- first best_ask <= target limit - buffer

  The queue-adjusted fill is a conservative shadow assumption for resting bids
  that may be behind existing liquidity. Marketable policies at entry fill at
  the observed decision ask immediately.

Inputs:
  data/analysis_output/unified_signals/signals_master.jsonl
  data/analysis_output/unified_signals/signal_book_snapshots.jsonl

Outputs:
  data/analysis_output/execution_replay/
    queue_aware_execution_replay_summary.json
    queue_aware_execution_replay_rows.jsonl
    queue_aware_execution_replay_rows.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_UNIFIED_ROOT = PROJECT_DIR / "data" / "analysis_output" / "unified_signals"
DEFAULT_SIGNALS_MASTER_PATH = DEFAULT_UNIFIED_ROOT / "signals_master.jsonl"
DEFAULT_SNAPSHOTS_PATH = DEFAULT_UNIFIED_ROOT / "signal_book_snapshots.jsonl"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "execution_replay"

DEFAULT_QUEUE_BUFFER_CENTS = 1.0
DEFAULT_MAX_FILL_SECONDS = 120.0

POLICIES = ("current_limit", "limit_p1c", "limit_p2c", "taker_like")
FILL_MODE_TOUCH = "touch"
FILL_MODE_QUEUE_ADJUSTED = "queue_adjusted"

OUTPUT_COLUMNS = [
    "mode",
    "session_date",
    "bet_id",
    "policy",
    "fill_model",
    "game_pk",
    "away_abbrev",
    "home_abbrev",
    "line",
    "inning",
    "state_value_strategy",
    "current_state_value_edge",
    "shadow_phantom_risk_band",
    "stake",
    "decision_ask",
    "posted_limit",
    "policy_limit",
    "filled",
    "marketable_at_entry",
    "fill_seconds",
    "fill_price",
    "fill_source",
    "won",
    "realized_profit",
    "realized_roi",
    "fair_value",
    "model_edge_at_fill",
    "model_ev_per_stake",
    "entry_spread",
    "capture_window_seconds",
    "queue_buffer_cents",
]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build queue-aware execution price replay.")
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
    p.add_argument("--mode", choices=["live", "paper", "both"], default="live")
    p.add_argument("--min-date", type=str, default="", help="Inclusive YYYY-MM-DD.")
    p.add_argument("--max-date", type=str, default="", help="Inclusive YYYY-MM-DD.")
    p.add_argument(
        "--queue-buffer-cents",
        type=float,
        default=DEFAULT_QUEUE_BUFFER_CENTS,
        help=(
            "Queue-adjusted resting fill buffer in cents. A 1c buffer requires "
            "best_ask <= limit - 0.01 for passive fills (default: 1.0)."
        ),
    )
    p.add_argument(
        "--max-fill-seconds",
        type=float,
        default=DEFAULT_MAX_FILL_SECONDS,
        help=f"Replay horizon in seconds from signal capture start (default: {DEFAULT_MAX_FILL_SECONDS}).",
    )
    p.add_argument(
        "--include-unsettled",
        action="store_true",
        help="Include rows without final outcome. They can fill in replay but have null realized P&L.",
    )
    p.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Output directory (default: {DEFAULT_OUTPUT_ROOT}).",
    )
    p.add_argument("--strict", action="store_true", help="Fail if no replay rows are produced.")
    return p.parse_args(argv)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        if math.isnan(out):
            return None
        return out
    except Exception:
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _safe_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"true", "1", "yes", "y"}:
        return True
    if s in {"false", "0", "no", "n"}:
        return False
    return None


def _date_in_range(date_str: str, min_date: str, max_date: str) -> bool:
    if min_date and date_str < min_date:
        return False
    if max_date and date_str > max_date:
        return False
    return True


def _infer_session_date(row: Dict[str, Any]) -> str:
    session_date = str(row.get("session_date") or "")
    if len(session_date) == 10:
        return session_date
    placed_at = str(row.get("placed_at") or row.get("order_placed_at") or "")
    if len(placed_at) >= 10:
        return placed_at[:10]
    bet_id = str(row.get("bet_id") or "")
    if len(bet_id) >= 10 and bet_id[4] == "-" and bet_id[7] == "-":
        return bet_id[:10]
    return ""


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _build_snapshot_map(rows: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        mode = str(row.get("mode") or "")
        bet_id = str(row.get("bet_id") or "")
        if not mode or not bet_id:
            continue
        grouped[(mode, bet_id)].append(row)
    for snapshots in grouped.values():
        snapshots.sort(
            key=lambda r: (
                _safe_float(r.get("elapsed_s")) or 0.0,
                _safe_int(r.get("seq")) or 0,
                str(r.get("ts") or ""),
            )
        )
    return dict(grouped)


def _round_price(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(max(0.01, min(0.99, value)), 2)


def _policy_limit(policy: str, row: Dict[str, Any]) -> Optional[float]:
    posted_limit = _safe_float(row.get("posted_limit"))
    if posted_limit is None:
        posted_limit = _safe_float(row.get("limit_price"))
    decision_ask = _safe_float(row.get("decision_ask"))
    if decision_ask is None:
        decision_ask = _safe_float(row.get("entry_ask"))

    if policy == "current_limit":
        return _round_price(posted_limit)
    if policy == "limit_p1c":
        return _round_price(posted_limit + 0.01) if posted_limit is not None else None
    if policy == "limit_p2c":
        return _round_price(posted_limit + 0.02) if posted_limit is not None else None
    if policy == "taker_like":
        return _round_price(decision_ask)
    raise ValueError(f"unknown policy: {policy}")


def _snapshot_ask(snapshot: Dict[str, Any]) -> Optional[float]:
    ask = _safe_float(snapshot.get("best_ask"))
    if ask is not None:
        return ask
    return _safe_float(snapshot.get("ask"))


def _capture_window_seconds(snapshots: List[Dict[str, Any]], max_fill_seconds: float) -> Optional[float]:
    values = [
        _safe_float(s.get("elapsed_s"))
        for s in snapshots
        if _safe_float(s.get("elapsed_s")) is not None and (_safe_float(s.get("elapsed_s")) or 0.0) <= max_fill_seconds
    ]
    if not values:
        return None
    return round(max(values), 6)


def _simulate_policy_fill(
    *,
    policy: str,
    policy_limit: Optional[float],
    decision_ask: Optional[float],
    snapshots: List[Dict[str, Any]],
    fill_model: str,
    queue_buffer: float,
    max_fill_seconds: float,
) -> Dict[str, Any]:
    if policy_limit is None:
        return {
            "filled": False,
            "marketable_at_entry": False,
            "fill_seconds": None,
            "fill_price": None,
            "fill_source": "missing_policy_limit",
            "capture_window_seconds": _capture_window_seconds(snapshots, max_fill_seconds),
        }

    t0_ask = decision_ask
    if t0_ask is None and snapshots:
        t0_ask = _snapshot_ask(snapshots[0])

    is_taker_like = policy == "taker_like"
    marketable = t0_ask is not None and policy_limit >= t0_ask - 1e-9
    if is_taker_like or marketable:
        if t0_ask is None:
            return {
                "filled": False,
                "marketable_at_entry": False,
                "fill_seconds": None,
                "fill_price": None,
                "fill_source": "missing_entry_ask",
                "capture_window_seconds": _capture_window_seconds(snapshots, max_fill_seconds),
            }
        return {
            "filled": True,
            "marketable_at_entry": True,
            "fill_seconds": 0.0,
            "fill_price": round(t0_ask, 6),
            "fill_source": "entry_ask",
            "capture_window_seconds": _capture_window_seconds(snapshots, max_fill_seconds),
        }

    threshold = policy_limit
    if fill_model == FILL_MODE_QUEUE_ADJUSTED:
        threshold = round(policy_limit - queue_buffer, 6)
    elif fill_model != FILL_MODE_TOUCH:
        raise ValueError(f"unknown fill_model: {fill_model}")

    for snapshot in snapshots:
        elapsed = _safe_float(snapshot.get("elapsed_s"))
        if elapsed is None or elapsed > max_fill_seconds:
            continue
        ask = _snapshot_ask(snapshot)
        if ask is None:
            continue
        if ask <= threshold + 1e-9:
            return {
                "filled": True,
                "marketable_at_entry": False,
                "fill_seconds": round(elapsed, 6),
                "fill_price": round(policy_limit, 6),
                "fill_source": (
                    "best_ask_touch"
                    if fill_model == FILL_MODE_TOUCH
                    else f"best_ask_through_{queue_buffer:.2f}"
                ),
                "capture_window_seconds": _capture_window_seconds(snapshots, max_fill_seconds),
            }

    return {
        "filled": False,
        "marketable_at_entry": False,
        "fill_seconds": None,
        "fill_price": None,
        "fill_source": "no_fill_in_capture",
        "capture_window_seconds": _capture_window_seconds(snapshots, max_fill_seconds),
    }


def _profit_for_fill(
    *,
    stake: Optional[float],
    fill_price: Optional[float],
    won: Optional[bool],
) -> Tuple[Optional[float], Optional[float]]:
    if stake is None or fill_price is None or fill_price <= 0 or won is None:
        return None, None
    profit = (stake / fill_price) - stake if won else -stake
    return round(profit, 6), round(profit / stake, 6) if stake > 0 else None


def _model_ev_per_stake(fair_value: Optional[float], fill_price: Optional[float]) -> Optional[float]:
    if fair_value is None or fill_price is None or fill_price <= 0:
        return None
    return round((fair_value / fill_price) - 1.0, 6)


def build_replay_rows(
    signal_rows: List[Dict[str, Any]],
    snapshot_map: Dict[Tuple[str, str], List[Dict[str, Any]]],
    *,
    mode: str = "live",
    min_date: str = "",
    max_date: str = "",
    queue_buffer_cents: float = DEFAULT_QUEUE_BUFFER_CENTS,
    max_fill_seconds: float = DEFAULT_MAX_FILL_SECONDS,
    include_unsettled: bool = False,
) -> List[Dict[str, Any]]:
    queue_buffer = queue_buffer_cents / 100.0
    out: List[Dict[str, Any]] = []
    for signal in signal_rows:
        row_mode = str(signal.get("mode") or "")
        if mode != "both" and row_mode != mode:
            continue
        session_date = _infer_session_date(signal)
        if not _date_in_range(session_date, min_date, max_date):
            continue
        settled = bool(_safe_bool(signal.get("settled")))
        won = _safe_bool(signal.get("won_counterfactual"))
        if won is None:
            won = _safe_bool(signal.get("realized_win"))
        if not include_unsettled and (not settled or won is None):
            continue
        bet_id = str(signal.get("bet_id") or "")
        if not bet_id:
            continue
        snapshots = snapshot_map.get((row_mode, bet_id), [])
        decision_ask = _safe_float(signal.get("decision_ask"))
        if decision_ask is None:
            decision_ask = _safe_float(signal.get("entry_ask"))
        stake = _safe_float(signal.get("stake"))
        fair_value = _safe_float(signal.get("fair_value"))
        posted_limit = _safe_float(signal.get("posted_limit"))
        if posted_limit is None:
            posted_limit = _safe_float(signal.get("limit_price"))

        for policy in POLICIES:
            policy_limit = _policy_limit(policy, signal)
            for fill_model in (FILL_MODE_TOUCH, FILL_MODE_QUEUE_ADJUSTED):
                fill = _simulate_policy_fill(
                    policy=policy,
                    policy_limit=policy_limit,
                    decision_ask=decision_ask,
                    snapshots=snapshots,
                    fill_model=fill_model,
                    queue_buffer=queue_buffer,
                    max_fill_seconds=max_fill_seconds,
                )
                fill_price = _safe_float(fill.get("fill_price"))
                filled = bool(fill.get("filled"))
                profit, roi = _profit_for_fill(
                    stake=stake,
                    fill_price=fill_price if filled else None,
                    won=won,
                )
                model_ev = _model_ev_per_stake(fair_value, fill_price if filled else policy_limit)
                model_edge = (
                    round(fair_value - fill_price, 6)
                    if fair_value is not None and filled and fill_price is not None
                    else round(fair_value - policy_limit, 6)
                    if fair_value is not None and policy_limit is not None
                    else None
                )

                out.append(
                    {
                        "mode": row_mode,
                        "session_date": session_date,
                        "bet_id": bet_id,
                        "policy": policy,
                        "fill_model": fill_model,
                        "game_pk": _safe_int(signal.get("game_pk")),
                        "away_abbrev": signal.get("away_abbrev"),
                        "home_abbrev": signal.get("home_abbrev"),
                        "line": signal.get("line"),
                        "inning": _safe_int(signal.get("inning")),
                        "state_value_strategy": signal.get("state_value_strategy"),
                        "current_state_value_edge": _safe_float(signal.get("current_state_value_edge")),
                        "shadow_phantom_risk_band": signal.get("shadow_phantom_risk_band"),
                        "stake": stake,
                        "decision_ask": decision_ask,
                        "posted_limit": posted_limit,
                        "policy_limit": policy_limit,
                        "filled": filled,
                        "marketable_at_entry": bool(fill.get("marketable_at_entry")),
                        "fill_seconds": fill.get("fill_seconds"),
                        "fill_price": fill_price if filled else None,
                        "fill_source": fill.get("fill_source"),
                        "won": won,
                        "realized_profit": profit,
                        "realized_roi": roi,
                        "fair_value": fair_value,
                        "model_edge_at_fill": model_edge,
                        "model_ev_per_stake": model_ev,
                        "entry_spread": _safe_float(signal.get("t0_spread")),
                        "capture_window_seconds": fill.get("capture_window_seconds"),
                        "queue_buffer_cents": queue_buffer_cents,
                    }
                )
    out.sort(
        key=lambda r: (
            str(r.get("session_date") or ""),
            str(r.get("bet_id") or ""),
            str(r.get("policy") or ""),
            str(r.get("fill_model") or ""),
        )
    )
    return out


def _mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return round(mean(values), 6)


def _values(rows: List[Dict[str, Any]], field: str) -> List[float]:
    vals: List[float] = []
    for row in rows:
        value = _safe_float(row.get(field))
        if value is not None:
            vals.append(value)
    return vals


def summarize_group(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    filled = [r for r in rows if bool(r.get("filled"))]
    wins = [r for r in filled if r.get("won") is True]
    losses = [r for r in filled if r.get("won") is False]
    stake_deployed = sum(_safe_float(r.get("stake")) or 0.0 for r in filled)
    profit = sum(_safe_float(r.get("realized_profit")) or 0.0 for r in filled)
    return {
        "signals": len(rows),
        "filled": len(filled),
        "fill_rate": round(len(filled) / len(rows), 6) if rows else None,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_if_filled": round(len(wins) / len(filled), 6) if filled else None,
        "stake_deployed": round(stake_deployed, 6),
        "realized_profit": round(profit, 6),
        "realized_roi": round(profit / stake_deployed, 6) if stake_deployed > 0 else None,
        "avg_fill_price": _mean(_values(filled, "fill_price")),
        "avg_fill_seconds": _mean(_values(filled, "fill_seconds")),
        "avg_model_edge_at_fill": _mean(_values(rows, "model_edge_at_fill")),
        "avg_model_ev_per_stake": _mean(_values(rows, "model_ev_per_stake")),
        "marketable_at_entry": sum(1 for r in rows if bool(r.get("marketable_at_entry"))),
    }


def build_summary(
    rows: List[Dict[str, Any]],
    *,
    config: Dict[str, Any],
    warnings: List[str],
) -> Dict[str, Any]:
    by_policy_fill_model: Dict[str, Dict[str, Any]] = {}
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get("policy") or ""), str(row.get("fill_model") or ""))].append(row)

    for (policy, fill_model), group in sorted(grouped.items()):
        by_policy_fill_model[f"{policy}__{fill_model}"] = summarize_group(group)

    # Delta versus current limit within each fill model.
    for fill_model in (FILL_MODE_TOUCH, FILL_MODE_QUEUE_ADJUSTED):
        base_key = f"current_limit__{fill_model}"
        base = by_policy_fill_model.get(base_key, {})
        base_profit = _safe_float(base.get("realized_profit")) or 0.0
        base_filled = _safe_int(base.get("filled")) or 0
        for policy in POLICIES:
            key = f"{policy}__{fill_model}"
            row = by_policy_fill_model.get(key)
            if not row:
                continue
            row["profit_delta_vs_current_limit"] = round(
                (_safe_float(row.get("realized_profit")) or 0.0) - base_profit,
                6,
            )
            row["fills_delta_vs_current_limit"] = (
                (_safe_int(row.get("filled")) or 0) - base_filled
            )

    by_policy = {
        policy: {
            fill_model: by_policy_fill_model.get(f"{policy}__{fill_model}", {})
            for fill_model in (FILL_MODE_TOUCH, FILL_MODE_QUEUE_ADJUSTED)
        }
        for policy in POLICIES
    }

    return {
        "generated_at_utc": _now_iso(),
        "description": (
            "Queue-aware execution replay. Offline only; compares price policies "
            "by realized profit/ROI and model EV, not fill rate alone."
        ),
        "config": config,
        "counts": {
            "rows": len(rows),
            "unique_signals": len({(r.get("mode"), r.get("bet_id")) for r in rows}),
            "warnings": len(warnings),
        },
        "by_policy": by_policy,
        "by_policy_fill_model": by_policy_fill_model,
        "warnings": warnings[:200],
    }


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_outputs(output_root: Path, rows: List[Dict[str, Any]], summary: Dict[str, Any]) -> Dict[str, str]:
    output_root.mkdir(parents=True, exist_ok=True)
    summary_path = output_root / "queue_aware_execution_replay_summary.json"
    rows_jsonl = output_root / "queue_aware_execution_replay_rows.jsonl"
    rows_csv = output_root / "queue_aware_execution_replay_rows.csv"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    write_jsonl(rows_jsonl, rows)
    write_csv(rows_csv, rows)
    return {
        "summary": str(summary_path),
        "rows_jsonl": str(rows_jsonl),
        "rows_csv": str(rows_csv),
    }


def print_console_summary(summary: Dict[str, Any]) -> None:
    print("\nQueue-aware execution replay")
    print("Policy                Fill model       Filled  Fill%    Profit     ROI    dProfit")
    for policy in POLICIES:
        for fill_model in (FILL_MODE_TOUCH, FILL_MODE_QUEUE_ADJUSTED):
            metrics = summary["by_policy"].get(policy, {}).get(fill_model, {})
            fill_rate = metrics.get("fill_rate")
            roi = metrics.get("realized_roi")
            print(
                "{:<21} {:<15} {:>6} {:>6} {:>9} {:>7} {:>9}".format(
                    policy,
                    fill_model,
                    metrics.get("filled", 0),
                    "n/a" if fill_rate is None else f"{fill_rate * 100:.1f}%",
                    f"{float(metrics.get('realized_profit') or 0.0):+.2f}",
                    "n/a" if roi is None else f"{roi * 100:.1f}%",
                    f"{float(metrics.get('profit_delta_vs_current_limit') or 0.0):+.2f}",
                )
            )
    print("")


def _validate_args(args: argparse.Namespace) -> None:
    if args.min_date:
        datetime.strptime(args.min_date, "%Y-%m-%d")
    if args.max_date:
        datetime.strptime(args.max_date, "%Y-%m-%d")
    if args.min_date and args.max_date and args.min_date > args.max_date:
        raise SystemExit("--min-date must be <= --max-date")
    if args.queue_buffer_cents < 0:
        raise SystemExit("--queue-buffer-cents must be >= 0")
    if args.max_fill_seconds < 0:
        raise SystemExit("--max-fill-seconds must be >= 0")


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    _validate_args(args)
    warnings: List[str] = []
    signal_rows = _read_jsonl(args.signals_master)
    snapshot_rows = _read_jsonl(args.snapshots)
    if not signal_rows:
        warnings.append(f"no signal rows loaded from {args.signals_master}")
    if not snapshot_rows:
        warnings.append(f"no snapshot rows loaded from {args.snapshots}")
    snapshot_map = _build_snapshot_map(snapshot_rows)

    rows = build_replay_rows(
        signal_rows,
        snapshot_map,
        mode=args.mode,
        min_date=args.min_date,
        max_date=args.max_date,
        queue_buffer_cents=args.queue_buffer_cents,
        max_fill_seconds=args.max_fill_seconds,
        include_unsettled=args.include_unsettled,
    )
    if args.strict and not rows:
        raise SystemExit("strict mode failed: no replay rows produced")

    summary = build_summary(
        rows,
        config={
            "mode": args.mode,
            "min_date": args.min_date or None,
            "max_date": args.max_date or None,
            "signals_master": str(args.signals_master),
            "snapshots": str(args.snapshots),
            "queue_buffer_cents": args.queue_buffer_cents,
            "max_fill_seconds": args.max_fill_seconds,
            "include_unsettled": bool(args.include_unsettled),
            "profit_model": "constant_usdc_stake; shares=stake/fill_price; win_payout=shares",
        },
        warnings=warnings,
    )
    paths = write_outputs(args.output_root, rows, summary)
    print(f"Wrote {paths['summary']}")
    print(f"Wrote {paths['rows_jsonl']}")
    print(f"Wrote {paths['rows_csv']}")
    print_console_summary(summary)


if __name__ == "__main__":
    main()
