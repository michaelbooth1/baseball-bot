"""
session_report.py -- Live session analytics for the MLB Polymarket O/U bot.

Reads all session JSON files and prints a structured report:
  1. Overall P&L (filled vs counterfactual cancelled)
  2. Fill win rate by inning
  3. Fill win rate by edge bucket
  4. Fill win rate by Stage-2 delta bucket
  5. ask_drop_5s distribution (ALL bets with ask_5s recorded, not just filled)
  6. Per-session P&L table with actual session params
  7. Current gate simulation
  8. Per-venue/park breakdown (uses venue_name field if present, else team lookup)
  9. Near-gate bets (within margin of any threshold -- flag for future tuning)

Usage:
    python scripts/analysis/session_report.py [--sessions-dir PATH]
"""

import argparse
import glob
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, median

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_SESSIONS_DIR = PROJECT_DIR / "data" / "live_trading" / "sessions"

# ---------------------------------------------------------------------------
# Team -> home park lookup (used for historical bets without venue_name field)
# ---------------------------------------------------------------------------
TEAM_PARK = {
    "ARI": "Chase Field",            "ATL": "Truist Park",
    "BAL": "Camden Yards",           "BOS": "Fenway Park",
    "CHC": "Wrigley Field",          "CWS": "Guaranteed Rate Field",
    "CIN": "Great American Ball Park", "CLE": "Progressive Field",
    "COL": "Coors Field",            "DET": "Comerica Park",
    "HOU": "Minute Maid Park",       "KC":  "Kauffman Stadium",
    "LAA": "Angel Stadium",          "LAD": "Dodger Stadium",
    "MIA": "loanDepot park",         "MIL": "American Family Field",
    "MIN": "Target Field",           "NYM": "Citi Field",
    "NYY": "Yankee Stadium",         "OAK": "Oakland Coliseum",
    "ATH": "Sutter Health Park",     "PHI": "Citizens Bank Park",
    "PIT": "PNC Park",               "SD":  "Petco Park",
    "SF":  "Oracle Park",            "SEA": "T-Mobile Park",
    "STL": "Busch Stadium",          "TB":  "Tropicana Field",
    "TEX": "Globe Life Field",       "TOR": "Rogers Centre",
    "WSH": "Nationals Park",
}

# Parks known to significantly suppress scoring (S2 will often be negative here)
SUPPRESSIVE_PARKS = {
    "Oracle Park", "Petco Park", "Dodger Stadium", "T-Mobile Park",
    "Wrigley Field", "PNC Park", "Tropicana Field", "Nationals Park",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bucket(value: float, step: float) -> str:
    lo = math.floor(value / step) * step
    hi = lo + step
    return f"{lo:.2f}-{hi:.2f}"


def _pct(n: int, d: int) -> str:
    return f"{n/d:6.1%}" if d else "  --- "


def _roi(profit: float, staked: float) -> str:
    return f"{profit/staked:+.1%}" if staked else "  --- "


def _divider(width: int = 72) -> None:
    print("-" * width)


def _header(title: str, width: int = 72) -> None:
    print()
    print("=" * width)
    print(f"  {title}")
    print("=" * width)


def _venue(b: dict) -> str:
    """Return venue name: from bet record if present, else team->park lookup."""
    v = b.get("venue_name", "")
    if v:
        return v
    return TEAM_PARK.get(b.get("home_abbrev", ""), "Unknown")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_sessions(sessions_dir: Path):
    files = sorted(glob.glob(str(sessions_dir / "*.json")))
    if not files:
        print(f"No session files found in {sessions_dir}", file=sys.stderr)
        sys.exit(1)
    sessions = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            sessions.append(json.load(fh))
    return sessions


def classify_bet(b: dict):
    """Return (signal_win, fill_type, won)."""
    final = b.get("final_total")
    line = float(b["line"])
    signal_win = (final > line) if final is not None else None

    status = b.get("order_status", "")
    won = b.get("won")

    if status == "filled" and won is not None:
        fill_type = "filled"
    elif status == "cancelled":
        fill_type = "cancelled"
    else:
        fill_type = "pending"

    return signal_win, fill_type, won


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def section_overall(bets):
    _header("1. OVERALL SUMMARY")
    fw = fl = cw = cl = pending = 0
    total_profit = total_staked = 0.0

    for b in bets:
        signal_win, fill_type, won = classify_bet(b)
        if fill_type == "filled":
            if won is True:
                fw += 1; total_profit += b.get("profit") or 0; total_staked += b.get("stake") or 0
            elif won is False:
                fl += 1; total_profit += b.get("profit") or 0; total_staked += b.get("stake") or 0
        elif fill_type == "cancelled":
            if signal_win is True: cw += 1
            elif signal_win is False: cl += 1
            else: pending += 1
        else:
            pending += 1

    print(f"  {'Category':<30s} {'W':>4} {'L':>4} {'WR':>7} {'Staked':>10} {'Profit':>10} {'ROI':>8}")
    _divider()
    print(f"  {'FILLED (settled)':30s} {fw:4d} {fl:4d} {_pct(fw, fw+fl):>7} "
          f"${total_staked:9.2f} ${total_profit:9.2f} {_roi(total_profit, total_staked):>8}")
    print(f"  {'CANCELLED (counterfactual)':30s} {cw:4d} {cl:4d} {_pct(cw, cw+cl):>7}")
    print(f"  {'PENDING/UNKNOWN':30s}      {'':>4} {pending:>7}")
    all_sw = cw + cl + fw + fl
    print(f"\n  Combined signal WR: {_pct(cw+fw, all_sw)} ({cw+fw}/{all_sw})")
    print(f"  Fill WR vs Cancel WR gap = {(fw/(fw+fl) - cw/(cw+cl))*100:+.1f}pp (Winner's Curse)")


def section_by_inning(bets):
    _header("2. FILL WIN RATE BY INNING")
    bkts = defaultdict(lambda: [0, 0, 0.0, 0.0])
    for b in bets:
        _, ft, won = classify_bet(b)
        if ft != "filled" or won is None: continue
        k = b["inning"]
        if won: bkts[k][0] += 1; bkts[k][2] += b.get("profit") or 0; bkts[k][3] += b.get("stake") or 0
        else:   bkts[k][1] += 1; bkts[k][2] += b.get("profit") or 0; bkts[k][3] += b.get("stake") or 0

    print(f"  {'Inning':>6} {'W':>4} {'L':>4} {'WR':>7} {'ROI':>8}")
    _divider(40)
    for k in sorted(bkts):
        w, l, p, s = bkts[k]
        print(f"  {k:6d} {w:4d} {l:4d} {_pct(w, w+l):>7} {_roi(p, s):>8}")


def section_by_edge(bets):
    _header("3. FILL WIN RATE BY EDGE BUCKET (0.05 increments)")
    bkts = defaultdict(lambda: [0, 0, 0.0, 0.0])
    for b in bets:
        _, ft, won = classify_bet(b)
        if ft != "filled" or won is None: continue
        k = _bucket(b["edge"], 0.05)
        if won: bkts[k][0] += 1; bkts[k][2] += b.get("profit") or 0; bkts[k][3] += b.get("stake") or 0
        else:   bkts[k][1] += 1; bkts[k][2] += b.get("profit") or 0; bkts[k][3] += b.get("stake") or 0

    print(f"  {'Edge bucket':>12} {'W':>4} {'L':>4} {'WR':>7} {'ROI':>8}")
    _divider(45)
    for k in sorted(bkts):
        w, l, p, s = bkts[k]
        flag = " <-- DANGER" if w == 0 and l >= 2 else ""
        print(f"  {k:>12} {w:4d} {l:4d} {_pct(w, w+l):>7} {_roi(p, s):>8}{flag}")


def section_by_s2(bets):
    _header("4. FILL WIN RATE BY STAGE-2 DELTA BUCKET")
    bkts = defaultdict(lambda: [0, 0, 0.0, 0.0])
    for b in bets:
        _, ft, won = classify_bet(b)
        if ft != "filled" or won is None: continue
        k = _bucket(b.get("stage2_run_env_delta") or 0.0, 0.05)
        if won: bkts[k][0] += 1; bkts[k][2] += b.get("profit") or 0; bkts[k][3] += b.get("stake") or 0
        else:   bkts[k][1] += 1; bkts[k][2] += b.get("profit") or 0; bkts[k][3] += b.get("stake") or 0

    print(f"  {'S2 delta':>14} {'W':>4} {'L':>4} {'WR':>7} {'ROI':>8}  (negative = suppressive park/weather)")
    _divider(60)
    for k in sorted(bkts):
        w, l, p, s = bkts[k]
        print(f"  {k:>14} {w:4d} {l:4d} {_pct(w, w+l):>7} {_roi(p, s):>8}")


def section_ask_drop(bets):
    _header("5. ASK_DROP_5S -- ALL BETS WITH ask_5s RECORDED")
    print("  ask_drop_5s = entry_ask - ask_5s.  Positive = ask fell (market rejected).")
    print("  Negative = ask rose (market confirmed). Includes fills AND cancels.")
    print()
    print(f"  {'Bet':35s} {'type':>10} {'drop':>7} {'outcome':>8}  note")
    _divider(80)

    filled_wins_drops, filled_loss_drops = [], []
    cancel_wins_drops, cancel_loss_drops = [], []

    for b in bets:
        drop = b.get("ask_drop_5s")
        if drop is None: continue
        signal_win, fill_type, won = classify_bet(b)
        label = f"{b['away_abbrev']}@{b['home_abbrev']} O{b['line']}"
        if signal_win is True: outcome = "WIN"
        elif signal_win is False: outcome = "LOSS"
        else: outcome = "PEND"

        note = ""
        if drop >= 0.08:
            note = "ask-reversal fires"
        elif drop < -0.10:
            note = "strong confirmation"
        elif drop < 0:
            note = "market confirmed"

        print(f"  {label:35s} {fill_type:>10} {drop:+7.3f} {outcome:>8}  {note}")

        if fill_type == "filled":
            if signal_win is True: filled_wins_drops.append(drop)
            elif signal_win is False: filled_loss_drops.append(drop)
        elif fill_type == "cancelled":
            if signal_win is True: cancel_wins_drops.append(drop)

    print()
    def _stats(vals, label):
        if not vals: return
        print(f"  {label} (n={len(vals)}): mean={mean(vals):+.3f}  "
              f"min={min(vals):+.3f}  max={max(vals):+.3f}")

    _stats(filled_wins_drops,  "Filled WINS ")
    _stats(filled_loss_drops,  "Filled LOSSES")
    _stats(cancel_wins_drops,  "Cancelled (would-be WINS)")


def section_per_session(sessions):
    _header("6. PER-SESSION P&L WITH ACTIVE GATE FLAGS")
    print(f"  {'Date':12s} {'Fills':>6} {'W':>4} {'L':>4} {'WR':>7} {'Staked':>9} {'Profit':>10} {'ROI':>8}  Active gates")
    _divider(90)

    # Gate keys that indicate each protection is active in a session's params
    gate_keys = {
        "8f": "max_base_fv",
        "8g": "fv_ask_gap_max",
        "8e": "blowout_adj_lead_min",
        "8h": "s2_suppress_max",
        "8i": "sp_era_threshold",
        "fv_decay_ask": "fv_decay_min_ask_drop",
        "kelly_cap": "kelly_max_edge",
    }

    for sess in sessions:
        date = sess.get("date", "?")
        params = sess.get("params", {})
        bets = sess.get("bets", [])
        w = l = 0; staked = profit = 0.0
        for b in bets:
            _, ft, won = classify_bet(b)
            if ft != "filled" or won is None: continue
            if won: w += 1
            else: l += 1
            staked += b.get("stake") or 0
            profit += b.get("profit") or 0
        fills = w + l

        active = [name for name, key in gate_keys.items() if key in params]
        active_str = ",".join(active) if active else "baseline-only"

        print(f"  {date:12s} {fills:6d} {w:4d} {l:4d} {_pct(w, fills):>7} "
              f"${staked:8.2f} ${profit:9.2f} {_roi(profit, staked):>8}  [{active_str}]")


def section_gate_simulation(bets):
    _header("7. CURRENT GATE THRESHOLDS -- SIMULATION ON ALL HISTORICAL FILLS")
    gate_8f = 0.99;  gate_8g_e = 0.28; gate_8g_i = 7
    gate_8e_l = 6;   gate_8e_al = 4;   gate_8e_ai = 7
    gate_8h_s = -0.20; gate_8h_i = 6

    print(f"  8f: base_fv>={gate_8f}  |  8g: edge>{gate_8g_e}/inn>={gate_8g_i}  |  "
          f"8e: trail<=1 lead>={gate_8e_l}/inn>=6 OR lead>={gate_8e_al}/inn>={gate_8e_ai}")
    print(f"  8h: s2<={gate_8h_s}/inn>={gate_8h_i}")
    print()
    print(f"  {'Gate':6s} {'Blocks':>6}  {'Saves(L blocked)':>18}  {'Costs(W blocked)':>18}")
    _divider()

    results = {g: [0, 0] for g in ["8f","8g","8e","8h","any"]}
    for b in bets:
        sw, ft, won = classify_bet(b)
        if ft != "filled" or sw is None: continue
        bfv = b.get("base_fair_value") or 0.0
        edge = b["edge"]; inn = b["inning"]
        s2 = b.get("stage2_run_env_delta") or 0.0
        a = b.get("away_score_before", 0); h = b.get("home_score_before", 0)
        lead = abs(a - h); trail = min(a, h)

        f8f  = bfv >= gate_8f
        f8g  = edge > gate_8g_e and inn >= gate_8g_i
        f8e  = trail <= 1 and inn >= 6 and (lead >= gate_8e_l or (lead >= gate_8e_al and inn >= gate_8e_ai))
        f8h  = s2 <= gate_8h_s and inn >= gate_8h_i
        fany = f8f or f8g or f8e or f8h

        for name, fires in [("8f",f8f),("8g",f8g),("8e",f8e),("8h",f8h),("any",fany)]:
            if fires:
                if not sw: results[name][0] += 1
                else:      results[name][1] += 1

    for name in ["8f","8g","8e","8h","any"]:
        bl, bw = results[name]
        print(f"  {name:6s} {bl+bw:6d}   saves={bl:3d} losses blocked   costs={bw:3d} wins blocked")

    print()
    print("  'any' = at least one gate would have fired (unique bets blocked)")


def section_venue(bets):
    _header("8. PER-VENUE FILL WIN RATE (park analytics)")
    print("  Venue derived from bet.venue_name (new) or home_abbrev lookup (historical).")
    print(f"  {'Venue':35s} {'W':>4} {'L':>4} {'WR':>7} {'ROI':>8}  {'S2 range':>16}  suppressive?")
    _divider(85)

    bkts = defaultdict(lambda: [0, 0, 0.0, 0.0, [], []])  # W,L,profit,stake,s2_vals,edges
    for b in bets:
        _, ft, won = classify_bet(b)
        if ft != "filled" or won is None: continue
        venue = _venue(b)
        s2 = b.get("stage2_run_env_delta") or 0.0
        bkts[venue][4].append(s2)
        bkts[venue][5].append(b["edge"])
        if won: bkts[venue][0] += 1; bkts[venue][2] += b.get("profit") or 0; bkts[venue][3] += b.get("stake") or 0
        else:   bkts[venue][1] += 1; bkts[venue][2] += b.get("profit") or 0; bkts[venue][3] += b.get("stake") or 0

    for venue in sorted(bkts, key=lambda v: -(bkts[v][0]+bkts[v][1])):
        w, l, p, s, s2s, _ = bkts[venue]
        s2_range = f"{min(s2s):+.2f} to {max(s2s):+.2f}" if s2s else "  ---"
        flag = "[suppressive]" if venue in SUPPRESSIVE_PARKS else ""
        print(f"  {venue:35s} {w:4d} {l:4d} {_pct(w, w+l):>7} {_roi(p, s):>8}  {s2_range:>16}  {flag}")


def section_near_gate(bets):
    _header("9. NEAR-GATE BETS (filled bets that just passed a gate threshold)")
    print("  Shows bets that were NOT blocked but are within 0.02 of triggering a gate.")
    print("  'just passed' means the gate did not fire but almost did (permissive side).")
    print("  Cluster of losses here = evidence to lower that threshold.")
    print()

    gate_8f = 0.99; gate_8g_e = 0.28; gate_8g_i = 7
    gate_8e_l = 6;  gate_8e_al = 4;   gate_8e_ai = 7
    gate_8h_s = -0.20; gate_8h_i = 6
    margin = 0.02

    found = False
    for b in bets:
        _, ft, won = classify_bet(b)
        if ft != "filled" or won is None: continue
        bfv  = b.get("base_fair_value") or 0.0
        edge = b["edge"]; inn = b["inning"]
        s2   = b.get("stage2_run_env_delta") or 0.0
        aw   = b.get("away_score_before", 0); hw = b.get("home_score_before", 0)
        trail = min(aw, hw); lead = abs(aw - hw)
        label = f"{b['away_abbrev']}@{b['home_abbrev']} O{b['line']}"
        outcome = "WIN" if won else "LOSS"

        # Check if this bet is already blocked by any gate (don't double-report)
        f8f = bfv >= gate_8f
        f8g = edge > gate_8g_e and inn >= gate_8g_i
        f8e = trail <= 1 and inn >= 6 and (lead >= gate_8e_l or (lead >= gate_8e_al and inn >= gate_8e_ai))
        f8h = s2 <= gate_8h_s and inn >= gate_8h_i
        if f8f or f8g or f8e or f8h:
            continue  # already blocked, skip

        near = []
        # "just passed" = passed the gate (permissive side) but within margin
        # Gate 8f: fires at base_fv >= 0.99.  Near = [0.97, 0.99)
        if gate_8f - margin <= bfv < gate_8f:
            near.append(f"8f(base_fv={bfv:.3f} < {gate_8f})")
        # Gate 8g: fires at edge > 0.28 AND inn >= 7.  Near edge = [0.26, 0.28] in inn>=7
        if inn >= gate_8g_i and gate_8g_e - margin <= edge <= gate_8g_e:
            near.append(f"8g(edge={edge:.3f} <= {gate_8g_e}, inn={inn})")
        # Gate 8e-adj: fires at trail<=1, lead>=4, inn>=7.  Near lead = [3, 4) in inn>=7
        if trail <= 1 and inn >= gate_8e_ai and gate_8e_al - 1 <= lead < gate_8e_al:
            near.append(f"8e-adj(lead={lead}, thresh={gate_8e_al}, inn={inn})")
        # Gate 8h: fires at s2 <= -0.20 AND inn >= 6.  Near s2 = (-0.20, -0.18] in inn>=6
        if inn >= gate_8h_i and gate_8h_s < s2 <= gate_8h_s + margin:
            near.append(f"8h(s2={s2:.3f} > {gate_8h_s}, inn={inn})")

        if near:
            found = True
            print(f"  {label:35s} inn={inn:2d} edge={edge:.3f} S2={s2:+.3f} {outcome}  near: {'; '.join(near)}")

    if not found:
        print("  No filled bets found near any gate threshold (within 0.02 on permissive side).")
    print()
    print("  ask_reversal near-miss: cancels with ask_drop_5s in [0.06, 0.08) would appear here")
    print("  if tracked. Add these manually from Section 5 data if threshold tuning is needed.")


def section_shadow(sessions):
    _header("10. SHADOW GATE ANALYSIS (relaxed-threshold counterfactuals)")
    print("  Per-gate: signals that were blocked but would pass under relaxed thresholds.")
    print("  'would_pass' = relaxed threshold would have let the signal through.")
    print("  Join to outcomes JSONL (candidate_universe/*_outcomes.jsonl) for win-rate analysis.")
    print()

    # Aggregate across all sessions
    total_eval = total_pass = total_blocked = 0
    gate1_blocked = gate1_pass = gate3_blocked = gate3_pass = 0
    by_reason: dict = defaultdict(lambda: [0, 0])  # reason -> [evaluated, would_pass]

    for sess in sessions:
        summ = sess.get("summary", {})
        total_eval    += summ.get("shadow_relaxed_evaluated", 0)
        total_pass    += summ.get("shadow_relaxed_would_pass", 0)
        total_blocked += summ.get("shadow_relaxed_still_blocked", 0)
        gate1_blocked += summ.get("shadow_gate1_blocked", 0)
        gate1_pass    += summ.get("shadow_gate1_would_pass", 0)
        gate3_blocked += summ.get("shadow_gate3_blocked", 0)
        gate3_pass    += summ.get("shadow_gate3_would_pass", 0)
        for reason, cnt in summ.get("shadow_relaxed_eval_by_reason", {}).items():
            by_reason[reason][0] += cnt
        for reason, cnt in summ.get("shadow_relaxed_would_pass_by_reason", {}).items():
            by_reason[reason][1] += cnt

    print(f"  Full-pipeline shadow (Gates 6-8i, signals past Gate 5):")
    print(f"    Evaluated: {total_eval}  |  Would pass relaxed: {total_pass}  |  Still blocked: {total_blocked}")
    print()

    if by_reason:
        print(f"  {'Gate reason':<35s} {'Eval':>6} {'Pass':>6} {'Pass%':>7}  relaxed threshold")
        _divider(80)
        for reason in sorted(by_reason, key=lambda r: -by_reason[r][0]):
            ev, wp = by_reason[reason]
            pct = f"{wp/ev:6.1%}" if ev else "   --- "
            # Describe what the relaxed threshold is for each gate
            desc = {
                "gate_min_current_total": "total >= 3 (strict: >= 4)",
                "gate_runs_needed_max":   "rn <= 4.0 (strict: <= 3.5)",
                "gate_close_game_runs_needed": "rn < 4.5 (strict: < 4.0)",
                "gate_inning5_runs_needed": "rn < 3.0 (strict: < 2.5)",
                "gate_inning6_runs_needed": "rn < 3.0 (strict: < 2.5)",
                "gate_blowout":           "lead>=7/inn>=6 OR lead>=5/inn>=7",
                "gate_stage2_suppression":"S2<=-0.24/inn>=7 (strict: <=-0.20/inn>=6)",
                "gate_fv_saturation":     "base_fv < 1.0 (strict: < 0.99)",
                "gate_fv_saturation_v2":  "base_fv < 0.995 (strict: < 0.99)",
                "gate_fv_ask_gap":        "edge<=0.33/inn>=8 (strict: <=0.28/inn>=7)",
                "gate_min_edge":          "edge >= min_edge - 0.03",
                "gate_sp_era":            "edge >= min_edge + 0.02 (strict: + 0.03)",
                "gate_runs_pace":         "pace >= line - 2.0 (strict: >= line - 1.5)",
            }.get(reason, "")
            print(f"  {reason:<35s} {ev:6d} {wp:6d} {pct:>7}  {desc}")

    print()
    print(f"  Gate 1 (inning min) -- session counts only, no per-signal logging:")
    print(f"    Blocked: {gate1_blocked}  |  Would pass if 1 inning earlier: {gate1_pass}  "
          f"({_pct(gate1_pass, gate1_blocked):>6} of blocks)")
    print()
    print(f"  Gate 3 (min entry ask) -- session counts only, no per-signal logging:")
    print(f"    Blocked: {gate3_blocked}  |  Would pass at ask 5pp lower: {gate3_pass}  "
          f"({_pct(gate3_pass, gate3_blocked):>6} of blocks)")
    print()
    if total_eval == 0 and gate1_blocked == 0:
        print("  No shadow data found. Run signal_engine.py or live_engine.py with --shadow-relaxed-enabled.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Live session analytics report")
    parser.add_argument("--sessions-dir", type=Path, default=DEFAULT_SESSIONS_DIR)
    args = parser.parse_args()

    sessions = load_sessions(args.sessions_dir)
    all_bets = [b for sess in sessions for b in sess.get("bets", [])]

    print(f"\nMLB Polymarket O/U Bot -- Session Analytics Report")
    print(f"Sessions: {len(sessions)}  |  Total bets: {len(all_bets)}")
    print(f"Data range: {sessions[0]['date']} to {sessions[-1]['date']}")

    section_overall(all_bets)
    section_by_inning(all_bets)
    section_by_edge(all_bets)
    section_by_s2(all_bets)
    section_ask_drop(all_bets)
    section_per_session(sessions)
    section_gate_simulation(all_bets)
    section_venue(all_bets)
    section_near_gate(all_bets)
    section_shadow(sessions)
    print()


if __name__ == "__main__":
    main()
