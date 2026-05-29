"""Tests for the Tier-2 per-game metadata cache (home-plate umpire + officials)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_DIR = Path(__file__).resolve().parents[1]
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
if str(TRADING_DIR) not in sys.path:
    sys.path.insert(0, str(TRADING_DIR))

import game_meta_client as gmc  # noqa: E402


def _boxscore(officials):
    return {"officials": officials}


_HP = {"official": {"id": 4321, "fullName": "Angel Hernandez"}, "officialType": "Home Plate"}
_1B = {"official": {"id": 5, "fullName": "Joe West"}, "officialType": "First Base"}


class ExtractionTests(unittest.TestCase):
    def test_extract_officials(self):
        offs = gmc.extract_officials(_boxscore([_HP, _1B]))
        self.assertEqual(len(offs), 2)
        self.assertEqual(offs[0], {"type": "Home Plate", "id": 4321, "name": "Angel Hernandez"})

    def test_extract_hp_umpire(self):
        self.assertEqual(gmc.extract_hp_umpire(_boxscore([_1B, _HP])), (4321, "Angel Hernandez"))

    def test_extract_hp_umpire_absent(self):
        self.assertEqual(gmc.extract_hp_umpire(_boxscore([_1B])), (None, ""))

    def test_extract_handles_empty(self):
        self.assertEqual(gmc.extract_officials({}), [])
        self.assertEqual(gmc.extract_hp_umpire({}), (None, ""))

    def test_flatten(self):
        flat = gmc.flatten_game_meta_cache_game(
            {"hp_umpire_id": 4321, "hp_umpire_name": "Angel Hernandez", "officials": [_HP]}
        )
        self.assertTrue(flat["game_meta_available"])
        self.assertEqual(flat["hp_umpire_id"], 4321)
        self.assertEqual(flat["hp_umpire_name"], "Angel Hernandez")

    def test_flatten_unavailable(self):
        flat = gmc.flatten_game_meta_cache_game(
            {"hp_umpire_id": None, "hp_umpire_name": "", "officials": []}
        )
        self.assertFalse(flat["game_meta_available"])
        self.assertIsNone(flat["hp_umpire_id"])
        self.assertIsNone(flat["hp_umpire_name"])


class BuildCacheTests(unittest.TestCase):
    def test_build_success(self):
        sched = lambda d, t: [101, 102]
        box = {101: _boxscore([_HP, _1B]), 102: _boxscore([_1B])}  # 102 has no HP yet
        payload = gmc.build_game_meta_cache(
            "2026-05-29",
            schedule_fetcher=sched,
            boxscore_fetcher=lambda pk, t: box[pk],
        )
        self.assertEqual(payload["coverage"]["scheduled_games"], 2)
        self.assertEqual(payload["coverage"]["games_with_umpire"], 1)
        g101 = next(g for g in payload["games"] if g["game_pk"] == 101)
        self.assertEqual(g101["hp_umpire_id"], 4321)
        g102 = next(g for g in payload["games"] if g["game_pk"] == 102)
        self.assertIsNone(g102["hp_umpire_id"])

    def test_per_game_fetch_failure_is_fail_open(self):
        def box(pk, t):
            if pk == 102:
                raise RuntimeError("boom")
            return _boxscore([_HP])
        payload = gmc.build_game_meta_cache(
            "2026-05-29", schedule_fetcher=lambda d, t: [101, 102], boxscore_fetcher=box,
        )
        self.assertEqual(payload["coverage"]["scheduled_games"], 2)
        self.assertEqual(payload["coverage"]["games_with_umpire"], 1)
        g102 = next(g for g in payload["games"] if g["game_pk"] == 102)
        self.assertFalse(g102["fetch_ok"])
        self.assertTrue(any("boxscore_fetch_failed:102" in w for w in payload["warnings"]))

    def test_schedule_failure_is_fail_open(self):
        def sched(d, t):
            raise RuntimeError("net down")
        payload = gmc.build_game_meta_cache("2026-05-29", schedule_fetcher=sched, boxscore_fetcher=lambda pk, t: {})
        self.assertEqual(payload["coverage"]["scheduled_games"], 0)
        self.assertEqual(payload["games"], [])
        self.assertTrue(any("schedule_fetch_failed" in w for w in payload["warnings"]))

    def test_write_and_load_round_trip(self):
        payload = gmc.build_game_meta_cache(
            "2026-05-29",
            schedule_fetcher=lambda d, t: [101],
            boxscore_fetcher=lambda pk, t: _boxscore([_HP]),
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "game_meta_2026-05-29.json"
            gmc.write_game_meta_cache(payload, path)
            self.assertTrue(path.exists())
            by_game = gmc.load_game_meta_features_by_game(path)
            self.assertIn(101, by_game)
            self.assertEqual(by_game[101]["hp_umpire_id"], 4321)
            self.assertTrue(by_game[101]["game_meta_available"])


class EngineJoinTests(unittest.TestCase):
    def test_candidate_row_includes_game_meta_when_loaded(self):
        import signal_pipeline_payload as spp
        engine = SimpleNamespace(
            _next_candidate_id=lambda g, l: "c1",
            _prob_calibration_mode="off",
            _game_meta_cache_loaded=True,
            _game_meta_features_by_game_pk={
                777: {"game_meta_available": True, "hp_umpire_id": 4321,
                      "hp_umpire_name": "Angel Hernandez"},
            },
        )
        # bind the real method for the join
        import signal_engine as se
        engine._game_meta_fields_for_game = lambda pk: se.SignalEngine._game_meta_fields_for_game(engine, pk)
        ctx = SimpleNamespace(
            game=SimpleNamespace(game_pk=777, away_abbrev="NYY", home_abbrev="BOS"),
            market=SimpleNamespace(line="8.5", over_token_id="o", under_token_id="u"),
            inning=6, inning_state="Top", outs=1, runners_on=1,
            away_score=3, home_score=2, current_total=5, best_bid=0.6, ask=0.67,
            book={}, away_inning_runs=(), home_inning_runs=(),
        )
        payload = spp.build_base_candidate_payload(engine, ctx)
        self.assertEqual(payload["hp_umpire_id"], 4321)
        self.assertEqual(payload["hp_umpire_name"], "Angel Hernandez")
        self.assertTrue(payload["game_meta_available"])

    def test_game_meta_fields_unavailable_when_loaded_but_missing(self):
        import signal_engine as se
        engine = SimpleNamespace(
            _game_meta_cache_loaded=True, _game_meta_features_by_game_pk={},
        )
        out = se.SignalEngine._game_meta_fields_for_game(engine, 999)
        self.assertEqual(out, {"game_meta_available": False})

    def test_game_meta_fields_empty_when_not_loaded(self):
        import signal_engine as se
        engine = SimpleNamespace()
        out = se.SignalEngine._game_meta_fields_for_game(engine, 999)
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()
