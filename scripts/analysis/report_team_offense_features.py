#!/usr/bin/env python3
"""
report_team_offense_features.py -- Phase 2b sanity report.

Reports on the per-(team, date) feature table from
`build_team_offense_features.py`:

  1. Distribution stats per feature (n / mean / std / p5 / p25 / p50 / p75 / p95)
  2. Pairwise Pearson correlation between numeric features (sample-based)
  3. Single-feature predictive power for next-game runs scored:
       For each (team, date), join the team's actual runs on that date and
       compute the feature's:
         - Pearson correlation with realized runs
         - Mean Squared Error of using `feature_value` as a prediction
         - vs the trivial mean-MLB-RPG baseline

The point: tell us which single windows / decompositions actually predict
next-game runs, so Phase 4 can build a sensible blend.

Output:
  data/analysis_output/team_offense_calibration/phase2_feature_report.json
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
DEFAULT_FEATURES = PROJECT_DIR / "data" / "analysis_output" / "team_offense_calibration" / "team_features.jsonl"
DEFAULT_GAME_LOG = PROJECT_DIR / "cache" / "team_game_log.json"
DEFAULT_OUTPUT = PROJECT_DIR / "data" / "analysis_output" / "team_offense_calibration" / "phase2_feature_report.json"

LOGGER = logging.getLogger("phase2_feature_report")


# ---------------------------------------------------------------------------
# Stats helpers (no numpy dependency)
# ---------------------------------------------------------------------------


def percentiles(sorted_values: List[float], qs: Tuple[float, ...]) -> Dict[str, float]:
    if not sorted_values:
        return {f"p{int(q*100)}": float("nan") for q in qs}
    out = {}
    for q in qs:
        idx = q * (len(sorted_values) - 1)
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            out[f"p{int(q*100)}"] = sorted_values[lo]
        else:
            frac = idx - lo
            out[f"p{int(q*100)}"] = sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac
    return out


def describe(values: List[float]) -> Dict[str, float]:
    n = len(values)
    if n == 0:
        return {"n": 0}
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / max(1, n - 1)
    std = math.sqrt(var)
    sv = sorted(values)
    out = {"n": n, "mean": round(mean, 4), "std": round(std, 4),
           "min": round(sv[0], 4), "max": round(sv[-1], 4)}
    out.update({k: round(v, 4) for k, v in percentiles(sv, (0.05, 0.25, 0.5, 0.75, 0.95)).items()})
    return out


def pearson(xs: List[float], ys: List[float]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx2 = sum((x - mx) ** 2 for x in xs)
    dy2 = sum((y - my) ** 2 for y in ys)
    den = math.sqrt(dx2 * dy2)
    if den == 0:
        return float("nan")
    return num / den


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def load_features(path: Path) -> Tuple[Dict[Tuple[str, str], Dict[str, float]], List[str]]:
    rows: Dict[Tuple[str, str], Dict[str, float]] = {}
    feature_keys: List[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            team = r.pop("team")
            date = r.pop("date")
            rows[(team, date)] = r
            if not feature_keys:
                feature_keys = list(r.keys())
    return rows, feature_keys


def load_realized_runs(game_log_path: Path) -> Dict[Tuple[str, str], int]:
    """Returns {(team, date): runs_scored_in_first_game_that_date}."""
    with open(game_log_path, encoding="utf-8") as f:
        d = json.load(f)
    runs_by: Dict[Tuple[str, str], int] = {}
    for g in d.get("games", []):
        # In rare doubleheaders, prefer the first encountered (we only need one
        # outcome per (team, date) for the predictive-power check).
        for team, runs in [(g["away"], int(g["away_runs"])), (g["home"], int(g["home_runs"]))]:
            key = (team, g["date"])
            if key not in runs_by:
                runs_by[key] = runs
    return runs_by


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run(args) -> None:
    LOGGER.info("Loading features from %s", args.features)
    features, feature_keys = load_features(args.features)
    LOGGER.info("  %d (team,date) rows; features: %s", len(features), feature_keys)

    LOGGER.info("Loading realized runs from %s", args.game_log)
    runs = load_realized_runs(args.game_log)

    # Distribution stats per feature ---------------------------------------
    by_feature_values: Dict[str, List[float]] = defaultdict(list)
    for r in features.values():
        for k, v in r.items():
            if isinstance(v, (int, float)):
                by_feature_values[k].append(float(v))
    distributions = {k: describe(v) for k, v in by_feature_values.items()}

    # Correlation matrix (Pearson) -----------------------------------------
    keys_for_corr = [k for k in feature_keys if k != "n_games_in_season"]
    feature_matrix: Dict[str, List[float]] = {k: [] for k in keys_for_corr}
    for r in features.values():
        # Only include rows where ALL these features are non-null.
        if any(r.get(k) is None for k in keys_for_corr):
            continue
        for k in keys_for_corr:
            feature_matrix[k].append(float(r[k]))
    n_complete = len(next(iter(feature_matrix.values()))) if feature_matrix else 0
    LOGGER.info("Pairwise correlation on %d complete-row sample", n_complete)
    correlation = {}
    for i, ki in enumerate(keys_for_corr):
        row = {}
        for kj in keys_for_corr:
            row[kj] = round(pearson(feature_matrix[ki], feature_matrix[kj]), 4)
        correlation[ki] = row

    # Predictive power on next-game runs -----------------------------------
    LOGGER.info("Joining next-game runs to features for predictive-power check")
    pred_features = [k for k in feature_keys if k.endswith("_rpg") or k.startswith(("season_rpg", "prior_season", "momentum_rpg", "rolling_rpg", "home_rpg", "road_rpg", "decay_weighted"))]
    pred_features = [k for k in pred_features if k in feature_keys]
    # Only meaningful predictors:
    predictive_keys = [
        "prior_season_rpg",
        "season_rpg_to_date",
        "momentum_rpg_5",
        "momentum_rpg_10",
        "momentum_rpg_20",
        "rolling_rpg_50",
        "decay_weighted_rpg",
        "home_rpg_30",
        "road_rpg_30",
    ]

    paired: Dict[str, List[Tuple[float, float]]] = {k: [] for k in predictive_keys}
    pairs_full: List[Tuple[Dict[str, float], int]] = []  # for blend baselines

    for (team, date), feat in features.items():
        actual = runs.get((team, date))
        if actual is None:
            continue
        for k in predictive_keys:
            v = feat.get(k)
            if v is not None:
                paired[k].append((float(v), float(actual)))
        if all(feat.get(k) is not None for k in ["season_rpg_to_date", "prior_season_rpg", "momentum_rpg_10"]):
            pairs_full.append((feat, actual))

    mean_actual = (
        sum(actual for _, actual in pairs_full) / len(pairs_full) if pairs_full else 0.0
    )
    base_mse = (
        sum((mean_actual - actual) ** 2 for _, actual in pairs_full) / len(pairs_full)
        if pairs_full
        else float("nan")
    )

    predictive_power: Dict[str, Dict[str, float]] = {}
    for k in predictive_keys:
        pairs = paired[k]
        if len(pairs) < 100:
            predictive_power[k] = {"n": len(pairs)}
            continue
        xs = [v for v, _ in pairs]
        ys = [a for _, a in pairs]
        r = pearson(xs, ys)
        mse = sum((v - a) ** 2 for v, a in pairs) / len(pairs)
        predictive_power[k] = {
            "n": len(pairs),
            "pearson_r": round(r, 4),
            "mse_as_predictor": round(mse, 4),
            "mse_baseline_mean_mlb": round(base_mse, 4),
            "mse_reduction_vs_baseline": round((base_mse - mse) / base_mse, 4) if base_mse > 0 else None,
        }

    # Best-of pairwise blends (simple convex combinations).
    # Just exploratory: weight-grid 0.0/0.1/.../1.0 between two features.
    LOGGER.info("Searching small blend grid (season vs momentum_10)")
    best_blends: Dict[str, Dict[str, float]] = {}
    grids = [
        ("season_rpg_to_date", "momentum_rpg_10"),
        ("season_rpg_to_date", "prior_season_rpg"),
        ("rolling_rpg_50", "season_rpg_to_date"),
        ("decay_weighted_rpg", "season_rpg_to_date"),
        ("decay_weighted_rpg", "prior_season_rpg"),
    ]
    for (ka, kb) in grids:
        best = None
        for w in [round(x * 0.05, 2) for x in range(0, 21)]:
            xs, ys = [], []
            for feat, actual in pairs_full:
                a = feat.get(ka)
                b = feat.get(kb)
                if a is None or b is None:
                    continue
                xs.append(w * a + (1 - w) * b)
                ys.append(float(actual))
            if len(xs) < 100:
                continue
            mse = sum((x - y) ** 2 for x, y in zip(xs, ys)) / len(xs)
            r = pearson(xs, ys)
            if best is None or mse < best["mse"]:
                best = {"weight_a": w, "weight_b": round(1 - w, 2), "mse": round(mse, 4),
                        "pearson_r": round(r, 4), "n": len(xs)}
        if best:
            best_blends[f"{ka} x {kb}"] = best

    out = {
        "schema_version": 1,
        "n_team_date_rows": len(features),
        "n_with_realized_runs": sum(1 for k in features if k in runs),
        "n_complete_correlation_sample": n_complete,
        "distributions": distributions,
        "correlation": correlation,
        "predictive_power_next_game_runs": predictive_power,
        "blend_grid_search": best_blends,
        "comment": (
            "Predictive power is `single feature value as direct point-prediction "
            "of runs scored next game`. MSE-of-mean is the baseline. Use this only "
            "as a relative indicator -- a feature that beats baseline by 5% MSE is "
            "carrying signal. Phase 4 fits proper joint regressions with the "
            "Stage-1+2 logit as offset."
        ),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    LOGGER.info("Wrote %s", args.output)

    # Console summary -------------------------------------------------------
    print()
    print("=== Distributions (key features) ===")
    print(f"  {'feature':>22} {'n':>8} {'mean':>7} {'std':>6} {'p5':>6} {'p50':>6} {'p95':>6}")
    for k in ["prior_season_rpg", "season_rpg_to_date", "momentum_rpg_5", "momentum_rpg_10",
              "momentum_rpg_20", "rolling_rpg_50", "decay_weighted_rpg",
              "home_rpg_30", "road_rpg_30"]:
        s = distributions.get(k, {})
        if s.get("n"):
            print(f"  {k:>22} {s['n']:>8,} {s['mean']:>7.3f} {s['std']:>6.3f} "
                  f"{s['p5']:>6.2f} {s['p50']:>6.2f} {s['p95']:>6.2f}")

    print()
    print("=== Predictive power: how well each single feature predicts next-game runs ===")
    print(f"  baseline (predict mean MLB RPG): MSE = {base_mse:.4f}, n={len(pairs_full):,}")
    print(f"  {'feature':>22} {'n':>8} {'corr':>7} {'mse':>7} {'reduction_vs_base':>18}")
    for k in predictive_keys:
        s = predictive_power.get(k, {})
        if "pearson_r" in s:
            red = s.get("mse_reduction_vs_baseline")
            red_str = f"{red:+.2%}" if red is not None else "n/a"
            print(f"  {k:>22} {s['n']:>8,} {s['pearson_r']:>+7.4f} {s['mse_as_predictor']:>7.4f} {red_str:>18}")

    print()
    print("=== Best blends (simple convex combinations, grid search) ===")
    print(f"  {'pair':>50} {'w_a':>5} {'w_b':>5} {'mse':>7} {'corr':>7}")
    for k, v in best_blends.items():
        print(f"  {k:>50} {v['weight_a']:>5.2f} {v['weight_b']:>5.2f} {v['mse']:>7.4f} {v['pearson_r']:>+7.4f}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--features", type=Path, default=DEFAULT_FEATURES)
    p.add_argument("--game-log", type=Path, default=DEFAULT_GAME_LOG)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run(args)


if __name__ == "__main__":
    main()
