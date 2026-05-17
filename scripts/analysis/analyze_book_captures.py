#!/usr/bin/env python3
"""
Analyze post-signal book captures for limit order pricing calibration.

Reads the JSONL files written by PaperTradingEngine._start_book_capture() and
produces a report showing:
  - How quickly the ask retraces after a signal (fill window estimation)
  - Optimal limit price candidates at each elapsed-time threshold
  - Fill probability estimates by spread bucket
  - Estimated P&L improvement from limit vs taker pricing
  - Ask velocity in first 5s (predicts fill likelihood)
  - Simulated vs actual fill comparison (requires --sessions-root)
  - Multi-line same-game exposure (correlated risk)
  - Per-session performance breakdown
  - Crossed/invalid book detection

Usage:
    python scripts/analysis/analyze_book_captures.py
    python scripts/analysis/analyze_book_captures.py --date 2026-04-13
    python scripts/analysis/analyze_book_captures.py --spread-factor 0.65
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CAPTURES_ROOT = PROJECT_DIR / "data" / "live_trading" / "book_captures"
DEFAULT_LEDGER_PATH   = PROJECT_DIR / "data" / "live_trading" / "master_ledger.jsonl"
DEFAULT_SESSIONS_ROOT = PROJECT_DIR / "data" / "live_trading" / "sessions"
DEFAULT_MIN_ANALYSIS_DATE = "2026-04-20"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_capture(path: Path) -> Optional[dict]:
    """Load a single capture JSONL file. Returns dict with 'signal' and 'snapshots'."""
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    if not lines:
        return None
    header = json.loads(lines[0])
    if header.get("type") != "signal":
        return None
    snapshots = []
    for line in lines[1:]:
        try:
            rec = json.loads(line)
            if rec.get("type") == "snapshot":
                snapshots.append(rec)
        except Exception:
            continue
    return {"signal": header, "snapshots": snapshots}


def load_all_captures(
    root: Path,
    date: Optional[str] = None,
    min_date: Optional[str] = None,
) -> List[dict]:
    captures = []
    if date:
        dirs = [root / date] if (root / date).exists() else []
    else:
        dirs = sorted(root.iterdir()) if root.exists() else []
        if min_date:
            dirs = [d for d in dirs if d.is_dir() and d.name >= min_date]
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.jsonl")):
            c = load_capture(f)
            if c:
                captures.append(c)
    return captures


def load_sessions(
    root: Path,
    date: Optional[str] = None,
    min_date: Optional[str] = None,
) -> Dict[str, dict]:
    """Load session JSONs and return {bet_id: bet_dict} across all sessions."""
    bets: Dict[str, dict] = {}
    if not root.exists():
        return bets
    pattern = f"{date}_session.json" if date else "*_session.json"
    for f in sorted(root.glob(pattern)):
        if not date and min_date:
            session_date = f.name.replace("_session.json", "")
            if session_date < min_date:
                continue
        try:
            sess = json.loads(f.read_text(encoding="utf-8"))
            for bet in sess.get("bets", []):
                bets[bet["bet_id"]] = bet
        except Exception:
            continue
    return bets


def load_ledger(path: Path) -> Dict[str, dict]:
    """Build bet_id -> bet dict from master ledger."""
    bets = {}
    if not path.exists():
        return bets
    for line in path.read_text(encoding="utf-8").strip().splitlines():
        try:
            b = json.loads(line)
            bets[b["bet_id"]] = b
        except Exception:
            continue
    return bets


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def _ask_at_elapsed(snapshots: List[dict], target_s: float) -> Optional[float]:
    """Return ask price from the snapshot closest to target_s elapsed."""
    best = None
    best_diff = float("inf")
    for snap in snapshots:
        diff = abs(snap.get("elapsed_s", 0) - target_s)
        if diff < best_diff:
            best_diff = diff
            ask = snap.get("book", {}).get("best_ask")
            if ask is not None:
                best = ask
    return best


def _bid_at_elapsed(snapshots: List[dict], target_s: float) -> Optional[float]:
    best = None
    best_diff = float("inf")
    for snap in snapshots:
        diff = abs(snap.get("elapsed_s", 0) - target_s)
        if diff < best_diff:
            best_diff = diff
            bid = snap.get("book", {}).get("best_bid")
            if bid is not None:
                best = bid
    return best


def _simulated_limit_fill(snapshots: List[dict], limit_price: float,
                           max_elapsed: float = 60.0) -> Optional[float]:
    """Return elapsed_s when a limit order at limit_price would have filled.

    A limit BUY fills when ask <= limit_price (someone willing to sell at or
    below our bid). Returns None if it didn't fill within max_elapsed seconds.
    """
    for snap in snapshots:
        elapsed = snap.get("elapsed_s", 999)
        if elapsed > max_elapsed:
            break
        ask = snap.get("book", {}).get("best_ask")
        if ask is not None and ask <= limit_price:
            return elapsed
    return None


def _spread_bucket(spread: float) -> str:
    if spread < 0:
        return "crossed (bid>ask)"
    if spread < 0.03:
        return "tight   (<0.03)"
    if spread < 0.08:
        return "moderate(0.03-0.08)"
    if spread < 0.15:
        return "wide    (0.08-0.15)"
    return "very_wide(>=0.15)"


def _ask_velocity(snapshots: List[dict], window_s: float = 5.0) -> Optional[float]:
    """Return ask change in cents from t=0 to window_s.

    Positive = ask fell (toward our limit, good for fill).
    Negative = ask rose (away from our limit, bad for fill).
    """
    ask0 = None
    for s in snapshots:
        if s.get("seq") == 0:
            ask0 = s.get("book", {}).get("best_ask")
            break
    if ask0 is None and snapshots:
        ask0 = snapshots[0].get("book", {}).get("best_ask")
    if ask0 is None:
        return None
    ask_w = _ask_at_elapsed(snapshots, window_s)
    if ask_w is None:
        return None
    return round((ask0 - ask_w) * 100, 1)  # positive = ask dropped (favorable)


def _is_crossed_book(snapshots: List[dict]) -> bool:
    """Return True if the t=0 snapshot has bid >= ask (invalid book)."""
    t0 = next((s for s in snapshots if s.get("seq") == 0), None)
    if t0 is None and snapshots:
        t0 = snapshots[0]
    if t0 is None:
        return False
    book = t0.get("book", {})
    ask = book.get("best_ask")
    bid = book.get("best_bid")
    if ask is None or bid is None:
        return False
    return bid >= ask


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze(captures: List[dict], ledger: Dict[str, dict],
            spread_factor: float = 0.65, stake: float = 100.0,
            sessions: Optional[Dict[str, dict]] = None) -> None:

    if not captures:
        print("No capture files found.")
        return

    print(f"\n{'='*80}")
    print(f"  BOOK CAPTURE ANALYSIS — {len(captures)} signals")
    print(f"  Limit price formula: bid + spread × {spread_factor:.2f}  (spread_factor)")
    print(f"{'='*80}\n")

    # ---- Per-signal table ----
    header = (f"{'Bet':<32} {'Line':<6} {'Inn':<4} {'Ask0':<6} {'Bid0':<6} "
              f"{'Spread':<8} {'Limit':<6} {'Ask@2s':<8} {'Ask@5s':<8} "
              f"{'FillT':<8} {'SavedC':<8} {'Result':<6}")
    print(header)
    print("-" * len(header))

    rows = []
    for cap in captures:
        sig = cap["signal"]
        snaps = cap["snapshots"]
        bet_id = sig.get("bet_id", "")
        bet = ledger.get(bet_id, {})
        result = ("WIN" if bet.get("won") else "LOSS") if bet.get("settled") else "PENDING"

        # t=0 values
        t0_snap = next((s for s in snaps if s.get("seq") == 0), None)
        if t0_snap is None and snaps:
            t0_snap = snaps[0]

        ask0 = (t0_snap["book"].get("best_ask") if t0_snap else None) or sig.get("entry_ask")
        bid0 = (t0_snap["book"].get("best_bid") if t0_snap else None)
        if ask0 is None or bid0 is None:
            continue

        spread0 = ask0 - bid0
        # Computed limit price
        limit_raw = bid0 + spread0 * spread_factor
        fv = sig.get("fair_value", 1.0)
        min_edge = 0.16 if float(sig.get("line", "7.5")) >= 8.5 else 0.15
        limit_ceil = fv - min_edge
        limit_price = round(min(limit_raw, limit_ceil, ask0 - 0.01), 2)
        limit_price = max(limit_price, round(bid0 + 0.01, 2))

        # Ask evolution
        ask_2s = _ask_at_elapsed(snaps, 2.0)
        ask_5s = _ask_at_elapsed(snaps, 5.0)

        # Simulated fill time
        fill_t = _simulated_limit_fill(snaps, limit_price, max_elapsed=30.0)
        fill_str = f"{fill_t:.1f}s" if fill_t is not None else "NO_FILL"

        # Cents saved vs taker at ask0
        saved_c = round((ask0 - limit_price) * 100, 1) if fill_t is not None else 0.0

        game = f"{sig.get('away_abbrev','')}@{sig.get('home_abbrev','')}"
        label = f"{game} {sig.get('line','')}"

        rows.append({
            "bet_id": bet_id,
            "label": label,
            "line": sig.get("line", ""),
            "inning": sig.get("inning", 0),
            "fair_value": fv,
            "ask0": ask0,
            "bid0": bid0,
            "spread0": spread0,
            "limit_price": limit_price,
            "ask_2s": ask_2s,
            "ask_5s": ask_5s,
            "fill_t": fill_t,
            "saved_c": saved_c,
            "result": result,
            "won": bet.get("won"),
            "settled": bet.get("settled", False),
            "profit_at_ask": bet.get("profit"),
            "stake": bet.get("stake", stake),
        })

        ask2_str = f"{ask_2s:.3f}" if ask_2s else "  ---"
        ask5_str = f"{ask_5s:.3f}" if ask_5s else "  ---"
        print(f"{label:<32} {sig.get('line',''):<6} {sig.get('inning',0):<4} "
              f"{ask0:<6.3f} {bid0:<6.3f} {spread0:<8.3f} "
              f"{limit_price:<6.3f} {ask2_str:<8} {ask5_str:<8} "
              f"{fill_str:<8} {saved_c:<8.1f} {result:<6}")

    if not rows:
        print("No rows with complete data.")
        return

    # ---- Summary statistics ----
    filled = [r for r in rows if r["fill_t"] is not None]
    unfilled = [r for r in rows if r["fill_t"] is None]
    fill_rate = len(filled) / len(rows) * 100 if rows else 0

    print(f"\n{'='*80}")
    print(f"  SUMMARY")
    print(f"{'='*80}")
    print(f"  Total signals:     {len(rows)}")
    print(f"  Filled (<= 30s):   {len(filled)}  ({fill_rate:.0f}%)")
    print(f"  Not filled (>30s): {len(unfilled)}")

    if filled:
        avg_fill_t = sum(r["fill_t"] for r in filled) / len(filled)
        avg_saved  = sum(r["saved_c"] for r in filled) / len(filled)
        print(f"  Avg fill time:     {avg_fill_t:.1f}s")
        print(f"  Avg cents saved:   {avg_saved:.1f}¢  (vs taker at ask)")

    # ---- Spread bucket analysis ----
    print(f"\n  Fill rate by spread bucket (spread_factor={spread_factor:.2f}):")
    print(f"  {'Bucket':<22} {'N':>4}  {'Filled':>7}  {'Fill%':>6}  {'AvgSave':>8}  {'AvgFillT':>9}")
    print(f"  {'-'*65}")
    buckets: Dict[str, List[dict]] = {}
    for r in rows:
        bkt = _spread_bucket(r["spread0"])
        buckets.setdefault(bkt, []).append(r)
    for bkt in sorted(buckets.keys()):
        brows = buckets[bkt]
        bf = [r for r in brows if r["fill_t"] is not None]
        pct = len(bf) / len(brows) * 100 if brows else 0
        avg_save = sum(r["saved_c"] for r in bf) / len(bf) if bf else 0
        avg_ft   = sum(r["fill_t"] for r in bf) / len(bf) if bf else 0
        print(f"  {bkt:<22} {len(brows):>4}  {len(bf):>7}  {pct:>6.0f}%  "
              f"{avg_save:>7.1f}¢  {avg_ft:>8.1f}s")

    # ---- P&L impact simulation ----
    print(f"\n  P&L IMPACT SIMULATION (settled bets only):")
    settled_rows = [r for r in rows if r["settled"] and r["won"] is not None]
    if not settled_rows:
        print("  No settled bets in this capture set yet.")
    else:
        taker_profit = sum(r.get("profit_at_ask", 0) or 0 for r in settled_rows)
        limit_profit = 0.0
        for r in settled_rows:
            stk = r["stake"]
            if r["fill_t"] is not None:
                # Filled at limit_price
                if r["won"]:
                    limit_profit += stk / r["limit_price"] - stk
                else:
                    limit_profit -= stk
            else:
                # Missed bet — no P&L either way (0)
                pass  # conservatively assume we cancel and miss the bet

        wins_taker  = sum(1 for r in settled_rows if r["won"])
        wins_limit  = sum(1 for r in settled_rows if r["won"] and r["fill_t"] is not None)
        print(f"  Settled bets:            {len(settled_rows)}")
        print(f"  Taker profit (at ask0):  ${taker_profit:.2f}  ({wins_taker}W)")
        print(f"  Limit profit (simulated):${limit_profit:.2f}  ({wins_limit}W + {len(unfilled)} missed)")
        delta = limit_profit - taker_profit
        print(f"  Net improvement:         ${delta:+.2f}")

    # ---- Ask retrace analysis ----
    print(f"\n  ASK RETRACE ANALYSIS:")
    print(f"  {'Bet':<32} {'Ask@0':<7} {'Ask@2s':<8} {'Ask@5s':<8} "
          f"{'Drop@2s':<9} {'Drop@5s':<9} {'Spread':<8}")
    print(f"  {'-'*75}")
    for r in rows:
        if r["ask_2s"] is None and r["ask_5s"] is None:
            continue
        drop2 = round((r["ask0"] - r["ask_2s"]) * 100, 1) if r["ask_2s"] else None
        drop5 = round((r["ask0"] - r["ask_5s"]) * 100, 1) if r["ask_5s"] else None
        d2str = f"{drop2:+.1f}¢" if drop2 is not None else "  ---"
        d5str = f"{drop5:+.1f}¢" if drop5 is not None else "  ---"
        a2str = f"{r['ask_2s']:.3f}" if r["ask_2s"] else "  ---"
        a5str = f"{r['ask_5s']:.3f}" if r["ask_5s"] else "  ---"
        print(f"  {r['label']:<32} {r['ask0']:<7.3f} {a2str:<8} {a5str:<8} "
              f"{d2str:<9} {d5str:<9} {r['spread0']:.3f}")

    # ---- Ask velocity analysis ----
    print(f"\n  ASK VELOCITY (first 5s after signal):")
    print(f"  Positive = ask DROPPED toward our limit (favorable)")
    print(f"  Negative = ask ROSE away from our limit (unfavorable)")
    print(f"  {'Bet':<32} {'Ask0':<7} {'Vel@1s':>8} {'Vel@5s':>8} {'Trend':>10} {'Result':<6}")
    print(f"  {'-'*75}")

    vel_rows = []
    for cap in captures:
        sig = cap["signal"]
        snaps = cap["snapshots"]
        bet_id = sig.get("bet_id", "")
        bet = ledger.get(bet_id, {})
        result = ("WIN" if bet.get("won") else "LOSS") if bet.get("settled") else "PENDING"

        t0_snap = next((s for s in snaps if s.get("seq") == 0), snaps[0] if snaps else None)
        if t0_snap is None:
            continue
        ask0 = t0_snap["book"].get("best_ask")
        if ask0 is None:
            continue

        v1 = _ask_velocity(snaps, 1.0)
        v5 = _ask_velocity(snaps, 5.0)
        if v5 is None:
            continue

        # Positive velocity = ask dropped (favorable for our limit)
        trend = "FALLING" if v5 > 0.5 else ("RISING" if v5 < -0.5 else "STABLE")
        trend_icon = "✓" if v5 > 0.5 else ("✗" if v5 < -0.5 else "—")
        v1s = f"{v1:+.1f}¢" if v1 is not None else "  ---"
        v5s = f"{v5:+.1f}¢"
        game = f"{sig.get('away_abbrev','')}@{sig.get('home_abbrev','')}"
        label = f"{game} {sig.get('line','')}"
        print(f"  {label:<32} {ask0:<7.3f} {v1s:>8} {v5s:>8} {trend:>10} {result:<6}")
        vel_rows.append({"v5": v5, "v1": v1 or 0})

    if vel_rows:
        falling = sum(1 for r in vel_rows if r["v5"] > 0.5)
        rising  = sum(1 for r in vel_rows if r["v5"] < -0.5)
        stable  = len(vel_rows) - falling - rising
        avg_v5  = sum(r["v5"] for r in vel_rows) / len(vel_rows)
        print(f"\n  Ask rose (unfavorable):   {rising:>3}  ({rising/len(vel_rows)*100:.0f}%)")
        print(f"  Ask stable:               {stable:>3}  ({stable/len(vel_rows)*100:.0f}%)")
        print(f"  Ask fell (favorable):     {falling:>3}  ({falling/len(vel_rows)*100:.0f}%)")
        print(f"  Avg ask velocity @5s:     {avg_v5:+.1f}¢  ({'favorable' if avg_v5 > 0 else 'unfavorable'})")

    # ---- Simulated vs actual fill comparison ----
    if sessions:
        print(f"\n  SIMULATED vs ACTUAL FILL COMPARISON:")
        print(f"  {'Bet':<34} {'Sim30s':>7} {'Actual':>10} {'LimSim':>8} {'LimReal':>8} {'Delta':>7} {'Note':<20}")
        print(f"  {'-'*100}")

        sim_fill_actual_cancel = 0
        sim_nofill_actual_fill = 0
        agreement = 0

        for cap in captures:
            sig = cap["signal"]
            snaps = cap["snapshots"]
            bet_id = sig.get("bet_id", "")
            session_bet = sessions.get(bet_id)
            if session_bet is None:
                continue

            t0_snap = next((s for s in snaps if s.get("seq") == 0), snaps[0] if snaps else None)
            if t0_snap is None:
                continue
            ask0 = t0_snap["book"].get("best_ask")
            bid0 = t0_snap["book"].get("best_bid")
            if ask0 is None or bid0 is None:
                continue

            spread0 = ask0 - bid0
            limit_raw = bid0 + spread0 * spread_factor
            fv_sig = sig.get("fair_value", 1.0)
            line_val = float(sig.get("line", "7.5"))
            min_edge = 0.16 if line_val >= 8.5 else 0.15
            edge_cap = fv_sig - min_edge
            limit_sim = round(min(limit_raw, edge_cap, ask0 - 0.01), 2)
            limit_sim = max(limit_sim, round(bid0 + 0.01, 2))

            sim_fill_t = _simulated_limit_fill(snaps, limit_sim, max_elapsed=30.0)
            sim_filled = sim_fill_t is not None
            actual_filled = session_bet.get("order_status") == "filled"
            actual_limit = session_bet.get("limit_price")
            delta = round(limit_sim - (actual_limit or 0), 3) if actual_limit else None

            sim_str = f"{sim_fill_t:.1f}s" if sim_filled else "NO"
            act_str = "FILLED" if actual_filled else "cancelled"

            note = ""
            if sim_filled and not actual_filled:
                sim_fill_actual_cancel += 1
                note = "sim-fill missed?"
            elif not sim_filled and actual_filled:
                sim_nofill_actual_fill += 1
                note = "passive fill later"
            else:
                agreement += 1
                note = "agree"

            crossed_flag = " [CROSSED]" if spread0 < 0 else ""
            delta_str = f"{delta:+.3f}" if delta is not None else "  N/A"
            game = f"{sig.get('away_abbrev','')}@{sig.get('home_abbrev','')}"
            label = f"{game} {sig.get('line','')}"
            print(f"  {label:<34} {sim_str:>7} {act_str:>10} {limit_sim:>8.3f} "
                  f"{str(actual_limit or '?'):>8} {delta_str:>7} {note}{crossed_flag}")

        total_compared = sim_fill_actual_cancel + sim_nofill_actual_fill + agreement
        if total_compared:
            print(f"\n  Agreement:                   {agreement}/{total_compared}")
            print(f"  Sim-fill but actually miss:  {sim_fill_actual_cancel}  "
                  f"(order placed after window / limit too low / latency)")
            print(f"  No-sim-fill but actually hit:{sim_nofill_actual_fill}  "
                  f"(passive bid filled hours later)")

    # ---- Multi-line same-game exposure ----
    game_signals: Dict[int, List[dict]] = defaultdict(list)
    for cap in captures:
        sig = cap["signal"]
        game_pk = sig.get("game_pk")
        if game_pk:
            game_signals[game_pk].append(sig)

    multi_games = {pk: sigs for pk, sigs in game_signals.items() if len(sigs) > 1}
    if multi_games:
        print(f"\n  MULTI-LINE SAME-GAME EXPOSURE (correlated risk):")
        for pk, sigs in sorted(multi_games.items()):
            game = f"{sigs[0].get('away_abbrev')}@{sigs[0].get('home_abbrev')}"
            lines = [s.get("line") for s in sigs]
            stakes = [s.get("stake", stake) for s in sigs]
            total_exp = sum(stakes)
            print(f"  game_pk={pk}  {game}  lines={lines}  "
                  f"total_exposure=${total_exp:.0f}  ({len(sigs)} signals)")
    else:
        print(f"\n  MULTI-LINE SAME-GAME EXPOSURE: none (all signals on distinct games)")

    # ---- Per-session breakdown (requires sessions) ----
    if sessions:
        by_date: Dict[str, List[dict]] = defaultdict(list)
        for cap in captures:
            bid_parts = cap["signal"].get("bet_id", "").split("_")
            if bid_parts:
                by_date[bid_parts[0]].append(cap)

        if len(by_date) > 1:
            print(f"\n  PER-SESSION BREAKDOWN:")
            print(f"  {'Date':<12} {'Signals':>8} {'Sim-Fills':>10} {'Sim-Fill%':>10} "
                  f"{'ActFills':>9} {'ActFill%':>10} {'SigWins':>8} {'SigWin%':>8}")
            print(f"  {'-'*80}")
            for date in sorted(by_date.keys()):
                date_caps = by_date[date]
                n = len(date_caps)
                sim_f = 0
                act_f = 0
                sig_wins = 0
                for cap in date_caps:
                    bet_id = cap["signal"].get("bet_id", "")
                    snaps = cap["snapshots"]
                    t0 = next((s for s in snaps if s.get("seq") == 0), snaps[0] if snaps else None)
                    if t0:
                        ask0 = t0["book"].get("best_ask")
                        bid0 = t0["book"].get("best_bid")
                        if ask0 and bid0:
                            sp = ask0 - bid0
                            fv_sig = cap["signal"].get("fair_value", 1.0)
                            line_val = float(cap["signal"].get("line", "7.5"))
                            min_edge = 0.16 if line_val >= 8.5 else 0.15
                            edge_cap = fv_sig - min_edge
                            lp = round(min(bid0 + sp * spread_factor, edge_cap, ask0 - 0.01), 2)
                            lp = max(lp, round(bid0 + 0.01, 2))
                            if _simulated_limit_fill(snaps, lp, 30.0) is not None:
                                sim_f += 1
                    sb = sessions.get(bet_id, {})
                    if sb.get("order_status") == "filled":
                        act_f += 1
                    if sb.get("won"):
                        sig_wins += 1
                sim_pct = f"{sim_f/n*100:.0f}%" if n else "N/A"
                act_pct = f"{act_f/n*100:.0f}%" if n else "N/A"
                sw_pct  = f"{sig_wins/n*100:.0f}%" if n else "N/A"
                print(f"  {date:<12} {n:>8} {sim_f:>10} {sim_pct:>10} "
                      f"{act_f:>9} {act_pct:>10} {sig_wins:>8} {sw_pct:>8}")

    # ---- Tuning recommendation ----
    if len(rows) >= 5:
        print(f"\n  SPREAD_FACTOR TUNING RECOMMENDATION:")
        for sf in (0.50, 0.60, 0.65, 0.70, 0.80, 0.90):
            filled_at_sf = 0
            for r in rows:
                bid0 = r["bid0"]
                ask0 = r["ask0"]
                spread0 = r["spread0"]
                fv_sig = r.get("fair_value", 1.0)
                line_val = float(r.get("line", "7.5"))
                min_edge = 0.16 if line_val >= 8.5 else 0.15
                edge_cap = fv_sig - min_edge
                lp_raw = bid0 + spread0 * sf
                lp = round(min(lp_raw, edge_cap, ask0 - 0.01), 2)
                lp = max(lp, round(bid0 + 0.01, 2))
                ft = _simulated_limit_fill(
                    next(c["snapshots"] for c in captures if c["signal"]["bet_id"] == r["bet_id"]),
                    lp, max_elapsed=30.0
                )
                if ft is not None:
                    filled_at_sf += 1
            pct = filled_at_sf / len(rows) * 100
            marker = " <-- current" if abs(sf - spread_factor) < 0.01 else ""
            print(f"    spread_factor={sf:.2f}:  {filled_at_sf}/{len(rows)} filled  ({pct:.0f}%){marker}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Analyze post-signal book captures.")
    p.add_argument("--date", default=None, help="Specific date (YYYY-MM-DD) or all if omitted.")
    p.add_argument(
        "--min-date",
        default=DEFAULT_MIN_ANALYSIS_DATE,
        help=(
            "Minimum date floor for pooled multi-day analysis (YYYY-MM-DD). "
            "Ignored when --date is provided. "
            f"(default: {DEFAULT_MIN_ANALYSIS_DATE})"
        ),
    )
    p.add_argument("--captures-root", type=Path, default=None,
                   help="Root directory for book captures (default: live_trading).")
    p.add_argument("--ledger", type=Path, default=None,
                   help="Path to master_ledger.jsonl (default: live_trading).")
    p.add_argument("--sessions-root", type=Path, default=None,
                   help="Root directory for session JSONs (enables sim vs actual comparison).")
    p.add_argument("--paper", action="store_true",
                   help="Use paper_trading paths instead of live_trading.")
    p.add_argument("--spread-factor", type=float, default=0.65,
                   help="Position in spread for limit price (default: 0.65).")
    p.add_argument("--stake", type=float, default=20.0)
    args = p.parse_args()

    if args.paper:
        paper_root = PROJECT_DIR / "data" / "paper_trading"
        if args.captures_root is None:
            args.captures_root = paper_root / "book_captures"
        if args.ledger is None:
            args.ledger = paper_root / "master_ledger.jsonl"
    else:
        if args.captures_root is None:
            args.captures_root = DEFAULT_CAPTURES_ROOT
        if args.ledger is None:
            args.ledger = DEFAULT_LEDGER_PATH
        if args.sessions_root is None:
            args.sessions_root = DEFAULT_SESSIONS_ROOT

    min_date = None if args.date else args.min_date
    captures = load_all_captures(args.captures_root, args.date, min_date=min_date)
    ledger   = load_ledger(args.ledger)
    sessions = (
        load_sessions(args.sessions_root, args.date, min_date=min_date)
        if args.sessions_root
        else None
    )
    analyze(captures, ledger, spread_factor=args.spread_factor, stake=args.stake,
            sessions=sessions)



if __name__ == "__main__":
    main()
