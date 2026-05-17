#!/usr/bin/env python3
"""
analyze_window_2026_04_28_to_05_03.py -- Compare winners vs. losers in the
2026-04-28 to 2026-05-03 live trading window. One-shot analysis script.

Sources:
  - data/live_trading/sessions/<date>_session.json  (preferred; full schema)
  - data/live_trading/master_ledger.jsonl           (fallback for missing days
                                                     by merging all rows per
                                                     bet_id into one record)

Outputs to stdout:
  Section 1: Per-day overview
  Section 2: Winner vs loser feature contrast
  Section 3: Loss anatomy (per-loss diagnostic)
  Section 4: Cancel anatomy
  Section 5: Pattern flags + recommendations
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_DIR = Path(__file__).resolve().parents[2]
SESSIONS_DIR = PROJECT_DIR / "data" / "live_trading" / "sessions"
LEDGER_PATH = PROJECT_DIR / "data" / "live_trading" / "master_ledger.jsonl"
OUTCOMES_DIR = PROJECT_DIR / "data" / "live_trading" / "candidate_universe"

WINDOW_DATES = (
    "2026-04-28", "2026-04-29", "2026-04-30",
    "2026-05-01", "2026-05-02", "2026-05-03",
)


def _safe(v: Any, default: Any = None) -> Any:
    return v if v is not None else default


def _fmt(v: Any, fmt: str = "{:.3f}") -> str:
    if v is None:
        return "  -  "
    try:
        return fmt.format(float(v))
    except (TypeError, ValueError):
        return str(v)


def _load_session_bets() -> Dict[str, List[dict]]:
    """date -> list of bet dicts. Skips dates with no session JSON."""
    out: Dict[str, List[dict]] = {}
    for d in WINDOW_DATES:
        p = SESSIONS_DIR / f"{d}_session.json"
        if not p.exists():
            continue
        s = json.loads(p.read_text(encoding="utf-8"))
        for b in s.get("bets", []):
            b["_session_date"] = d
        out[d] = s.get("bets", [])
    return out


def _reconstruct_from_ledger(date: str) -> List[dict]:
    """Merge all ledger rows per bet_id; keep rows whose placed_at is on date.

    Strategy: for each bet_id seen on this date, walk all rows and merge
    non-null fields (later rows overwrite earlier). Settled state is on the
    later rows.
    """
    rows = [
        json.loads(l)
        for l in LEDGER_PATH.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    # Find bet_ids placed on this date
    placed_on_date = {
        r["bet_id"] for r in rows
        if r.get("bet_id") and r.get("placed_at", "").startswith(date)
    }
    merged: Dict[str, dict] = {}
    for r in rows:
        bid = r.get("bet_id")
        if not bid or bid not in placed_on_date:
            continue
        cur = merged.setdefault(bid, {})
        for k, v in r.items():
            if v is not None and v != "":
                cur[k] = v
    out = list(merged.values())
    # Backfill won/profit from per-day outcomes JSONL when ledger row lacks them
    outcomes_path = OUTCOMES_DIR / f"{date}_outcomes.jsonl"
    outcomes = []
    if outcomes_path.exists():
        outcomes = [
            json.loads(l)
            for l in outcomes_path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
    for b in out:
        b["_session_date"] = date
        if b.get("order_status") != "filled":
            continue
        # 1. Try outcomes JSONL match (has authoritative final scores + over_hit)
        if b.get("won") is None:
            match = next(
                (o for o in outcomes
                 if o.get("game_pk") == b.get("game_pk") and o.get("line") == b.get("line")),
                None,
            )
            if match is not None:
                b.setdefault("final_away", match.get("final_away"))
                b.setdefault("final_home", match.get("final_home"))
                b.setdefault("final_total", match.get("final_total"))
                b["won"] = bool(match.get("over_hit"))
        # 2. Fallback: derive won from ledger profit (settle rows append realized P&L
        #    even when the per-(game,line) outcomes row is missing).
        if b.get("won") is None:
            profit = b.get("profit")
            if isinstance(profit, (int, float)) and profit != 0:
                b["won"] = profit > 0
        # 3. Recompute profit from realized fill if it is missing or zero on a win.
        if (
            b.get("won") is True
            and (b.get("profit") in (None, 0, 0.0))
        ):
            try:
                fill_price = float(b.get("actual_fill_price") or b.get("fill_price") or b["entry_ask"])
                stake = float(b["stake"])
                payout = stake / fill_price if fill_price > 0 else 0.0
                b["profit"] = round(payout - stake, 2)
            except Exception:
                pass
    return out


def load_window_bets() -> List[dict]:
    """All bets in the window. Prefers session JSON; reconstructs from ledger
    for any date with no session JSON."""
    by_date = _load_session_bets()
    all_bets = []
    for d in WINDOW_DATES:
        bets = by_date.get(d)
        if bets is None:
            bets = _reconstruct_from_ledger(d)
            if bets:
                print(f"  [reconstructed {d} from ledger: {len(bets)} bet(s)]")
        all_bets.extend(bets or [])
    return all_bets


# ---------------------------------------------------------------------------
# Section 1: Per-day overview
# ---------------------------------------------------------------------------

def section_1_per_day(bets: List[dict]) -> None:
    print("=" * 90)
    print("SECTION 1 -- PER-DAY OVERVIEW")
    print("=" * 90)
    print(f"{'date':<12} {'placed':>6} {'filled':>6} {'W':>3} {'L':>3} "
          f"{'WR%':>5} {'PnL':>9} {'avg_edge':>9} {'avg_ask':>8}")
    grouped: Dict[str, List[dict]] = defaultdict(list)
    for b in bets:
        grouped[b["_session_date"]].append(b)
    overall_w = overall_l = 0
    overall_pnl = 0.0
    for d in WINDOW_DATES:
        day = grouped.get(d, [])
        placed = len(day)
        filled = [b for b in day if b.get("order_status") == "filled"]
        wins = [b for b in filled if b.get("won") is True]
        losses = [b for b in filled if b.get("won") is False]
        pnl = sum(float(b.get("profit") or 0) for b in filled)
        avg_edge = statistics.mean([float(b["edge"]) for b in day if b.get("edge") is not None]) if day else 0.0
        avg_ask = statistics.mean([float(b.get("actual_fill_price") or b.get("entry_ask") or 0) for b in filled]) if filled else 0.0
        wr = (len(wins) / len(filled) * 100) if filled else 0.0
        print(f"{d:<12} {placed:>6} {len(filled):>6} {len(wins):>3} {len(losses):>3} "
              f"{wr:>4.0f}% ${pnl:>7.2f} {avg_edge:>9.3f} {avg_ask:>8.3f}")
        overall_w += len(wins)
        overall_l += len(losses)
        overall_pnl += pnl
    print("-" * 90)
    total_filled = overall_w + overall_l
    overall_wr = (overall_w / total_filled * 100) if total_filled else 0.0
    print(f"{'WINDOW':<12} {len(bets):>6} {total_filled:>6} {overall_w:>3} {overall_l:>3} "
          f"{overall_wr:>4.0f}% ${overall_pnl:>7.2f}")
    print()


# ---------------------------------------------------------------------------
# Section 2: Winner vs loser contrast
# ---------------------------------------------------------------------------

NUMERIC_FIELDS = [
    ("edge", "edge"),
    ("entry_ask", "ask"),
    ("base_fair_value", "base_fv"),
    ("fair_value", "fv"),
    ("stage2_run_env_delta", "S2_delta"),
    ("team_offense_delta", "S3_delta"),
    ("inning", "inning"),
    ("outs", "outs"),
    ("away_score_before", "away_score"),
    ("home_score_before", "home_score"),
    ("runners_on", "runners"),
    ("inferred_runs", "inferred_runs"),
    ("ltp_at_signal", "ltp"),
    ("current_state_value_edge", "cur_edge"),
    ("current_state_value_fv_raw", "cur_fv"),
    ("shadow_fv_inferred_lift", "fv_lift"),
    ("shadow_no_event_edge", "no_evt_edge"),
    ("shadow_phantom_risk_score", "phantom_risk"),
    ("shadow_p_score_event_proxy", "p_score_evt"),
    ("ask_drop_5s", "ask_drop_5s"),
    ("execution_spread", "exec_spread"),
]


def _stat(vals: List[Optional[float]], stat: str = "mean") -> Optional[float]:
    cleaned = [float(v) for v in vals if v is not None and v != ""]
    if not cleaned:
        return None
    if stat == "mean":
        return statistics.mean(cleaned)
    if stat == "median":
        return statistics.median(cleaned)
    if stat == "min":
        return min(cleaned)
    if stat == "max":
        return max(cleaned)
    return None


def section_2_winner_vs_loser(bets: List[dict]) -> None:
    print("=" * 90)
    print("SECTION 2 -- WINNERS vs LOSERS (filled bets only)")
    print("=" * 90)
    filled = [b for b in bets if b.get("order_status") == "filled"]
    wins = [b for b in filled if b.get("won") is True]
    losses = [b for b in filled if b.get("won") is False]
    print(f"  N wins={len(wins)}  N losses={len(losses)}")
    print()
    print(f"{'feature':<14} {'win_mean':>10} {'loss_mean':>10} {'win_med':>10} "
          f"{'loss_med':>10} {'delta(W-L)':>11}")
    print("-" * 90)
    for raw, label in NUMERIC_FIELDS:
        w_vals = [b.get(raw) for b in wins]
        l_vals = [b.get(raw) for b in losses]
        w_mean = _stat(w_vals, "mean")
        l_mean = _stat(l_vals, "mean")
        w_med = _stat(w_vals, "median")
        l_med = _stat(l_vals, "median")
        delta = (w_mean - l_mean) if w_mean is not None and l_mean is not None else None
        print(f"{label:<14} {_fmt(w_mean):>10} {_fmt(l_mean):>10} "
              f"{_fmt(w_med):>10} {_fmt(l_med):>10} {_fmt(delta, '{:+.3f}'):>11}")
    # Categorical
    print()
    print("Phantom risk band:")
    for grp, name in [(wins, "wins"), (losses, "losses")]:
        c = Counter(b.get("shadow_phantom_risk_band") or "?" for b in grp)
        print(f"  {name:>8}: {dict(c)}")
    print("State value strategy:")
    for grp, name in [(wins, "wins"), (losses, "losses")]:
        c = Counter(b.get("state_value_strategy") or "?" for b in grp)
        print(f"  {name:>8}: {dict(c)}")
    print()


# ---------------------------------------------------------------------------
# Section 3: Loss anatomy
# ---------------------------------------------------------------------------

def section_3_loss_anatomy(bets: List[dict]) -> None:
    print("=" * 90)
    print("SECTION 3 -- LOSS ANATOMY (per-loss row)")
    print("=" * 90)
    losses = [b for b in bets if b.get("order_status") == "filled" and b.get("won") is False]
    if not losses:
        print("  no losses in window")
        return
    print(f"{'date':<11} {'matchup':<11} {'L':<5} {'inn':<6} {'score':>7} "
          f"{'ask':>5} {'fv':>5} {'edge':>5} {'cur_e':>6} {'lift':>5} "
          f"{'phant':>6} {'final':>6} {'pnl':>7} {'gap_to_line':>11}")
    print("-" * 110)
    for b in sorted(losses, key=lambda x: x["_session_date"]):
        score = f"{b['away_score_before']}-{b['home_score_before']}"
        final = f"{b.get('final_away','?')}-{b.get('final_home','?')}"
        try:
            gap = float(b.get("final_total", 0)) - float(b["line"])
        except Exception:
            gap = None
        print(
            f"{b['_session_date']:<11} "
            f"{b['away_abbrev']}@{b['home_abbrev']:<6} "
            f"O{b['line']:<4} "
            f"{b['inning']}{b['inning_state'][:1]} "
            f"o{b['outs']}    "
            f"{score:>7} "
            f"{_fmt(b.get('actual_fill_price',b['entry_ask']),'{:.2f}'):>5} "
            f"{_fmt(b['fair_value'],'{:.2f}'):>5} "
            f"{_fmt(b['edge'],'{:.2f}'):>5} "
            f"{_fmt(b.get('current_state_value_edge'),'{:+.2f}'):>6} "
            f"{_fmt(b.get('shadow_fv_inferred_lift'),'{:+.2f}'):>5} "
            f"{(b.get('shadow_phantom_risk_band') or '?'):>6} "
            f"{final:>6} "
            f"${b.get('profit',0):>5.2f} "
            f"{_fmt(gap, '{:+.1f}'):>11}"
        )
    print()


# ---------------------------------------------------------------------------
# Section 4: Cancel anatomy
# ---------------------------------------------------------------------------

def section_4_cancels(bets: List[dict]) -> None:
    print("=" * 90)
    print("SECTION 4 -- CANCELLED / UNFILLED")
    print("=" * 90)
    cancelled = [b for b in bets if b.get("order_status") == "cancelled"]
    if not cancelled:
        print("  no cancellations in window")
        return
    for b in cancelled:
        reason = b.get("cancel_reason") or "?"
        won = b.get("won")
        cf = "would-have-WON" if won is True else "would-have-LOST" if won is False else "no-outcome"
        final = f"{b.get('final_away','?')}-{b.get('final_home','?')}"
        print(f"  {b['_session_date']} {b['away_abbrev']}@{b['home_abbrev']} L{b['line']} "
              f"inn={b['inning']}{b['inning_state'][:1]} ask={b['entry_ask']:.2f} "
              f"edge={b['edge']:.2f} reason={reason} -> {cf} (final {final})")
    print()


# ---------------------------------------------------------------------------
# Section 5: Pattern flags
# ---------------------------------------------------------------------------

def section_5_patterns(bets: List[dict]) -> None:
    print("=" * 90)
    print("SECTION 5 -- PATTERN FLAGS")
    print("=" * 90)
    filled = [b for b in bets if b.get("order_status") == "filled"]
    settled = [b for b in filled if b.get("won") is not None]
    if not settled:
        print("  no settled bets")
        return

    flags = []

    # ---- Pattern A: phantom-risk band correlates with outcome
    high = [b for b in settled if (b.get("shadow_phantom_risk_band") or "") == "high"]
    if high:
        wr = sum(1 for b in high if b["won"]) / len(high)
        pnl = sum(b.get("profit", 0) for b in high)
        flags.append(
            f"A. shadow_phantom_risk_band=high: n={len(high)} WR={wr*100:.0f}% pnl=${pnl:+.2f}"
        )

    # ---- Pattern B: low/negative current_state_value_edge
    low_cur = [b for b in settled if isinstance(b.get("current_state_value_edge"), (int, float))
               and b["current_state_value_edge"] < 0.03]
    if low_cur:
        wr = sum(1 for b in low_cur if b["won"]) / len(low_cur)
        pnl = sum(b.get("profit", 0) for b in low_cur)
        flags.append(
            f"B. current_state_value_edge<0.03 (weak current support): n={len(low_cur)} "
            f"WR={wr*100:.0f}% pnl=${pnl:+.2f}"
        )

    # ---- Pattern C: edge>0.20 (high edge - phantom run risk)
    big_edge = [b for b in settled if b.get("edge", 0) > 0.20]
    if big_edge:
        wr = sum(1 for b in big_edge if b["won"]) / len(big_edge)
        pnl = sum(b.get("profit", 0) for b in big_edge)
        flags.append(
            f"C. edge>0.20 (high signal edge): n={len(big_edge)} WR={wr*100:.0f}% pnl=${pnl:+.2f}"
        )

    # ---- Pattern D: high-line bets (>=8.5)
    high_line = [b for b in settled if float(b.get("line", 0)) >= 8.5]
    if high_line:
        wr = sum(1 for b in high_line if b["won"]) / len(high_line)
        pnl = sum(b.get("profit", 0) for b in high_line)
        flags.append(
            f"D. line>=8.5: n={len(high_line)} WR={wr*100:.0f}% pnl=${pnl:+.2f}"
        )

    # ---- Pattern E: late innings (>=7)
    late = [b for b in settled if int(b.get("inning", 0)) >= 7]
    if late:
        wr = sum(1 for b in late if b["won"]) / len(late)
        pnl = sum(b.get("profit", 0) for b in late)
        flags.append(
            f"E. inning>=7: n={len(late)} WR={wr*100:.0f}% pnl=${pnl:+.2f}"
        )

    # ---- Pattern F: ask>=0.70 entries
    high_ask = [b for b in settled if float(b.get("entry_ask", 0)) >= 0.70]
    if high_ask:
        wr = sum(1 for b in high_ask if b["won"]) / len(high_ask)
        pnl = sum(b.get("profit", 0) for b in high_ask)
        flags.append(
            f"F. entry_ask>=0.70: n={len(high_ask)} WR={wr*100:.0f}% pnl=${pnl:+.2f}"
        )

    # ---- Pattern G: S2 negative (suppressive park/weather)
    s2_neg = [b for b in settled if (b.get("stage2_run_env_delta") or 0) < -0.05]
    if s2_neg:
        wr = sum(1 for b in s2_neg if b["won"]) / len(s2_neg)
        pnl = sum(b.get("profit", 0) for b in s2_neg)
        flags.append(
            f"G. S2_delta<-0.05 (suppressive env): n={len(s2_neg)} "
            f"WR={wr*100:.0f}% pnl=${pnl:+.2f}"
        )

    # ---- Pattern H: ltp_ask_gap signal
    ltp_gap = []
    for b in settled:
        try:
            gap = abs(float(b["entry_ask"]) - float(b["ltp_at_signal"]))
            if gap > 0.10:
                ltp_gap.append((b, gap))
        except (TypeError, ValueError, KeyError):
            pass
    if ltp_gap:
        wr = sum(1 for b, _ in ltp_gap if b["won"]) / len(ltp_gap)
        pnl = sum(b.get("profit", 0) for b, _ in ltp_gap)
        flags.append(
            f"H. abs(ask-ltp)>0.10 (stale-ltp regime): n={len(ltp_gap)} "
            f"WR={wr*100:.0f}% pnl=${pnl:+.2f}"
        )

    for f in flags:
        print(f"  {f}")
    print()
    print("Reference: window n=", len(settled), "WR=",
          f"{sum(1 for b in settled if b['won'])/len(settled)*100:.0f}%",
          "pnl=$", f"{sum(b.get('profit',0) for b in settled):+.2f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Loading bets from", WINDOW_DATES[0], "to", WINDOW_DATES[-1])
    bets = load_window_bets()
    print(f"Loaded {len(bets)} bets total\n")
    section_1_per_day(bets)
    section_2_winner_vs_loser(bets)
    section_3_loss_anatomy(bets)
    section_4_cancels(bets)
    section_5_patterns(bets)


if __name__ == "__main__":
    main()
