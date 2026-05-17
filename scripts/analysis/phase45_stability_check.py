#!/usr/bin/env python3
"""
phase45_stability_check.py -- Sub-window stability check on Model 3 coefficients.

Phase 4 fit Model 3 on the full 2021-2024 training window and reported a
counter-intuitive negative coefficient on `prior_season_rpg`. Before shipping
v2, we want to confirm that sign is stable across sub-windows of the training
data, not a property of a single anomalous season.

Sub-windows:
  - 2021 alone
  - 2022 alone
  - 2023 alone
  - 2024 alone
  - 2021-2022
  - 2022-2023
  - 2023-2024
  - 2021-2024 (the Phase 4 fit, included for direct comparison)

For each sub-window:
  - Refit Model 3 (b_prior, b_season, b_momentum) using training-only EB
    shrinkage parameters.
  - Walk-forward validation on the FOLLOWING season (e.g. 2021->2022 val,
    2021-2022->2023 val, 2023-2024->2025 val).
  - Report Brier on the validation season and whether b_prior stays negative.

Output:
  data/analysis_output/team_offense_calibration/phase45_stability.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

PROJECT_DIR = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
DEFAULT_TABLE = PROJECT_DIR / "data" / "analysis_output" / "team_offense_calibration" / "training_table.jsonl"
DEFAULT_FEATURES = PROJECT_DIR / "data" / "analysis_output" / "team_offense_calibration" / "team_features.jsonl"
DEFAULT_GAME_LOG = PROJECT_DIR / "cache" / "team_game_log.json"
DEFAULT_OUTPUT = PROJECT_DIR / "data" / "analysis_output" / "team_offense_calibration" / "phase45_stability.json"

LOGGER = logging.getLogger("phase45_stability_check")

# Reuse helpers from calibrate_team_offense_v2 by direct path import.
sys.path.insert(0, str(ANALYSIS_DIR))
from calibrate_team_offense_v2 import (  # noqa: E402
    load_features,
    load_mlb_avg,
    load_table_join_features,
    estimate_within_variance,
    estimate_tau_sq,
    shrink,
    fit_model_3,
    predict_model_3,
    brier,
    log_loss,
    EPS,
)


def slice_data(arr: Dict[str, np.ndarray], mask: np.ndarray) -> Dict[str, np.ndarray]:
    return {k: v[mask] for k, v in arr.items() if isinstance(v, np.ndarray)}


def run_subwindow(
    data: Dict[str, np.ndarray],
    features: Dict,
    mlb_avg: float,
    train_seasons: Tuple[str, ...],
    val_seasons: Tuple[str, ...],
) -> Dict[str, object]:
    """Refit Model 3 with EB shrinkage parameters estimated on this train window."""
    # Estimate sigma^2 and tau^2 ONLY on the sub-window training years
    sigma_sq = estimate_within_variance(None, features, mlb_avg, train_seasons)
    eff_n = {
        "rolling_rpg_50": 50,
        "season_rpg_to_date": 70,
        "prior_season_rpg": 162,
        "momentum_rpg_10": 10,
    }
    tau_sq_window: Dict[str, float] = {}
    for k, n_w in eff_n.items():
        tau_sq_window[k] = estimate_tau_sq(features, train_seasons, k, n_w, sigma_sq)

    # Build a copy of `data` with this sub-window's shrinkage applied.
    # Important: shrinkage parameters change per sub-window (sigma, tau both
    # estimated on the sub-window's training years).
    sub = dict(data)
    for k, n_w in eff_n.items():
        for side in ("away", "home"):
            raw = sub[f"{side}_{k}"]
            sub[f"{side}_{k}_shrunk"] = shrink(raw, n_w, mlb_avg, sigma_sq, tau_sq_window[k])

    train_mask = np.isin(sub["season"], list(train_seasons))
    val_mask = np.isin(sub["season"], list(val_seasons))
    train = slice_data(sub, train_mask)
    val = slice_data(sub, val_mask)

    sk_blend = {
        "prior":    {"away": "away_prior_season_rpg_shrunk",   "home": "home_prior_season_rpg_shrunk"},
        "season":   {"away": "away_season_rpg_to_date_shrunk", "home": "home_season_rpg_to_date_shrunk"},
        "momentum": {"away": "away_momentum_rpg_10_shrunk",    "home": "home_momentum_rpg_10_shrunk"},
    }

    if len(train["y"]) < 1000:
        return {"train_seasons": list(train_seasons), "n_train": int(len(train["y"])),
                "error": "insufficient training data"}

    m3 = fit_model_3(train, mlb_avg, sk_blend)

    out: Dict[str, object] = {
        "train_seasons": list(train_seasons),
        "val_seasons": list(val_seasons),
        "n_train": int(len(train["y"])),
        "n_val": int(len(val["y"])),
        "sigma_within_sq": round(sigma_sq, 4),
        "tau_sq_per_window": {k: round(v, 4) for k, v in tau_sq_window.items()},
        "betas": {k: round(v, 4) for k, v in m3.items()},
        "b_prior_sign": "negative" if m3["beta_prior"] < 0 else "positive",
    }

    if len(val["y"]) > 0:
        # Score Model 3 vs Baseline B (v1) and Baseline A on validation.
        p_m3 = predict_model_3(val, m3, mlb_avg, sk_blend)
        p_a = val["p"]  # Baseline A: no Stage-3
        # Baseline B v1
        a = np.clip(val["away_rolling_rpg_50"], 0.55 * mlb_avg, 1.55 * mlb_avg)
        h = np.clip(val["home_rolling_rpg_50"], 0.55 * mlb_avg, 1.55 * mlb_avg)
        diff_v1 = (a + h) - 2.0 * mlb_avg
        w_lin = np.maximum(0.0, (9.0 - val["inning"].astype(np.float64)) / 8.0)
        delta_v1 = np.clip(0.20 * diff_v1 * w_lin, -0.60, 0.60)
        p_b = 1.0 / (1.0 + np.exp(-(val["logit_p"] + delta_v1)))
        out["val_brier_baseline_A"] = round(brier(val["y"], p_a), 6)
        out["val_brier_baseline_B"] = round(brier(val["y"], p_b), 6)
        out["val_brier_model_3"] = round(brier(val["y"], p_m3), 6)
        out["val_logloss_model_3"] = round(log_loss(val["y"], p_m3), 6)
        # Relative Brier improvement of Model 3 vs A and B
        out["val_brier_pct_vs_A"] = round((out["val_brier_baseline_A"] - out["val_brier_model_3"]) / out["val_brier_baseline_A"] * 100, 4)
        out["val_brier_pct_vs_B"] = round((out["val_brier_baseline_B"] - out["val_brier_model_3"]) / out["val_brier_baseline_B"] * 100, 4)

    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--training-table", type=Path, default=DEFAULT_TABLE)
    p.add_argument("--features", type=Path, default=DEFAULT_FEATURES)
    p.add_argument("--game-log", type=Path, default=DEFAULT_GAME_LOG)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    LOGGER.info("Loading features and joining calibration table")
    features = load_features(args.features)
    mlb_avg = load_mlb_avg(args.game_log)
    LOGGER.info("  mlb_avg_rpg=%.3f, %d (team,date) feature rows", mlb_avg, len(features))

    required = ["rolling_rpg_50", "prior_season_rpg", "season_rpg_to_date", "momentum_rpg_10"]
    data = load_table_join_features(args.training_table, features, required)
    LOGGER.info("  joined rows: %d", len(data["y"]))

    # Sub-windows: train -> val
    sub_windows = [
        # Single seasons
        (("2021",), ("2022",)),
        (("2022",), ("2023",)),
        (("2023",), ("2024",)),
        (("2024",), ("2025",)),
        # Two-year windows
        (("2021", "2022"), ("2023",)),
        (("2022", "2023"), ("2024",)),
        (("2023", "2024"), ("2025",)),
        # Three-year window
        (("2021", "2022", "2023"), ("2024",)),
        # Full training window (Phase 4 reference)
        (("2021", "2022", "2023", "2024"), ("2025",)),
    ]

    results: List[Dict] = []
    for tr, vl in sub_windows:
        LOGGER.info("Sub-window train=%s -> val=%s", tr, vl)
        r = run_subwindow(data, features, mlb_avg, tr, vl)
        if "betas" in r:
            b = r["betas"]
            LOGGER.info("  betas: prior=%+.4f season=%+.4f momentum=%+.4f  |  val_brier_M3=%.6f vs B=%.6f (%.3f%% rel)",
                        b["beta_prior"], b["beta_season"], b["beta_momentum"],
                        r.get("val_brier_model_3", float("nan")),
                        r.get("val_brier_baseline_B", float("nan")),
                        r.get("val_brier_pct_vs_B", float("nan")))
        results.append(r)

    payload = {
        "schema_version": 1,
        "n_calibration_rows": int(len(data["y"])),
        "mlb_avg_rpg": round(mlb_avg, 4),
        "subwindows": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    LOGGER.info("Wrote %s", args.output)

    # Console summary -----------------------------------------------------
    print()
    print("=== SUB-WINDOW STABILITY OF MODEL 3 COEFFICIENTS ===")
    print(f"  {'train':<24} {'val':<10} {'n_train':>8}  {'b_prior':>9} {'b_season':>9} {'b_momentum':>11}  {'val_brier_M3':>13} {'val_pct_vs_B':>12}")
    for r in results:
        if "betas" not in r:
            continue
        b = r["betas"]
        train_str = "+".join(r["train_seasons"])
        val_str = "+".join(r["val_seasons"])
        print(f"  {train_str:<24} {val_str:<10} {r['n_train']:>8,}  "
              f"{b['beta_prior']:>+9.4f} {b['beta_season']:>+9.4f} {b['beta_momentum']:>+11.4f}  "
              f"{r.get('val_brier_model_3', float('nan')):>13.6f} {r.get('val_brier_pct_vs_B', float('nan')):>+11.3f}%")

    print()
    print("=== STABILITY SUMMARY ===")
    n_neg = sum(1 for r in results if r.get("betas", {}).get("beta_prior", 0) < 0)
    n_total = sum(1 for r in results if "betas" in r)
    print(f"  b_prior NEGATIVE in {n_neg}/{n_total} sub-windows")
    n_pos_season = sum(1 for r in results if r.get("betas", {}).get("beta_season", 0) > 0)
    n_pos_mom = sum(1 for r in results if r.get("betas", {}).get("beta_momentum", 0) > 0)
    print(f"  b_season POSITIVE in {n_pos_season}/{n_total} sub-windows")
    print(f"  b_momentum POSITIVE in {n_pos_mom}/{n_total} sub-windows")
    n_brier_better = sum(1 for r in results if r.get("val_brier_pct_vs_B", -999) > 0)
    print(f"  Model 3 beats Baseline B on val Brier in {n_brier_better}/{n_total} sub-windows")


if __name__ == "__main__":
    main()
