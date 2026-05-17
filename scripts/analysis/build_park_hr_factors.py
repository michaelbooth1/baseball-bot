#!/usr/bin/env python3
"""Build per-(park, season) home-run factor cache for Stage-2.

Park HR factor = (park HR per game in that season)
                  / (league HR per game in that season).

Why this is not redundant with Stage-2's static `park` bucket:
  - Stage-2's `park` table absorbs the AGGREGATE per-park empirical Over rate
    over the full training corpus. It cannot capture year-over-year drift in
    a single park's HR-friendliness (juiced ball, fence moves, humidor,
    altitude tweaks, etc.).
  - Park HR factor is time-varying per (park, year), so when the model is
    applied to a 2026 game it consults the most recent computed factor for
    that park rather than a multi-year average.

Output:
  cache/park_hr_factors.json
  {
    "schema_version": 1,
    "generated_at_utc": "...",
    "league_hr_per_game_by_year": {"2021": 1.21, "2022": 1.07, ...},
    "prior_n_shrinkage": 30,
    "by_park": {
      "Coors Field": {
        "2021": {"games": 81, "hrs": 158, "raw_factor": 1.62, "shrunk_factor": 1.41},
        "2022": {...},
        ...
      },
      ...
    }
  }

The runtime applier (`stage2_run_env_model._load_hr_factors`) reads
`shrunk_factor` and bucketizes it via `parse_hr_factor_bin(factor)`.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

LOGGER = logging.getLogger("build_park_hr_factors")

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = PROJECT_DIR / "data"
DEFAULT_OUTPUT = PROJECT_DIR / "cache" / "park_hr_factors.json"
DEFAULT_SEASONS = ["2021", "2022", "2023", "2024", "2025", "2026"]
DEFAULT_PRIOR_N = 30  # games of league-average prior; shrinks small samples toward 1.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build per-(park, season) HR factor cache.")
    p.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    p.add_argument("--season-type", type=str, default="regular")
    p.add_argument("--seasons", nargs="+", default=DEFAULT_SEASONS)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument(
        "--prior-n", type=int, default=DEFAULT_PRIOR_N,
        help="Shrinkage prior in games of league-average HRs (default: 30).",
    )
    return p.parse_args()


def _scan_park_year(data_root: Path, season_type: str, seasons) -> Tuple[
    Dict[Tuple[str, str], Dict[str, int]],
    Dict[str, Dict[str, int]],
]:
    """Returns:
      by_park_year[(park, year)] = {"games": N, "hrs": M}
      by_year[year] = {"games": N, "hrs": M}  (league totals)
    """
    by_park_year: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(
        lambda: {"games": 0, "hrs": 0}
    )
    by_year: Dict[str, Dict[str, int]] = defaultdict(lambda: {"games": 0, "hrs": 0})

    files_total = 0
    files_used = 0
    files_skipped = 0

    for season in seasons:
        pattern = str(data_root / "games" / season_type / season / "**" / "*.json")
        files = sorted(glob.glob(pattern, recursive=True))
        LOGGER.info("Year %s: %d files", season, len(files))
        files_total += len(files)

        for fpath in files:
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    d = json.load(f)
            except Exception as exc:
                LOGGER.debug("Skip %s: %s", fpath, exc)
                files_skipped += 1
                continue

            gd = d.get("gameData", {}) or {}
            status = (gd.get("status", {}) or {}).get("abstractGameState", "")
            if status != "Final":
                files_skipped += 1
                continue

            venue_name = str((gd.get("venue", {}) or {}).get("name") or "").strip()
            if not venue_name:
                files_skipped += 1
                continue

            date_txt = str((gd.get("datetime", {}) or {}).get("dateTime", "") or "")
            year: Optional[str] = None
            if len(date_txt) >= 4 and date_txt[:4].isdigit():
                year = date_txt[:4]
            if year is None:
                # Fallback: pull from path
                parts = Path(fpath).parts
                for part in parts:
                    if len(part) == 4 and part.isdigit():
                        year = part
                        break
            if year is None:
                files_skipped += 1
                continue

            plays = ((d.get("liveData", {}) or {}).get("plays", {}) or {}).get(
                "allPlays", []
            ) or []
            hr_count = 0
            for p in plays:
                event_type = str(((p.get("result") or {}).get("eventType") or "")).lower()
                if event_type == "home_run":
                    hr_count += 1

            key = (venue_name, year)
            by_park_year[key]["games"] += 1
            by_park_year[key]["hrs"] += hr_count
            by_year[year]["games"] += 1
            by_year[year]["hrs"] += hr_count
            files_used += 1

    LOGGER.info(
        "Scanned %d files (%d Final games used, %d skipped).",
        files_total, files_used, files_skipped,
    )
    return by_park_year, by_year


def _compute_factors(
    by_park_year: Dict[Tuple[str, str], Dict[str, int]],
    by_year: Dict[str, Dict[str, int]],
    prior_n: int,
) -> Tuple[Dict[str, Dict[str, dict]], Dict[str, float]]:
    """Compute per-(park, year) HR factor with shrinkage toward league mean.

    Shrinkage formula:
        shrunk_hr_per_game = (park_hrs + prior_n * league_hr_per_game) / (games + prior_n)
        shrunk_factor = shrunk_hr_per_game / league_hr_per_game

    With prior_n=30 games, a park with 81 games has effective n=111: data
    pulls toward league mean by ~27%, leaving most of the season's signal.
    A new park with 5 games has effective n=35: data pulls toward 1.0 by ~86%.
    """
    league_rates: Dict[str, float] = {}
    for year, t in by_year.items():
        if t["games"] > 0:
            league_rates[year] = t["hrs"] / t["games"]

    by_park: Dict[str, Dict[str, dict]] = {}
    for (park, year), t in by_park_year.items():
        league_rate = league_rates.get(year)
        if league_rate is None or league_rate <= 0:
            continue
        games = int(t["games"])
        hrs = int(t["hrs"])
        if games <= 0:
            continue
        raw_rate = hrs / games
        shrunk_rate = (hrs + prior_n * league_rate) / (games + prior_n)
        raw_factor = raw_rate / league_rate
        shrunk_factor = shrunk_rate / league_rate
        by_park.setdefault(park, {})[year] = {
            "games": games,
            "hrs": hrs,
            "raw_hr_per_game": round(raw_rate, 4),
            "league_hr_per_game": round(league_rate, 4),
            "raw_factor": round(raw_factor, 4),
            "shrunk_factor": round(shrunk_factor, 4),
        }

    return by_park, league_rates


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    by_park_year, by_year = _scan_park_year(
        data_root=args.data_root,
        season_type=args.season_type,
        seasons=args.seasons,
    )
    if not by_park_year:
        LOGGER.error("No (park, year) data found. Aborting.")
        return 1

    by_park, league_rates = _compute_factors(by_park_year, by_year, args.prior_n)

    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "season_type": args.season_type,
        "seasons_scanned": list(args.seasons),
        "prior_n_shrinkage": int(args.prior_n),
        "league_hr_per_game_by_year": {y: round(r, 4) for y, r in sorted(league_rates.items())},
        "by_park": dict(sorted(by_park.items())),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    # Operator-friendly summary.
    LOGGER.info("League HR/game by year: %s", payload["league_hr_per_game_by_year"])
    LOGGER.info("Wrote %d parks x up to %d years -> %s",
                len(by_park), len(args.seasons), args.output)
    # Highlight the most/least HR-friendly parks in the most recent year.
    last_year = max(league_rates.keys()) if league_rates else None
    if last_year:
        ranked = []
        for park, by_year_map in by_park.items():
            entry = by_year_map.get(last_year)
            if entry is not None:
                ranked.append((park, entry["shrunk_factor"], entry["games"]))
        ranked.sort(key=lambda x: x[1], reverse=True)
        LOGGER.info("Top 5 HR-friendly (%s, shrunk_factor):", last_year)
        for park, factor, games in ranked[:5]:
            LOGGER.info("  %-30s  %.3f  (n=%d games)", park, factor, games)
        LOGGER.info("Bottom 5 HR-friendly (%s, shrunk_factor):", last_year)
        for park, factor, games in ranked[-5:]:
            LOGGER.info("  %-30s  %.3f  (n=%d games)", park, factor, games)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
