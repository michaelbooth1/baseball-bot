#!/usr/bin/env python3
"""Refresh per-game metadata cache (home-plate umpire + officials) for a date.

Tier-2 capture (2026-05-29). Startup-safe and fail-open, mirroring
refresh_game_weather.py: fetches each scheduled game's boxscore once and
writes cache/game_meta/game_meta_<date>.json. The live engine loads this at
startup and joins the home-plate umpire onto candidate rows by game_pk.

Timing: umpires only appear in the boxscore once lineups post (~1-2h pre-game)
or the game is live, so a near-game-time refresh (as the live engine runs)
captures most; an early-morning refresh for night games records them
unavailable. Re-running later fills them in.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.trading.game_meta_client import (  # noqa: E402
    DEFAULT_GAME_META_CACHE_DIR,
    build_game_meta_cache,
    default_game_meta_cache_path,
    write_game_meta_cache,
)

LOGGER = logging.getLogger("refresh_game_meta")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Refresh per-game umpire/officials cache for MLB games.")
    p.add_argument("--date", required=True, help="MLB schedule date YYYY-MM-DD.")
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_GAME_META_CACHE_DIR)
    p.add_argument("--output-path", type=Path, default=None)
    p.add_argument("--timeout", type=float, default=8.0)
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    output_path = args.output_path or default_game_meta_cache_path(args.date, args.cache_dir)
    payload = build_game_meta_cache(date_str=args.date, timeout=float(args.timeout))
    write_game_meta_cache(payload, output_path)
    coverage = payload.get("coverage", {}) or {}
    compact = {
        "date": args.date,
        "output_path": str(output_path),
        "coverage": coverage,
        "warnings": payload.get("warnings", [])[:20],
    }
    print(json.dumps(compact, indent=2, sort_keys=True))
    LOGGER.info(
        "Game-meta cache refreshed: date=%s games=%s with_umpire=%s output=%s",
        args.date,
        coverage.get("scheduled_games"),
        coverage.get("games_with_umpire"),
        output_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
