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


class ProbabilityCalibrationPerLineTests(unittest.TestCase):
    """Per-line calibrator stratification (2026-06-06). Validates that
    when a (family, line) curve is present in the artifact, the runtime
    uses it; otherwise falls back to the family-pooled curve. Strictly
    additive change -- artifacts without a ``lines`` block must behave
    identically to pre-change runtime."""

    def _payload_with_per_line(self):
        """Family-pooled curve is identity (a=1,b=0). Per-line curve
        for line '5.5' is a strong downshift (a=1,b=-2), pulling
        probability ~0.5 below the family-pooled level. Other lines
        fall back to the pooled identity curve."""
        return {
            "schema_version": 2,
            "family_mode": "separate",
            "default_family": "score_event_transition",
            "families": {
                "score_event_transition": {
                    "selected_method": "platt",
                    "methods": {"platt": {"params": {"a": 1.0, "b": 0.0}}},
                    "lines": {
                        "5.5": {
                            "selected_method": "platt",
                            "methods": {
                                "platt": {"params": {"a": 1.0, "b": -2.0}}
                            },
                            "n_train": 150,
                        },
                    },
                },
            },
        }

    def test_per_line_curve_overrides_family_pooled_when_present(self) -> None:
        cal = ProbabilityCalibrator.from_payload(self._payload_with_per_line())
        # raw 0.80, pooled = identity, so pooled_out = 0.80
        pooled = cal.calibrate(0.80, model_family="score_event_transition")
        self.assertAlmostEqual(pooled, 0.80, places=4)
        # raw 0.80, per-line: sigmoid(1*logit(0.80) - 2.0) approx 0.36
        per_line = cal.calibrate(
            0.80, model_family="score_event_transition", line="5.5",
        )
        self.assertLess(per_line, 0.50)
        self.assertGreater(per_line, 0.30)

    def test_per_line_falls_back_to_family_pooled_when_line_absent(self) -> None:
        cal = ProbabilityCalibrator.from_payload(self._payload_with_per_line())
        # line "7.5" has no per-line curve -> falls back to pooled identity.
        out_75 = cal.calibrate(
            0.80, model_family="score_event_transition", line="7.5",
        )
        self.assertAlmostEqual(out_75, 0.80, places=4)
        out_pooled = cal.calibrate(0.80, model_family="score_event_transition")
        self.assertAlmostEqual(out_75, out_pooled, places=6)

    def test_per_line_lookup_with_no_line_kwarg_uses_pooled(self) -> None:
        cal = ProbabilityCalibrator.from_payload(self._payload_with_per_line())
        # When caller omits line (line=None), runtime MUST behave
        # identically to pre-per-line. This preserves callers that
        # haven't been threaded with line yet.
        out_no_line = cal.calibrate(0.80, model_family="score_event_transition")
        self.assertAlmostEqual(out_no_line, 0.80, places=4)

    def test_has_line_curve_reports_presence_correctly(self) -> None:
        cal = ProbabilityCalibrator.from_payload(self._payload_with_per_line())
        self.assertTrue(
            cal.has_line_curve("score_event_transition", "5.5")
        )
        self.assertFalse(
            cal.has_line_curve("score_event_transition", "7.5")
        )
        # None line returns False -- no per-line curve possible.
        self.assertFalse(
            cal.has_line_curve("score_event_transition", None)
        )
        # Unknown family returns False.
        self.assertFalse(
            cal.has_line_curve("no_score_drift", "5.5")
        )

    def test_numeric_line_normalizes_to_string_key(self) -> None:
        """The artifact stores line keys as strings ('5.5'). When the
        engine passes the numeric float 5.5, the runtime must canonicalize
        to the same key. Mismatch would silently drop per-line behavior."""
        cal = ProbabilityCalibrator.from_payload(self._payload_with_per_line())
        out_str = cal.calibrate(
            0.80, model_family="score_event_transition", line="5.5",
        )
        out_num = cal.calibrate(
            0.80, model_family="score_event_transition", line=5.5,
        )
        self.assertAlmostEqual(out_str, out_num, places=6)

    def test_legacy_artifact_without_lines_block_unchanged(self) -> None:
        """Back-compat: a calibrator artifact with no ``lines`` block
        in any family must produce the exact same calibrated probability
        whether or not a line kwarg is passed."""
        payload = {
            "schema_version": 2,
            "family_mode": "separate",
            "default_family": "score_event_transition",
            "families": {
                "score_event_transition": {
                    "selected_method": "platt",
                    "methods": {"platt": {"params": {"a": 0.5, "b": 0.3}}},
                },
            },
        }
        cal = ProbabilityCalibrator.from_payload(payload)
        out_no_line = cal.calibrate(0.75, model_family="score_event_transition")
        out_with_line = cal.calibrate(
            0.75, model_family="score_event_transition", line="5.5",
        )
        self.assertAlmostEqual(out_no_line, out_with_line, places=8)

    def test_builder_writes_lines_block_when_threshold_met(self) -> None:
        """Integration: builder must materialize the lines block in
        the calibration artifact when --per-line-min-rows is set and
        cohort size is sufficient. Below threshold, the line is absent
        and the runtime falls back to family-pooled (covered above)."""
        # Build a synthetic input with 80 rows on line 5.5 (below threshold)
        # and 200 rows on line 7.5 (above threshold). Only 7.5 should
        # get a per-line block; 5.5 should not appear under "lines".
        rows = []
        # 7.5 cohort: positives well-correlated with raw_prob
        for i in range(200):
            raw = 0.40 + (i % 50) * 0.012  # spans 0.40 .. 0.99
            win = 1 if raw > 0.70 else 0
            rows.append({
                "mode": "live",
                "session_date": "2026-05-15",
                "candidate_id": f"cand-75-{i}",
                "signal_model_family": "score_event_transition",
                "label_available": True,
                "label_final_available": True,
                "target_over_win": win,
                "fair_value_raw": raw,
                "decision_ask": 0.65,
                "line": "7.5",
            })
        # 5.5 cohort: below threshold (80 rows)
        for i in range(80):
            raw = 0.50 + (i % 30) * 0.015
            win = 1 if raw > 0.85 else 0
            rows.append({
                "mode": "live",
                "session_date": "2026-05-15",
                "candidate_id": f"cand-55-{i}",
                "signal_model_family": "score_event_transition",
                "label_available": True,
                "label_final_available": True,
                "target_over_win": win,
                "fair_value_raw": raw,
                "decision_ask": 0.60,
                "line": "5.5",
            })
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
                "--per-line-min-rows", "100",
                "--no-stability-gate",
            ])
            payload = json.loads(
                (root / "out" / "signal_win_calibration.json").read_text(encoding="utf-8")
            )
            family = payload["families"]["score_event_transition"]
            self.assertIn("lines", family)
            self.assertIn("7.5", family["lines"])
            self.assertNotIn("5.5", family["lines"])
            line_75 = family["lines"]["7.5"]
            self.assertEqual(line_75.get("n_train"), 200)
            self.assertIn(line_75.get("selected_method"), {"platt", "isotonic"})

    def test_builder_omits_lines_block_when_disabled(self) -> None:
        """Default --per-line-min-rows=0 must keep the legacy schema
        (no lines block) for back-compat with existing readers."""
        rows = []
        for i in range(60):
            raw = 0.55 + (i % 20) * 0.02
            win = 1 if raw > 0.75 else 0
            rows.append({
                "mode": "live",
                "session_date": "2026-05-15",
                "candidate_id": f"cand-{i}",
                "signal_model_family": "score_event_transition",
                "label_available": True,
                "label_final_available": True,
                "target_over_win": win,
                "fair_value_raw": raw,
                "decision_ask": 0.60,
                "line": "7.5",
            })
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
            payload = json.loads(
                (root / "out" / "signal_win_calibration.json").read_text(encoding="utf-8")
            )
            family = payload["families"]["score_event_transition"]
            self.assertNotIn("lines", family)


if __name__ == "__main__":
    unittest.main()
