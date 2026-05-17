#!/usr/bin/env python3
"""
report_team_offense_residuals.py -- Phase 1 diagnostic on the calibration table.

Reads
  data/analysis_output/team_offense_calibration/training_table.jsonl
and produces an interpretable report on the residual
  over_hit - base_fv_stage1_plus_stage2
broken out by inning, line, season, and team-offense regime.

Goal: see whether the current Stage-3 magnitude (LOGIT_DELTA_PER_RUN = 0.20)
aligns with what the raw residual actually implies, BEFORE we fit anything.

Team-offense regime: builds a simple 50-game rolling RPG per team using ALL
final-game outcomes from the same corpus (no leakage; `before_date` strictly
less than the row's date). Then each row's matchup is bucketed by
`away_rpg + home_rpg - 2 * mlb_avg_rpg`:
    high_offense  diff >  +0.5
    avg_offense   abs(diff) <= 0.5
    low_offense   diff <  -0.5

Output:
  data/analysis_output/team_offense_calibration/phase1_residual_report.json
  + console summary

Usage:
  python scripts/analysis/report_team_offense_residuals.py
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_TABLE = PROJECT_DIR / "data" / "analysis_output" / "team_offense_calibration" / "training_table.jsonl"
DEFAULT_OUT = PROJECT_DIR / "data" / "analysis_output" / "team_offense_calibration" / "phase1_residual_report.json"
DEFAULT_GAME_LOG = PROJECT_DIR / "cache" / "team_game_log.json"

LOGGER = logging.getLogger("report_team_offense_residuals")

EPS = 1e-6
N_GAMES_WINDOW = 50
MIN_GAMES_FOR_RPG = 10


def _clamp01(p: float) -> float:
    return max(EPS, min(1.0 - EPS, p))


def _logit(p: float) -> float:
    p = _clamp01(p)
    return math.log(p / (1.0 - p))


# ---------------------------------------------------------------------------
# Team-offense priors (50-game rolling RPG, leak-free)
# ---------------------------------------------------------------------------


def build_team_history(game_log_path: Path) -> Tuple[Dict[str, List[Tuple[str, int]]], float]:
    """
    Returns ({team_abbrev: sorted-asc list of (date, runs_scored)}, mlb_avg_rpg).
    """
    with open(game_log_path, encoding="utf-8") as f:
        d = json.load(f)
    by_team: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for g in d.get("games", []):
        by_team[g["away"]].append((g["date"], int(g["away_runs"])))
        by_team[g["home"]].append((g["date"], int(g["home_runs"])))
    for t in by_team:
        by_team[t].sort(key=lambda x: x[0])
    mlb_avg = float(d.get("mlb_avg_rpg", 4.45))
    return by_team, mlb_avg


def _bisect_strict_less(items: List[Tuple[str, int]], date: str) -> int:
    """Return index of first item with date >= `date` (so items[:idx] are all strictly before)."""
    lo, hi = 0, len(items)
    while lo < hi:
        mid = (lo + hi) // 2
        if items[mid][0] < date:
            lo = mid + 1
        else:
            hi = mid
    return lo


def rolling_rpg(history: List[Tuple[str, int]], before_date: str, n: int = N_GAMES_WINDOW) -> Optional[float]:
    """Mean runs scored over the last `n` games strictly before `before_date`."""
    idx = _bisect_strict_less(history, before_date)
    window = history[max(0, idx - n):idx]
    if len(window) < MIN_GAMES_FOR_RPG:
        return None
    return sum(r for _, r in window) / len(window)


# ---------------------------------------------------------------------------
# Aggregation buckets
# ---------------------------------------------------------------------------


class Bucket:
    __slots__ = ("n", "sum_y", "sum_p", "sum_p_logit", "sum_y_minus_p", "sum_brier")

    def __init__(self) -> None:
        self.n = 0
        self.sum_y = 0
        self.sum_p = 0.0
        self.sum_p_logit = 0.0
        self.sum_y_minus_p = 0.0
        self.sum_brier = 0.0

    def add(self, y: int, p: float) -> None:
        self.n += 1
        self.sum_y += y
        self.sum_p += p
        self.sum_p_logit += _logit(p)
        self.sum_y_minus_p += (y - p)
        self.sum_brier += (y - p) ** 2

    def summary(self) -> Dict[str, float]:
        if self.n == 0:
            return {"n": 0}
        mean_y = self.sum_y / self.n
        mean_p = self.sum_p / self.n
        mean_logit_p = self.sum_p_logit / self.n
        # "Implied logit shift" -- how much we'd need to add to logit(p) so that
        # mean(sigmoid(logit(p) + delta)) ~= mean(y). Closed-form is messy;
        # report the simpler "mean residual on probability scale" plus a
        # logit-space approximation: logit(mean_y) - mean(logit(p)).
        try:
            implied_logit_shift = _logit(mean_y) - mean_logit_p
        except Exception:
            implied_logit_shift = float("nan")
        return {
            "n": self.n,
            "mean_realized": round(mean_y, 4),
            "mean_predicted": round(mean_p, 4),
            "mean_residual_prob": round(mean_y - mean_p, 4),
            "implied_logit_shift": round(implied_logit_shift, 4),
            "mean_brier": round(self.sum_brier / self.n, 4),
        }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def matchup_regime(away_rpg: Optional[float], home_rpg: Optional[float], mlb_avg: float) -> str:
    if away_rpg is None or home_rpg is None:
        return "missing_rpg"
    diff = (away_rpg + home_rpg) - 2.0 * mlb_avg
    if diff > 0.5:
        return "high_offense"
    if diff < -0.5:
        return "low_offense"
    return "avg_offense"


def runs_diff_bucket(away_rpg: Optional[float], home_rpg: Optional[float], mlb_avg: float) -> str:
    """Finer regime split for the implied-shift-per-run check."""
    if away_rpg is None or home_rpg is None:
        return "missing_rpg"
    diff = (away_rpg + home_rpg) - 2.0 * mlb_avg
    if diff <= -1.5:
        return "<=-1.5"
    if diff <= -0.5:
        return "-1.5_to_-0.5"
    if diff < 0.5:
        return "-0.5_to_+0.5"
    if diff < 1.5:
        return "+0.5_to_+1.5"
    return ">=+1.5"


def run(args) -> None:
    LOGGER.info("Loading team history from %s", args.game_log)
    by_team, mlb_avg = build_team_history(args.game_log)
    LOGGER.info("  %d teams, mlb_avg_rpg=%.3f", len(by_team), mlb_avg)

    LOGGER.info("Streaming training table from %s", args.training_table)

    overall = Bucket()
    by_inning: Dict[int, Bucket] = defaultdict(Bucket)
    by_line: Dict[float, Bucket] = defaultdict(Bucket)
    by_season: Dict[str, Bucket] = defaultdict(Bucket)
    by_regime: Dict[str, Bucket] = defaultdict(Bucket)
    by_diff_bucket: Dict[str, Bucket] = defaultdict(Bucket)
    by_inning_line: Dict[Tuple[int, float], Bucket] = defaultdict(Bucket)
    by_inning_regime: Dict[Tuple[int, str], Bucket] = defaultdict(Bucket)
    by_diff_inning: Dict[Tuple[str, int], Bucket] = defaultdict(Bucket)

    # Cache RPG lookups by (team, date) since multiple lines/innings share them.
    rpg_cache: Dict[Tuple[str, str], Optional[float]] = {}

    def get_rpg(team: str, date: str) -> Optional[float]:
        key = (team, date)
        if key in rpg_cache:
            return rpg_cache[key]
        history = by_team.get(team, [])
        v = rolling_rpg(history, date)
        rpg_cache[key] = v
        return v

    n_processed = 0
    n_with_rpg = 0
    with open(args.training_table, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            p = r.get("base_fv_stage1_plus_stage2")
            if p is None:
                continue
            y = int(r["over_hit"])
            inning = int(r["inning"])
            line_val = float(r["line"])
            season = r["season"]

            away_rpg = get_rpg(r["away"], r["date"])
            home_rpg = get_rpg(r["home"], r["date"])
            regime = matchup_regime(away_rpg, home_rpg, mlb_avg)
            diff = runs_diff_bucket(away_rpg, home_rpg, mlb_avg)

            overall.add(y, p)
            by_inning[inning].add(y, p)
            by_line[line_val].add(y, p)
            by_season[season].add(y, p)
            by_regime[regime].add(y, p)
            by_diff_bucket[diff].add(y, p)
            by_inning_line[(inning, line_val)].add(y, p)
            by_inning_regime[(inning, regime)].add(y, p)
            by_diff_inning[(diff, inning)].add(y, p)

            if regime != "missing_rpg":
                n_with_rpg += 1
            n_processed += 1
            if n_processed % 200000 == 0:
                LOGGER.info("  %d rows processed", n_processed)

    LOGGER.info("Total rows processed: %d (%d with RPG)", n_processed, n_with_rpg)

    report = {
        "schema_version": 1,
        "training_table": str(args.training_table),
        "n_rows": n_processed,
        "n_rows_with_rpg": n_with_rpg,
        "mlb_avg_rpg": round(mlb_avg, 4),
        "rolling_rpg_window": N_GAMES_WINDOW,
        "overall": overall.summary(),
        "by_inning": {str(k): by_inning[k].summary() for k in sorted(by_inning)},
        "by_line": {f"{k:.1f}": by_line[k].summary() for k in sorted(by_line)},
        "by_season": {k: by_season[k].summary() for k in sorted(by_season)},
        "by_regime": {k: by_regime[k].summary() for k in by_regime},
        "by_diff_bucket": {k: by_diff_bucket[k].summary() for k in
                           ["<=-1.5", "-1.5_to_-0.5", "-0.5_to_+0.5", "+0.5_to_+1.5", ">=+1.5", "missing_rpg"]
                           if k in by_diff_bucket},
        "by_inning_line": {f"{k[0]}|{k[1]:.1f}": by_inning_line[k].summary() for k in sorted(by_inning_line)},
        "by_inning_regime": {f"{k[0]}|{k[1]}": by_inning_regime[k].summary() for k in sorted(by_inning_regime)},
        "by_diff_inning": {f"{k[0]}|{k[1]}": by_diff_inning[k].summary() for k in sorted(by_diff_inning, key=lambda x: (x[1], x[0]))},
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    LOGGER.info("Wrote %s", args.output)

    # Console summary -----------------------------------------------------
    print()
    print("=== OVERALL ===")
    o = report["overall"]
    print(f"  n={o['n']:,}  mean_realized={o['mean_realized']}  "
          f"mean_predicted={o['mean_predicted']}  "
          f"residual={o['mean_residual_prob']:+.4f}  "
          f"implied_logit_shift={o['implied_logit_shift']:+.4f}  "
          f"brier={o['mean_brier']}")

    print()
    print("=== BY INNING (residual + implied logit shift) ===")
    print(f"  {'inn':>4} {'n':>10} {'realized':>10} {'predicted':>10} {'residual':>10} {'logit_shift':>13} {'brier':>8}")
    for inn in sorted(report["by_inning"], key=int):
        s = report["by_inning"][inn]
        print(f"  {inn:>4} {s['n']:>10,} {s['mean_realized']:>10.4f} {s['mean_predicted']:>10.4f} "
              f"{s['mean_residual_prob']:>+10.4f} {s['implied_logit_shift']:>+13.4f} {s['mean_brier']:>8.4f}")

    print()
    print("=== BY LINE ===")
    print(f"  {'line':>5} {'n':>10} {'realized':>10} {'predicted':>10} {'residual':>10} {'logit_shift':>13}")
    for ln in sorted(report["by_line"], key=float):
        s = report["by_line"][ln]
        print(f"  {ln:>5} {s['n']:>10,} {s['mean_realized']:>10.4f} {s['mean_predicted']:>10.4f} "
              f"{s['mean_residual_prob']:>+10.4f} {s['implied_logit_shift']:>+13.4f}")

    print()
    print("=== BY MATCHUP REGIME (offense vs MLB avg) ===")
    print(f"  {'regime':>15} {'n':>10} {'realized':>10} {'predicted':>10} {'residual':>10} {'logit_shift':>13}")
    for k in ["high_offense", "avg_offense", "low_offense", "missing_rpg"]:
        if k not in report["by_regime"]:
            continue
        s = report["by_regime"][k]
        print(f"  {k:>15} {s['n']:>10,} {s['mean_realized']:>10.4f} {s['mean_predicted']:>10.4f} "
              f"{s['mean_residual_prob']:>+10.4f} {s['implied_logit_shift']:>+13.4f}")

    print()
    print("=== BY EXPECTED-TOTAL DIFF FROM MLB AVG (logit shift / run check) ===")
    print(f"  {'diff':>16} {'n':>10} {'realized':>10} {'predicted':>10} {'residual':>10} {'logit_shift':>13}")
    last_shift = None
    for k in ["<=-1.5", "-1.5_to_-0.5", "-0.5_to_+0.5", "+0.5_to_+1.5", ">=+1.5"]:
        if k not in report["by_diff_bucket"]:
            continue
        s = report["by_diff_bucket"][k]
        print(f"  {k:>16} {s['n']:>10,} {s['mean_realized']:>10.4f} {s['mean_predicted']:>10.4f} "
              f"{s['mean_residual_prob']:>+10.4f} {s['implied_logit_shift']:>+13.4f}")
        last_shift = s

    print()
    print("=== BY SEASON ===")
    print(f"  {'season':>8} {'n':>10} {'realized':>10} {'predicted':>10} {'residual':>10} {'logit_shift':>13}")
    for k in sorted(report["by_season"]):
        s = report["by_season"][k]
        print(f"  {k:>8} {s['n']:>10,} {s['mean_realized']:>10.4f} {s['mean_predicted']:>10.4f} "
              f"{s['mean_residual_prob']:>+10.4f} {s['implied_logit_shift']:>+13.4f}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--training-table", type=Path, default=DEFAULT_TABLE)
    p.add_argument("--game-log", type=Path, default=DEFAULT_GAME_LOG)
    p.add_argument("--output", type=Path, default=DEFAULT_OUT)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run(args)


if __name__ == "__main__":
    main()
