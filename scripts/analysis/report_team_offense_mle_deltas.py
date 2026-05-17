#!/usr/bin/env python3
"""
report_team_offense_mle_deltas.py -- Phase 1 sidecar: MLE logit-delta fits.

For each (offense-diff bucket) and each (offense-diff bucket x inning) cell,
fit the MLE additive logit-delta that would best calibrate
`base_fv_stage1_plus_stage2` to the observed `over_hit` outcomes.

Model: y_i ~ Bernoulli(sigmoid(logit(p_i) + delta))
MLE solved by Newton iteration on the log-likelihood.

This is the *honest* answer to "what additive logit shift would Stage-3 need
to apply on this subgroup?" -- replaces the Jensen-biased
`logit(mean_y) - mean(logit_p)` heuristic from the basic residual report.

Output:
  data/analysis_output/team_offense_calibration/phase1_mle_deltas.json
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
DEFAULT_OUT = PROJECT_DIR / "data" / "analysis_output" / "team_offense_calibration" / "phase1_mle_deltas.json"
DEFAULT_GAME_LOG = PROJECT_DIR / "cache" / "team_game_log.json"

LOGGER = logging.getLogger("phase1_mle_deltas")
EPS = 1e-6
N_GAMES_WINDOW = 50
MIN_GAMES_FOR_RPG = 10


def _clamp01(p): return max(EPS, min(1.0 - EPS, p))
def _logit(p):  return math.log(_clamp01(p) / (1.0 - _clamp01(p)))
def _sigmoid(x): return 1.0 / (1.0 + math.exp(-x))


def fit_delta(rows: List[Tuple[int, float]], max_iter: int = 50, tol: float = 1e-9) -> Optional[float]:
    """Newton's method MLE for additive logit shift. Returns None if rows empty."""
    if not rows:
        return None
    delta = 0.0
    for _ in range(max_iter):
        score = 0.0
        hess = 0.0
        for y, lp in rows:
            p = _sigmoid(lp + delta)
            score += (y - p)
            hess += p * (1 - p)
        if hess < 1e-12:
            break
        step = score / hess
        delta += step
        if abs(step) < tol:
            break
    return delta


def build_team_history(path: Path) -> Tuple[Dict[str, List[Tuple[str, int]]], float]:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    by_team: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for g in d.get("games", []):
        by_team[g["away"]].append((g["date"], int(g["away_runs"])))
        by_team[g["home"]].append((g["date"], int(g["home_runs"])))
    for t in by_team:
        by_team[t].sort(key=lambda x: x[0])
    return by_team, float(d.get("mlb_avg_rpg", 4.45))


def _bisect_strict_less(items: List[Tuple[str, int]], date: str) -> int:
    lo, hi = 0, len(items)
    while lo < hi:
        mid = (lo + hi) // 2
        if items[mid][0] < date:
            lo = mid + 1
        else:
            hi = mid
    return lo


def rolling_rpg(history: List[Tuple[str, int]], before_date: str) -> Optional[float]:
    idx = _bisect_strict_less(history, before_date)
    window = history[max(0, idx - N_GAMES_WINDOW):idx]
    if len(window) < MIN_GAMES_FOR_RPG:
        return None
    return sum(r for _, r in window) / len(window)


def diff_bucket_5way(diff: Optional[float]) -> Optional[str]:
    if diff is None:
        return None
    if diff <= -1.5: return "<=-1.5"
    if diff <= -0.5: return "-1.5_to_-0.5"
    if diff <  0.5: return "-0.5_to_+0.5"
    if diff <  1.5: return "+0.5_to_+1.5"
    return ">=+1.5"


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

    LOGGER.info("Loading team history")
    by_team, mlb_avg = build_team_history(args.game_log)

    LOGGER.info("Streaming training table")
    by_diff: Dict[str, List[Tuple[int, float]]] = defaultdict(list)
    by_diff_inn: Dict[Tuple[str, int], List[Tuple[int, float]]] = defaultdict(list)
    by_diff_line: Dict[Tuple[str, float], List[Tuple[int, float]]] = defaultdict(list)
    by_diff_continuous: Dict[float, List[Tuple[int, float]]] = defaultdict(list)
    rpg_cache: Dict[Tuple[str, str], Optional[float]] = {}

    def get_rpg(team: str, date: str) -> Optional[float]:
        k = (team, date)
        if k in rpg_cache:
            return rpg_cache[k]
        v = rolling_rpg(by_team.get(team, []), date)
        rpg_cache[k] = v
        return v

    n = 0
    with open(args.training_table, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            p = r.get("base_fv_stage1_plus_stage2")
            if p is None:
                continue
            y = int(r["over_hit"])
            lp = _logit(p)
            a = get_rpg(r["away"], r["date"])
            h = get_rpg(r["home"], r["date"])
            if a is None or h is None:
                continue
            diff = (a + h) - 2.0 * mlb_avg
            b = diff_bucket_5way(diff)
            if b is None:
                continue
            by_diff[b].append((y, lp))
            by_diff_inn[(b, int(r["inning"]))].append((y, lp))
            by_diff_line[(b, float(r["line"]))].append((y, lp))
            by_diff_continuous[round(diff * 2) / 2].append((y, lp))
            n += 1
            if n % 250000 == 0:
                LOGGER.info("  %d rows processed", n)

    LOGGER.info("Fitting MLE deltas...")
    out: Dict = {
        "schema_version": 1,
        "n_rows_with_rpg": n,
        "mlb_avg_rpg": round(mlb_avg, 4),
        "rolling_rpg_window": N_GAMES_WINDOW,
        "by_diff_bucket": {},
        "by_diff_inning": {},
        "by_diff_line": {},
        "by_diff_continuous": {},
    }
    for k in ["<=-1.5", "-1.5_to_-0.5", "-0.5_to_+0.5", "+0.5_to_+1.5", ">=+1.5"]:
        if k in by_diff:
            d = fit_delta(by_diff[k])
            out["by_diff_bucket"][k] = {"n": len(by_diff[k]), "mle_delta": round(d, 4)}

    for (b, inn) in sorted(by_diff_inn.keys(), key=lambda x: (x[1], x[0])):
        rows = by_diff_inn[(b, inn)]
        if len(rows) < 200:
            continue
        d = fit_delta(rows)
        out["by_diff_inning"][f"{b}|inn={inn}"] = {"n": len(rows), "mle_delta": round(d, 4)}

    for (b, ln) in sorted(by_diff_line.keys(), key=lambda x: (x[0], x[1])):
        rows = by_diff_line[(b, ln)]
        if len(rows) < 200:
            continue
        d = fit_delta(rows)
        out["by_diff_line"][f"{b}|line={ln}"] = {"n": len(rows), "mle_delta": round(d, 4)}

    for d_val in sorted(by_diff_continuous.keys()):
        rows = by_diff_continuous[d_val]
        if len(rows) < 1000:
            continue
        delta = fit_delta(rows)
        out["by_diff_continuous"][f"{d_val:+.2f}"] = {"n": len(rows), "mle_delta": round(delta, 4)}

    # Linear fit through the per-half-run continuous bucket centers (slope/run).
    pts = [(float(k), v["mle_delta"], v["n"]) for k, v in out["by_diff_continuous"].items() if abs(float(k)) <= 2.5]
    if pts:
        sw = sum(n for _, _, n in pts)
        sx = sum(x * n for x, _, n in pts) / sw
        sy = sum(y * n for _, y, n in pts) / sw
        sxx = sum((x - sx) ** 2 * n for x, _, n in pts) / sw
        sxy = sum((x - sx) * (y - sy) * n for x, y, n in pts) / sw
        slope = sxy / sxx if sxx > 0 else float("nan")
        intercept = sy - slope * sx
        out["per_run_linear_fit"] = {
            "slope_logit_per_run": round(slope, 4),
            "intercept": round(intercept, 4),
            "domain_runs": [-2.5, 2.5],
            "weighted_n": sw,
            "comment": (
                "MLE per-run logit slope, weighted by row count. Compare to "
                "current LOGIT_DELTA_PER_RUN=0.20 in team_offense_model.py."
            ),
        }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    LOGGER.info("Wrote %s", args.output)

    print()
    print("=== HEADLINE: per-run logit slope (vs current LOGIT_DELTA_PER_RUN = 0.20) ===")
    if "per_run_linear_fit" in out:
        f = out["per_run_linear_fit"]
        print(f"  slope = {f['slope_logit_per_run']:+.4f} logit per run "
              f"(intercept = {f['intercept']:+.4f}, weighted n = {f['weighted_n']:,})")
        print(f"  current model: 0.2000 logit/run -- ratio: "
              f"{f['slope_logit_per_run'] / 0.20:.2f}x")

    print()
    print("=== MLE delta by diff bucket ===")
    print(f"  {'diff_bucket':>16} {'n':>10} {'mle_delta':>10}")
    for k in ["<=-1.5", "-1.5_to_-0.5", "-0.5_to_+0.5", "+0.5_to_+1.5", ">=+1.5"]:
        if k in out["by_diff_bucket"]:
            v = out["by_diff_bucket"][k]
            print(f"  {k:>16} {v['n']:>10,} {v['mle_delta']:>+10.4f}")

    print()
    print("=== MLE delta vs inning, top/bottom-quartile diff buckets only ===")
    print(f"  {'bucket':>16} {'inn':>4} {'n':>10} {'mle_delta':>10}")
    for inn in range(1, 10):
        for k in ["<=-1.5", ">=+1.5"]:
            key = f"{k}|inn={inn}"
            if key in out["by_diff_inning"]:
                v = out["by_diff_inning"][key]
                print(f"  {k:>16} {inn:>4} {v['n']:>10,} {v['mle_delta']:>+10.4f}")


if __name__ == "__main__":
    main()
