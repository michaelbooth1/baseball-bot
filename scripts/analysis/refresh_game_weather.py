#!/usr/bin/env python3
"""
Refresh stadium-level weather cache for a run date.

This script is startup-safe: failures are surfaced through exit codes and
compact JSON output, but live startup decides whether those failures are fatal.
The cache is the canonical live Weather v2 input for Stage-2 fair-value
adjustments; provider failures degrade to unknown weather buckets.
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

from scripts.trading.weather_client import (  # noqa: E402
    DEFAULT_STADIUM_METADATA_PATH,
    DEFAULT_WEATHER_CACHE_DIR,
    build_game_weather_cache,
    default_weather_cache_path,
    write_game_weather_cache,
)


LOGGER = logging.getLogger("refresh_game_weather")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Refresh local stadium weather cache for MLB games.")
    p.add_argument("--date", required=True, help="MLB schedule date YYYY-MM-DD.")
    p.add_argument("--provider", choices=["open-meteo", "none"], default="open-meteo")
    p.add_argument("--metadata-path", type=Path, default=DEFAULT_STADIUM_METADATA_PATH)
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_WEATHER_CACHE_DIR)
    p.add_argument("--output-path", type=Path, default=None)
    p.add_argument("--timeout", type=float, default=8.0)
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    output_path = args.output_path or default_weather_cache_path(args.date, args.cache_dir)
    payload = build_game_weather_cache(
        date_str=args.date,
        metadata_path=args.metadata_path,
        provider=args.provider,
        timeout=float(args.timeout),
    )
    write_game_weather_cache(payload, output_path)
    compact = {
        "date": args.date,
        "output_path": str(output_path),
        "coverage": payload.get("coverage", {}),
        "warnings": payload.get("warnings", [])[:20],
    }
    print(json.dumps(compact, indent=2, sort_keys=True))
    LOGGER.info(
        "Weather cache refreshed: date=%s provider=%s games=%s provider_ok=%s output=%s",
        args.date,
        args.provider,
        (payload.get("coverage") or {}).get("scheduled_games"),
        (payload.get("coverage") or {}).get("provider_ok"),
        output_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
