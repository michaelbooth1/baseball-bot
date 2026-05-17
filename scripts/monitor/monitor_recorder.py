"""On-disk recorder for schedule, market map, and per-(game, line, side) book ticks.

One handle per (game_pk, line, side) is held open and append-flushed each tick;
``close_game`` and ``close_all`` release them. Per-game locks serialize writes
inside the polling thread pool.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, Tuple

from monitor_models import GameMarketMatch, ScheduledGame
from monitor_utils import _game_dir_name, _now_iso


class LocalRecorder:
    def __init__(self, output_root: Path, date_str: str):
        self.output_root = output_root
        self.date_str = date_str
        self._file_handles: Dict[Tuple[int, str, str], object] = {}
        self._locks: Dict[int, threading.Lock] = {}
        self._meta_written: set[int] = set()

    def _game_dir(self, game: ScheduledGame) -> Path:
        return self.output_root / self.date_str / _game_dir_name(game.away_abbrev, game.home_abbrev, game.game_pk)

    def write_schedule(self, payload: dict) -> Path:
        schedule_dir = self.output_root / "schedules"
        schedule_dir.mkdir(parents=True, exist_ok=True)
        path = schedule_dir / f"mlb_schedule_{self.date_str}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        return path

    def write_game_meta(self, game: ScheduledGame, match: GameMarketMatch) -> Path:
        gdir = self._game_dir(game)
        gdir.mkdir(parents=True, exist_ok=True)
        path = gdir / "meta.json"
        payload = {
            "written_at": _now_iso(),
            "date": self.date_str,
            "game": {
                "game_pk": game.game_pk,
                "game_date": game.game_date,
                "start_time_utc": game.start_time_utc,
                "away_abbrev": game.away_abbrev,
                "home_abbrev": game.home_abbrev,
                "away_name": game.away_name,
                "home_name": game.home_name,
            },
            "polymarket_event": {
                "slug": match.event_slug,
                "title": match.event_title,
                "discovered_at": match.discovered_at,
            },
            "ou_markets": [
                {
                    "line": m.line,
                    "market_id": m.market_id,
                    "question": m.question,
                    "over_token_id": m.over_token_id,
                    "under_token_id": m.under_token_id,
                }
                for m in sorted(match.markets, key=lambda x: float(x.line))
            ],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        self._meta_written.add(game.game_pk)
        return path

    def write_market_map(self, matches: Dict[int, GameMarketMatch], games: Dict[int, ScheduledGame]) -> Path:
        root = self.output_root / self.date_str
        root.mkdir(parents=True, exist_ok=True)
        path = root / "market_map.json"
        payload = {
            "written_at": _now_iso(),
            "date": self.date_str,
            "games": [],
        }
        for game_pk, match in sorted(matches.items()):
            game = games.get(game_pk)
            if game is None:
                continue
            payload["games"].append(
                {
                    "game_pk": game_pk,
                    "away_abbrev": game.away_abbrev,
                    "home_abbrev": game.home_abbrev,
                    "event_slug": match.event_slug,
                    "event_title": match.event_title,
                    "line_count": len(match.markets),
                    "lines": sorted([m.line for m in match.markets], key=float),
                }
            )
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        return path

    def _file_for(self, game: ScheduledGame, line: str, side: str):
        key = (game.game_pk, line, side)
        if key in self._file_handles:
            return self._file_handles[key]
        gdir = self._game_dir(game)
        gdir.mkdir(parents=True, exist_ok=True)
        fname = f"ou_{line.replace('.', '_')}_{side}.jsonl"
        f = open(gdir / fname, "a", encoding="utf-8")
        self._file_handles[key] = f
        return f

    def append_snapshot(self, game: ScheduledGame, line: str, side: str, payload: dict) -> None:
        lock = self._locks.setdefault(game.game_pk, threading.Lock())
        with lock:
            fh = self._file_for(game=game, line=line, side=side)
            fh.write(json.dumps(payload) + "\n")
            fh.flush()  # ensure data reaches disk; guards against tick loss on crash

    def close_game(self, game_pk: int) -> None:
        close_keys = [k for k in self._file_handles.keys() if k[0] == game_pk]
        for key in close_keys:
            try:
                self._file_handles[key].close()
            except Exception:
                pass
            del self._file_handles[key]
        self._locks.pop(game_pk, None)

    def close_all(self) -> None:
        keys = list(self._file_handles.keys())
        for key in keys:
            try:
                self._file_handles[key].close()
            except Exception:
                pass
            del self._file_handles[key]
