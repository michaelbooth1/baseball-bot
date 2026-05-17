import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
if str(TRADING_DIR) not in sys.path:
    sys.path.insert(0, str(TRADING_DIR))

from probability_calibration import ProbabilityCalibrator  # noqa: E402
from model_families import infer_signal_model_family  # noqa: E402
from scripts.analysis import calibrate_signal_probabilities as calib  # noqa: E402


class ProbabilityCalibrationFamilyTests(unittest.TestCase):
    def test_historical_unified_bet_rows_default_to_score_event(self) -> None:
        row = {
            "bet_id": "2026-04-24_1_8.5_0001",
            "source_has_session_bet": True,
            "fair_value": 0.82,
        }
        self.assertEqual(infer_signal_model_family(row), "score_event_transition")

    def test_runtime_routes_family_calibrators(self) -> None:
        payload = {
            "schema_version": 2,
            "family_mode": "separate",
            "default_family": "score_event_transition",
            "families": {
                "score_event_transition": {
                    "selected_method": "platt",
                    "methods": {"platt": {"params": {"a": 1.0, "b": 0.0}}},
                },
                "no_score_drift": {
                    "selected_method": "platt",
                    "methods": {"platt": {"params": {"a": 0.0, "b": 0.0}}},
                },
            },
        }
        cal = ProbabilityCalibrator.from_payload(payload)

        self.assertAlmostEqual(cal.calibrate(0.80, model_family="score_event_transition"), 0.80)
        self.assertAlmostEqual(cal.calibrate(0.80, model_family="no_score_drift"), 0.50)
        self.assertEqual(cal.method_for_family("no_score_drift"), "platt")
        self.assertTrue(cal.has_family("no_score_drift"))
        self.assertFalse(cal.has_family("missing_family"))
        self.assertAlmostEqual(
            cal.calibrate(
                0.80,
                model_family="missing_family",
                allow_family_fallback=False,
            ),
            0.80,
        )

    def test_single_family_artifact_does_not_claim_no_score_drift_support(self) -> None:
        payload = {
            "schema_version": 1,
            "selected_method": "platt",
            "methods": {"platt": {"params": {"a": 1.0, "b": 0.0}}},
            "data": {"model_family": "score_event_transition"},
        }
        cal = ProbabilityCalibrator.from_payload(payload)

        self.assertTrue(cal.has_family("score_event_transition"))
        self.assertFalse(cal.has_family("no_score_drift"))
        self.assertEqual(cal.method_for_family("no_score_drift"), "identity")

    def test_calibration_script_outputs_separate_candidate_families(self) -> None:
        rows = [
            {
                "mode": "live",
                "session_date": "2026-05-01",
                "candidate_id": "score-a",
                "decision": "trade",
                "signal_model_family": "score_event_transition",
                "label_available": True,
                "target_counterfactual_win": 1,
                "fair_value_raw": 0.90,
                "decision_ask": 0.70,
            },
            {
                "mode": "live",
                "session_date": "2026-05-02",
                "candidate_id": "score-b",
                "decision": "trade",
                "signal_model_family": "score_event_transition",
                "label_available": True,
                "target_counterfactual_win": 0,
                "fair_value_raw": 0.85,
                "decision_ask": 0.72,
            },
            {
                "mode": "live",
                "session_date": "2026-05-01",
                "candidate_id": "drift-a",
                "decision": "shadow_no_score_drift",
                "signal_model_family": "no_score_drift",
                "label_available": True,
                "target_counterfactual_win": 1,
                "fair_value_raw": 0.68,
                "decision_ask": 0.54,
            },
            {
                "mode": "live",
                "session_date": "2026-05-02",
                "candidate_id": "drift-b",
                "decision": "shadow_no_score_drift",
                "signal_model_family": "no_score_drift",
                "label_available": True,
                "target_counterfactual_win": 1,
                "fair_value_raw": 0.66,
                "decision_ask": 0.52,
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_path = root / "candidates.jsonl"
            with open(input_path, "w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")

            calib.main([
                "--input-path", str(input_path),
                "--input-kind", "candidate_universe",
                "--output-root", str(root / "out"),
                "--family-mode", "separate",
                "--val-frac", "0.0",
                "--test-frac", "0.4",
                "--no-stability-gate",
            ])

            payload = json.loads((root / "out" / "signal_win_calibration.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["family_mode"], "separate")
            self.assertEqual(set(payload["families"].keys()), {"score_event_transition", "no_score_drift"})
            self.assertEqual(payload["data"]["model_family_counts"]["score_event_transition"], 2)
            self.assertEqual(payload["data"]["model_family_counts"]["no_score_drift"], 2)


class IdentityRejectionGuardTests(unittest.TestCase):
    """Verify the train-ECE guard that overrides identity when validation
    is degenerate but raw FV is materially worse-calibrated than a
    challenger on the train split."""

    def _eval(
        self,
        *,
        raw_val_logloss,
        platt_val_logloss,
        iso_val_logloss,
        raw_train_ece,
        platt_train_ece,
        iso_train_ece,
    ):
        return {
            "raw": {
                "validation": {"logloss": raw_val_logloss},
                "train": {"ece_10": raw_train_ece},
            },
            "platt": {
                "validation": {"logloss": platt_val_logloss},
                "train": {"ece_10": platt_train_ece},
            },
            "isotonic": {
                "validation": {"logloss": iso_val_logloss},
                "train": {"ece_10": iso_train_ece},
            },
        }

    def test_guard_overrides_identity_when_train_ece_is_far_worse(self) -> None:
        # Mirrors the score_event_transition failure mode: validation is
        # degenerate so identity wins on logloss, but train ECE shows
        # platt is dramatically better calibrated.
        method_eval = self._eval(
            raw_val_logloss=0.083,
            platt_val_logloss=0.304,
            iso_val_logloss=0.378,
            raw_train_ece=0.268,
            platt_train_ece=0.003,
            iso_train_ece=0.220,
        )
        selected, audit = calib._select_best_method(
            method_eval, identity_rejection_train_ece_delta=0.05
        )
        self.assertEqual(selected, "platt")
        self.assertEqual(audit["primary_winner"], "raw")
        self.assertEqual(audit["challenger"], "platt")
        self.assertTrue(audit["identity_rejection_applied"])
        self.assertGreater(audit["identity_rejection_train_ece_gap"], 0.05)

    def test_guard_does_not_fire_when_train_ece_gap_is_small(self) -> None:
        # Validation picks raw, but train ECE gap < threshold -- keep identity.
        method_eval = self._eval(
            raw_val_logloss=0.083,
            platt_val_logloss=0.304,
            iso_val_logloss=0.378,
            raw_train_ece=0.060,
            platt_train_ece=0.030,
            iso_train_ece=0.045,
        )
        selected, audit = calib._select_best_method(
            method_eval, identity_rejection_train_ece_delta=0.05
        )
        self.assertEqual(selected, "identity")
        self.assertFalse(audit["identity_rejection_applied"])

    def test_guard_does_not_fire_when_validation_already_picks_challenger(self) -> None:
        # Validation correctly prefers isotonic; guard should not override.
        method_eval = self._eval(
            raw_val_logloss=0.773,
            platt_val_logloss=0.642,
            iso_val_logloss=0.507,
            raw_train_ece=0.245,
            platt_train_ece=0.117,
            iso_train_ece=0.079,
        )
        selected, audit = calib._select_best_method(
            method_eval, identity_rejection_train_ece_delta=0.05
        )
        self.assertEqual(selected, "isotonic")
        self.assertFalse(audit["identity_rejection_applied"])
        self.assertEqual(audit["primary_winner"], "isotonic")

    def test_guard_threshold_zero_disables(self) -> None:
        # threshold=0 makes any positive gap qualify. With explicit equality
        # it should not fire (gap == 0 is not > 0); but any tiny gap should.
        method_eval = self._eval(
            raw_val_logloss=0.083,
            platt_val_logloss=0.304,
            iso_val_logloss=0.378,
            raw_train_ece=0.10,
            platt_train_ece=0.10,
            iso_train_ece=0.10,
        )
        selected, audit = calib._select_best_method(
            method_eval, identity_rejection_train_ece_delta=0.0
        )
        # Tied train ECE with threshold 0: gap (0) >= threshold (0) -- guard
        # fires and picks the first challenger encountered.
        self.assertEqual(selected, "platt")
        self.assertTrue(audit["identity_rejection_applied"])

    def test_artifact_records_selection_audit(self) -> None:
        # Build a tiny dataset that triggers the guard so the produced
        # artifact carries the audit record under each family.
        rows = [
            # 6 train rows, mixed labels, raw FV very high (overconfident).
            {"mode": "live", "session_date": d, "candidate_id": f"s-{i}",
             "signal_model_family": "score_event_transition",
             "label_final_available": True, "target_over_win": y,
             "fair_value_raw": 0.95, "decision_ask": 0.70}
            for i, (d, y) in enumerate([
                ("2026-05-01", 1), ("2026-05-01", 0),
                ("2026-05-02", 1), ("2026-05-02", 0),
                ("2026-05-03", 1), ("2026-05-03", 0),
            ])
        ] + [
            # 2 validation rows, both positive (degenerate).
            {"mode": "live", "session_date": "2026-05-04", "candidate_id": "v-1",
             "signal_model_family": "score_event_transition",
             "label_final_available": True, "target_over_win": 1,
             "fair_value_raw": 0.95, "decision_ask": 0.70},
            # 1 test row.
            {"mode": "live", "session_date": "2026-05-05", "candidate_id": "t-1",
             "signal_model_family": "score_event_transition",
             "label_final_available": True, "target_over_win": 1,
             "fair_value_raw": 0.95, "decision_ask": 0.70},
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_path = root / "rows.jsonl"
            with open(input_path, "w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row) + "\n")
            calib.main([
                "--input-path", str(input_path),
                "--input-kind", "auto",
                "--output-root", str(root / "out"),
                "--family-mode", "separate",
                "--no-stability-gate",
            ])
            payload = json.loads((root / "out" / "signal_win_calibration.json").read_text(encoding="utf-8"))
            family = payload["families"]["score_event_transition"]
            audit = family.get("selection_audit") or {}
            self.assertEqual(audit.get("primary_winner"), "raw")
            self.assertIn(audit.get("selected"), {"platt", "isotonic"})
            self.assertTrue(audit.get("identity_rejection_applied"))


if __name__ == "__main__":
    unittest.main()
