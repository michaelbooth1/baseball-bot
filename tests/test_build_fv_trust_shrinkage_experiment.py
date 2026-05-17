import json
import tempfile
import unittest
from pathlib import Path

from scripts.analysis import build_fv_trust_shrinkage_experiment as shrink


def _row(**overrides):
    base = {
        "mode": "live",
        "session_date": "2026-05-13",
        "candidate_id": "cand_1",
        "game_pk": 101,
        "line": "8.5",
        "signal_model_family": "score_event_transition",
        "decision_ask": 0.70,
        "decision_market_mid_no_vig": 0.64,
        "fair_value": 0.90,
        "target_over_win": 1,
        "target_taker_profit_units": 1.0 / 0.70 - 1.0,
        "inferred_state_effective_n_proxy": 100,
        "inferred_state_stage1_trust_weight": 0.713495,
        "current_state_value_effective_n_proxy": 25,
        "current_state_value_stage1_trust_weight": 0.268384,
    }
    base.update(overrides)
    return base


class FvTrustShrinkageExperimentTests(unittest.TestCase):
    def test_support_weight_controls_distance_from_market_anchor(self):
        row = _row(fair_value=0.92, decision_ask=0.70)
        low = _row(fair_value=0.92, decision_ask=0.70, inferred_state_effective_n_proxy=5)
        high = _row(fair_value=0.92, decision_ask=0.70, inferred_state_effective_n_proxy=250)

        low_probs = shrink.build_variant_probabilities(low, taus=[80])
        high_probs = shrink.build_variant_probabilities(high, taus=[80])
        raw_gap = abs(row["fair_value"] - row["decision_ask"])
        low_gap = abs(low_probs["shrink_primary_support_tau80_to_ask"] - row["decision_ask"])
        high_gap = abs(high_probs["shrink_primary_support_tau80_to_ask"] - row["decision_ask"])

        self.assertLess(low_gap, high_gap)
        self.assertLess(high_gap, raw_gap)

    def test_family_primary_support_uses_current_state_for_no_score_drift(self):
        row = _row(
            signal_model_family="no_score_drift",
            inferred_state_effective_n_proxy=None,
            current_state_value_effective_n_proxy=160,
        )

        self.assertEqual(shrink._support_primary(row), 160)
        probs = shrink.build_variant_probabilities(row, taus=[80])
        self.assertIn("shrink_primary_support_tau80_to_mid_no_vig_or_ask", probs)

    def test_main_writes_report_and_predictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "rows.jsonl"
            rows = [
                _row(candidate_id="score_1", target_over_win=1, session_date="2026-05-11"),
                _row(candidate_id="score_2", target_over_win=0, session_date="2026-05-12"),
                _row(
                    candidate_id="drift_1",
                    signal_model_family="no_score_drift",
                    target_over_win=1,
                    session_date="2026-05-13",
                    inferred_state_effective_n_proxy=None,
                    current_state_value_effective_n_proxy=90,
                ),
            ]
            input_path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )

            shrink.main(
                [
                    "--input-path",
                    str(input_path),
                    "--output-root",
                    str(root / "out"),
                    "--mode",
                    "live",
                    "--min-family-rows",
                    "1",
                    "--taus",
                    "80",
                ]
            )

            report_path = root / "out" / "fv_trust_shrinkage_report.json"
            predictions_path = root / "out" / "fv_trust_shrinkage_predictions.jsonl"
            self.assertTrue(report_path.exists())
            self.assertTrue(predictions_path.exists())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertIn("score_event_transition", report["by_family"])
            self.assertIn("no_score_drift", report["by_family"])
            self.assertGreaterEqual(
                report["by_family"]["score_event_transition"]["support_coverage"]["primary_support_rows"],
                2,
            )


if __name__ == "__main__":
    unittest.main()
