"""Player / count / scheduling capture tests.

These fields are already in memory on the ScheduledGame (current pitcher,
balls/strikes, start_time_utc, day_night, both starters, current batter +
on-deck) but were dropped before logging. The shared
`models.signal_context_fields` helper is the single source for both the
candidate row (`build_base_candidate_payload`) and the bet record. Absent
fields resolve to None so the candidate writer's None-stripping keeps rows
clean (no empty-string bloat).

Tier-1 (2026-05-28): current pitcher, count, scheduling.
Tier-2 (2026-05-29): both starters + ERAs, current batter + on-deck.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_DIR = Path(__file__).resolve().parents[1]
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
if str(TRADING_DIR) not in sys.path:
    sys.path.insert(0, str(TRADING_DIR))

from models import BetRecord, signal_context_fields  # noqa: E402
import signal_pipeline_payload as spp  # noqa: E402


def _full_game():
    return SimpleNamespace(
        game_pk=777,
        away_abbrev="NYY",
        home_abbrev="BOS",
        venue_name="Fenway Park",
        start_time_utc="2026-05-29T23:10:00Z",
        day_night="night",
        current_pitcher_id=12345,
        current_pitcher_name="Gerrit Cole",
        current_pitcher_era=2.85,
        away_starter_id=12345,
        away_starter_name="Gerrit Cole",
        away_starter_era=2.85,
        home_starter_id=54321,
        home_starter_name="Brayan Bello",
        home_starter_era=4.10,
        batter_id=99,
        batter_name="Aaron Judge",
        on_deck_id=100,
        on_deck_name="Juan Soto",
        score=SimpleNamespace(balls=2, strikes=1),
    )


class SignalContextFieldsTests(unittest.TestCase):
    def test_full_game_extracts_all_fields(self):
        f = signal_context_fields(_full_game())
        self.assertEqual(f["balls"], 2)
        self.assertEqual(f["strikes"], 1)
        self.assertEqual(f["current_pitcher_id"], 12345)
        self.assertEqual(f["current_pitcher_name"], "Gerrit Cole")
        self.assertEqual(f["current_pitcher_era"], 2.85)
        self.assertEqual(f["start_time_utc"], "2026-05-29T23:10:00Z")
        self.assertEqual(f["day_night"], "night")
        # Tier-2
        self.assertEqual(f["away_starter_id"], 12345)
        self.assertEqual(f["away_starter_name"], "Gerrit Cole")
        self.assertEqual(f["away_starter_era"], 2.85)
        self.assertEqual(f["home_starter_id"], 54321)
        self.assertEqual(f["home_starter_name"], "Brayan Bello")
        self.assertEqual(f["home_starter_era"], 4.10)
        self.assertEqual(f["batter_id"], 99)
        self.assertEqual(f["batter_name"], "Aaron Judge")
        self.assertEqual(f["on_deck_name"], "Juan Soto")

    def test_bare_stub_returns_none_for_absent(self):
        f = signal_context_fields(SimpleNamespace(game_pk=1))
        for key in (
            "balls", "strikes", "current_pitcher_id", "current_pitcher_name",
            "current_pitcher_era", "start_time_utc", "day_night",
            "away_starter_id", "away_starter_name", "away_starter_era",
            "home_starter_id", "home_starter_name", "home_starter_era",
            "batter_id", "batter_name", "on_deck_id", "on_deck_name",
        ):
            self.assertIn(key, f)        # key always present for safe access
            self.assertIsNone(f[key])    # but None when absent (writer strips)

    def test_missing_score_object(self):
        g = SimpleNamespace(game_pk=1, current_pitcher_id=9)  # no .score
        f = signal_context_fields(g)
        self.assertIsNone(f["balls"])
        self.assertIsNone(f["strikes"])
        self.assertEqual(f["current_pitcher_id"], 9)

    def test_bet_record_carries_context(self):
        bet = BetRecord(
            bet_id="b1", placed_at="t", game_pk=777, away_abbrev="NYY",
            home_abbrev="BOS", line="8.5", side="over", entry_ask=0.6,
            fair_value=0.7, base_fair_value=0.65, stage2_run_env_delta=0.0,
            team_offense_delta=0.0, edge=0.1, inferred_runs=1, inning=6,
            inning_state="Top", outs=1, away_score_before=3, home_score_before=2,
            inferred_away_after=4, inferred_home_after=2, stake=10.0, runners_on=1,
            **signal_context_fields(_full_game()),
        )
        self.assertEqual(bet.current_pitcher_id, 12345)
        self.assertEqual(bet.current_pitcher_era, 2.85)
        self.assertEqual(bet.balls, 2)
        self.assertEqual(bet.day_night, "night")
        self.assertEqual(bet.home_starter_name, "Brayan Bello")
        self.assertEqual(bet.home_starter_era, 4.10)
        self.assertEqual(bet.batter_name, "Aaron Judge")


class CandidateRowCaptureTests(unittest.TestCase):
    def _engine(self):
        return SimpleNamespace(
            _next_candidate_id=lambda game_pk, line: f"{game_pk}_{line}_0001",
            _prob_calibration_mode="off",
        )

    def _ctx(self, game):
        return SimpleNamespace(
            game=game,
            market=SimpleNamespace(line="8.5", over_token_id="ot", under_token_id="ut"),
            inning=6, inning_state="Top", outs=1, runners_on=1,
            away_score=3, home_score=2, current_total=5,
            best_bid=0.60, ask=0.67,
            book={"under_best_bid": None, "under_best_ask": None},
            away_inning_runs=(), home_inning_runs=(),
        )

    def test_candidate_row_captures_context_when_present(self):
        payload = spp.build_base_candidate_payload(self._engine(), self._ctx(_full_game()))
        self.assertEqual(payload["balls"], 2)
        self.assertEqual(payload["current_pitcher_id"], 12345)
        self.assertEqual(payload["current_pitcher_name"], "Gerrit Cole")
        self.assertEqual(payload["day_night"], "night")
        self.assertEqual(payload["away_starter_name"], "Gerrit Cole")
        self.assertEqual(payload["home_starter_era"], 4.10)
        self.assertEqual(payload["batter_name"], "Aaron Judge")
        self.assertEqual(payload["on_deck_name"], "Juan Soto")

    def test_candidate_row_none_when_absent(self):
        game = SimpleNamespace(game_pk=777, away_abbrev="NYY", home_abbrev="BOS")
        payload = spp.build_base_candidate_payload(self._engine(), self._ctx(game))
        # Keys present (None) in the in-memory payload; the writer strips Nones.
        self.assertIsNone(payload["balls"])
        self.assertIsNone(payload["current_pitcher_id"])
        self.assertIsNone(payload["current_pitcher_name"])
        self.assertIsNone(payload["day_night"])
        self.assertIsNone(payload["away_starter_name"])
        self.assertIsNone(payload["batter_name"])


if __name__ == "__main__":
    unittest.main()
