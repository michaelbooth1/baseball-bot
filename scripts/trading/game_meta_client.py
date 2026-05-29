#!/usr/bin/env python3
"""game_meta_client.py -- per-game metadata cache (home-plate umpire + officials).

Tier-2 data capture (2026-05-29). Some per-game context is NOT in the
lightweight schedule the monitor polls -- most importantly the home-plate
umpire, a classic Over/Under factor (a tight/loose strike zone shifts run
scoring). It lives in the boxscore endpoint
(`/api/v1/game/{game_pk}/boxscore` -> `officials`), which is too heavy to
poll per tick. So we fetch it ONCE per game into a per-date cache, mirroring
the Weather v2 cache pattern (`cache/weather/game_weather_<date>.json`), and
the engine joins it onto candidate rows by `game_pk` at write time.

Timing caveat: umpires are only published in the boxscore once lineups are
posted (~1-2h before first pitch) or the game is live. A morning refresh for
night games will see no officials yet (recorded as unavailable); a refresh
nearer game time -- as happens when the live engine boots its daily refresh
just before the slate -- captures them. Fail-open throughout: any fetch
error degrades to "unavailable", never blocks startup.

Public surface (mirrors weather_client):
  - GAME_META_FEATURE_FIELD_KEYS              -- flat keys joined to candidate rows
  - DEFAULT_GAME_META_CACHE_DIR
  - default_game_meta_cache_path(date, cache_dir=...)
  - build_game_meta_cache(date_str, *, timeout, schedule_fetcher, boxscore_fetcher)
  - write_game_meta_cache(payload, path)
  - flatten_game_meta_cache_game(game_row) -> dict
  - load_game_meta_features_by_game(path) -> {game_pk: {flat fields}}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("game_meta_client")

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_GAME_META_CACHE_DIR = PROJECT_DIR / "cache" / "game_meta"
MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
MLB_BOXSCORE_URL = "https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore"
SCHEMA_VERSION = 1

# Flat fields joined onto each candidate row (model-safe subset). The full
# officials list is kept in the cache file for audit but NOT flattened onto
# every candidate row, to avoid bloat.
GAME_META_FEATURE_FIELD_KEYS: Tuple[str, ...] = (
    "game_meta_available",
    "hp_umpire_id",
    "hp_umpire_name",
)


def default_game_meta_cache_path(date_str: str, cache_dir: Path = DEFAULT_GAME_META_CACHE_DIR) -> Path:
    return Path(cache_dir) / f"game_meta_{date_str}.json"


# --------------------------------------------------------------------------
# Pure extraction (unit-tested without network)
# --------------------------------------------------------------------------
def extract_officials(boxscore: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return [{type, id, name}] from a boxscore `officials` array."""
    out: List[Dict[str, Any]] = []
    for row in (boxscore or {}).get("officials", []) or []:
        if not isinstance(row, dict):
            continue
        official = row.get("official", {}) or {}
        oid = official.get("id")
        try:
            oid = int(oid) if oid is not None else None
        except (TypeError, ValueError):
            oid = None
        out.append({
            "type": str(row.get("officialType") or ""),
            "id": oid,
            "name": str(official.get("fullName") or ""),
        })
    return out


def extract_hp_umpire(boxscore: Dict[str, Any]) -> Tuple[Optional[int], str]:
    """Return (home_plate_umpire_id, name) or (None, "")."""
    for official in extract_officials(boxscore):
        if official["type"].strip().lower() == "home plate":
            return official["id"], official["name"]
    return None, ""


def flatten_game_meta_cache_game(game_row: Dict[str, Any]) -> Dict[str, Any]:
    """Project one cache game row into the flat candidate-row feature subset."""
    return {
        "game_meta_available": bool(game_row.get("hp_umpire_id") is not None
                                    or game_row.get("officials")),
        "hp_umpire_id": game_row.get("hp_umpire_id"),
        "hp_umpire_name": game_row.get("hp_umpire_name") or None,
    }


# --------------------------------------------------------------------------
# Network fetchers (default; injectable for tests)
# --------------------------------------------------------------------------
def _default_schedule_fetcher(date_str: str, timeout: float) -> List[int]:
    import requests  # lazy
    resp = requests.get(
        MLB_SCHEDULE_URL,
        params={"sportId": 1, "date": date_str},
        timeout=timeout,
        headers={"User-Agent": "MLB-Poly-OU-GameMeta/1.0"},
    )
    resp.raise_for_status()
    payload = resp.json()
    game_pks: List[int] = []
    for date_row in payload.get("dates", []) or []:
        for g in date_row.get("games", []) or []:
            pk = g.get("gamePk")
            if pk is not None:
                try:
                    game_pks.append(int(pk))
                except (TypeError, ValueError):
                    continue
    return game_pks


def _default_boxscore_fetcher(game_pk: int, timeout: float) -> Dict[str, Any]:
    import requests  # lazy
    resp = requests.get(
        MLB_BOXSCORE_URL.format(game_pk=game_pk),
        timeout=timeout,
        headers={"User-Agent": "MLB-Poly-OU-GameMeta/1.0"},
    )
    resp.raise_for_status()
    return resp.json()


# --------------------------------------------------------------------------
# Build + persist
# --------------------------------------------------------------------------
def build_game_meta_cache(
    date_str: str,
    *,
    timeout: float = 8.0,
    schedule_fetcher: Optional[Callable[[str, float], List[int]]] = None,
    boxscore_fetcher: Optional[Callable[[int, float], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Fetch per-game metadata (HP umpire + officials) for one date.

    Fail-open: schedule failure -> empty cache with a warning; per-game
    boxscore failure -> that game recorded as unavailable. Never raises.
    """
    schedule_fetcher = schedule_fetcher or _default_schedule_fetcher
    boxscore_fetcher = boxscore_fetcher or _default_boxscore_fetcher

    warnings: List[str] = []
    try:
        game_pks = list(schedule_fetcher(date_str, timeout))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"schedule_fetch_failed: {exc!r}")
        game_pks = []

    games: List[Dict[str, Any]] = []
    with_umpire = 0
    for pk in game_pks:
        try:
            boxscore = boxscore_fetcher(pk, timeout)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"boxscore_fetch_failed:{pk}: {exc!r}")
            games.append({"game_pk": pk, "hp_umpire_id": None,
                          "hp_umpire_name": "", "officials": [], "fetch_ok": False})
            continue
        officials = extract_officials(boxscore)
        hp_id, hp_name = extract_hp_umpire(boxscore)
        if hp_id is not None:
            with_umpire += 1
        games.append({
            "game_pk": pk,
            "hp_umpire_id": hp_id,
            "hp_umpire_name": hp_name,
            "officials": officials,
            "fetch_ok": True,
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "date": date_str,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "coverage": {
            "scheduled_games": len(game_pks),
            "games_with_umpire": with_umpire,
        },
        "warnings": warnings,
        "games": games,
    }


def write_game_meta_cache(payload: Dict[str, Any], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def load_game_meta_features_by_game(path: Path) -> Dict[int, Dict[str, Any]]:
    """Load a cache file into {game_pk: flat feature dict} for candidate joins."""
    path = Path(path)
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    out: Dict[int, Dict[str, Any]] = {}
    for game_row in payload.get("games", []) or []:
        pk = game_row.get("game_pk")
        if pk is None:
            continue
        try:
            out[int(pk)] = flatten_game_meta_cache_game(game_row)
        except (TypeError, ValueError):
            continue
    return out
