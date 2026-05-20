#!/usr/bin/env python3
"""
scripts/analysis/recalibrate_gates.py

Performs counterfactual grid search over the post-FV gate stack parameters:
  - extreme_edge_max (phantom-run protection cap)
  - edge_threshold (min edge for line < 8.5)
  - edge_threshold_high_line (min edge for line >= 8.5)

For both Stage-3 v1 and Stage-3 v2 models, evaluating P&L and trade counts
over the historical calibration opportunities in:
  data/analysis_output/calibration_opportunity_training/calibration_opportunity_training_table.jsonl
"""

import sys
import math
import json
import bisect
from pathlib import Path
from typing import Dict, List, Tuple, Optional

# Set up paths to import modules
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "analysis"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "trading"))

from team_offense_model import TeamOffenseModel
from line_state import _ask_edge_boost


def _logit(p: float) -> float:
    p = max(1e-6, min(1.0 - 1e-6, p))
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def get_v1_delta(model: TeamOffenseModel, away: str, home: str, date: str, inning: int) -> float:
    mlb_avg = model.mlb_avg_rpg
    h_away = model._by_team.get(away)
    h_home = model._by_team.get(home)
    if not h_away or not h_home:
        return 0.0
    idx_away = bisect.bisect_left(h_away.dates_only, date)
    idx_home = bisect.bisect_left(h_home.dates_only, date)
    prior_away = h_away.entries[:idx_away]
    prior_home = h_home.entries[:idx_home]
    if len(prior_away) < 10 or len(prior_home) < 10:
        return 0.0
    r50_away = sum(r for _, r in prior_away[-50:]) / len(prior_away[-50:])
    r50_home = sum(r for _, r in prior_home[-50:]) / len(prior_home[-50:])
    a_c = max(0.55 * mlb_avg, min(1.55 * mlb_avg, r50_away))
    h_c = max(0.55 * mlb_avg, min(1.55 * mlb_avg, r50_home))
    diff = (a_c + h_c) - 2.0 * mlb_avg
    w = max(0.0, (9.0 - float(inning)) / 8.0)
    delta = max(-0.60, min(0.60, 0.20 * diff * w))
    return delta


def run_grid_search():
    table_path = PROJECT_ROOT / "data" / "analysis_output" / "calibration_opportunity_training" / "calibration_opportunity_training_table.jsonl"
    if not table_path.exists():
        print(f"Error: {table_path} not found.")
        sys.exit(1)

    print("Loading TeamOffenseModel...")
    model = TeamOffenseModel.load()

    print("Loading calibration opportunities...")
    opportunities = []
    with open(table_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            opportunities.append(json.loads(line))
    print(f"Loaded {len(opportunities)} opportunities.")

    # Cache the precalculated edges for v1 and v2 to speed up the grid search
    processed_rows = []
    skipped_rows_no_base_fv = 0

    for r in opportunities:
        family = r.get("signal_model_family")
        base_fv = r.get("base_fair_value")
        ask = r.get("decision_ask")
        line_val = float(r.get("line") or 0)
        pnl_units = r.get("target_taker_profit_units")

        if base_fv is None or ask is None or pnl_units is None:
            skipped_rows_no_base_fv += 1
            continue

        s2_delta = r.get("stage2_run_env_delta") or 0.0
        logit_fv_s2 = _logit(base_fv) + s2_delta

        away = r.get("away_abbrev")
        home = r.get("home_abbrev")
        date = r.get("session_date")
        inning = r.get("inning")

        if family == "score_event_transition" and r.get("team_offense_delta") is not None:
            # Recompute v1
            v1_delta = get_v1_delta(model, away, home, date, inning)
            fv_v1 = _sigmoid(logit_fv_s2 + v1_delta)
            edge_v1 = fv_v1 - ask

            # Recompute v2
            v2_delta = model.get_matchup_delta(away, home, date, inning)
            fv_v2 = _sigmoid(logit_fv_s2 + v2_delta)
            edge_v2 = fv_v2 - ask
        else:
            # For other families, or when team offense delta was not used, edge is constant
            edge_v1 = r.get("edge")
            edge_v2 = r.get("edge")

        processed_rows.append({
            "line": line_val,
            "ask": ask,
            "pnl_units": pnl_units,
            "edge_v1": edge_v1,
            "edge_v2": edge_v2,
            "family": family
        })

    print(f"Processed {len(processed_rows)} valid rows (skipped {skipped_rows_no_base_fv} due to missing fields).")

    # Define sweep grid
    extreme_edge_sweep = [0.15, 0.16, 0.17, 0.18, 0.19, 0.20, 0.21, 0.22, 0.23, 0.24, 0.25, 0.30, 99.0]
    min_edge_sweep = [0.10, 0.11, 0.12, 0.13, 0.14, 0.15, 0.16, 0.17, 0.18, 0.19, 0.20]

    # Run the grid search
    results_v1 = []
    results_v2 = []

    for extreme_cap in extreme_edge_sweep:
        for min_edge_base in min_edge_sweep:
            # We assume edge_threshold_high_line = min_edge_base + 0.01
            min_edge_high = min_edge_base + 0.01

            pnl_v1, trades_v1 = 0.0, 0
            pnl_v2, trades_v2 = 0.0, 0

            for r in processed_rows:
                # Ask edge ramp boost calculation
                ask_boost = _ask_edge_boost(ask=r["ask"], start=0.75, end=0.90, max_boost=0.05)
                threshold = (min_edge_high if r["line"] >= 8.5 else min_edge_base) + ask_boost

                # Evaluate V1
                if r["edge_v1"] >= threshold and r["edge_v1"] <= extreme_cap:
                    pnl_v1 += r["pnl_units"]
                    trades_v1 += 1

                # Evaluate V2
                if r["edge_v2"] >= threshold and r["edge_v2"] <= extreme_cap:
                    pnl_v2 += r["pnl_units"]
                    trades_v2 += 1

            results_v1.append({
                "extreme_cap": extreme_cap,
                "min_edge_base": min_edge_base,
                "min_edge_high": min_edge_high,
                "pnl": pnl_v1,
                "trades": trades_v1
            })
            results_v2.append({
                "extreme_cap": extreme_cap,
                "min_edge_base": min_edge_base,
                "min_edge_high": min_edge_high,
                "pnl": pnl_v2,
                "trades": trades_v2
            })

    # Sort results
    results_v1.sort(key=lambda x: x["pnl"], reverse=True)
    results_v2.sort(key=lambda x: x["pnl"], reverse=True)

    # Let's print comparisons for the baseline config (extreme=0.22, min=0.15, high_min=0.16)
    base_v1 = next(res for res in results_v1 if abs(res["extreme_cap"] - 0.22) < 1e-4 and abs(res["min_edge_base"] - 0.15) < 1e-4)
    base_v2 = next(res for res in results_v2 if abs(res["extreme_cap"] - 0.22) < 1e-4 and abs(res["min_edge_base"] - 0.15) < 1e-4)

    print("\n" + "=" * 80)
    print("BASELINE COMPARISON (Current Gates: extreme_edge_max=0.22, min_edge=0.15)")
    print("=" * 80)
    print(f"V1 (Prod baseline): PnL = {base_v1['pnl']:.2f} units, Trades = {base_v1['trades']}")
    print(f"V2 (Naive Swap):    PnL = {base_v2['pnl']:.2f} units, Trades = {base_v2['trades']}")
    print(f"Net change:         PnL = {base_v2['pnl'] - base_v1['pnl']:.2f} units")

    print("\n" + "=" * 80)
    print("TOP 10 CONFIGURATIONS FOR V2 MODEL")
    print("=" * 80)
    print(f"{'Rank':<4} {'Extreme Cap':<12} {'Min Edge Base':<15} {'Min Edge High':<15} {'PnL (Units)':<12} {'Trades':<8}")
    print("-" * 70)
    for rank, res in enumerate(results_v2[:15], 1):
        cap_str = "no gate" if res["extreme_cap"] >= 90.0 else f"{res['extreme_cap']:.2f}"
        print(f"{rank:<4} {cap_str:<12} {res['min_edge_base']:<15.2f} {res['min_edge_high']:<15.2f} {res['pnl']:<12.2f} {res['trades']:<8}")

    print("\n" + "=" * 80)
    print("TOP 5 CONFIGURATIONS FOR V1 MODEL (For Reference)")
    print("=" * 80)
    print(f"{'Rank':<4} {'Extreme Cap':<12} {'Min Edge Base':<15} {'Min Edge High':<15} {'PnL (Units)':<12} {'Trades':<8}")
    print("-" * 70)
    for rank, res in enumerate(results_v1[:5], 1):
        cap_str = "no gate" if res["extreme_cap"] >= 90.0 else f"{res['extreme_cap']:.2f}"
        print(f"{rank:<4} {cap_str:<12} {res['min_edge_base']:<15.2f} {res['min_edge_high']:<15.2f} {res['pnl']:<12.2f} {res['trades']:<8}")


if __name__ == "__main__":
    run_grid_search()
