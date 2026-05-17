import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
MONITOR_DIR = PROJECT_DIR / "scripts" / "monitor"
for path in (PROJECT_DIR, MONITOR_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scripts.trading.scoring_path_features import compute_scoring_path_fields  # noqa: E402
from monitor_stats_client import MLBStatsClient  # noqa: E402


class ScoringPathFeatureTests(unittest.TestCase):
    def test_steady_2_2_after_four_has_high_scoring_rate_and_low_burst(self):
        fields = compute_scoring_path_fields(
            away_inning_runs=[1, 0, 1, 0],
            home_inning_runs=[0, 1, 0, 1],
            current_inning=4,
        )

        self.assertTrue(fields["scoring_path_available"])
        self.assertEqual(fields["scoring_path_inning_runs"], "1-1-1-1")
        self.assertEqual(fields["scoring_path_innings_observed"], 4)
        self.assertEqual(fields["scoring_path_runs_observed"], 4)
        self.assertAlmostEqual(fields["scoring_inning_rate"], 1.0)
        self.assertAlmostEqual(fields["scoring_half_rate"], 0.5)
        self.assertAlmostEqual(fields["burst_share"], 0.25)
        self.assertEqual(fields["scoreless_streak"], 0)
        self.assertAlmostEqual(fields["recent2_run_share"], 0.5)
        self.assertAlmostEqual(fields["weighted_run_inning_norm"], 0.625)
        self.assertAlmostEqual(fields["inning_run_slope"], 0.0)

    def test_burst_2_2_after_four_has_low_scoring_rate_and_long_streak(self):
        fields = compute_scoring_path_fields(
            away_inning_runs=[2, 0, 0, 0],
            home_inning_runs=[2, 0, 0, 0],
            current_inning=4,
        )

        self.assertEqual(fields["scoring_path_inning_runs"], "4-0-0-0")
        self.assertAlmostEqual(fields["scoring_inning_rate"], 0.25)
        self.assertAlmostEqual(fields["scoring_half_rate"], 0.25)
        self.assertAlmostEqual(fields["burst_share"], 1.0)
        self.assertEqual(fields["scoreless_streak"], 3)
        self.assertAlmostEqual(fields["recent2_run_share"], 0.0)
        self.assertAlmostEqual(fields["weighted_run_inning_norm"], 0.25)
        self.assertLess(fields["inning_run_slope"], 0.0)

    def test_missing_path_returns_null_feature_family(self):
        fields = compute_scoring_path_fields(away_inning_runs=[], home_inning_runs=[], current_inning=4)

        self.assertTrue(all(value is None for value in fields.values()))

    def test_monitor_parser_carries_linescore_inning_runs(self):
        client = MLBStatsClient.__new__(MLBStatsClient)
        client._pitcher_cache = {}
        payload = {
            "dates": [
                {
                    "games": [
                        {
                            "gamePk": 123,
                            "gameDate": "2026-05-16T17:00:00Z",
                            "status": {
                                "abstractGameState": "Live",
                                "detailedState": "In Progress",
                            },
                            "teams": {
                                "away": {"score": 2, "team": {"abbreviation": "AWY", "name": "Away"}},
                                "home": {"score": 2, "team": {"abbreviation": "HOM", "name": "Home"}},
                            },
                            "venue": {"name": "Test Park"},
                            "linescore": {
                                "currentInning": 4,
                                "inningState": "End",
                                "outs": 3,
                                "balls": 0,
                                "strikes": 0,
                                "innings": [
                                    {"num": 1, "away": {"runs": 1}, "home": {"runs": 0}},
                                    {"num": 2, "away": {"runs": 0}, "home": {"runs": 1}},
                                    {"num": 3, "away": {"runs": 1}, "home": {"runs": 0}},
                                    {"num": 4, "away": {"runs": 0}, "home": {"runs": 1}},
                                ],
                            },
                        }
                    ]
                }
            ]
        }

        games = client.parse_games(payload)

        self.assertEqual(games[123].score.away_inning_runs, [1, 0, 1, 0])
        self.assertEqual(games[123].score.home_inning_runs, [0, 1, 0, 1])


if __name__ == "__main__":
    unittest.main()
