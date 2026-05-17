"""
build_pitcher_cache.py — Fetch current-season ERA for MLB pitchers.

Writes cache/pitcher_cache.json keyed by pitcher_id (as string).
Run once per day before the trading session, or weekly for stable ERA.

Usage:
    python scripts/analysis/build_pitcher_cache.py [--season 2026] [--min-ip 10]
"""

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_CACHE_PATH = PROJECT_DIR / "cache" / "pitcher_cache.json"
MLB_STATS_URL = "https://statsapi.mlb.com/api/v1/stats"

# League average ERA as fallback when a pitcher has no data or too few IP.
# Updated for 2024/2025 MLB season; adjust manually if the game changes significantly.
MLB_AVG_ERA = 4.20

LOGGER = logging.getLogger(__name__)


def fetch_pitching_stats(season: int, game_type: str = "R", min_ip: float = 10.0) -> dict:
    """
    Fetch season pitching stats from MLB Stats API.
    Returns dict keyed by pitcher_id -> {name, era, ip, gs}.
    Pitchers with fewer than min_ip innings pitched are excluded (ERA is too noisy).
    """
    session = requests.Session()
    session.headers.update({"User-Agent": "MLB-Poly-Pitcher-Cache/1.0", "Accept": "application/json"})

    all_splits = []
    offset = 0
    limit = 500

    while True:
        params = {
            "stats": "season",
            "group": "pitching",
            "season": season,
            "gameType": game_type,
            "limit": limit,
            "offset": offset,
            "fields": "stats,splits,player,id,fullName,stat,era,inningsPitched,gamesStarted,wins,losses",
        }
        LOGGER.info("Fetching pitching stats offset=%d limit=%d ...", offset, limit)
        resp = session.get(MLB_STATS_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        stats_list = data.get("stats", [])
        if not stats_list:
            break
        splits = stats_list[0].get("splits", [])
        if not splits:
            break
        all_splits.extend(splits)
        if len(splits) < limit:
            break
        offset += limit
        time.sleep(0.3)  # polite rate limiting

    LOGGER.info("Total pitcher splits fetched: %d", len(all_splits))

    pitchers = {}
    skipped_low_ip = 0
    skipped_no_era = 0

    for split in all_splits:
        player = split.get("player", {}) or {}
        stat = split.get("stat", {}) or {}

        pid = player.get("id")
        name = player.get("fullName", "")
        if pid is None:
            continue

        # IP comes as string like "45.1" (45 innings 1 out = 45.333...), parse correctly
        ip_raw = stat.get("inningsPitched", "0") or "0"
        try:
            ip_parts = str(ip_raw).split(".")
            ip = int(ip_parts[0])
            if len(ip_parts) > 1:
                outs = int(ip_parts[1])
                ip += outs / 3.0
        except Exception:
            ip = 0.0

        if ip < min_ip:
            skipped_low_ip += 1
            continue

        era_raw = stat.get("era", "") or ""
        try:
            era = float(era_raw)
        except (ValueError, TypeError):
            # "-.--" or missing means 0 ER in enough innings; treat as excellent but use avg
            # to avoid exploding the gate in unexpected ways
            if ip >= min_ip:
                era = 0.0  # 0 ERA with sufficient IP = genuinely excellent
            else:
                skipped_no_era += 1
                continue

        gs = int(stat.get("gamesStarted", 0) or 0)

        pitchers[str(pid)] = {
            "name": name,
            "era": round(era, 2),
            "ip": round(ip, 1),
            "gs": gs,
        }

    LOGGER.info(
        "Pitchers in cache: %d  (skipped low_ip=%d  no_era=%d)",
        len(pitchers), skipped_low_ip, skipped_no_era,
    )
    return pitchers


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    p = argparse.ArgumentParser(description="Build pitcher ERA cache from MLB Stats API")
    p.add_argument("--season", type=int, default=datetime.now().year)
    p.add_argument("--min-ip", type=float, default=5.0,
                   help="Minimum innings pitched to include pitcher (default: 5.0). "
                        "Note: the MLB Stats API itself filters to ~23+ IP early-season; "
                        "this param is a post-fetch floor for additional noise control.")
    p.add_argument("--cache-path", type=Path, default=DEFAULT_CACHE_PATH)
    args = p.parse_args()

    pitchers = fetch_pitching_stats(season=args.season, min_ip=args.min_ip)

    if not pitchers:
        LOGGER.error("No pitcher data fetched — check MLB Stats API connectivity")
        return

    # Compute ERA statistics for logging
    eras = [v["era"] for v in pitchers.values() if v["era"] > 0]
    avg_era = sum(eras) / len(eras) if eras else MLB_AVG_ERA
    LOGGER.info("Computed avg ERA across %d pitchers: %.2f (MLB_AVG constant: %.2f)", len(eras), avg_era, MLB_AVG_ERA)

    cache = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "season": args.season,
        "min_ip_threshold": args.min_ip,
        "mlb_avg_era": MLB_AVG_ERA,
        "pitcher_count": len(pitchers),
        "pitchers": pitchers,
    }

    args.cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(args.cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)

    LOGGER.info("Pitcher cache written to %s  (%d pitchers)", args.cache_path, len(pitchers))


if __name__ == "__main__":
    main()
