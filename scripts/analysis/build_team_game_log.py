#!/usr/bin/env python3
"""
Build team game log from raw MLB game feeds.

Scans data/games/regular/{year}/**/*.json and extracts final scores,
writing a compact cache to cache/team_game_log.json. Used by
TeamOffenseModel to compute rolling runs-per-game without re-scanning
raw feeds on every startup.

Usage:
    python baseball/scripts/analysis/build_team_game_log.py
    python baseball/scripts/analysis/build_team_game_log.py --seasons 2024 2025 2026
    python baseball/scripts/analysis/build_team_game_log.py --output path/to/log.json
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import sys
import time
from pathlib import Path

LOGGER = logging.getLogger("build_team_game_log")

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = PROJECT_DIR / "data"
DEFAULT_OUTPUT = PROJECT_DIR / "cache" / "team_game_log.json"
DEFAULT_SEASONS = ["2021", "2022", "2023", "2024", "2025", "2026"]


def scan_games(data_root: Path, seasons: list[str]) -> list[dict]:
    """Scan raw game feeds and return list of final-game result dicts."""
    games: list[dict] = []
    skipped = 0

    for year in seasons:
        pattern = str(data_root / "games" / "regular" / year / "**" / "*.json")
        files = sorted(glob.glob(pattern, recursive=True))
        LOGGER.info("  Year %s: %d files", year, len(files))
        year_games = 0

        for fpath in files:
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    d = json.load(f)
            except Exception as exc:
                LOGGER.debug("Skip %s: %s", fpath, exc)
                skipped += 1
                continue

            gd = d.get("gameData", {}) or {}
            status = (gd.get("status", {}) or {}).get("abstractGameState", "")
            if status != "Final":
                continue

            teams = gd.get("teams", {}) or {}
            away_abbrev = (teams.get("away", {}) or {}).get("abbreviation", "")
            home_abbrev = (teams.get("home", {}) or {}).get("abbreviation", "")
            date = (gd.get("datetime", {}) or {}).get("officialDate", "")

            ld = d.get("liveData", {}) or {}
            ls_teams = (ld.get("linescore", {}) or {}).get("teams", {}) or {}
            away_runs = (ls_teams.get("away", {}) or {}).get("runs")
            home_runs = (ls_teams.get("home", {}) or {}).get("runs")

            if not (away_abbrev and home_abbrev and date
                    and away_runs is not None and home_runs is not None):
                skipped += 1
                continue

            games.append({
                "date": date,
                "away": away_abbrev,
                "home": home_abbrev,
                "away_runs": int(away_runs),
                "home_runs": int(home_runs),
            })
            year_games += 1

        LOGGER.info("    -> %d valid final games", year_games)

    games.sort(key=lambda g: g["date"])
    LOGGER.info("Total: %d games loaded, %d files skipped", len(games), skipped)
    return games


def compute_mlb_avg(games: list[dict]) -> float:
    """MLB average runs per team per game (half of expected total)."""
    total_runs = sum(g["away_runs"] + g["home_runs"] for g in games)
    total_game_sides = len(games) * 2
    return total_runs / total_game_sides if total_game_sides > 0 else 4.45


def build_log(
    data_root: Path = DEFAULT_DATA_ROOT,
    seasons: list[str] = DEFAULT_SEASONS,
    output_path: Path = DEFAULT_OUTPUT,
) -> dict:
    """Build and write the game log. Returns the payload dict."""
    t0 = time.time()
    LOGGER.info("Scanning game feeds under %s for seasons %s ...", data_root, seasons)
    games = scan_games(data_root, seasons)
    mlb_avg_rpg = compute_mlb_avg(games)

    payload = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seasons": seasons,
        "total_games": len(games),
        "mlb_avg_rpg": round(mlb_avg_rpg, 4),
        "mlb_avg_total": round(2 * mlb_avg_rpg, 4),
        "games": games,
    }

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)

    elapsed = time.time() - t0
    LOGGER.info(
        "Wrote %d games to %s (%.1fs)  MLB avg RPG=%.3f  avg total=%.3f",
        len(games), output_path, elapsed, mlb_avg_rpg, 2 * mlb_avg_rpg,
    )
    return payload


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser(description="Build team game log cache.")
    p.add_argument(
        "--seasons", nargs="+", default=DEFAULT_SEASONS,
        help=f"Seasons to include (default: {' '.join(DEFAULT_SEASONS)})",
    )
    p.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help=f"Output path (default: {DEFAULT_OUTPUT})",
    )
    p.add_argument(
        "--data-root", type=Path, default=DEFAULT_DATA_ROOT,
        help=f"Data root (default: {DEFAULT_DATA_ROOT})",
    )
    args = p.parse_args()
    build_log(data_root=args.data_root, seasons=args.seasons, output_path=args.output)


if __name__ == "__main__":
    main()
