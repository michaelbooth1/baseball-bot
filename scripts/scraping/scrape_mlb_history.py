#!/usr/bin/env python3
"""
Scrape historical MLB game data from the official MLB StatsAPI.

Storage layout mirrors the NHL project structure:

  baseball/data/
    games/<season_type>/<YYYY>/<MM>/<DD>/<gamePk>.json
    schedules/<YYYY>/<MM>/schedule_<YYYY-MM>.json
    manifests/monthly/manifest_<YYYY-MM>.json
    manifests/runs/run_<timestamp>.json

Default mode pulls regular-season games (`gameType=R`) for the last 5 years.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"

MLB_API_BASE = "https://statsapi.mlb.com/api/v1"
MLB_FEED_BASE = "https://statsapi.mlb.com/api/v1.1/game"

SEASON_FOLDER_BY_GAME_TYPE = {
    "R": "regular",      # regular season
    "S": "spring",       # spring training
    "F": "playoffs",     # post-season
    "D": "all_star",     # all-star game
    "W": "world_baseball_classic",
}


@dataclass(frozen=True)
class GameRef:
    game_pk: int
    game_type: str
    game_date: str  # YYYY-MM-DD from schedule date bucket
    status: str
    home_abbrev: str
    away_abbrev: str


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scrape historical MLB games from StatsAPI.")
    p.add_argument(
        "--lookback-years",
        type=int,
        default=5,
        help="How many years back to scrape when --start-date is not provided (default: 5).",
    )
    p.add_argument(
        "--start-date",
        type=str,
        default="",
        help="Inclusive YYYY-MM-DD. Overrides --lookback-years when set.",
    )
    p.add_argument(
        "--end-date",
        type=str,
        default="",
        help="Inclusive YYYY-MM-DD. Defaults to today.",
    )
    p.add_argument(
        "--game-types",
        type=str,
        default="R",
        help="Comma-separated MLB gameType codes (default: R). Example: R,F",
    )
    p.add_argument(
        "--sport-id",
        type=int,
        default=1,
        help="MLB sport ID for schedule endpoint (default: 1).",
    )
    p.add_argument(
        "--max-workers",
        type=int,
        default=8,
        help="Parallel workers for game feed fetches (default: 8).",
    )
    p.add_argument(
        "--timeout-seconds",
        type=float,
        default=15.0,
        help="HTTP timeout in seconds (default: 15).",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=4,
        help="Retry attempts for transient HTTP failures (default: 4).",
    )
    p.add_argument(
        "--backoff-seconds",
        type=float,
        default=1.25,
        help="Base exponential backoff in seconds (default: 1.25).",
    )
    p.add_argument(
        "--sleep-between-requests",
        type=float,
        default=0.0,
        help="Optional delay after each game fetch (default: 0.0).",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing game files. Default is skip-existing.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover schedule only. Do not download game feeds.",
    )
    return p.parse_args()


def parse_date_or_raise(raw: str, field_name: str) -> date:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as e:
        raise ValueError(f"Invalid {field_name}: {raw}. Expected YYYY-MM-DD.") from e


def resolve_date_range(args: argparse.Namespace) -> Tuple[date, date]:
    end_d = parse_date_or_raise(args.end_date, "end-date") if args.end_date else date.today()
    if args.start_date:
        start_d = parse_date_or_raise(args.start_date, "start-date")
    else:
        start_d = end_d - timedelta(days=365 * args.lookback_years)
    if start_d > end_d:
        raise ValueError(f"start-date {start_d} is after end-date {end_d}.")
    return start_d, end_d


def month_windows(start_d: date, end_d: date) -> Iterable[Tuple[date, date]]:
    cur = date(start_d.year, start_d.month, 1)
    while cur <= end_d:
        next_month = date(cur.year + (1 if cur.month == 12 else 0), 1 if cur.month == 12 else cur.month + 1, 1)
        month_end = next_month - timedelta(days=1)
        win_start = max(cur, start_d)
        win_end = min(month_end, end_d)
        yield win_start, win_end
        cur = next_month


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def request_json(
    session: requests.Session,
    url: str,
    *,
    params: Optional[Dict[str, str]] = None,
    timeout_s: float,
    retries: int,
    backoff_s: float,
) -> dict:
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout_s)
            # Retry transient statuses.
            if resp.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {resp.status_code} {resp.reason}")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # requests + status failures
            last_err = e
            if attempt == retries:
                break
            sleep_s = backoff_s * (2 ** (attempt - 1))
            time.sleep(sleep_s)
    raise RuntimeError(f"Failed request after {retries} attempts: {url} ({last_err})")


def fetch_month_schedule(
    session: requests.Session,
    start_d: date,
    end_d: date,
    sport_id: int,
    game_types: str,
    timeout_s: float,
    retries: int,
    backoff_s: float,
) -> dict:
    params = {
        "sportId": str(sport_id),
        "startDate": start_d.isoformat(),
        "endDate": end_d.isoformat(),
        "gameType": game_types,
    }
    url = f"{MLB_API_BASE}/schedule"
    return request_json(
        session,
        url,
        params=params,
        timeout_s=timeout_s,
        retries=retries,
        backoff_s=backoff_s,
    )


def extract_games(schedule_json: dict) -> List[GameRef]:
    games: List[GameRef] = []
    for day in schedule_json.get("dates", []):
        game_date = day.get("date", "")
        for g in day.get("games", []):
            game_pk = int(g.get("gamePk", 0) or 0)
            if game_pk <= 0:
                continue
            game_type = str(g.get("gameType", "") or "").upper()
            status = str((g.get("status") or {}).get("detailedState", "") or "")
            teams = g.get("teams") or {}
            home_abbrev = str(((teams.get("home") or {}).get("team") or {}).get("abbreviation", "") or "")
            away_abbrev = str(((teams.get("away") or {}).get("team") or {}).get("abbreviation", "") or "")
            games.append(
                GameRef(
                    game_pk=game_pk,
                    game_type=game_type,
                    game_date=game_date,
                    status=status,
                    home_abbrev=home_abbrev,
                    away_abbrev=away_abbrev,
                )
            )
    return games


def game_output_path(g: GameRef) -> Path:
    season_folder = SEASON_FOLDER_BY_GAME_TYPE.get(g.game_type, g.game_type.lower() or "other")
    dt = parse_date_or_raise(g.game_date, "game_date")
    return (
        DATA_DIR
        / "games"
        / season_folder
        / f"{dt.year:04d}"
        / f"{dt.month:02d}"
        / f"{dt.day:02d}"
        / f"{g.game_pk}.json"
    )


# 2026-06-04: retry-with-backoff parameters for atomic-rename. On
# Windows, `os.replace` fails with PermissionError (WinError 32)
# when another process holds the target file open for reading --
# this surfaced as a persistent `scrape_active_schedule` refresh
# failure when the live engine was tailing `schedule_2026-05.json`
# during the daily refresh. Linux doesn't have this problem.
# 5 attempts with exponential backoff = 0.1+0.2+0.4+0.8+1.6 = 3.1s
# total wait, plenty for a typical brief read-handle release.
_ATOMIC_REPLACE_MAX_ATTEMPTS = 5
_ATOMIC_REPLACE_BASE_DELAY_S = 0.1


def _atomic_replace_with_retry(
    src: Path,
    dst: Path,
    *,
    max_attempts: int = _ATOMIC_REPLACE_MAX_ATTEMPTS,
    base_delay_s: float = _ATOMIC_REPLACE_BASE_DELAY_S,
) -> None:
    """Atomic rename `src` -> `dst`, retrying on Windows file-lock
    PermissionErrors. Re-raises the last error if all attempts fail.

    Why: another process (e.g., the live engine tailing this same
    schedule file) can hold the target open for reading during a
    refresh's atomic-write. Windows refuses the rename in that case;
    Linux honors it. A small bounded retry-with-backoff lets the
    reader's handle clear naturally and the rename succeeds on the
    second or third attempt almost every time.
    """
    last_err: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            src.replace(dst)
            return
        except PermissionError as exc:
            last_err = exc
            if attempt + 1 >= max_attempts:
                break
            time.sleep(base_delay_s * (2 ** attempt))
    # All attempts exhausted -- re-raise the last PermissionError so
    # the caller's existing error handling (and the refresh step's
    # failure logging) works exactly as before.
    if last_err is not None:
        raise last_err


def save_json(path: Path, payload: dict) -> None:
    ensure_dir(path.parent)
    # Unique per-process temp name (2026-06-15 fix). The launcher's daily
    # refresh and the dry-run engine's startup refresh (run together per the
    # operational guidance) both call scrape_active_schedule and would write
    # the SAME `<file>.tmp`; one wins the atomic replace and the loser hit
    # `FileNotFoundError: ...<file>.tmp` because its temp was already consumed.
    # A pid + random-token suffix keeps each writer's temp private (random,
    # not nanotime -- Windows' coarse clock lets two threads collide on
    # time_ns); the atomic replace then just races to last-writer-wins on the
    # real file (same deterministic schedule content), which is safe.
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}_{os.urandom(8).hex()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        _atomic_replace_with_retry(tmp, path)
    finally:
        # On success the temp was renamed away; on failure clean up our own
        # temp so a crashed/raced writer doesn't leave debris behind.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _cached_game_is_final(path: Path) -> bool:
    """Inspect a cached game JSON and return True only if the recorded
    state is `Final`. Pre-game placeholders and in-progress snapshots
    return False so the caller re-fetches them. 2026-05-20 audit fix:
    previously --skip-existing skipped ANY cached file, leaving stale
    pre-game placeholders forever -- the daily review's
    settlement_truth_health then fired persistent `game_not_final_yet`
    alerts (e.g. 7 such bets all from 2026-05-13 found in 2026-05-19
    audit). Malformed/unreadable cache also returns False so it gets
    rewritten by the next fetch.
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            cached = json.load(f)
        status = (
            cached.get("gameData", {})
            .get("status", {})
            .get("abstractGameState")
        )
        return str(status or "").lower() == "final"
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def fetch_and_store_game(
    g: GameRef,
    *,
    timeout_s: float,
    retries: int,
    backoff_s: float,
    sleep_between_requests: float,
    overwrite: bool,
) -> Tuple[int, str, Path]:
    out_path = game_output_path(g)
    if out_path.exists() and not overwrite:
        # Only treat the cache as authoritative when it's a Final game.
        # Otherwise (pre-game placeholder, in-progress snapshot, or
        # corrupt) re-fetch -- the MLB scraper otherwise leaves stale
        # placeholders forever.
        if _cached_game_is_final(out_path):
            # Counter name kept as "skipped_exists" for back-compat with
            # downstream rollups; semantically this is now "skipped
            # because cached AND final" (pre-game/in-progress cached
            # files fall through and get re-fetched).
            return g.game_pk, "skipped_exists", out_path
        # fall through -- re-fetch the non-final cache

    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "NHL-BOT-Baseball/1.0"})
    url = f"{MLB_FEED_BASE}/{g.game_pk}/feed/live"
    payload = request_json(
        session,
        url,
        timeout_s=timeout_s,
        retries=retries,
        backoff_s=backoff_s,
    )
    save_json(out_path, payload)
    if sleep_between_requests > 0:
        time.sleep(sleep_between_requests)
    return g.game_pk, "downloaded", out_path


def month_schedule_path(win_start: date) -> Path:
    return DATA_DIR / "schedules" / f"{win_start.year:04d}" / f"{win_start.month:02d}" / f"schedule_{win_start.year:04d}-{win_start.month:02d}.json"


def month_manifest_path(win_start: date) -> Path:
    return DATA_DIR / "manifests" / "monthly" / f"manifest_{win_start.year:04d}-{win_start.month:02d}.json"


def run_manifest_path() -> Path:
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    return DATA_DIR / "manifests" / "runs" / f"run_{ts}.json"


def main() -> None:
    args = parse_args()
    start_d, end_d = resolve_date_range(args)
    game_types = ",".join([x.strip().upper() for x in args.game_types.split(",") if x.strip()])
    if not game_types:
        raise ValueError("At least one game type is required.")

    print("=" * 72)
    print("MLB Historical Scrape")
    print("=" * 72)
    print(f"Date range:      {start_d} -> {end_d}")
    print(f"Game types:      {game_types}")
    print(f"Sport ID:        {args.sport_id}")
    print(f"Max workers:     {args.max_workers}")
    print(f"Overwrite:       {args.overwrite}")
    print(f"Dry run:         {args.dry_run}")
    print(f"Output root:     {DATA_DIR}")
    print("=" * 72)

    ensure_dir(DATA_DIR)

    summary = {
        "started_at_utc": datetime.utcnow().isoformat() + "Z",
        "start_date": start_d.isoformat(),
        "end_date": end_d.isoformat(),
        "game_types": game_types,
        "sport_id": args.sport_id,
        "max_workers": args.max_workers,
        "overwrite": args.overwrite,
        "dry_run": args.dry_run,
        "months_processed": 0,
        "games_discovered": 0,
        "games_downloaded": 0,
        "games_skipped_exists": 0,
        "games_failed": 0,
        "errors": [],
        "monthly": [],
    }

    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "NHL-BOT-Baseball/1.0"})

    for win_start, win_end in month_windows(start_d, end_d):
        month_id = f"{win_start.year:04d}-{win_start.month:02d}"
        print(f"\n[{month_id}] Fetching schedule {win_start} -> {win_end} ...")
        try:
            sched = fetch_month_schedule(
                session,
                win_start,
                win_end,
                args.sport_id,
                game_types,
                timeout_s=args.timeout_seconds,
                retries=args.retries,
                backoff_s=args.backoff_seconds,
            )
        except Exception as e:
            msg = f"schedule_failed month={month_id}: {e}"
            print(f"  ERROR: {msg}")
            summary["errors"].append(msg)
            summary["months_processed"] += 1
            continue

        schedule_path = month_schedule_path(win_start)
        save_json(schedule_path, sched)

        games = extract_games(sched)
        summary["games_discovered"] += len(games)

        month_stats = {
            "month": month_id,
            "window_start": win_start.isoformat(),
            "window_end": win_end.isoformat(),
            "schedule_path": str(schedule_path),
            "games_discovered": len(games),
            "downloaded": 0,
            "skipped_exists": 0,
            "failed": 0,
        }

        manifest_games: List[dict] = []
        for g in games:
            manifest_games.append(
                {
                    "game_pk": g.game_pk,
                    "game_type": g.game_type,
                    "season_folder": SEASON_FOLDER_BY_GAME_TYPE.get(g.game_type, g.game_type.lower() or "other"),
                    "game_date": g.game_date,
                    "status": g.status,
                    "home_abbrev": g.home_abbrev,
                    "away_abbrev": g.away_abbrev,
                    "output_path": str(game_output_path(g)),
                }
            )

        if args.dry_run or not games:
            print(f"  games={len(games)} (dry-run, no game feed downloads)")
            save_json(
                month_manifest_path(win_start),
                {
                    "month": month_id,
                    "window_start": win_start.isoformat(),
                    "window_end": win_end.isoformat(),
                    "generated_at_utc": datetime.utcnow().isoformat() + "Z",
                    "stats": month_stats,
                    "games": manifest_games,
                },
            )
            summary["monthly"].append(month_stats)
            summary["months_processed"] += 1
            continue

        print(f"  games={len(games)} — downloading feeds ...")
        with cf.ThreadPoolExecutor(max_workers=args.max_workers) as pool:
            futures = [
                pool.submit(
                    fetch_and_store_game,
                    g,
                    timeout_s=args.timeout_seconds,
                    retries=args.retries,
                    backoff_s=args.backoff_seconds,
                    sleep_between_requests=args.sleep_between_requests,
                    overwrite=args.overwrite,
                )
                for g in games
            ]
            for fut in cf.as_completed(futures):
                try:
                    game_pk, status, out_path = fut.result()
                    if status == "downloaded":
                        month_stats["downloaded"] += 1
                        summary["games_downloaded"] += 1
                    elif status == "skipped_exists":
                        month_stats["skipped_exists"] += 1
                        summary["games_skipped_exists"] += 1
                    else:
                        month_stats["failed"] += 1
                        summary["games_failed"] += 1
                        summary["errors"].append(f"unknown_status game={game_pk} status={status}")
                except Exception as e:
                    month_stats["failed"] += 1
                    summary["games_failed"] += 1
                    summary["errors"].append(f"game_fetch_failed month={month_id}: {e}")

        save_json(
            month_manifest_path(win_start),
            {
                "month": month_id,
                "window_start": win_start.isoformat(),
                "window_end": win_end.isoformat(),
                "generated_at_utc": datetime.utcnow().isoformat() + "Z",
                "stats": month_stats,
                "games": manifest_games,
            },
        )

        print(
            "  complete:"
            f" downloaded={month_stats['downloaded']}"
            f" skipped={month_stats['skipped_exists']}"
            f" failed={month_stats['failed']}"
        )

        summary["monthly"].append(month_stats)
        summary["months_processed"] += 1

    summary["finished_at_utc"] = datetime.utcnow().isoformat() + "Z"
    run_path = run_manifest_path()
    save_json(run_path, summary)

    print("\n" + "=" * 72)
    print("SCRAPE SUMMARY")
    print("=" * 72)
    print(f"Months processed:   {summary['months_processed']}")
    print(f"Games discovered:   {summary['games_discovered']}")
    print(f"Downloaded:         {summary['games_downloaded']}")
    print(f"Skipped existing:   {summary['games_skipped_exists']}")
    print(f"Failed:             {summary['games_failed']}")
    print(f"Run manifest:       {run_path}")
    if summary["errors"]:
        print(f"Errors logged:      {len(summary['errors'])} (see run manifest)")
    print("=" * 72)


if __name__ == "__main__":
    main()
