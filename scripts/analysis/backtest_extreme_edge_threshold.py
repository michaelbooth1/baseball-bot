#!/usr/bin/env python3
"""
backtest_extreme_edge_threshold.py -- TR19 walk-forward certification.

Counterfactual replay: given the historical live bets we actually placed,
what would cumulative P&L have been under different `extreme_edge_max`
thresholds? Sweeps thresholds 0.18 / 0.20 / 0.22 / 0.25 / 0.30 / inf and
reports kept-vs-blocked decomposition with W/L for each cohort.

Why a counterfactual replay (not the walk_forward_runner harness):
The TR19 change is a single gate threshold, not a model retraining. The
walk_forward_runner needs >=17 days of training data per window and our
unified live signals_master only has 15 unique dates -- it plans 0 windows.
For a single-threshold change the right tool is "would this gate have
blocked bets that lost money in reality, and let through bets that won?"

Data sources (preferred -> fallback):
  1. data/live_trading/sessions/<date>_session.json   (full schema, all dates with sessions)
  2. data/live_trading/master_ledger.jsonl             (merged-by-bet_id for missing dates)
  3. data/live_trading/candidate_universe/<date>_outcomes.jsonl (won/final backfill)

Usage:
    python scripts/analysis/backtest_extreme_edge_threshold.py
    python scripts/analysis/backtest_extreme_edge_threshold.py --start 2026-04-04
    python scripts/analysis/backtest_extreme_edge_threshold.py --thresholds 0.18 0.20 0.22 0.25 0.30
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_DIR = Path(__file__).resolve().parents[2]
SESSIONS_DIR = PROJECT_DIR / "data" / "live_trading" / "sessions"
LEDGER_PATH = PROJECT_DIR / "data" / "live_trading" / "master_ledger.jsonl"
OUTCOMES_DIR = PROJECT_DIR / "data" / "live_trading" / "candidate_universe"


def _ledger_bets_by_date() -> Dict[str, List[dict]]:
    """Reconstruct bets by date from master_ledger.jsonl, merging all rows
    per bet_id, then backfilling won/final from outcomes JSONL or profit sign."""
    rows = [
        json.loads(l)
        for l in LEDGER_PATH.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    merged: Dict[str, dict] = defaultdict(dict)
    for r in rows:
        bid = r.get("bet_id")
        if not bid:
            continue
        for k, v in r.items():
            if v is not None and v != "":
                merged[bid][k] = v
    by_date: Dict[str, List[dict]] = defaultdict(list)
    for bid, b in merged.items():
        d = b.get("placed_at", "")[:10]
        if not d:
            continue
        by_date[d].append(b)
    # Backfill won/profit per date using outcomes JSONL
    for d, bets in by_date.items():
        op = OUTCOMES_DIR / f"{d}_outcomes.jsonl"
        outcomes = []
        if op.exists():
            outcomes = [
                json.loads(l)
                for l in op.read_text(encoding="utf-8").splitlines()
                if l.strip()
            ]
        for b in bets:
            if b.get("order_status") != "filled":
                continue
            if b.get("won") is None:
                match = next(
                    (o for o in outcomes
                     if o.get("game_pk") == b.get("game_pk")
                     and o.get("line") == b.get("line")),
                    None,
                )
                if match is not None:
                    b.setdefault("final_away", match.get("final_away"))
                    b.setdefault("final_home", match.get("final_home"))
                    b.setdefault("final_total", match.get("final_total"))
                    b["won"] = bool(match.get("over_hit"))
            if b.get("won") is None:
                profit = b.get("profit")
                if isinstance(profit, (int, float)) and profit != 0:
                    b["won"] = profit > 0
            if b.get("won") is True and b.get("profit") in (None, 0, 0.0):
                try:
                    fp = float(b.get("actual_fill_price")
                               or b.get("fill_price")
                               or b["entry_ask"])
                    stake = float(b["stake"])
                    payout = stake / fp if fp > 0 else 0.0
                    b["profit"] = round(payout - stake, 2)
                except Exception:
                    pass
    return dict(by_date)


def load_settled_filled(start: date, end: date) -> List[dict]:
    """All settled filled bets in [start, end] inclusive, preferring session
    JSONs and falling back to ledger reconstruction for missing dates."""
    seen_bet_ids = set()
    out: List[dict] = []
    cur = start
    ledger_by_date = _ledger_bets_by_date()
    while cur <= end:
        d = cur.isoformat()
        sp = SESSIONS_DIR / f"{d}_session.json"
        if sp.exists():
            session = json.loads(sp.read_text(encoding="utf-8"))
            for b in session.get("bets", []):
                if b.get("order_status") != "filled":
                    continue
                if b.get("won") is None:
                    continue
                bid = b.get("bet_id")
                if bid in seen_bet_ids:
                    continue
                seen_bet_ids.add(bid)
                b["_session_date"] = d
                out.append(b)
        else:
            # Fallback: reconstructed from ledger
            for b in ledger_by_date.get(d, []):
                if b.get("order_status") != "filled":
                    continue
                if b.get("won") is None:
                    continue
                bid = b.get("bet_id")
                if bid in seen_bet_ids:
                    continue
                seen_bet_ids.add(bid)
                b["_session_date"] = d
                out.append(b)
        cur += timedelta(days=1)
    return out


def evaluate_threshold(bets: List[dict], threshold: float) -> Dict[str, object]:
    """Partition bets into kept vs blocked at `threshold` and aggregate."""
    kept = [b for b in bets if float(b.get("edge") or 0) <= threshold]
    blocked = [b for b in bets if float(b.get("edge") or 0) > threshold]
    kept_w = sum(1 for b in kept if b["won"])
    kept_l = len(kept) - kept_w
    kept_pnl = sum(float(b.get("profit") or 0) for b in kept)
    blocked_w = sum(1 for b in blocked if b["won"])
    blocked_l = len(blocked) - blocked_w
    blocked_pnl = sum(float(b.get("profit") or 0) for b in blocked)
    kept_stake = sum(float(b.get("stake") or 0) for b in kept)
    kept_roi = (kept_pnl / kept_stake * 100) if kept_stake else 0.0
    return {
        "threshold": threshold,
        "kept_n": len(kept),
        "kept_w": kept_w,
        "kept_l": kept_l,
        "kept_wr": (kept_w / len(kept) * 100) if kept else 0.0,
        "kept_pnl": kept_pnl,
        "kept_stake": kept_stake,
        "kept_roi": kept_roi,
        "blocked_n": len(blocked),
        "blocked_w": blocked_w,
        "blocked_l": blocked_l,
        "blocked_wr": (blocked_w / len(blocked) * 100) if blocked else 0.0,
        "blocked_pnl": blocked_pnl,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=str, default=None,
                        help="Start date YYYY-MM-DD. Default: 30 days before --end.")
    parser.add_argument("--end", type=str, default=None,
                        help="End date YYYY-MM-DD. Default: today.")
    parser.add_argument(
        "--thresholds", type=float, nargs="+",
        default=[0.18, 0.20, 0.22, 0.25, 0.30, 99.0],
        help="extreme_edge_max thresholds to sweep (99 = no gate).",
    )
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = (date.fromisoformat(args.start) if args.start
             else end - timedelta(days=30))

    print(f"Loading settled-filled bets from {start} to {end}")
    bets = load_settled_filled(start, end)
    if not bets:
        print("  no bets found in window")
        return
    by_date = defaultdict(list)
    for b in bets:
        by_date[b["_session_date"]].append(b)
    print(f"  loaded {len(bets)} settled filled bets across {len(by_date)} dates")
    actual_w = sum(1 for b in bets if b["won"])
    actual_pnl = sum(float(b.get("profit") or 0) for b in bets)
    print(f"  actual realized: {actual_w}W/{len(bets)-actual_w}L  "
          f"WR={actual_w/len(bets)*100:.0f}%  PnL=${actual_pnl:+.2f}")
    print()

    # Per-edge-cohort breakdown
    print("=" * 80)
    print("EDGE COHORT BREAKDOWN (informational)")
    print("=" * 80)
    cohorts = [
        ("<=0.15",    lambda e: e <= 0.15),
        ("0.15-0.18", lambda e: 0.15 < e <= 0.18),
        ("0.18-0.20", lambda e: 0.18 < e <= 0.20),
        ("0.20-0.22", lambda e: 0.20 < e <= 0.22),
        ("0.22-0.25", lambda e: 0.22 < e <= 0.25),
        ("0.25-0.30", lambda e: 0.25 < e <= 0.30),
        (">0.30",     lambda e: e > 0.30),
    ]
    print(f"{'cohort':<12} {'n':>3} {'W':>3} {'L':>3} {'WR%':>5} "
          f"{'PnL':>9} {'stake':>8} {'ROI%':>7} {'avg_fill':>9}")
    for name, pred in cohorts:
        sub = [b for b in bets if pred(float(b.get("edge") or 0))]
        if not sub:
            print(f"{name:<12}   0")
            continue
        w = sum(1 for b in sub if b["won"])
        l = len(sub) - w
        pnl = sum(float(b.get("profit") or 0) for b in sub)
        stake = sum(float(b.get("stake") or 0) for b in sub)
        roi = pnl / stake * 100 if stake else 0
        fps = [float(b.get("actual_fill_price") or b.get("fill_price")
                     or b.get("entry_ask") or 0) for b in sub]
        afp = sum(fps) / len(fps) if fps else 0
        print(f"{name:<12} {len(sub):>3} {w:>3} {l:>3} {w/len(sub)*100:>4.0f}% "
              f"${pnl:>7.2f} ${stake:>6.2f} {roi:>6.1f}% {afp:>9.3f}")
    print()

    # Threshold sweep
    print("=" * 80)
    print("THRESHOLD SWEEP -- kept vs blocked under each extreme_edge_max")
    print("=" * 80)
    print(f"{'threshold':<10} {'kept_n':>6} {'kept_W/L':>9} {'kept_WR%':>9} "
          f"{'kept_PnL':>10} {'kept_ROI%':>10} | "
          f"{'blocked_n':>9} {'blocked_W/L':>11} {'blocked_PnL':>12}")
    print("-" * 110)
    rows = []
    for t in sorted(args.thresholds):
        res = evaluate_threshold(bets, t)
        rows.append(res)
        label = "no gate" if t >= 1.0 else f"{t:.2f}"
        print(f"{label:<10} "
              f"{res['kept_n']:>6} "
              f"{res['kept_w']:>3}/{res['kept_l']:<5} "
              f"{res['kept_wr']:>8.0f}% "
              f"${res['kept_pnl']:>8.2f} "
              f"{res['kept_roi']:>9.1f}% | "
              f"{res['blocked_n']:>9} "
              f"{res['blocked_w']:>3}/{res['blocked_l']:<7} "
              f"${res['blocked_pnl']:>10.2f}")
    print()

    # Summary: which threshold is best by kept-PnL?
    best = max(rows, key=lambda r: r["kept_pnl"])
    nogate = next(r for r in rows if r["threshold"] >= 1.0)
    print(f"Best threshold by kept-PnL: {best['threshold']:.2f}  "
          f"(${best['kept_pnl']:+.2f} on {best['kept_n']} kept bets, "
          f"ROI {best['kept_roi']:+.1f}%)")
    delta = best["kept_pnl"] - nogate["kept_pnl"]
    print(f"Improvement vs no-gate: ${delta:+.2f}  "
          f"(blocks {nogate['kept_n']-best['kept_n']} bets that were net "
          f"{nogate['kept_pnl']-best['kept_pnl']:+.2f} in reality)")
    # Specifically: TR17 vs TR19 deltas
    by_t = {r["threshold"]: r for r in rows}
    if 0.30 in by_t and 0.22 in by_t:
        tr17 = by_t[0.30]
        tr19 = by_t[0.22]
        print()
        print("TR17 (0.30) vs TR19 (0.22) head-to-head:")
        print(f"  TR17: kept={tr17['kept_n']} W/L={tr17['kept_w']}/{tr17['kept_l']} "
              f"PnL=${tr17['kept_pnl']:+.2f} ROI={tr17['kept_roi']:+.1f}%")
        print(f"  TR19: kept={tr19['kept_n']} W/L={tr19['kept_w']}/{tr19['kept_l']} "
              f"PnL=${tr19['kept_pnl']:+.2f} ROI={tr19['kept_roi']:+.1f}%")
        d_kept = tr17["kept_n"] - tr19["kept_n"]
        d_pnl = tr19["kept_pnl"] - tr17["kept_pnl"]
        print(f"  TR19 blocks {d_kept} additional bets vs TR17, "
              f"changing P&L by ${d_pnl:+.2f}")


if __name__ == "__main__":
    main()
