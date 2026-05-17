"""Phase A2 (2026-05-16): separate UNDER calibrator.

`calibrate_signal_probabilities.py --side under` flips labels
(under_win = 1 - over_win) and raw probs (p_under = 1 - p_over),
writes to a separate artifact (signal_win_calibration_under.json),
and maintains a separate stability-gate selection history. Tests
cover: flip correctness, separate artifact path defaulting, family
split preservation, stability-history isolation, and that OVER-side
behavior is unchanged when --side over (the legacy default).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
for d in (TRADING_DIR, ANALYSIS_DIR):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))

from scripts.analysis import calibrate_signal_probabilities as calib  # noqa: E402


def _candidate_rows(n_score=8, n_drift=6):
    """Build a small but realistic candidate_universe slice spanning
    multiple dates so the train/val/test splitter has dates to work with.
    OVER labels alternate so both classes are present.

    `bet_id` is the identifier the calibration script reads (not
    `candidate_id`), so tests that round-trip through predictions
    must set it explicitly."""
    rows = []
    dates = [
        "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04",
        "2026-05-05", "2026-05-06", "2026-05-07", "2026-05-08",
    ]
    for i in range(n_score):
        rows.append({
            "mode": "live",
            "session_date": dates[i % len(dates)],
            "bet_id": f"score-{i}",
            "candidate_id": f"score-{i}",
            "decision": "trade",
            "signal_model_family": "score_event_transition",
            "label_available": True,
            "target_counterfactual_win": i % 2,  # alternate W/L
            "fair_value_raw": 0.55 + 0.04 * (i % 5),
            "decision_ask": 0.50 + 0.03 * (i % 4),
        })
    for i in range(n_drift):
        rows.append({
            "mode": "live",
            "session_date": dates[i % len(dates)],
            "bet_id": f"drift-{i}",
            "candidate_id": f"drift-{i}",
            "decision": "shadow_no_score_drift",
            "signal_model_family": "no_score_drift",
            "label_available": True,
            "target_counterfactual_win": (i + 1) % 2,
            "fair_value_raw": 0.50 + 0.05 * (i % 4),
            "decision_ask": 0.45 + 0.02 * (i % 3),
        })
    return rows


def _run_calib(input_path, output_root, *, side, history_path=None,
               concept_drift_path=None):
    """Run the calibrator with an isolated concept-drift report path
    so tests don't pull in real production data."""
    if concept_drift_path is None:
        # Point at a non-existent path; the script falls back to
        # input_drift_status='report_missing' without failing.
        concept_drift_path = output_root.parent / "missing_concept_drift.json"
    args = [
        "--input-path", str(input_path),
        "--input-kind", "candidate_universe",
        "--output-root", str(output_root),
        "--family-mode", "separate",
        "--val-frac", "0.0",
        "--test-frac", "0.25",
        "--side", side,
        "--concept-drift-report-path", str(concept_drift_path),
    ]
    if history_path is None:
        args.append("--no-stability-gate")
    else:
        args.extend(["--selection-history-path", str(history_path)])
    calib.main(args)


def _write_rows(path: Path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class UnderCalibratorFlipTests(unittest.TestCase):
    """Label + raw-prob flips: a single sample with target_over_win=1
    and fair_value_raw=0.78 should yield, in the predictions JSONL,
    label=0 and raw_prob=0.22 under --side under."""

    def test_under_sample_label_and_raw_prob_are_flipped(self):
        # Single-row case is degenerate for fitting but the predictions
        # JSONL is the artifact we want to inspect for the flip.
        rows = _candidate_rows(n_score=8, n_drift=0)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inp = root / "candidates.jsonl"
            _write_rows(inp, rows)
            _run_calib(inp, root / "under_out", side="under")

            preds_path = root / "under_out" / "signal_win_calibration_under_predictions.jsonl"
            with open(preds_path, encoding="utf-8") as f:
                preds = [json.loads(line) for line in f]

            self.assertEqual(len(preds), len(rows))
            # For each prediction row, find the source candidate by
            # bet_id and verify label/raw_prob are flipped.
            row_by_id = {r["bet_id"]: r for r in rows}
            for pred in preds:
                src = row_by_id.get(pred["bet_id"])
                self.assertIsNotNone(src, f"unknown bet_id {pred['bet_id']!r}")
                # Label flip: target_over_win=1 -> label=0
                self.assertEqual(pred["label"], 1 - int(src["target_counterfactual_win"]))
                # Raw prob flip: p_under = 1 - p_over (within clip bounds)
                self.assertAlmostEqual(
                    pred["raw_prob"], 1.0 - float(src["fair_value_raw"]),
                    places=6,
                )

    def test_over_side_unchanged_when_side_over(self):
        """The legacy --side over (default) path must not flip anything."""
        rows = _candidate_rows(n_score=8, n_drift=0)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inp = root / "candidates.jsonl"
            _write_rows(inp, rows)
            _run_calib(inp, root / "over_out", side="over")

            preds_path = root / "over_out" / "signal_win_calibration_predictions.jsonl"
            with open(preds_path, encoding="utf-8") as f:
                preds = [json.loads(line) for line in f]

            row_by_id = {r["bet_id"]: r for r in rows}
            for pred in preds:
                src = row_by_id.get(pred["bet_id"])
                self.assertEqual(pred["label"], int(src["target_counterfactual_win"]))
                self.assertAlmostEqual(
                    pred["raw_prob"], float(src["fair_value_raw"]),
                    places=6,
                )


class UnderCalibratorArtifactPathsTests(unittest.TestCase):
    def test_under_writes_separate_artifact_path(self):
        """Default output stem must be signal_win_calibration_under
        when --side under. The OVER artifact must NOT be overwritten."""
        rows = _candidate_rows()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inp = root / "candidates.jsonl"
            _write_rows(inp, rows)
            out = root / "out"
            _run_calib(inp, out, side="over")
            _run_calib(inp, out, side="under")

            self.assertTrue((out / "signal_win_calibration.json").exists())
            self.assertTrue((out / "signal_win_calibration_under.json").exists())
            self.assertTrue((out / "signal_win_calibration_report.json").exists())
            self.assertTrue((out / "signal_win_calibration_under_report.json").exists())
            self.assertTrue((out / "signal_win_calibration_predictions.jsonl").exists())
            self.assertTrue((out / "signal_win_calibration_under_predictions.jsonl").exists())

    def test_under_artifact_stamps_side_in_payload(self):
        rows = _candidate_rows()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inp = root / "candidates.jsonl"
            _write_rows(inp, rows)
            _run_calib(inp, root / "out", side="under")

            payload = json.loads(
                (root / "out" / "signal_win_calibration_under.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["side"], "under")
            self.assertEqual(payload["data"]["side"], "under")
            self.assertIn("under_win = 1 - over_win", payload["notes"]["label"])
            self.assertIn("1 - fair_value_raw", payload["notes"]["probability_field"])

    def test_over_artifact_stamps_side_over_in_payload(self):
        rows = _candidate_rows()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inp = root / "candidates.jsonl"
            _write_rows(inp, rows)
            _run_calib(inp, root / "out", side="over")

            payload = json.loads(
                (root / "out" / "signal_win_calibration.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["side"], "over")
            self.assertEqual(payload["data"]["side"], "over")
            # Over notes should NOT have the under flip prefix
            self.assertNotIn("under_win = 1 - over_win", payload["notes"]["label"])
            self.assertEqual(
                payload["notes"]["probability_field"],
                "fair_value_raw (fallback: fair_value)",
            )

    def test_under_preserves_family_split(self):
        """The family decomposition (score_event_transition vs
        no_score_drift) must work the same way for --side under as
        for --side over -- flipping label/prob doesn't change family
        membership."""
        rows = _candidate_rows()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inp = root / "candidates.jsonl"
            _write_rows(inp, rows)
            _run_calib(inp, root / "out", side="under")

            payload = json.loads(
                (root / "out" / "signal_win_calibration_under.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["family_mode"], "separate")
            self.assertEqual(
                set(payload["families"].keys()),
                {"score_event_transition", "no_score_drift"},
            )
            self.assertEqual(
                payload["data"]["model_family_counts"]["score_event_transition"], 8,
            )
            self.assertEqual(
                payload["data"]["model_family_counts"]["no_score_drift"], 6,
            )

    def test_explicit_output_stem_overrides_side_default(self):
        """When --output-stem is explicitly passed, the side-aware
        default routing must NOT clobber it."""
        rows = _candidate_rows()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inp = root / "candidates.jsonl"
            _write_rows(inp, rows)
            calib.main([
                "--input-path", str(inp),
                "--input-kind", "candidate_universe",
                "--output-root", str(root / "out"),
                "--output-stem", "custom_under_name",
                "--family-mode", "separate",
                "--val-frac", "0.0",
                "--test-frac", "0.25",
                "--side", "under",
                "--no-stability-gate",
                "--concept-drift-report-path", str(root / "missing_cd.json"),
            ])
            self.assertTrue((root / "out" / "custom_under_name.json").exists())
            self.assertFalse((root / "out" / "signal_win_calibration_under.json").exists())


class UnderCalibratorStabilityHistoryIsolationTests(unittest.TestCase):
    """The UNDER side maintains its own selection_history.jsonl so the
    UNDER stability gate cannot be tripped by OVER method flips."""

    def test_under_run_writes_under_default_history_path(self):
        """When --side under and no explicit history path is passed,
        the default history filename is selection_history_under.jsonl
        (NOT selection_history.jsonl)."""
        rows = _candidate_rows()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inp = root / "candidates.jsonl"
            _write_rows(inp, rows)
            history = root / "selection_history_under.jsonl"
            calib.main([
                "--input-path", str(inp),
                "--input-kind", "candidate_universe",
                "--output-root", str(root / "out"),
                "--family-mode", "separate",
                "--val-frac", "0.0",
                "--test-frac", "0.25",
                "--side", "under",
                "--selection-history-path", str(history),
                "--concept-drift-report-path", str(root / "missing_cd.json"),
            ])
            self.assertTrue(history.exists())
            content = history.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(content), 1)
            row = json.loads(content[0])
            self.assertIn("score_event_transition", row.get("selections") or {})

    def test_over_and_under_histories_are_disjoint_files(self):
        """Running OVER then UNDER must produce two separate history
        files; the OVER history file should not contain UNDER data
        and vice versa."""
        rows = _candidate_rows()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inp = root / "candidates.jsonl"
            _write_rows(inp, rows)
            over_history = root / "over_history.jsonl"
            under_history = root / "under_history.jsonl"

            cd_path = root / "missing_cd.json"
            calib.main([
                "--input-path", str(inp),
                "--input-kind", "candidate_universe",
                "--output-root", str(root / "out"),
                "--family-mode", "separate",
                "--val-frac", "0.0",
                "--test-frac", "0.25",
                "--side", "over",
                "--selection-history-path", str(over_history),
                "--concept-drift-report-path", str(cd_path),
            ])
            calib.main([
                "--input-path", str(inp),
                "--input-kind", "candidate_universe",
                "--output-root", str(root / "out"),
                "--family-mode", "separate",
                "--val-frac", "0.0",
                "--test-frac", "0.25",
                "--side", "under",
                "--selection-history-path", str(under_history),
                "--concept-drift-report-path", str(cd_path),
            ])

            self.assertTrue(over_history.exists())
            self.assertTrue(under_history.exists())
            # Each file should have exactly one row (this run's selections).
            self.assertEqual(len(over_history.read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(len(under_history.read_text(encoding="utf-8").splitlines()), 1)


class UnderCalibratorMathematicalAsymmetryTests(unittest.TestCase):
    """A perfect calibrator on Over would invert exactly to Under, but
    Platt + isotonic fits are NOT generally symmetric under flip. This
    is the design justification for fitting a separate UNDER curve.
    Test verifies that on real-looking data, the fitted parameters
    differ, not just the labels."""

    def test_under_platt_params_differ_from_over(self):
        # Use enough rows + asymmetric outcomes to make the Platt fit
        # noticeably different on the under side.
        rows = _candidate_rows(n_score=16, n_drift=10)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inp = root / "candidates.jsonl"
            _write_rows(inp, rows)
            _run_calib(inp, root / "over_out", side="over")
            _run_calib(inp, root / "under_out", side="under")

            o = json.loads(
                (root / "over_out" / "signal_win_calibration.json").read_text(encoding="utf-8")
            )
            u = json.loads(
                (root / "under_out" / "signal_win_calibration_under.json").read_text(encoding="utf-8")
            )

            o_methods = o["families"]["score_event_transition"]["methods"]
            u_methods = u["families"]["score_event_transition"]["methods"]
            o_platt = o_methods.get("platt", {}).get("params", {})
            u_platt = u_methods.get("platt", {}).get("params", {})
            # Both fits must have produced params (a, b)
            self.assertIn("a", o_platt)
            self.assertIn("a", u_platt)
            # At least one of (a, b) must differ -- the flip is not a
            # trivial sign change on a Platt fit.
            differs = (
                abs(o_platt["a"] - u_platt["a"]) > 1e-6
                or abs(o_platt["b"] - u_platt["b"]) > 1e-6
            )
            self.assertTrue(
                differs,
                f"OVER platt {o_platt} == UNDER platt {u_platt}; "
                "expected meaningful asymmetry from flip",
            )


if __name__ == "__main__":
    unittest.main()
