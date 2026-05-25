#!/usr/bin/env python3
"""shared_market_data.py -- IPC-safe market-data payload helpers.

The shared paper runner keeps one monitor/watcher process responsible for
schedule, discovery, and book polling. Paper engine consumers receive plain
dict payloads over ``multiprocessing.connection`` and reconstruct the monitor
dataclasses expected by ``SignalEngine._on_tick_batch``.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from monitor_models import GameMarketMatch, OUMarket, ScheduledGame, ScheduleScore


SCHEMA_VERSION = 1


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc_iso(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def game_to_dict(game: ScheduledGame) -> Dict[str, Any]:
    return asdict(game)


def market_to_dict(market: OUMarket) -> Dict[str, Any]:
    return asdict(market)


def match_to_dict(match: GameMarketMatch) -> Dict[str, Any]:
    return asdict(match)


def score_from_dict(payload: Mapping[str, Any]) -> ScheduleScore:
    return ScheduleScore(
        away=payload.get("away"),
        home=payload.get("home"),
        inning=payload.get("inning"),
        inning_state=payload.get("inning_state"),
        outs=payload.get("outs"),
        balls=payload.get("balls"),
        strikes=payload.get("strikes"),
        runners_on=int(payload.get("runners_on") or 0),
        away_inning_runs=list(payload.get("away_inning_runs") or []),
        home_inning_runs=list(payload.get("home_inning_runs") or []),
    )


def game_from_dict(payload: Mapping[str, Any]) -> ScheduledGame:
    return ScheduledGame(
        game_pk=int(payload.get("game_pk") or 0),
        game_date=str(payload.get("game_date") or ""),
        start_time_utc=str(payload.get("start_time_utc") or ""),
        away_abbrev=str(payload.get("away_abbrev") or ""),
        home_abbrev=str(payload.get("home_abbrev") or ""),
        away_name=str(payload.get("away_name") or ""),
        home_name=str(payload.get("home_name") or ""),
        status_abstract=str(payload.get("status_abstract") or ""),
        status_detailed=str(payload.get("status_detailed") or ""),
        score=score_from_dict(payload.get("score") or {}),
        venue_name=str(payload.get("venue_name") or ""),
        current_pitcher_id=payload.get("current_pitcher_id"),
        current_pitcher_name=str(payload.get("current_pitcher_name") or ""),
        current_pitcher_era=float(payload.get("current_pitcher_era") or 4.20),
    )


def market_from_dict(payload: Mapping[str, Any]) -> OUMarket:
    return OUMarket(
        market_id=str(payload.get("market_id") or ""),
        question=str(payload.get("question") or ""),
        line=str(payload.get("line") or ""),
        over_token_id=str(payload.get("over_token_id") or ""),
        under_token_id=str(payload.get("under_token_id") or ""),
        deploying=bool(payload.get("deploying") or False),
    )


def match_from_dict(payload: Mapping[str, Any]) -> GameMarketMatch:
    return GameMarketMatch(
        game_pk=int(payload.get("game_pk") or 0),
        event_slug=str(payload.get("event_slug") or ""),
        event_title=str(payload.get("event_title") or ""),
        markets=[
            market_from_dict(row)
            for row in (payload.get("markets") or [])
            if isinstance(row, Mapping)
        ],
        discovered_at=str(payload.get("discovered_at") or ""),
    )


def encode_batch(
    *,
    sequence: int,
    date_str: str,
    games: Mapping[int, ScheduledGame],
    matches: Mapping[int, GameMarketMatch],
    active_games: Mapping[int, bool],
    tick_batch: Iterable[Tuple[ScheduledGame, OUMarket, str, dict]],
    health: Mapping[str, Any],
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for game, market, side, payload in tick_batch:
        rows.append(
            {
                "game_pk": int(game.game_pk),
                "line": str(market.line),
                "side": str(side),
                "market": market_to_dict(market),
                "payload": dict(payload or {}),
            }
        )
    return {
        "type": "tick_batch",
        "schema_version": SCHEMA_VERSION,
        "sequence": int(sequence),
        "emitted_at_utc": now_utc_iso(),
        "date_str": str(date_str),
        "games": {str(k): game_to_dict(v) for k, v in games.items()},
        "matches": {str(k): match_to_dict(v) for k, v in matches.items()},
        "active_games": {str(k): bool(v) for k, v in active_games.items()},
        "ticks": rows,
        "health": dict(health or {}),
    }


def decode_state(payload: Mapping[str, Any]) -> Tuple[Dict[int, ScheduledGame], Dict[int, GameMarketMatch], Dict[int, bool]]:
    games = {
        int(k): game_from_dict(v)
        for k, v in (payload.get("games") or {}).items()
        if isinstance(v, Mapping)
    }
    matches = {
        int(k): match_from_dict(v)
        for k, v in (payload.get("matches") or {}).items()
        if isinstance(v, Mapping)
    }
    active_games = {
        int(k): bool(v)
        for k, v in (payload.get("active_games") or {}).items()
    }
    return games, matches, active_games


def decode_tick_batch(payload: Mapping[str, Any]) -> List[Tuple[ScheduledGame, OUMarket, str, dict]]:
    games, matches, _active = decode_state(payload)
    out: List[Tuple[ScheduledGame, OUMarket, str, dict]] = []
    for row in payload.get("ticks") or []:
        if not isinstance(row, Mapping):
            continue
        game = games.get(int(row.get("game_pk") or 0))
        if game is None:
            continue
        market_payload = row.get("market")
        market = market_from_dict(market_payload) if isinstance(market_payload, Mapping) else None
        if market is None:
            match = matches.get(game.game_pk)
            line = str(row.get("line") or "")
            for candidate in (match.markets if match else []):
                if str(candidate.line) == line:
                    market = candidate
                    break
        if market is None:
            continue
        out.append((game, market, str(row.get("side") or ""), dict(row.get("payload") or {})))
    return out


def shutdown_payload(*, sequence: int, date_str: str, reason: str = "watcher_exit") -> Dict[str, Any]:
    return {
        "type": "shutdown",
        "schema_version": SCHEMA_VERSION,
        "sequence": int(sequence),
        "emitted_at_utc": now_utc_iso(),
        "date_str": str(date_str),
        "reason": str(reason),
    }
