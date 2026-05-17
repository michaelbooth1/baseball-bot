"""Domain dataclasses for the MLB Polymarket monitor.

These are the canonical types crossed by the monitor base class, the trading
engines (`scripts/trading`), and the test suite. Field shapes are part of the
public contract — bump tests in lockstep when changing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from monitor_constants import FINAL_STATES, LIVE_STATES, PREVIEW_STATES
from monitor_utils import _now_iso


@dataclass
class ScheduleScore:
    away: Optional[int]
    home: Optional[int]
    inning: Optional[int]
    inning_state: Optional[str]
    outs: Optional[int]
    balls: Optional[int]
    strikes: Optional[int]
    runners_on: int  # bitmask: 1=1st, 2=2nd, 4=3rd (0 = bases empty / unknown)
    away_inning_runs: List[int] = field(default_factory=list)
    home_inning_runs: List[int] = field(default_factory=list)


@dataclass
class ScheduledGame:
    game_pk: int
    game_date: str
    start_time_utc: str
    away_abbrev: str
    home_abbrev: str
    away_name: str
    home_name: str
    status_abstract: str
    status_detailed: str
    score: ScheduleScore

    # Venue identity is still carried by the monitor; Weather v2 owns live
    # temp/wind context through cache/weather/game_weather_<date>.json.
    venue_name: str = ""

    # Stage-4 pitcher quality context (populated from linescore.defense.pitcher).
    # Defaults to MLB_AVG_ERA (4.20) when pitcher cache is not loaded or pitcher
    # has no qualifying stats. Gate 8i only fires when ERA is below threshold,
    # so the default disables the gate when data is unavailable.
    current_pitcher_id: Optional[int] = None
    current_pitcher_name: str = ""
    current_pitcher_era: float = 4.20

    def is_live(self) -> bool:
        return self.status_abstract.lower() in LIVE_STATES

    def is_preview(self) -> bool:
        return self.status_abstract.lower() in PREVIEW_STATES

    def is_final(self) -> bool:
        if not (self.status_abstract.lower() == "final" or self.status_detailed.lower() in FINAL_STATES):
            return False
        # Guard against transient MLB API "Final" glitches that fire in early innings.
        # A real final game must have at least 8 innings on the board.
        inning = self.score.inning
        if inning is not None and inning < 8:
            return False
        return True


@dataclass
class OUMarket:
    market_id: str
    question: str
    line: str
    over_token_id: str
    under_token_id: str
    deploying: bool = False   # True = Polygon contract not yet deployed; book fetch will fail


@dataclass
class GameMarketMatch:
    game_pk: int
    event_slug: str
    event_title: str
    markets: List[OUMarket]
    discovered_at: str = field(default_factory=_now_iso)
