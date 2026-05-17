"""Polymarket Gamma event discovery for MLB schedules.

Resolves a ``ScheduledGame`` to a Polymarket event slug + the O/U markets it
contains. Uses two strategies:
1. Deterministic ``mlb-{away}-{home}-{date}`` slug guesses.
2. ``/events?query=`` keyword fallback, filtered by date proximity.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, List, Optional

import requests

from monitor_constants import GAMMA_BASE, OU_LINE_RE, TEAM_SLUGS
from monitor_models import GameMarketMatch, OUMarket, ScheduledGame
from monitor_utils import _normalize_slug_piece


class PolymarketDiscoveryClient:
    def __init__(self, timeout: float = 4.0):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "MLB-Poly-OU-Monitor/1.0",
            "Accept": "application/json",
            "Connection": "keep-alive",
        })
        _adapter = requests.adapters.HTTPAdapter(pool_connections=3, pool_maxsize=10, max_retries=0)
        self.session.mount("https://", _adapter)
        self.session.mount("http://", _adapter)

    def _events_by_slug(self, slug: str) -> List[dict]:
        url = f"{GAMMA_BASE}/events"
        resp = self.session.get(url, params={"slug": slug}, timeout=self.timeout)
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _events_by_query(self, query: str) -> List[dict]:
        url = f"{GAMMA_BASE}/events"
        params = {"query": query, "active": "true"}
        resp = self.session.get(url, params=params, timeout=self.timeout)
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _candidate_slugs(self, game: ScheduledGame, date_str: str) -> List[str]:
        away_slugs = TEAM_SLUGS.get(game.away_abbrev, [game.away_abbrev.lower()])
        home_slugs = TEAM_SLUGS.get(game.home_abbrev, [game.home_abbrev.lower()])
        base = datetime.strptime(date_str, "%Y-%m-%d").date()
        dates = [base]
        out: List[str] = []
        for d in dates:
            dstr = d.strftime("%Y-%m-%d")
            for a in away_slugs:
                for h in home_slugs:
                    out.append(f"mlb-{_normalize_slug_piece(a)}-{_normalize_slug_piece(h)}-{dstr}")
        return out

    @staticmethod
    def _parse_line(question: str) -> Optional[str]:
        q = str(question or "")
        m = OU_LINE_RE.search(q)
        if m:
            try:
                val = float(m.group(1))
                if val <= 0:
                    return None
                return f"{val:.1f}"
            except Exception:
                pass
        return None

    @staticmethod
    def _extract_ou_markets_from_event(event: dict) -> List[OUMarket]:
        out: Dict[str, OUMarket] = {}
        markets = event.get("markets", []) or []
        for m in markets:
            question = str(m.get("question") or "")
            line = PolymarketDiscoveryClient._parse_line(question)
            if line is None:
                continue
            token_ids_raw = m.get("clobTokenIds", "[]")
            try:
                token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
            except Exception:
                continue
            if not isinstance(token_ids, list) or len(token_ids) < 2:
                continue
            over_token = str(token_ids[0] or "")
            under_token = str(token_ids[1] or "")
            if not over_token or not under_token:
                continue
            is_deploying = bool(m.get("deploying")) or not bool(m.get("active"))
            out[line] = OUMarket(
                market_id=str(m.get("id") or ""),
                question=question,
                line=line,
                over_token_id=over_token,
                under_token_id=under_token,
                deploying=is_deploying,
            )
        return sorted(out.values(), key=lambda x: float(x.line))

    @staticmethod
    def _event_date_ok(event: dict, target_date: datetime.date) -> bool:
        start_date = str(event.get("startDate") or "")
        try:
            dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        except Exception:
            return True
        diff = abs((dt.date() - target_date).days)
        return diff <= 2

    def discover_for_game(
        self,
        game: ScheduledGame,
        date_str: str,
        blocked_slugs: Optional[set[str]] = None,
    ) -> Optional[GameMarketMatch]:
        blocked_slugs = blocked_slugs or set()
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        candidates: List[GameMarketMatch] = []

        # Strategy 1: deterministic slug guesses.
        for slug in self._candidate_slugs(game, date_str):
            events = self._events_by_slug(slug)
            for ev in events:
                event_slug = str(ev.get("slug") or "")
                if not event_slug.startswith("mlb-"):
                    continue
                if event_slug in blocked_slugs:
                    continue
                markets = self._extract_ou_markets_from_event(ev)
                if not markets:
                    continue
                candidates.append(
                    GameMarketMatch(
                        game_pk=game.game_pk,
                        event_slug=event_slug,
                        event_title=str(ev.get("title") or ""),
                        markets=markets,
                    )
                )

        # Strategy 2: query fallback.
        queries = [
            f"{game.away_name} vs {game.home_name}",
            f"{game.away_abbrev} vs {game.home_abbrev}",
            f"{game.away_name} at {game.home_name}",
        ]
        for q in queries:
            events = self._events_by_query(q)
            for ev in events:
                event_slug = str(ev.get("slug") or "")
                if not event_slug.startswith("mlb-"):
                    continue
                if event_slug in blocked_slugs:
                    continue
                if not self._event_date_ok(ev, target_date):
                    continue
                markets = self._extract_ou_markets_from_event(ev)
                if not markets:
                    continue
                candidates.append(
                    GameMarketMatch(
                        game_pk=game.game_pk,
                        event_slug=event_slug,
                        event_title=str(ev.get("title") or ""),
                        markets=markets,
                    )
                )
        if not candidates:
            return None
        return candidates[0]
