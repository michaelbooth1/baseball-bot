import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import evaluate_no_score_drift_policy as nsd_policy  # noqa: E402


class NoScoreDriftPolicyTests(unittest.TestCase):
    def test_dedups_to_first_candidate_per_game_line_score_segment(self):
        outcomes = {
            ("2026-04-30", "live", 101, "7.5"): {"over_hit": True, "final_total": 9},
        }
        candidates = [
            {
                "session_date": "2026-04-30",
                "mode": "live",
                "candidate_id": "first",
                "ts": "2026-04-30T17:00:00Z",
                "game_pk": 101,
                "line": "7.5",
                "decision": "shadow_no_score_drift",
                "away_score_before": 2,
                "home_score_before": 0,
                "inning": 9,
                "inning_state": "Top",
                "score_segment_key": "2-0",
                "decision_ask": 0.60,
                "current_state_value_edge": 0.14,
                "current_state_value_empirical_edge": 0.10,
                "shadow_no_score_drift_trigger": "both",
                "stadium_weather_exposure": "open_air",
                "weather_air_density_index": 0.98,
                "weather_wind_out_component_mph": 7.5,
            },
            {
                "session_date": "2026-04-30",
                "mode": "live",
                "candidate_id": "later_better_price",
                "ts": "2026-04-30T17:02:00Z",
                "game_pk": 101,
                "line": "7.5",
                "decision": "shadow_no_score_drift",
                "away_score_before": 2,
                "home_score_before": 0,
                "score_segment_key": "2-0",
                "decision_ask": 0.54,
                "current_state_value_edge": 0.18,
                "current_state_value_empirical_edge": 0.12,
                "shadow_no_score_drift_trigger": "both",
            },
        ]

        rows, counts = nsd_policy.build_policy_rows(candidates, outcomes)

        self.assertEqual(counts["raw_shadow_no_score_drift_rows"], 2)
        self.assertEqual(counts["policy_rows"], 1)
        self.assertEqual(counts["duplicate_rows_collapsed"], 1)
        self.assertEqual(rows[0]["candidate_id"], "first")
        self.assertEqual(rows[0]["duplicate_candidate_rows"], 2)
        self.assertEqual(rows[0]["expected_remaining_half_innings"], 2.0)
        self.assertEqual(rows[0]["home_skip_bottom9_risk"], 0.0)
        self.assertEqual(rows[0]["stadium_weather_exposure"], "open_air")
        self.assertAlmostEqual(rows[0]["weather_air_density_index"], 0.98)
        self.assertAlmostEqual(rows[0]["weather_wind_out_component_mph"], 7.5)
        self.assertEqual(rows[0]["support_regime"], "poisson_and_empirical_support")
        self.assertEqual(rows[0]["shadow_no_score_poisson_edge_bucket"], "0.10-0.15")
        self.assertEqual(rows[0]["shadow_no_score_empirical_edge_bucket"], "0.10-0.15")
        self.assertEqual(rows[0]["shadow_no_score_ask_bucket"], "0.55-0.70")
        self.assertEqual(rows[0]["shadow_no_score_drawdown_bucket"], "missing")
        self.assertEqual(rows[0]["shadow_score_event_current_edge_strong_ask_lt_085"], True)
        self.assertAlmostEqual(rows[0]["taker_profit_units"], 0.666667, places=6)

    def test_support_regimes_are_separated_in_summary(self):
        outcomes = {
            ("2026-04-30", "live", 101, "7.5"): {"over_hit": True, "final_total": 9},
            ("2026-04-30", "live", 102, "8.5"): {"over_hit": False, "final_total": 7},
            ("2026-04-30", "live", 103, "9.5"): {"over_hit": True, "final_total": 12},
        }
        candidates = [
            {
                "session_date": "2026-04-30",
                "mode": "live",
                "candidate_id": "both",
                "ts": "2026-04-30T17:00:00Z",
                "game_pk": 101,
                "line": "7.5",
                "decision": "shadow_no_score_drift",
                "score_segment_key": "2-0",
                "decision_ask": 0.50,
                "current_state_value_edge": 0.16,
                "current_state_value_empirical_edge": 0.09,
                "shadow_no_score_drift_trigger": "both",
            },
            {
                "session_date": "2026-04-30",
                "mode": "live",
                "candidate_id": "poisson_only",
                "ts": "2026-04-30T17:05:00Z",
                "game_pk": 102,
                "line": "8.5",
                "decision_reason": "state_value_no_score_drift",
                "score_segment_key": "1-1",
                "decision_ask": 0.64,
                "current_state_value_edge": 0.13,
                "current_state_value_empirical_edge": 0.03,
                "shadow_no_score_drift_trigger": "po_edge",
            },
            {
                "session_date": "2026-04-30",
                "mode": "live",
                "candidate_id": "empirical_only",
                "ts": "2026-04-30T17:10:00Z",
                "game_pk": 103,
                "line": "9.5",
                "state_value_strategy": "no_score_drift",
                "score_segment_key": "0-0",
                "decision_ask": 0.40,
                "current_state_value_edge": 0.05,
                "current_state_value_empirical_edge": 0.11,
                "shadow_no_score_drift_trigger": "empirical_edge",
            },
        ]

        rows, counts = nsd_policy.build_policy_rows(candidates, outcomes)
        report = nsd_policy.build_report(
            rows,
            counts,
            mode="live",
            min_date="2026-04-30",
            max_date="2026-04-30",
            min_poisson_edge=0.10,
            min_empirical_edge=0.08,
        )

        by_regime = report["by_support_regime"]
        self.assertEqual(by_regime["poisson_and_empirical_support"]["rows"], 1)
        self.assertEqual(by_regime["poisson_and_empirical_support"]["win_rate"], 1.0)
        self.assertEqual(by_regime["poisson_only_support"]["rows"], 1)
        self.assertEqual(by_regime["poisson_only_support"]["win_rate"], 0.0)
        self.assertEqual(by_regime["empirical_only_support"]["rows"], 1)
        self.assertEqual(by_regime["empirical_only_support"]["win_rate"], 1.0)
        self.assertEqual(report["overall"]["rows"], 3)
        self.assertEqual(report["overall"]["labeled_rows"], 3)
        self.assertIn("by_poisson_empirical_ask_drawdown", report)
        self.assertIn("by_poisson_edge_bucket", report)


if __name__ == "__main__":
    unittest.main()
