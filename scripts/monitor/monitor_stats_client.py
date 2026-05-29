"""MLB Stats API client + local pitcher ERA cache.

Owns:
- ``fetch_schedule_payload``: short retry budget so the polling loop never stalls.
- ``parse_games``: maps a schedule payload into ``ScheduledGame`` instances and
  enriches each with the current defensive pitcher's ERA from the local cache.
- ``_load_or_rebuild_pitcher_cache`` / ``_build_pitcher_cache``: bounded daily
  rebuild, with a same-season stale-fallback path so Gate 8i stays active during
  brief StatsAPI outages (TR14).
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests

from monitor_constants import (
    DEFAULT_PITCHER_CACHE_PATH,
    LOGGER,
    MLB_AVG_ERA,
    MLB_SCHEDULE_URL,
    PITCHER_CACHE_MAX_AGE_HOURS,
    PITCHER_CACHE_MIN_PITCHER_COUNT,
    PITCHER_CACHE_STALE_FALLBACK_MAX_AGE_HOURS,
)
from monitor_models import ScheduleScore, ScheduledGame
from monitor_utils import _safe_int


class MLBStatsClient:
    def __init__(
        self,
        timeout: float = 8.0,
        pitcher_cache_path: Optional[str] = None,
        pitcher_cache_refresh_date: Optional[str] = None,
    ):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "MLB-Poly-OU-Monitor/1.0",
            "Accept": "application/json",
            "Connection": "keep-alive",
        })
        # Explicit pool sizing: default (10) is too small for reliable reuse across the
        # 30s schedule refresh interval. pool_maxsize=20 matches the max_workers default
        # so connections are never queued waiting for a pool slot.
        _adapter = requests.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=20, max_retries=0)
        self.session.mount("https://", _adapter)
        self.session.mount("http://", _adapter)
        self._pitcher_cache: Dict[str, float] = {}   # pitcher_id -> ERA
        _cache_path = Path(pitcher_cache_path) if pitcher_cache_path else DEFAULT_PITCHER_CACHE_PATH
        self._load_or_rebuild_pitcher_cache(
            _cache_path,
            refresh_date=pitcher_cache_refresh_date,
        )

    def _pitcher_cache_built_date(self, path: Path) -> Optional[str]:
        """Return cache built_at date (YYYY-MM-DD), falling back to mtime date."""

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            built_at = str(data.get("built_at") or "").strip()
            if len(built_at) >= 10:
                return built_at[:10]
        except Exception:
            pass
        try:
            return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
        except Exception:
            return None

    def _validate_stale_pitcher_cache(self, path: Path) -> Tuple[bool, str, Dict[str, object]]:
        """Return whether an existing stale pitcher cache is safe as fallback."""

        meta: Dict[str, object] = {}
        try:
            age_hours = (time.time() - path.stat().st_mtime) / 3600
            meta["age_hours"] = age_hours
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            return False, f"read_error={exc}", meta

        season = data.get("season")
        pitchers = data.get("pitchers", {}) or {}
        pitcher_count = int(data.get("pitcher_count") or len(pitchers) or 0)
        current_season = datetime.now().year
        meta.update({
            "season": season,
            "current_season": current_season,
            "pitcher_count": pitcher_count,
        })

        if age_hours > PITCHER_CACHE_STALE_FALLBACK_MAX_AGE_HOURS:
            return (
                False,
                f"age_hours={age_hours:.1f} > max={PITCHER_CACHE_STALE_FALLBACK_MAX_AGE_HOURS}",
                meta,
            )
        try:
            season_int = int(season or 0)
        except (TypeError, ValueError):
            return False, f"season={season} is not parseable", meta
        if season_int != current_season:
            return False, f"season={season} != current_season={current_season}", meta
        if pitcher_count < PITCHER_CACHE_MIN_PITCHER_COUNT:
            return (
                False,
                f"pitcher_count={pitcher_count} < min={PITCHER_CACHE_MIN_PITCHER_COUNT}",
                meta,
            )
        return True, "ok", meta

    def _load_or_rebuild_pitcher_cache(
        self,
        path: Path,
        refresh_date: Optional[str] = None,
    ) -> None:
        """Load the pitcher ERA cache, rebuilding from MLB Stats API if stale or missing.

        [TR14] Hardened against transient StatsAPI outages:
        - If a cache was built earlier than the current local run date, refresh it.
        - If a fresh same-day cache exists on disk, use it (no rebuild attempt).
        - If rebuild is needed and fails but a bounded same-season stale cache
          exists, load it so Gate 8i remains active during brief API outages.
        """
        import time as _time
        needs_rebuild = False
        had_existing_cache = path.exists()
        can_load_cache = had_existing_cache
        if not had_existing_cache:
            LOGGER.info("Pitcher cache not found at %s — building now...", path)
            needs_rebuild = True
            can_load_cache = False
        else:
            age_hours = (_time.time() - path.stat().st_mtime) / 3600
            built_date = self._pitcher_cache_built_date(path)
            if refresh_date and built_date != refresh_date:
                LOGGER.info(
                    "Pitcher cache built date %s does not match run date %s — rebuilding...",
                    built_date or "unknown", refresh_date,
                )
                needs_rebuild = True
            elif age_hours > PITCHER_CACHE_MAX_AGE_HOURS:
                LOGGER.info(
                    "Pitcher cache is %.1f hours old (max %d) — rebuilding...",
                    age_hours, PITCHER_CACHE_MAX_AGE_HOURS,
                )
                needs_rebuild = True

        if needs_rebuild:
            built_ok = self._build_pitcher_cache(path)
            can_load_cache = bool(built_ok and path.exists())
            if not built_ok and had_existing_cache:
                fallback_ok, fallback_reason, meta = self._validate_stale_pitcher_cache(path)
                can_load_cache = fallback_ok
                if fallback_ok:
                    LOGGER.warning(
                        "Pitcher cache rebuild failed; falling back to bounded STALE cache "
                        "(age=%.1fh season=%s pitchers=%s) so Gate 8i remains active.",
                        float(meta.get("age_hours") or 0.0),
                        meta.get("season"),
                        meta.get("pitcher_count"),
                    )
                else:
                    LOGGER.warning(
                        "Pitcher cache rebuild failed; stale fallback rejected (%s). Gate 8i disabled.",
                        fallback_reason,
                    )

        if can_load_cache and path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                pitchers = data.get("pitchers", {})
                self._pitcher_cache = {str(pid): float(p.get("era", MLB_AVG_ERA))
                                       for pid, p in pitchers.items()}
                LOGGER.info("Pitcher cache loaded: %d pitchers from %s", len(self._pitcher_cache), path)
            except Exception as exc:
                LOGGER.warning("Failed to load pitcher cache: %s — Gate 8i disabled", exc)
        elif needs_rebuild:
            LOGGER.warning("No usable pitcher cache available — Gate 8i disabled")

    def _build_pitcher_cache(self, path: Path) -> bool:
        """Fetch current-season pitching stats from MLB Stats API and write the cache.

        [TR14] Returns True on success, False on failure (so the caller can
        decide whether to fall back to a stale on-disk cache). Retries up to
        3 times with exponential backoff (15s, 30s, 60s timeouts) before giving up.
        """
        import datetime as _dt
        import time as _time
        season = _dt.datetime.now().year
        MLB_STATS_URL = "https://statsapi.mlb.com/api/v1/stats"
        attempts = [
            {"timeout": 15, "wait_after_fail": 5},
            {"timeout": 30, "wait_after_fail": 15},
            {"timeout": 60, "wait_after_fail": 0},
        ]
        last_exc: Optional[Exception] = None
        resp = None
        for i, plan in enumerate(attempts, start=1):
            try:
                LOGGER.info(
                    "Fetching pitcher ERA data from MLB Stats API (season=%d, attempt %d/%d, timeout=%ds)...",
                    season, i, len(attempts), plan["timeout"],
                )
                resp = self.session.get(
                    MLB_STATS_URL,
                    params={
                        "stats": "season", "group": "pitching",
                        "season": season, "gameType": "R",
                        "limit": 500,
                        "fields": "stats,splits,player,id,fullName,stat,era,inningsPitched,gamesStarted",
                    },
                    timeout=plan["timeout"],
                )
                resp.raise_for_status()
                break  # success
            except Exception as exc:
                last_exc = exc
                LOGGER.warning(
                    "Pitcher cache fetch attempt %d/%d failed: %s",
                    i, len(attempts), exc,
                )
                if plan["wait_after_fail"] > 0 and i < len(attempts):
                    _time.sleep(plan["wait_after_fail"])
        else:
            LOGGER.warning(
                "All %d pitcher cache fetch attempts failed (last error: %s)",
                len(attempts), last_exc,
            )
            return False

        try:
            data = resp.json()
            splits = data.get("stats", [{}])[0].get("splits", [])

            pitchers: Dict[str, dict] = {}
            for split in splits:
                player = split.get("player", {}) or {}
                stat = split.get("stat", {}) or {}
                pid = player.get("id")
                if pid is None:
                    continue
                ip_raw = str(stat.get("inningsPitched", "0") or "0")
                try:
                    parts = ip_raw.split(".")
                    ip = int(parts[0]) + (int(parts[1]) / 3.0 if len(parts) > 1 else 0)
                except Exception:
                    ip = 0.0
                if ip < 5.0:
                    continue
                try:
                    era = float(stat.get("era") or MLB_AVG_ERA)
                except (ValueError, TypeError):
                    era = 0.0  # 0 ER in sufficient IP
                pitchers[str(pid)] = {
                    "name": player.get("fullName", ""),
                    "era": round(era, 2),
                    "ip": round(ip, 1),
                    "gs": int(stat.get("gamesStarted", 0) or 0),
                }

            cache = {
                "built_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "season": season,
                "mlb_avg_era": MLB_AVG_ERA,
                "pitcher_count": len(pitchers),
                "pitchers": pitchers,
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2)
            LOGGER.info("Pitcher cache built: %d pitchers -> %s", len(pitchers), path)
            return True
        except Exception as exc:
            LOGGER.warning("Failed to parse/persist pitcher cache: %s", exc)
            return False

    def _load_pitcher_cache(self, path: str) -> None:
        """Legacy method kept for compatibility — prefer _load_or_rebuild_pitcher_cache."""
        self._load_or_rebuild_pitcher_cache(Path(path))

    def fetch_schedule_payload(self, date_str: str) -> dict:
        """Fetch MLB schedule with a short retry budget.

        Schedule refresh runs on the main monitor loop, so retries must not
        stall order polling, fill detection, ask-reversal checks, or FV decay.
        A later refresh is preferable to freezing live order lifecycle work.
        """
        import time as _time
        params = {
            "sportId": 1,
            "date": date_str,
            # hydrate=team includes team abbreviations and richer identifiers.
            # Venue is needed for matching and Weather v2 stadium joins.
            # probablePitcher (2026-05-29) adds each team's starting pitcher
            # (id + fullName) for free in the schedule we already poll.
            "hydrate": "team,linescore,venue,probablePitcher",
        }
        fast_timeout = min(float(self.timeout), 4.0)
        retry_timeout = min(max(fast_timeout * 1.5, 5.0), 6.0)
        attempts = [
            {"timeout": fast_timeout, "wait_after_fail": 0.25},
            {"timeout": retry_timeout, "wait_after_fail": 0.0},
        ]
        last_exc: Optional[Exception] = None
        for i, plan in enumerate(attempts, start=1):
            try:
                resp = self.session.get(
                    MLB_SCHEDULE_URL, params=params, timeout=plan["timeout"],
                )
                resp.raise_for_status()
                if i > 1:
                    LOGGER.info(
                        "Schedule fetch succeeded on attempt %d/%d (timeout=%.0fs)",
                        i, len(attempts), plan["timeout"],
                    )
                return resp.json()
            except Exception as exc:
                last_exc = exc
                LOGGER.info(
                    "Schedule fetch attempt %d/%d failed (timeout=%.0fs): %s",
                    i, len(attempts), plan["timeout"], exc,
                )
                if plan["wait_after_fail"] > 0 and i < len(attempts):
                    _time.sleep(plan["wait_after_fail"])
        raise last_exc if last_exc else RuntimeError("schedule fetch failed with no exception captured")

    def parse_games(self, payload: dict) -> Dict[int, ScheduledGame]:
        out: Dict[int, ScheduledGame] = {}
        for date_row in payload.get("dates", []) or []:
            for g in date_row.get("games", []) or []:
                game_pk = _safe_int(g.get("gamePk"))
                if game_pk is None:
                    continue
                status = g.get("status", {}) or {}
                teams = g.get("teams", {}) or {}
                away = teams.get("away", {}) or {}
                home = teams.get("home", {}) or {}
                away_team = away.get("team", {}) or {}
                home_team = home.get("team", {}) or {}
                linescore = g.get("linescore", {}) or {}
                offense = linescore.get("offense", {}) or {}
                runners_on = (
                    (1 if "first" in offense else 0)
                    | (2 if "second" in offense else 0)
                    | (4 if "third" in offense else 0)
                )
                # Current batter + on-deck (dynamic, ~30s-stale at tick time).
                # Already present in linescore.offense; previously discarded.
                batter = offense.get("batter", {}) or {}
                on_deck = offense.get("onDeck", {}) or {}
                batter_id = _safe_int(batter.get("id"))
                batter_name = str(batter.get("fullName") or "")
                on_deck_id = _safe_int(on_deck.get("id"))
                on_deck_name = str(on_deck.get("fullName") or "")
                away_inning_runs = []
                home_inning_runs = []
                for inning_row in linescore.get("innings", []) or []:
                    if not isinstance(inning_row, dict):
                        continue
                    num = _safe_int(inning_row.get("num"))
                    if num is None or num <= 0 or num > 9:
                        continue
                    away_inning_runs.append(_safe_int(((inning_row.get("away") or {}).get("runs"))) or 0)
                    home_inning_runs.append(_safe_int(((inning_row.get("home") or {}).get("runs"))) or 0)
                score = ScheduleScore(
                    away=_safe_int(away.get("score")),
                    home=_safe_int(home.get("score")),
                    inning=_safe_int(linescore.get("currentInning")),
                    inning_state=str(linescore.get("inningState") or ""),
                    outs=_safe_int(linescore.get("outs")),
                    balls=_safe_int(linescore.get("balls")),
                    strikes=_safe_int(linescore.get("strikes")),
                    runners_on=runners_on,
                    away_inning_runs=away_inning_runs,
                    home_inning_runs=home_inning_runs,
                )
                # Venue is the join key for Weather v2. Schedule weather is
                # intentionally ignored so FV never falls back to old weather.
                venue_data = g.get("venue", {}) or {}

                # Stage-4: extract current pitcher from linescore.defense
                defense = linescore.get("defense", {}) or {}
                defense_pitcher = defense.get("pitcher", {}) or {}
                current_pitcher_id = _safe_int(defense_pitcher.get("id"))
                current_pitcher_name = str(defense_pitcher.get("fullName") or "")
                current_pitcher_era = MLB_AVG_ERA
                if current_pitcher_id is not None:
                    current_pitcher_era = self._pitcher_cache.get(
                        str(current_pitcher_id), MLB_AVG_ERA
                    )

                # Starting pitchers (both teams) from probablePitcher hydrate.
                # ERA pulled from the same local pitcher cache as the current
                # pitcher; None id => unknown (ERA stays None, not the 4.20
                # fallback, so absence is distinguishable).
                away_pp = away.get("probablePitcher", {}) or {}
                home_pp = home.get("probablePitcher", {}) or {}
                away_starter_id = _safe_int(away_pp.get("id"))
                home_starter_id = _safe_int(home_pp.get("id"))
                away_starter_era = (
                    self._pitcher_cache.get(str(away_starter_id))
                    if away_starter_id is not None else None
                )
                home_starter_era = (
                    self._pitcher_cache.get(str(home_starter_id))
                    if home_starter_id is not None else None
                )

                game = ScheduledGame(
                    game_pk=game_pk,
                    game_date=str(g.get("gameDate") or ""),
                    start_time_utc=str(g.get("gameDate") or ""),
                    away_abbrev=str(away_team.get("abbreviation") or "").upper(),
                    home_abbrev=str(home_team.get("abbreviation") or "").upper(),
                    away_name=str(away_team.get("name") or away_team.get("teamName") or ""),
                    home_name=str(home_team.get("name") or home_team.get("teamName") or ""),
                    status_abstract=str(status.get("abstractGameState") or ""),
                    status_detailed=str(status.get("detailedState") or ""),
                    score=score,
                    venue_name=str(venue_data.get("name") or ""),
                    day_night=str(g.get("dayNight") or ""),
                    current_pitcher_id=current_pitcher_id,
                    current_pitcher_name=current_pitcher_name,
                    current_pitcher_era=current_pitcher_era,
                    away_starter_id=away_starter_id,
                    away_starter_name=str(away_pp.get("fullName") or ""),
                    away_starter_era=away_starter_era,
                    home_starter_id=home_starter_id,
                    home_starter_name=str(home_pp.get("fullName") or ""),
                    home_starter_era=home_starter_era,
                    batter_id=batter_id,
                    batter_name=batter_name,
                    on_deck_id=on_deck_id,
                    on_deck_name=on_deck_name,
                )
                out[game_pk] = game
        return out
