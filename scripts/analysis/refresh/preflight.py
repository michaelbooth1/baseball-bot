"""Inline preflight handlers + the INLINE_HANDLERS registry.

These are cheap, in-process checks that surface broken state (missing
.env, unreadable caches, stale Stage-3 corpus) up front so failures
don't appear at first-tick of the live engine.

The @_inline decorator + INLINE_HANDLERS dict live here. All other
modules that register handlers must be imported by the package
__init__ so their decorators fire before any caller looks up the
registry.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .config import (
    RefreshConfig,
    STAGE1_HISTORY_FULL_SEASONS,
)
from .helpers import _stage1_expected_season_window


InlineHandler = Callable[[RefreshConfig], Tuple[bool, str]]
INLINE_HANDLERS: Dict[str, InlineHandler] = {}


def _inline(name: str) -> Callable[[InlineHandler], InlineHandler]:
    def deco(fn: InlineHandler) -> InlineHandler:
        INLINE_HANDLERS[name] = fn
        return fn
    return deco


def _read_env_file(env_path: Path) -> Dict[str, str]:
    if not env_path.exists():
        return {}
    out: Dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


@_inline("preflight_env_secrets")
def _handle_preflight_env_secrets(config: RefreshConfig) -> Tuple[bool, str]:
    env_path = config.env_path
    notes: List[str] = [f"env_path={env_path}"]
    if not env_path.exists():
        notes.append("WARNING: .env file not found (paper mode unaffected; live mode requires POLY_PRIVATE_KEY).")
        if config.require_poly_private_key:
            return False, "\n".join(notes)
        return True, "\n".join(notes)
    env = _read_env_file(env_path)
    poly_key = env.get("POLY_PRIVATE_KEY", "")
    if not poly_key:
        notes.append("WARNING: POLY_PRIVATE_KEY not set in .env (live trading will fail).")
        if config.require_poly_private_key:
            return False, "\n".join(notes)
        return True, "\n".join(notes)
    # Don't log the key value. Just confirm length looks plausible.
    notes.append(f"POLY_PRIVATE_KEY present (length={len(poly_key)})")
    return True, "\n".join(notes)


def _safe_load_json(path: Path) -> Tuple[Optional[object], Optional[str]]:
    if not path.exists():
        return None, f"missing: {path}"
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except Exception as exc:
        return None, f"unreadable: {path} ({exc})"


def _team_game_log_health(payload: object, active_year: str) -> Tuple[bool, str]:
    """Verify team_game_log has games for the active season for ~all teams."""
    if not isinstance(payload, dict):
        return False, "team_game_log payload is not a dict"
    games = payload.get("games") or []
    if not isinstance(games, list):
        return False, "team_game_log.games is not a list"
    teams_with_active_season: Dict[str, int] = {}
    teams_total: set = set()
    for g in games:
        if not isinstance(g, dict):
            continue
        date_str = str(g.get("date", ""))
        away = str(g.get("away", "") or "")
        home = str(g.get("home", "") or "")
        if away:
            teams_total.add(away)
        if home:
            teams_total.add(home)
        if date_str.startswith(active_year + "-"):
            if away:
                teams_with_active_season[away] = teams_with_active_season.get(away, 0) + 1
            if home:
                teams_with_active_season[home] = teams_with_active_season.get(home, 0) + 1
    if not teams_total:
        return False, "team_game_log has zero teams"
    coverage = len(teams_with_active_season) / max(len(teams_total), 1)
    if coverage < 0.8:
        return False, (
            f"only {len(teams_with_active_season)}/{len(teams_total)} teams have {active_year} games "
            f"(coverage {coverage:.0%}; Stage-3 season_to_date will be unreliable). "
            f"Run scrape_recent_games to backfill."
        )
    avg = sum(teams_with_active_season.values()) / max(len(teams_with_active_season), 1)
    return True, (
        f"team_game_log: {len(teams_total)} teams, "
        f"{len(teams_with_active_season)} with {active_year} games (avg {avg:.0f}/team)"
    )


def _stage1_cache_health(payload: object, active_date: str) -> Tuple[bool, str]:
    """Warn when Stage-1 cache does not cover the expected production history window."""
    if not isinstance(payload, dict):
        return False, "Stage-1 cache payload is not a dict"
    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        return True, "WARNING Stage-1 coverage metadata missing; rebuild with current build_mlb_ou_cache.py"

    expected_start, expected_end = _stage1_expected_season_window(active_date)
    expected = [str(year) for year in range(expected_start, expected_end + 1)]
    games_by_season = meta.get("games_by_season") or {}
    seasons = [str(season) for season in (meta.get("seasons") or [])]
    missing = [season for season in expected if str(season) not in games_by_season and str(season) not in seasons]
    history_start = str(meta.get("history_start_date") or "")
    history_end = str(meta.get("history_end_date") or "")
    total_games = meta.get("total_games", meta.get("games_loaded"))
    duplicate_skipped = meta.get("duplicate_game_files_skipped")
    if missing:
        return True, (
            "WARNING Stage-1 coverage: expected prior "
            f"{STAGE1_HISTORY_FULL_SEASONS} full seasons {expected_start}-{expected_end}, "
            f"missing metadata for {', '.join(missing)}. "
            "Rebuild with --min-season/--max-season after historical scrape."
        )
    if history_start and history_start > f"{expected_start}-12-31":
        return True, (
            f"WARNING Stage-1 coverage starts at {history_start}, later than expected season {expected_start}."
        )
    if history_end and history_end < f"{expected_end}-01-01":
        return True, (
            f"WARNING Stage-1 coverage ends at {history_end}, earlier than expected season {expected_end}."
        )
    return True, (
        f"ok Stage-1 production coverage: seasons {expected_start}-{expected_end}, "
        f"total_games={total_games}, history={history_start or 'unknown'}->{history_end or 'unknown'}, "
        f"duplicate_files_skipped={duplicate_skipped if duplicate_skipped is not None else 'unknown'}"
    )


@_inline("preflight_artifacts")
def _handle_preflight_artifacts(config: RefreshConfig) -> Tuple[bool, str]:
    notes: List[str] = []
    ok = True

    payload, err = _safe_load_json(config.mlb_ou_cache_path)
    if err:
        ok = False
        notes.append(f"FAIL Stage-1 (mlb_ou_cache.json): {err}")
    else:
        if isinstance(payload, dict) and not payload:
            ok = False
            notes.append("FAIL Stage-1 (mlb_ou_cache.json): empty dict")
        else:
            notes.append("ok Stage-1 (mlb_ou_cache.json) loaded")
            sub_ok, sub_msg = _stage1_cache_health(payload, config.active_date)
            if not sub_ok:
                ok = False
            notes.append(sub_msg)

    payload, err = _safe_load_json(config.stage2_cache_path)
    if err:
        ok = False
        notes.append(f"FAIL Stage-2 (mlb_stage2_run_env.json): {err}")
    else:
        notes.append("ok Stage-2 (mlb_stage2_run_env.json) loaded")

    payload, err = _safe_load_json(config.team_game_log_path)
    if err:
        ok = False
        notes.append(f"FAIL Stage-3 (team_game_log.json): {err}")
    else:
        active_year = config.active_date[:4]
        sub_ok, sub_msg = _team_game_log_health(payload, active_year)
        if not sub_ok:
            ok = False
            notes.append(f"FAIL Stage-3: {sub_msg}")
        else:
            notes.append(f"ok Stage-3: {sub_msg}")

    payload, err = _safe_load_json(config.pitcher_cache_path)
    if err:
        # Pitcher cache failures are warnings — Gate 8i is bypassable.
        notes.append(f"WARNING Pitcher cache: {err} (Gate 8i will be inactive).")
    else:
        if isinstance(payload, dict):
            n_pitchers = len(payload.get("pitchers", payload)) if isinstance(payload, dict) else 0
            notes.append(f"ok Pitcher cache ({n_pitchers} entries)")
        else:
            notes.append("ok Pitcher cache loaded")

    payload, err = _safe_load_json(config.park_hr_factors_path)
    if err:
        # Stage-2 hr_factor family degrades to UNKNOWN_BUCKET when this file
        # is missing -- non-fatal (other Stage-2 families still apply).
        notes.append(f"WARNING Park HR factors: {err} (Stage-2 hr_factor family will be inactive).")
    elif isinstance(payload, dict):
        by_park = payload.get("by_park") or {}
        active_year = config.active_date[:4]
        with_active = sum(1 for entries in by_park.values()
                          if isinstance(entries, dict) and active_year in entries)
        if not by_park:
            notes.append("WARNING Park HR factors: empty by_park (hr_factor family inactive).")
        elif with_active == 0:
            notes.append(
                f"WARNING Park HR factors: no entries for {active_year} "
                f"(hr_factor will use most-recent-year fallback)."
            )
        else:
            notes.append(f"ok Park HR factors: {len(by_park)} parks, {with_active} with {active_year} entries")

    return ok, "\n".join(notes)
