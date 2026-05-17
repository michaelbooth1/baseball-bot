import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_state_value_transition_report as sv_report  # noqa: E402


class StateValueTransitionReportTests(unittest.TestCase):
    def test_report_splits_phantom_and_no_score_regimes(self):
        outcomes = {
            ("2026-04-29", "live", 1, "6.5"): {"over_hit": False, "final_total": 5},
            ("2026-04-29", "live", 2, "7.5"): {"over_hit": True, "final_total": 16},
            ("2026-04-29", "live", 3, "8.5"): {"over_hit": True, "final_total": 9},
        }
        rows = [
            {
                "session_date": "2026-04-29",
                "mode": "live",
                "candidate_id": "score_bad",
                "game_pk": 1,
                "line": "6.5",
                "decision": "trade",
                "state_value_strategy": "score_event_transition",
                "decision_ask": 0.57,
                "edge": 0.21,
                "current_state_value_edge": -0.12,
                "shadow_phantom_risk_band": "high",
                "shadow_phantom_risk_score": 0.84,
                "shadow_fv_inferred_lift": 0.33,
            },
            {
                "session_date": "2026-04-29",
                "mode": "live",
                "candidate_id": "score_good",
                "game_pk": 2,
                "line": "7.5",
                "decision": "trade",
                "state_value_strategy": "score_event_transition",
                "decision_ask": 0.70,
                "edge": 0.21,
                "current_state_value_edge": 0.10,
                "shadow_phantom_risk_band": "low",
                "shadow_phantom_risk_score": 0.32,
                "shadow_fv_inferred_lift": 0.11,
            },
            {
                "session_date": "2026-04-29",
                "mode": "live",
                "candidate_id": "drift_emp",
                "game_pk": 3,
                "line": "8.5",
                "decision": "shadow_no_score_drift",
                "decision_ask": 0.44,
                "current_state_value_edge": -0.06,
                "current_state_value_empirical_edge": 0.11,
                "shadow_no_score_drift_trigger": "empirical_edge",
            },
        ]

        report = sv_report.build_report(rows, outcomes, top_n=10)

        self.assertEqual(report["counts"]["score_event_transition_rows"], 2)
        self.assertEqual(report["counts"]["shadow_no_score_drift_rows"], 1)
        bad_regime = report["score_event_transition"]["by_regime"][
            "high_phantom_negative_current_edge"
        ]
        self.assertEqual(bad_regime["rows"], 1)
        self.assertEqual(bad_regime["win_rate"], 0.0)
        good_regime = report["score_event_transition"]["by_regime"][
            "low_medium_phantom_positive_current_edge"
        ]
        self.assertEqual(good_regime["win_rate"], 1.0)
        drift_regime = report["no_score_drift"]["by_regime"]["empirical_support"]
        self.assertEqual(drift_regime["rows"], 1)
        self.assertEqual(drift_regime["win_rate"], 1.0)
        ranked = report["no_score_drift"]["ranked_empirical_support_rows"]
        self.assertEqual(ranked[0]["candidate_id"], "drift_emp")


if __name__ == "__main__":
    unittest.main()
