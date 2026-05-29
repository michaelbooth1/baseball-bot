"""Tests for scripts/analysis/build_feed_enrichment.py (Tier-3 offline enrichment)."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_feed_enrichment as fe  # noqa: E402


class HelperTests(unittest.TestCase):
    def test_parse_iso_handles_z(self):
        dt = fe.parse_iso("2026-05-27T20:27:11.884Z")
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt.year, 2026)
        self.assertIsNone(fe.parse_iso(""))
        self.assertIsNone(fe.parse_iso("garbage"))

    def test_half_and_defending_side(self):
        self.assertEqual(fe.half_from_inning_state("Top"), "top")
        self.assertEqual(fe.half_from_inning_state("Bottom"), "bottom")
        self.assertEqual(fe.half_from_inning_state("Middle"), "")
        self.assertEqual(fe.defending_side("top"), "home")   # away bats, home pitches
        self.assertEqual(fe.defending_side("bottom"), "away")

    def test_platoon_advantage(self):
        self.assertTrue(fe.platoon_batter_advantage("R", "L"))   # LHB vs RHP
        self.assertFalse(fe.platoon_batter_advantage("R", "R"))
        self.assertTrue(fe.platoon_batter_advantage("L", "S"))   # switch always adv
        self.assertIsNone(fe.platoon_batter_advantage("", "R"))


def _pitch_event(t, velo, in_play_exit=None):
    ev = {"isPitch": True, "startTime": t, "pitchData": {"startSpeed": velo}}
    if in_play_exit is not None:
        ev["hitData"] = {"launchSpeed": in_play_exit}
    return ev


def _synth_feed():
    """Bottom-1: away pitcher P1 (RHP) faces 2 batters (5 pitches). Then a new
    away pitcher P2 enters for batter 3."""
    return {
        "liveData": {
            "plays": {"allPlays": [
                {  # PA 0
                    "atBatIndex": 0,
                    "about": {"inning": 1, "halfInning": "bottom",
                              "startTime": "2026-05-27T20:00:00Z"},
                    "playEndTime": "2026-05-27T20:02:00Z",
                    "matchup": {"pitcher": {"id": 111, "fullName": "Pitcher One"},
                                "pitchHand": {"code": "R"}, "batSide": {"code": "L"}},
                    "playEvents": [
                        _pitch_event("2026-05-27T20:00:10Z", 95.0),
                        _pitch_event("2026-05-27T20:00:40Z", 94.5),
                        _pitch_event("2026-05-27T20:01:30Z", 94.0, in_play_exit=101.0),
                    ],
                },
                {  # PA 1 (same pitcher)
                    "atBatIndex": 1,
                    "about": {"inning": 1, "halfInning": "bottom",
                              "startTime": "2026-05-27T20:02:30Z"},
                    "playEndTime": "2026-05-27T20:04:00Z",
                    "matchup": {"pitcher": {"id": 111, "fullName": "Pitcher One"},
                                "pitchHand": {"code": "R"}, "batSide": {"code": "R"}},
                    "playEvents": [
                        _pitch_event("2026-05-27T20:03:00Z", 93.0),
                        _pitch_event("2026-05-27T20:03:30Z", 92.5),
                    ],
                },
                {  # PA 2 (reliever P2 enters)
                    "atBatIndex": 2,
                    "about": {"inning": 1, "halfInning": "bottom",
                              "startTime": "2026-05-27T20:05:00Z"},
                    "playEndTime": "2026-05-27T20:07:00Z",
                    "matchup": {"pitcher": {"id": 222, "fullName": "Pitcher Two"},
                                "pitchHand": {"code": "L"}, "batSide": {"code": "R"}},
                    "playEvents": [_pitch_event("2026-05-27T20:05:20Z", 88.0)],
                },
            ]},
            "boxscore": {"teams": {
                "away": {"players": {"ID900": {
                    "person": {"id": 900, "fullName": "Catcher A"},
                    "position": {"abbreviation": "C"}, "battingOrder": "300",
                }}},
                "home": {"players": {}},
            }},
        }
    }


class EnrichTests(unittest.TestCase):
    def setUp(self):
        self.tl = fe.build_timeline(_synth_feed())

    def test_timeline_built(self):
        self.assertEqual(len(self.tl.pas), 3)
        self.assertEqual(len(self.tl.pitches), 6)
        self.assertEqual(self.tl.starter_by_side["away"], 111)  # away pitches in bottom
        self.assertEqual(self.tl.catcher_by_side["away"], (900, "Catcher A"))

    def test_enrich_mid_first_pitcher(self):
        # ts during PA 1, after 4 pitches by P1 (3 in PA0 + 1 in PA1 at 20:03:00)
        cand = {"ts": "2026-05-27T20:03:10Z", "inning": 1, "inning_state": "Bottom",
                "current_pitcher_id": 111}
        out = fe.enrich_candidate(self.tl, cand)
        self.assertEqual(out["feed_match_quality"], "in_play_window")
        self.assertEqual(out["feed_pitcher_id"], 111)
        self.assertEqual(out["feed_pitcher_name"], "Pitcher One")
        self.assertEqual(out["pitch_count"], 4)
        self.assertEqual(out["batters_faced"], 2)        # PA0 + PA1
        self.assertEqual(out["times_through_order"], 1)
        self.assertTrue(out["pitcher_is_starter"])
        self.assertEqual(out["defending_pitchers_used"], 1)
        self.assertEqual(out["relievers_used"], 0)
        self.assertEqual(out["p_throws"], "R")
        self.assertEqual(out["b_bats"], "R")
        self.assertFalse(out["platoon_batter_advantage"])
        self.assertEqual(out["catcher_name"], "Catcher A")
        self.assertTrue(out["feed_state_agrees"])
        self.assertTrue(out["feed_pitcher_matches_candidate"])
        self.assertAlmostEqual(out["last_pitch_velo"], 93.0)
        self.assertAlmostEqual(out["last_exit_velo"], 101.0)  # the in-play from PA0

    def test_enrich_after_reliever(self):
        cand = {"ts": "2026-05-27T20:05:30Z", "inning": 1, "inning_state": "Bottom"}
        out = fe.enrich_candidate(self.tl, cand)
        self.assertEqual(out["feed_pitcher_id"], 222)
        self.assertEqual(out["pitch_count"], 1)
        self.assertEqual(out["batters_faced"], 1)
        self.assertFalse(out["pitcher_is_starter"])
        self.assertEqual(out["defending_pitchers_used"], 2)
        self.assertEqual(out["relievers_used"], 1)
        self.assertEqual(out["p_throws"], "L")

    def test_enrich_pregame(self):
        out = fe.enrich_candidate(self.tl, {"ts": "2026-05-27T19:00:00Z"})
        self.assertEqual(out["feed_match_quality"], "pregame")
        self.assertIsNone(out["pitch_count"])

    def test_enrich_no_ts(self):
        out = fe.enrich_candidate(self.tl, {"ts": ""})
        self.assertEqual(out["feed_match_quality"], "no_ts")


class ModelBearingFilterTests(unittest.TestCase):
    def test_filter(self):
        self.assertTrue(fe._is_model_bearing({"base_fair_value": 0.7}))
        self.assertTrue(fe._is_model_bearing({"fair_value_raw": 0.7}))
        self.assertTrue(fe._is_model_bearing({"decision": "trade"}))
        self.assertFalse(fe._is_model_bearing({"decision": "skip", "decision_reason": "gate_min_inning"}))


class EndToEndTests(unittest.TestCase):
    def test_main_writes_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # game feed at games-root/.../<game_pk>.json
            games = tmp / "games"
            games.mkdir()
            with open(games / "424242.json", "w", encoding="utf-8") as f:
                json.dump(_synth_feed(), f)
            # candidate universe
            cu = tmp / "cu"
            cu.mkdir()
            cands = [
                {"candidate_id": "c1", "game_pk": 424242, "line": "7.5",
                 "ts": "2026-05-27T20:03:10Z", "inning": 1, "inning_state": "Bottom",
                 "current_pitcher_id": 111, "base_fair_value": 0.7, "decision": "trade",
                 "session_date": "2026-05-27", "mode": "live"},
                # not model-bearing -> excluded
                {"candidate_id": "c2", "game_pk": 424242, "line": "7.5",
                 "ts": "2026-05-27T20:03:10Z", "decision": "skip",
                 "decision_reason": "gate_min_inning"},
                # model-bearing but game feed missing -> feed_found False
                {"candidate_id": "c3", "game_pk": 999999, "line": "8.5",
                 "ts": "2026-05-27T20:03:10Z", "decision": "trade"},
            ]
            with open(cu / "d_candidates.jsonl", "w", encoding="utf-8") as f:
                for c in cands:
                    f.write(json.dumps(c) + "\n")
            out = tmp / "out"
            rc = fe.main([
                "--roots", str(cu), "--games-root", str(games),
                "--output-root", str(out), "--output-stem", "fe",
            ])
            self.assertEqual(rc, 0)
            rows = [json.loads(x) for x in (out / "fe.jsonl").read_text(encoding="utf-8").splitlines()]
            by_id = {r["candidate_id"]: r for r in rows}
            self.assertIn("c1", by_id)
            self.assertNotIn("c2", by_id)   # filtered (not model-bearing)
            self.assertEqual(by_id["c1"]["pitch_count"], 4)
            self.assertFalse(by_id["c3"]["feed_found"])  # missing feed, fail-soft
            self.assertTrue((out / "fe.csv").exists())
            summary = json.loads((out / "fe_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["summary"]["n_candidates"], 2)
            self.assertEqual(summary["summary"]["games_without_feed"], 1)


if __name__ == "__main__":
    unittest.main()
