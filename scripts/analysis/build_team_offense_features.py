#!/usr/bin/env python3
"""
build_team_offense_features.py -- Phase 2 of team-offense V2 calibration.

For every (team, date) pair in the 2021-2026 corpus, computes a leakage-free
feature dict using ONLY games strictly before the target date.

Features (all "RPG" = runs scored per game by this team):
  prior_season_rpg          last full completed season (None for game 1 of corpus)
  season_rpg_to_date        current season, all prior games this season
  n_games_in_season         scalar
  momentum_rpg_5            trailing 5 games (any season; min support 3)
  momentum_rpg_10           trailing 10 games (min support 5)
  momentum_rpg_20           trailing 20 games (min support 10)
  rolling_rpg_50            trailing 50 games (matches current production model)
  decay_weighted_rpg        last 100 games, exp decay half-life = 15 games
  home_rpg_30               trailing 30 home games (min support 8)
  road_rpg_30               trailing 30 road games (min support 8)
  momentum_minus_season     momentum_rpg_10 - season_rpg_to_date  (derived)
  season_minus_prior        season_rpg_to_date - prior_season_rpg (derived)

Output JSONL: one row per (team, date) keyed `team|date`. Phase 4 modeling
joins this on the calibration table by `(team, date)` for both away and home.

  data/analysis_output/team_offense_calibration/team_features.jsonl
  data/analysis_output/team_offense_calibration/team_features_manifest.json

Usage:
  python scripts/analysis/build_team_offense_features.py
  python scripts/analysis/build_team_offense_features.py --game-log path/to/log.json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_GAME_LOG = PROJECT_DIR / "cache" / "team_game_log.json"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "data" / "analysis_output" / "team_offense_calibration"

LOGGER = logging.getLogger("build_team_offense_features")

# --- window sizes / minimums ----------------------------------------------

WINDOWS = (5, 10, 20, 50)
DECAY_HALF_LIFE = 15  # games
DECAY_LOOKBACK = 100
HOME_ROAD_WINDOW = 30

MIN_SUPPORT = {
    5: 3,
    10: 5,
    20: 10,
    50: 10,        # matches production-model MIN_GAMES_FOR_ESTIMATE
    100: 10,
    "decay": 10,
    "home_road": 8,
    "season": 5,
    "prior_season": 30,
}


def _safe_mean(values: List[int]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _decay_weighted(runs: List[int], half_life: int) -> Optional[float]:
    """Exp-decayed mean: most recent game weight 1, weight halves every `half_life`."""
    if not runs:
        return None
    n = len(runs)
    weights = [2.0 ** (-(n - 1 - i) / half_life) for i in range(n)]
    total_w = sum(weights)
    if total_w <= 0:
        return None
    weighted = sum(r * w for r, w in zip(runs, weights))
    return weighted / total_w


# ---------------------------------------------------------------------------
# History build
# ---------------------------------------------------------------------------


def build_history(game_log_path: Path) -> Tuple[Dict[str, List[Tuple[str, int, bool]]], float]:
    """
    Returns ({team: sorted-asc [(date, runs_scored, was_home)]}, mlb_avg_rpg).
    """
    with open(game_log_path, encoding="utf-8") as f:
        d = json.load(f)
    by_team: Dict[str, List[Tuple[str, int, bool]]] = defaultdict(list)
    for g in d.get("games", []):
        by_team[g["away"]].append((g["date"], int(g["away_runs"]), False))
        by_team[g["home"]].append((g["date"], int(g["home_runs"]), True))
    for t in by_team:
        by_team[t].sort(key=lambda x: x[0])
    return by_team, float(d.get("mlb_avg_rpg", 4.45))


# ---------------------------------------------------------------------------
# Feature computation per (team, date)
# ---------------------------------------------------------------------------


def _bisect_strict_less(items: List[Tuple[str, int, bool]], date: str) -> int:
    lo, hi = 0, len(items)
    while lo < hi:
        mid = (lo + hi) // 2
        if items[mid][0] < date:
            lo = mid + 1
        else:
            hi = mid
    return lo


def compute_features(
    history: List[Tuple[str, int, bool]],
    target_date: str,
) -> Dict[str, Optional[float]]:
    """
    All features computed using items at indexes [0, idx) where idx is first
    item with date >= target_date. Strictly leak-free: today's games are
    excluded.
    """
    idx = _bisect_strict_less(history, target_date)
    prior = history[:idx]  # everything strictly before target_date

    season = target_date[:4]
    season_runs = [r for d, r, _ in prior if d[:4] == season]
    n_games_in_season = len(season_runs)
    season_rpg_to_date = (
        _safe_mean(season_runs) if n_games_in_season >= MIN_SUPPORT["season"] else None
    )

    # Prior season: full year preceding `season`. Only valid if 2nd+ year of
    # team's history.
    prior_season = str(int(season) - 1)
    prior_season_runs = [r for d, r, _ in prior if d[:4] == prior_season]
    prior_season_rpg = (
        _safe_mean(prior_season_runs)
        if len(prior_season_runs) >= MIN_SUPPORT["prior_season"]
        else None
    )

    feat: Dict[str, Optional[float]] = {
        "n_games_in_season": n_games_in_season,
        "season_rpg_to_date": season_rpg_to_date,
        "prior_season_rpg": prior_season_rpg,
    }

    # Trailing windows (across season boundaries -- "momentum" is just
    # raw recency, not season-respecting).
    last_runs = [r for _, r, _ in prior]
    for w in WINDOWS:
        window = last_runs[-w:] if last_runs else []
        key = f"momentum_rpg_{w}" if w in (5, 10, 20) else f"rolling_rpg_{w}"
        feat[key] = _safe_mean(window) if len(window) >= MIN_SUPPORT[w] else None

    decay_window = last_runs[-DECAY_LOOKBACK:] if last_runs else []
    feat["decay_weighted_rpg"] = (
        _decay_weighted(decay_window, DECAY_HALF_LIFE)
        if len(decay_window) >= MIN_SUPPORT["decay"]
        else None
    )

    # Home / road splits.
    home_runs_list = [r for _, r, h in prior if h]
    road_runs_list = [r for _, r, h in prior if not h]
    feat["home_rpg_30"] = (
        _safe_mean(home_runs_list[-HOME_ROAD_WINDOW:])
        if len(home_runs_list) >= MIN_SUPPORT["home_road"]
        else None
    )
    feat["road_rpg_30"] = (
        _safe_mean(road_runs_list[-HOME_ROAD_WINDOW:])
        if len(road_runs_list) >= MIN_SUPPORT["home_road"]
        else None
    )

    # Derived signals (None-propagating).
    if feat["momentum_rpg_10"] is not None and season_rpg_to_date is not None:
        feat["momentum_minus_season"] = round(feat["momentum_rpg_10"] - season_rpg_to_date, 4)
    else:
        feat["momentum_minus_season"] = None
    if season_rpg_to_date is not None and prior_season_rpg is not None:
        feat["season_minus_prior"] = round(season_rpg_to_date - prior_season_rpg, 4)
    else:
        feat["season_minus_prior"] = None

    # Round all numeric features for compactness.
    for k, v in list(feat.items()):
        if isinstance(v, float):
            feat[k] = round(v, 4)

    return feat


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--game-log", type=Path, default=DEFAULT_GAME_LOG)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    LOGGER.info("Loading history from %s", args.game_log)
    by_team, mlb_avg = build_history(args.game_log)
    LOGGER.info("  %d teams, mlb_avg_rpg=%.3f", len(by_team), mlb_avg)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / "team_features.jsonl"
    manifest_path = args.output_dir / "team_features_manifest.json"

    started = time.time()
    rows = 0
    none_counts: Dict[str, int] = defaultdict(int)
    feature_keys: List[str] = []
    with open(output_path, "w", encoding="utf-8") as out:
        for team in sorted(by_team):
            history = by_team[team]
            seen_dates_for_team = set()
            for date, _runs, _home in history:
                # One row per (team, date). Doubleheaders share the row.
                if date in seen_dates_for_team:
                    continue
                seen_dates_for_team.add(date)
                feat = compute_features(history, date)
                row = {"team": team, "date": date, **feat}
                if not feature_keys:
                    feature_keys = [k for k in row.keys() if k not in ("team", "date")]
                for k, v in feat.items():
                    if v is None:
                        none_counts[k] += 1
                out.write(json.dumps(row, separators=(",", ":")) + "\n")
                rows += 1
            LOGGER.debug("  %s: %d (team,date) rows", team, len(seen_dates_for_team))

    elapsed = time.time() - started
    coverage = {k: round(1.0 - none_counts.get(k, 0) / max(1, rows), 4) for k in feature_keys}

    manifest = {
        "schema_version": 1,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_seconds": round(elapsed, 1),
        "args": {"game_log": str(args.game_log)},
        "n_teams": len(by_team),
        "mlb_avg_rpg": round(mlb_avg, 4),
        "rows_written": rows,
        "feature_keys": feature_keys,
        "feature_coverage": coverage,
        "windows": list(WINDOWS),
        "decay_half_life_games": DECAY_HALF_LIFE,
        "decay_lookback": DECAY_LOOKBACK,
        "home_road_window": HOME_ROAD_WINDOW,
        "min_support": MIN_SUPPORT,
        "output": str(output_path),
        "row_join_key": "(team, date)",
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    LOGGER.info("Wrote %d rows in %.1fs", rows, elapsed)
    LOGGER.info("Feature coverage:")
    for k, v in coverage.items():
        LOGGER.info("  %-22s %.2f%%", k, v * 100)
    LOGGER.info("Manifest: %s", manifest_path)


if __name__ == "__main__":
    main()
