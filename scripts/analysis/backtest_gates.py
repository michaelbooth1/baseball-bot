#!/usr/bin/env python3
"""
scripts/analysis/backtest_gates.py

Backtest the MLB O/U gate model on historical game data (default: 2025 season).

Uses inning-by-inning linescore data from stored MLB game JSONs to simulate every
potential bet opportunity: at each half-inning where a run scores, we check whether
our gate filters would have fired and record the final-total outcome.

Since we have no 2025 Polymarket prices, we simulate market price as:
    assumed_ask = fv - assumed_discount
and compute expected ROI at several assumed discount levels (0.10, 0.15, 0.20).

IMPORTANT: The Stage-1 FV cache was built on 2021-2025 data, so FV calibration on
those years is partially in-sample. Gate performance (inning/lead/rn filters) is
directionally valid and is the primary output of interest.

Usage:
    python scripts/analysis/backtest_gates.py
    python scripts/analysis/backtest_gates.py --year 2025
    python scripts/analysis/backtest_gates.py --year 2024
    python scripts/analysis/backtest_gates.py --all-years
    python scripts/analysis/backtest_gates.py --year 2025 --compare-configs
    python scripts/analysis/backtest_gates.py --year 2025 --lines 7.5 8.5
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_DIR = Path(__file__).resolve().parents[2]
CACHE_PATH = PROJECT_DIR / "cache" / "mlb_ou_cache.json"
GAMES_ROOT = PROJECT_DIR / "data" / "games" / "regular"

EXTRAS_BUCKET = 10
MAX_INDIVIDUAL_SCORE = 10
MAX_COMBINED_SCORE = 20

# Lines to simulate bets on
DEFAULT_LINES = ["7.5", "8.5"]

# Assumed market discount below FV (simulates the FADE_UNDER overreaction pattern)
ASSUMED_DISCOUNTS = [0.10, 0.15, 0.20]

# ---------------------------------------------------------------------------
# FV Cache (copied from analyze_polymarket_overreactions.py)
# ---------------------------------------------------------------------------

class OUCache:
    def __init__(self, cache_path: Path):
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        self.cells: Dict[str, dict] = data.get("cells", {})
        meta = data.get("meta", {})
        self.extras_bucket: int = int(meta.get("extras_bucket", EXTRAS_BUCKET))
        self.max_individual: int = MAX_INDIVIDUAL_SCORE
        self.max_combined: int = int(meta.get("max_combined", MAX_COMBINED_SCORE))

    def _cell_key(self, away: int, home: int, inning: int, half: str,
                  outs: int, bases: int) -> str:
        a = min(away, self.max_individual)
        h = min(home, self.max_individual)
        combined = a + h
        if combined > self.max_combined:
            excess = combined - self.max_combined
            if a >= h:
                a = max(0, a - excess)
            else:
                h = max(0, h - excess)
        inning_bucket = min(inning, self.extras_bucket)
        o = max(0, min(2, outs))
        return f"{a}_{h}_{inning_bucket}_{half}_{o}_{bases}"

    def lookup(self, away_score: int, home_score: int, inning: int,
               half: str, outs: int, line: str, bases: int = 0) -> Optional[float]:
        """Return Poisson-calibrated P(over line | game state). None if not in cache."""
        key = self._cell_key(away_score, home_score, inning, half, outs, bases)
        cell = self.cells.get(key)
        if cell is None:
            return None
        # Use Poisson-calibrated key (po75, po85, etc.)
        cache_key = "po" + str(int(float(line) * 10))
        return cell.get(cache_key)


# ---------------------------------------------------------------------------
# Gate logic helpers
# ---------------------------------------------------------------------------

def _runs_pace_ok(current_total: int, inning: int, line: float) -> bool:
    """TR3 pace filter: expected 9-inning pace must support the line."""
    if inning == 0:
        return False
    pace = (current_total / inning) * 9
    return pace >= (line - 1.5)


# ---------------------------------------------------------------------------
# Gate configurations (retroactive comparison)
# ---------------------------------------------------------------------------

@dataclass
class GateConfig:
    name: str
    min_inning: int = 4
    min_inning_high_line: int = 5
    min_entry_ask: float = 0.0       # FV proxy floor (0 = no filter)
    min_entry_ask_high_line: float = 0.0
    high_line_cutoff: float = 8.5
    min_current_total: int = 0       # 0 = no filter
    runs_needed_max: float = 99.0    # 99 = no filter
    pace_filter: bool = False
    min_close_game_rn: float = 99.0  # Gate 8b: 99 = no filter
    inn5_rn_max: float = 99.0        # Gate 8c: 99 = no filter
    inn6_rn_max: float = 99.0        # Gate 8d: 99 = no filter
    blowout_min_inning: int = 99     # Gate 8e disabled by default
    blowout_lead_min: int = 99
    blowout_adj_min_inning: int = 99
    blowout_adj_lead_min: int = 99
    blowout_trailing_max: int = -1
    max_base_fv: float = 1.01        # Gate 8f: disable when > 1.0
    inning_dedup_gap: int = 2
    inning_dedup_edge_gap: float = 0.05


# Define all historical configs for comparison
CONFIGS = {
    "TR1_baseline": GateConfig(
        name="TR1 Baseline",
        min_inning=4,
        min_entry_ask=0.0,
    ),
    "TR2": GateConfig(
        name="TR2 (ask>=0.45)",
        min_inning=4,
        min_entry_ask=0.45,
    ),
    "TR3": GateConfig(
        name="TR3 (ask>=0.55, total>=4, pace)",
        min_inning=4,
        min_inning_high_line=5,
        min_entry_ask=0.55,
        min_entry_ask_high_line=0.55,
        min_current_total=4,
        pace_filter=True,
    ),
    "TR5_codex": GateConfig(
        name="TR5/Codex (+ rn_max=4.0)",
        min_inning=4,
        min_inning_high_line=5,
        min_entry_ask=0.55,
        min_entry_ask_high_line=0.60,
        high_line_cutoff=8.5,
        min_current_total=4,
        runs_needed_max=4.0,
        pace_filter=True,
    ),
    "TR6": GateConfig(
        name="TR6 (+ Gate8b: close+rn>=4)",
        min_inning=4,
        min_inning_high_line=5,
        min_entry_ask=0.55,
        min_entry_ask_high_line=0.60,
        high_line_cutoff=8.5,
        min_current_total=4,
        runs_needed_max=4.0,
        pace_filter=True,
        min_close_game_rn=4.0,
    ),
    "TR8": GateConfig(
        name="TR8 (+ Gate8c: inn5+rn>=3.0)",
        min_inning=4,
        min_inning_high_line=5,
        min_entry_ask=0.55,
        min_entry_ask_high_line=0.60,
        high_line_cutoff=8.5,
        min_current_total=4,
        runs_needed_max=4.0,
        pace_filter=True,
        min_close_game_rn=4.0,
        inn5_rn_max=3.0,
    ),
    "TR9": GateConfig(
        name="TR9 (+ Gate8d: inn6+rn>=2.5)",
        min_inning=4,
        min_inning_high_line=5,
        min_entry_ask=0.55,
        min_entry_ask_high_line=0.60,
        high_line_cutoff=8.5,
        min_current_total=4,
        runs_needed_max=4.0,
        pace_filter=True,
        min_close_game_rn=4.0,
        inn5_rn_max=3.0,
        inn6_rn_max=2.5,
    ),
    "TR10": GateConfig(
        name="TR10 (edge/inn5/rn tightened)",
        min_inning=4,
        min_inning_high_line=5,
        min_entry_ask=0.55,
        min_entry_ask_high_line=0.60,
        high_line_cutoff=8.5,
        min_current_total=4,
        runs_needed_max=3.5,
        pace_filter=True,
        min_close_game_rn=4.0,
        inn5_rn_max=2.5,
        inn6_rn_max=2.5,
    ),
    "TR11": GateConfig(
        name="TR11 (+ blowout gate)",
        min_inning=4,
        min_inning_high_line=5,
        min_entry_ask=0.55,
        min_entry_ask_high_line=0.60,
        high_line_cutoff=8.5,
        min_current_total=4,
        runs_needed_max=3.5,
        pace_filter=True,
        min_close_game_rn=4.0,
        inn5_rn_max=2.5,
        inn6_rn_max=2.5,
        blowout_min_inning=6,
        blowout_lead_min=6,
        blowout_trailing_max=1,
    ),
    "TR13_current": GateConfig(
        name="TR13 Current (+ blowout-adj + FV saturation)",
        min_inning=4,
        min_inning_high_line=5,
        min_entry_ask=0.55,
        min_entry_ask_high_line=0.60,
        high_line_cutoff=8.5,
        min_current_total=4,
        runs_needed_max=3.5,
        pace_filter=True,
        min_close_game_rn=4.0,
        inn5_rn_max=2.5,
        inn6_rn_max=2.5,
        blowout_min_inning=6,
        blowout_lead_min=6,
        blowout_adj_min_inning=7,
        blowout_adj_lead_min=4,
        blowout_trailing_max=1,
        max_base_fv=0.98,
    ),
}

CURRENT_CONFIG_KEY = "TR13_current"


# ---------------------------------------------------------------------------
# Simulated bet record
# ---------------------------------------------------------------------------

@dataclass
class SimBet:
    game_pk: int
    away: str
    home: str
    line: float
    inning: int
    half: str          # "T" or "B"
    away_score: int    # at bet time (after 1st run scores this inning)
    home_score: int
    fv: float          # Stage-1 FV at bet time
    runs_needed: float
    lead: int
    final_total: int
    won: bool          # final_total > line


# ---------------------------------------------------------------------------
# Apply gates and return True if bet should be placed
# ---------------------------------------------------------------------------

def apply_gates(
    inning: int,
    half: str,
    away_score: int,
    home_score: int,
    line: float,
    fv: float,
    cfg: GateConfig,
) -> bool:
    """Return True if all gate filters pass for this game state."""
    current_total = away_score + home_score
    runs_needed = line - current_total
    lead = abs(away_score - home_score)

    # Gate 1: minimum inning
    min_inn = cfg.min_inning_high_line if line >= cfg.high_line_cutoff else cfg.min_inning
    if inning < min_inn:
        return False

    # Gate 2: minimum entry ask (FV proxy)
    min_ask = (cfg.min_entry_ask_high_line if line >= cfg.high_line_cutoff
               else cfg.min_entry_ask)
    if fv < min_ask:
        return False

    # Gate 8f: FV saturation skip (TR12/TR13 family)
    if fv >= cfg.max_base_fv:
        return False

    # Gate 6: minimum current total
    if current_total < cfg.min_current_total:
        return False

    # Gate 7: runs pace filter
    if cfg.pace_filter and not _runs_pace_ok(current_total, inning, line):
        return False

    # Gate 8: runs_needed_max
    if runs_needed > cfg.runs_needed_max:
        return False

    # Gate 8b: close game + high runs needed (TR6)
    if lead < 2 and runs_needed >= cfg.min_close_game_rn:
        return False

    # Gate 8c: inning 5 bullpen transition dead zone (TR8)
    if inning == 5 and runs_needed >= cfg.inn5_rn_max:
        return False

    # Gate 8d: inning 6 setup-reliever dead zone (TR9)
    if inning == 6 and runs_needed >= cfg.inn6_rn_max:
        return False

    # Gate 8e + TR13 blowout-adjacent gate
    trailing_runs = min(away_score, home_score)
    is_full_blowout = (
        inning >= cfg.blowout_min_inning
        and lead >= cfg.blowout_lead_min
        and trailing_runs <= cfg.blowout_trailing_max
    )
    is_blowout_adjacent = (
        inning >= cfg.blowout_adj_min_inning
        and lead >= cfg.blowout_adj_lead_min
        and trailing_runs <= cfg.blowout_trailing_max
    )
    if is_full_blowout or is_blowout_adjacent:
        return False

    return True


# ---------------------------------------------------------------------------
# Simulate one game
# ---------------------------------------------------------------------------

def simulate_game(
    game_path: Path,
    cache: OUCache,
    cfg: GateConfig,
    lines: List[str],
) -> List[SimBet]:
    """Process one game JSON and return all simulated bets under the given config."""
    try:
        with open(game_path, encoding="utf-8") as f:
            game = json.load(f)
    except Exception:
        return []

    # Only process completed games
    status = game.get("gameData", {}).get("status", {}).get("detailedState", "")
    if "Final" not in status and "Completed" not in status:
        return []

    livedata = game.get("liveData", {})
    linescore = livedata.get("linescore", {})
    innings_data = linescore.get("innings", [])
    if not innings_data:
        return []

    # Final score from linescore teams (most reliable)
    ls_teams = linescore.get("teams", {})
    final_away = ls_teams.get("away", {}).get("runs")
    final_home = ls_teams.get("home", {}).get("runs")
    if final_away is None or final_home is None:
        return []
    final_total = final_away + final_home

    # Team abbreviations
    gd_teams = game.get("gameData", {}).get("teams", {})
    away_abbrev = gd_teams.get("away", {}).get("abbreviation", "AWY")
    home_abbrev = gd_teams.get("home", {}).get("abbreviation", "HME")
    game_pk = game.get("gamePk", 0)

    bets: List[SimBet] = []

    # Per-line inning dedup tracking (Gate 10)
    last_bet_inning: Dict[float, int] = {}
    last_bet_fv: Dict[float, float] = {}

    cum_away = 0
    cum_home = 0

    for inn_data in innings_data:
        inning_num = int(inn_data.get("num", 0))
        if inning_num == 0:
            continue

        # --- Top of inning (away batting) ---
        away_runs = inn_data.get("away", {}).get("runs", 0) or 0
        if away_runs > 0:
            # Simulate bet at moment first away run scored
            # State: cum_away+1, cum_home, outs=0 (approximation: mid-inning)
            state_away = cum_away + 1
            state_home = cum_home
            for line_str in lines:
                line_val = float(line_str)
                fv = cache.lookup(
                    away_score=state_away,
                    home_score=state_home,
                    inning=inning_num,
                    half="T",
                    outs=0,
                    line=line_str,
                )
                if fv is None:
                    continue

                # Inning dedup gate
                prev_inn = last_bet_inning.get(line_val, -99)
                if inning_num - prev_inn < cfg.inning_dedup_gap:
                    prev_fv = last_bet_fv.get(line_val, 0.0)
                    if fv - prev_fv <= cfg.inning_dedup_edge_gap:
                        continue

                if apply_gates(inning_num, "T", state_away, state_home,
                               line_val, fv, cfg):
                    runs_needed = line_val - (state_away + state_home)
                    lead = abs(state_away - state_home)
                    bets.append(SimBet(
                        game_pk=game_pk,
                        away=away_abbrev,
                        home=home_abbrev,
                        line=line_val,
                        inning=inning_num,
                        half="T",
                        away_score=state_away,
                        home_score=state_home,
                        fv=fv,
                        runs_needed=runs_needed,
                        lead=lead,
                        final_total=final_total,
                        won=(final_total > line_val),
                    ))
                    last_bet_inning[line_val] = inning_num
                    last_bet_fv[line_val] = fv

        cum_away += away_runs

        # --- Bottom of inning (home batting) ---
        home_runs = inn_data.get("home", {}).get("runs", 0) or 0
        if home_runs > 0:
            state_away = cum_away
            state_home = cum_home + 1
            for line_str in lines:
                line_val = float(line_str)
                fv = cache.lookup(
                    away_score=state_away,
                    home_score=state_home,
                    inning=inning_num,
                    half="B",
                    outs=0,
                    line=line_str,
                )
                if fv is None:
                    continue

                # Inning dedup gate
                prev_inn = last_bet_inning.get(line_val, -99)
                if inning_num - prev_inn < cfg.inning_dedup_gap:
                    prev_fv = last_bet_fv.get(line_val, 0.0)
                    if fv - prev_fv <= cfg.inning_dedup_edge_gap:
                        continue

                if apply_gates(inning_num, "B", state_away, state_home,
                               line_val, fv, cfg):
                    runs_needed = line_val - (state_away + state_home)
                    lead = abs(state_away - state_home)
                    bets.append(SimBet(
                        game_pk=game_pk,
                        away=away_abbrev,
                        home=home_abbrev,
                        line=line_val,
                        inning=inning_num,
                        half="B",
                        away_score=state_away,
                        home_score=state_home,
                        fv=fv,
                        runs_needed=runs_needed,
                        lead=lead,
                        final_total=final_total,
                        won=(final_total > line_val),
                    ))
                    last_bet_inning[line_val] = inning_num
                    last_bet_fv[line_val] = fv

        cum_home += home_runs

    return bets


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def wilson_ci(wins: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score confidence interval for a proportion."""
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


def roi_at_discount(bets: List[SimBet], discount: float) -> float:
    """Expected ROI assuming market priced at fv - discount per bet."""
    if not bets:
        return 0.0
    total_profit = 0.0
    for b in bets:
        entry_ask = max(0.01, b.fv - discount)
        if b.won:
            total_profit += (1.0 / entry_ask) - 1.0
        else:
            total_profit -= 1.0
    return total_profit / len(bets)


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def load_games(years: List[int]) -> List[Path]:
    paths = []
    for year in years:
        year_dir = GAMES_ROOT / str(year)
        if not year_dir.exists():
            continue
        for root, _, files in os.walk(year_dir):
            for f in files:
                if f.endswith(".json"):
                    paths.append(Path(root) / f)
    return sorted(paths)


def analyze(bets: List[SimBet], label: str = "", discount: float = 0.15) -> None:
    """Print a detailed breakdown of the simulated bets."""
    n = len(bets)
    if n == 0:
        print(f"  {label}: No bets placed.")
        return
    wins = sum(1 for b in bets if b.won)
    wr = wins / n
    lo, hi = wilson_ci(wins, n)
    roi = roi_at_discount(bets, discount)
    avg_fv = sum(b.fv for b in bets) / n

    print(f"\n{'='*72}")
    print(f"  {label}")
    print(f"{'='*72}")
    print(f"  Bets:      {n:,}")
    print(f"  Wins:      {wins:,}  ({wr:.1%})")
    print(f"  95% CI:    [{lo:.1%}, {hi:.1%}]")
    print(f"  Avg FV:    {avg_fv:.3f}")
    print(f"  ROI@{discount:.0%}:   {roi:+.1%}  (assumed market = fv - {discount:.2f})")

    # Break-even win rate at avg entry ask
    avg_ask = avg_fv - discount
    be_wr = avg_ask
    print(f"  Break-even WR: {be_wr:.1%}  (at avg ask={avg_ask:.3f})")
    margin = wr - be_wr
    print(f"  Edge margin:   {margin:+.1%}")

    # --- By inning ---
    print(f"\n  By Inning:")
    print(f"  {'Inn':<5} {'N':>6} {'W':>6} {'WR':>7} {'95%CI':>18} {'AvgFV':>7} {'ROI@{:.0%}'.format(discount):>8}")
    print(f"  {'-'*60}")
    for inn in sorted(set(b.inning for b in bets)):
        sub = [b for b in bets if b.inning == inn]
        w = sum(1 for b in sub if b.won)
        wr_i = w / len(sub)
        lo_i, hi_i = wilson_ci(w, len(sub))
        avg_fv_i = sum(b.fv for b in sub) / len(sub)
        roi_i = roi_at_discount(sub, discount)
        print(f"  {inn:<5} {len(sub):>6,} {w:>6,} {wr_i:>7.1%} "
              f"[{lo_i:.1%},{hi_i:.1%}]{'':<2} {avg_fv_i:>7.3f} {roi_i:>8.1%}")

    # --- By runs_needed bucket ---
    print(f"\n  By Runs Needed:")
    print(f"  {'rn_bucket':<14} {'N':>6} {'W':>6} {'WR':>7} {'95%CI':>18} {'ROI':>8}")
    print(f"  {'-'*60}")
    buckets = [
        ("rn <= 1.5", lambda b: b.runs_needed <= 1.5),
        ("rn (1.5,2.5]", lambda b: 1.5 < b.runs_needed <= 2.5),
        ("rn (2.5,3.5]", lambda b: 2.5 < b.runs_needed <= 3.5),
        ("rn (3.5,4.5]", lambda b: 3.5 < b.runs_needed <= 4.5),
        ("rn > 4.5", lambda b: b.runs_needed > 4.5),
    ]
    for name, pred in buckets:
        sub = [b for b in bets if pred(b)]
        if not sub:
            continue
        w = sum(1 for b in sub if b.won)
        wr_i = w / len(sub)
        lo_i, hi_i = wilson_ci(w, len(sub))
        roi_i = roi_at_discount(sub, discount)
        print(f"  {name:<14} {len(sub):>6,} {w:>6,} {wr_i:>7.1%} "
              f"[{lo_i:.1%},{hi_i:.1%}]{'':<2} {roi_i:>8.1%}")

    # --- By lead ---
    print(f"\n  By Lead (abs score diff):")
    print(f"  {'lead':<14} {'N':>6} {'W':>6} {'WR':>7} {'95%CI':>18} {'ROI':>8}")
    print(f"  {'-'*60}")
    lead_buckets = [
        ("lead = 0 (tied)", lambda b: b.lead == 0),
        ("lead = 1", lambda b: b.lead == 1),
        ("lead = 2-3", lambda b: 2 <= b.lead <= 3),
        ("lead = 4-5", lambda b: 4 <= b.lead <= 5),
        ("lead >= 6", lambda b: b.lead >= 6),
    ]
    for name, pred in lead_buckets:
        sub = [b for b in bets if pred(b)]
        if not sub:
            continue
        w = sum(1 for b in sub if b.won)
        wr_i = w / len(sub)
        lo_i, hi_i = wilson_ci(w, len(sub))
        roi_i = roi_at_discount(sub, discount)
        print(f"  {name:<14} {len(sub):>6,} {w:>6,} {wr_i:>7.1%} "
              f"[{lo_i:.1%},{hi_i:.1%}]{'':<2} {roi_i:>8.1%}")

    # --- By FV bucket ---
    print(f"\n  FV Calibration (FV bucket vs actual win rate):")
    print(f"  {'FV bucket':<14} {'N':>6} {'W':>6} {'Actual WR':>10} {'Avg FV':>8}")
    print(f"  {'-'*50}")
    fv_buckets = [
        ("[0.55,0.65)", lambda b: 0.55 <= b.fv < 0.65),
        ("[0.65,0.75)", lambda b: 0.65 <= b.fv < 0.75),
        ("[0.75,0.85)", lambda b: 0.75 <= b.fv < 0.85),
        ("[0.85,0.92)", lambda b: 0.85 <= b.fv < 0.92),
        ("[0.92,1.00)", lambda b: b.fv >= 0.92),
    ]
    for name, pred in fv_buckets:
        sub = [b for b in bets if pred(b)]
        if not sub:
            continue
        w = sum(1 for b in sub if b.won)
        wr_i = w / len(sub)
        avg_fv_i = sum(b.fv for b in sub) / len(sub)
        note = "OK calibrated" if abs(wr_i - avg_fv_i) < 0.05 else f"delta={wr_i-avg_fv_i:+.2f}"
        print(f"  {name:<14} {len(sub):>6,} {w:>6,} {wr_i:>10.1%} {avg_fv_i:>8.3f}  {note}")

    # --- By line ---
    print(f"\n  By Line:")
    for line_val in sorted(set(b.line for b in bets)):
        sub = [b for b in bets if b.line == line_val]
        w = sum(1 for b in sub if b.won)
        wr_i = w / len(sub)
        lo_i, hi_i = wilson_ci(w, len(sub))
        roi_i = roi_at_discount(sub, discount)
        print(f"    {line_val}: n={len(sub):,}  {w}W/{len(sub)-w}L  "
              f"WR={wr_i:.1%} [{lo_i:.1%},{hi_i:.1%}]  ROI={roi_i:+.1%}")

    # --- ROI sensitivity ---
    print(f"\n  ROI at various assumed market discounts:")
    for d in ASSUMED_DISCOUNTS:
        r = roi_at_discount(bets, d)
        be = (sum(b.fv for b in bets) / n) - d
        print(f"    Discount={d:.2f} (avg ask~{be:.3f}): ROI={r:+.1%}")

    # --- Statistical significance (normal approx, no scipy needed) ---
    be = avg_fv - discount
    se = math.sqrt(be * (1 - be) / n) if n > 0 else 1
    z = (wr - be) / se if se > 0 else 0
    # Normal CDF approximation (Hart 1968)
    def _norm_cdf(x: float) -> float:
        t = 1.0 / (1.0 + 0.2316419 * abs(x))
        poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
               + t * (-1.821255978 + t * 1.330274429))))
        p = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x**2) * poly
        return p if x >= 0 else 1.0 - p
    p_one = 1 - _norm_cdf(z)
    print(f"\n  Significance (vs break-even WR={be:.1%}):")
    print(f"    z = {z:.2f},  p (one-sided) = {p_one:.4f}  "
          f"({'SIGNIFICANT p<0.05' if p_one < 0.05 else 'not significant'})")


def compare_configs(
    game_paths: List[Path],
    cache: OUCache,
    lines: List[str],
    discount: float = 0.15,
) -> None:
    """Run all configs in a single pass and print a comparison table."""
    print(f"\n{'='*80}")
    print(f"  CONFIG COMPARISON  (assumed market discount = {discount:.2f})")
    print(f"  {'Config':<42} {'N':>6} {'WR':>7} {'95% CI':>18} {'ROI':>8} {'p-val':>7}")
    print(f"  {'-'*85}")

    def _norm_cdf(x: float) -> float:
        t = 1.0 / (1.0 + 0.2316419 * abs(x))
        poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
               + t * (-1.821255978 + t * 1.330274429))))
        p = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x**2) * poly
        return p if x >= 0 else 1.0 - p

    # Single pass: collect bets for ALL configs at once
    all_bets: Dict[str, List[SimBet]] = {k: [] for k in CONFIGS}
    for i, path in enumerate(game_paths):
        if i % 500 == 0 and i > 0:
            print(f"    ...processed {i}/{len(game_paths)} games", flush=True)
        for cfg_key, cfg in CONFIGS.items():
            all_bets[cfg_key].extend(simulate_game(path, cache, cfg, lines))

    for cfg_key, cfg in CONFIGS.items():
        bets = all_bets[cfg_key]
        n = len(bets)
        if n == 0:
            print(f"  {cfg.name:<42} {'0':>6}")
            continue
        wins = sum(1 for b in bets if b.won)
        wr = wins / n
        lo, hi = wilson_ci(wins, n)
        roi = roi_at_discount(bets, discount)
        avg_fv = sum(b.fv for b in bets) / n
        be = avg_fv - discount
        se = math.sqrt(be * (1 - be) / n) if n > 0 else 1
        z = (wr - be) / se if se > 0 else 0
        p = 1 - _norm_cdf(z)
        p_str = f"{p:.4f}"
        print(f"  {cfg.name:<42} {n:>6,} {wr:>7.1%} [{lo:.1%},{hi:.1%}]{'':<2} "
              f"{roi:>8.1%} {p_str:>7}")


def inn5_gate_validation(
    bets_no_gate: List[SimBet],
    bets_with_gate: List[SimBet],
    inn5_threshold: float = 2.5,
) -> None:
    """Validate the inn5_rn_max gate specifically."""
    blocked = [b for b in bets_no_gate
               if b.inning == 5 and b.runs_needed >= inn5_threshold]
    if not blocked:
        print(f"  No inn5+rn>={inn5_threshold:.1f} bets in dataset.")
        return
    wins_blocked = sum(1 for b in blocked if b.won)
    print(f"\n  Gate 8c (inn5+rn>={inn5_threshold:.1f}) retroactive validation:")
    print(f"    Would-be-blocked bets:  {len(blocked):,}")
    print(f"    Wins in blocked set:    {wins_blocked}  ({wins_blocked/len(blocked):.1%})")
    lo, hi = wilson_ci(wins_blocked, len(blocked))
    print(f"    95% CI:                [{lo:.1%}, {hi:.1%}]")
    saved = len(blocked) - wins_blocked
    print(f"    Losses saved:          {saved}")

    # Same rn at inning 4 for comparison
    inn4_same_rn = [b for b in bets_no_gate
                    if b.inning == 4 and inn5_threshold <= b.runs_needed < (inn5_threshold + 1.0)]
    if inn4_same_rn:
        w4 = sum(1 for b in inn4_same_rn if b.won)
        print(f"    Same rn[{inn5_threshold:.1f},{inn5_threshold + 1.0:.1f}) at inn4:  "
              f"{len(inn4_same_rn):,} bets, {w4}W/{len(inn4_same_rn)-w4}L  "
              f"({w4/len(inn4_same_rn):.1%} WR)")

    gate8b_check = [b for b in bets_no_gate
                    if b.lead < 2 and b.runs_needed >= 4.0]
    if gate8b_check:
        w8b = sum(1 for b in gate8b_check if b.won)
        print(f"\n  Gate 8b (close+rn>=4) retroactive validation:")
        print(f"    Would-be-blocked bets: {len(gate8b_check):,}")
        print(f"    Win rate:              {w8b}W/{len(gate8b_check)-w8b}L  ({w8b/len(gate8b_check):.1%})")
        lo8b, hi8b = wilson_ci(w8b, len(gate8b_check))
        print(f"    95% CI:               [{lo8b:.1%}, {hi8b:.1%}]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest gate model on historical MLB games.")
    parser.add_argument("--year", type=int, default=2025,
                        help="Single year to backtest (default: 2025).")
    parser.add_argument("--all-years", action="store_true",
                        help="Run on all available years (2021-2025).")
    parser.add_argument("--lines", nargs="+", default=DEFAULT_LINES,
                        help="Lines to simulate (default: 7.5 8.5).")
    parser.add_argument("--compare-configs", action="store_true",
                        help="Print retroactive comparison of all TR configs.")
    parser.add_argument("--discount", type=float, default=0.15,
                        help="Assumed market discount below FV (default: 0.15).")
    parser.add_argument("--cache", type=Path, default=CACHE_PATH)
    args = parser.parse_args()

    print("Loading FV cache...", end=" ", flush=True)
    try:
        cache = OUCache(args.cache)
    except FileNotFoundError:
        print(f"\nERROR: Cache not found at {args.cache}")
        sys.exit(1)
    print(f"OK ({len(cache.cells):,} cells)")

    years = list(range(2021, 2026)) if args.all_years else [args.year]
    print(f"Loading game files for {years}...", end=" ", flush=True)
    game_paths = load_games(years)
    print(f"{len(game_paths):,} games")

    if not game_paths:
        print("No game files found.")
        sys.exit(1)

    lines = args.lines
    discount = args.discount

    # --- Config comparison table (fast, runs all configs) ---
    if args.compare_configs:
        compare_configs(game_paths, cache, lines, discount)
        print()

    # --- Primary analysis: current config ---
    cfg_current = CONFIGS[CURRENT_CONFIG_KEY]
    print(f"\nRunning {CURRENT_CONFIG_KEY} simulation on {len(game_paths):,} games...", end=" ", flush=True)
    bets_current = []
    for path in game_paths:
        bets_current.extend(simulate_game(path, cache, cfg_current, lines))
    print(f"{len(bets_current):,} qualifying bets")

    label = f"{cfg_current.name} — {','.join(str(y) for y in years)} Season(s)  |  Lines: {', '.join(lines)}"
    analyze(bets_current, label=label, discount=discount)

    # --- Gate 8b/8c retroactive validation ---
    print(f"\nRunning baseline (TR5/Codex, no Gate8b/8c) for gate validation...",
          end=" ", flush=True)
    cfg_tr5 = CONFIGS["TR5_codex"]
    bets_tr5 = []
    for path in game_paths:
        bets_tr5.extend(simulate_game(path, cache, cfg_tr5, lines))
    print(f"{len(bets_tr5):,} bets")

    inn5_gate_validation(
        bets_no_gate=bets_tr5,
        bets_with_gate=bets_current,
        inn5_threshold=cfg_current.inn5_rn_max,
    )

    # --- Year-by-year breakdown (if all-years) ---
    if args.all_years:
        print(f"\n{'='*72}")
        print(f"  YEAR-BY-YEAR BREAKDOWN ({CURRENT_CONFIG_KEY} config, discount={discount:.2f})")
        print(f"  {'Year':<6} {'N':>6} {'WR':>7} {'95% CI':>18} {'ROI':>8}")
        print(f"  {'-'*48}")
        for yr in years:
            yr_paths = [p for p in game_paths if f"/{yr}/" in str(p) or f"\\{yr}\\" in str(p)]
            yr_bets = []
            for path in yr_paths:
                yr_bets.extend(simulate_game(path, cache, cfg_current, lines))
            if not yr_bets:
                continue
            w = sum(1 for b in yr_bets if b.won)
            wr = w / len(yr_bets)
            lo, hi = wilson_ci(w, len(yr_bets))
            roi = roi_at_discount(yr_bets, discount)
            print(f"  {yr:<6} {len(yr_bets):>6,} {wr:>7.1%} [{lo:.1%},{hi:.1%}]{'':<2} {roi:>8.1%}")

    print(f"\n{'='*72}")
    print("  NOTE: FV cache built on 2021-2025 data — FV calibration is partially")
    print("  in-sample. Gate quality (inning/lead/rn filters) results are valid.")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
