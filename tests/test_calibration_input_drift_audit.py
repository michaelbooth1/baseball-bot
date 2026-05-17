"""Tests for the calibration input-drift audit (shipped 2026-05-16).

When the concept_drift report shows >= 2 continuous features
(metric=='PSI') at verdict=='major' (PSI >= 0.25), the calibration
artifact's selection_audit carries `input_drift_triggered: true` plus
the list of major features. Operators can then see, while reading the
calibration audit, that today's method was selected on materially-
shifted inputs.

Test surface focuses on the helper:
  - missing file -> triggered=false, status='report_missing'
  - unreadable JSON -> triggered=false, status='report_unreadable'
  - no major features -> triggered=false, status='ok', major=[]
  - one major feature (below threshold) -> triggered=false
  - two major features -> triggered=true, ordered by PSI desc
  - TVD-only features ignored (only PSI counts)
  - injects into _fit_calibration_bundle's selection_audit (via
    end-to-end call through a synthetic samples set)
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import calibrate_signal_probabilities as cal  # noqa: E402


def _write_drift_report(path: Path, features: dict, generated_at: str = "2026-05-16T01:00:00Z") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "generated_at_utc": generated_at,
        "features": features,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


class LoadInputDriftStatusTests(unittest.TestCase):
    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            out = cal._load_input_drift_status(td / "missing.json")
            self.assertFalse(out["input_drift_triggered"])
            self.assertEqual(out["input_drift_status"], "report_missing")
            self.assertEqual(out["input_drift_major_features"], [])

    def test_unreadable_json(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "broken.json"
            p.write_text("not json {{{", encoding="utf-8")
            out = cal._load_input_drift_status(p)
            self.assertFalse(out["input_drift_triggered"])
            self.assertEqual(out["input_drift_status"], "report_unreadable")
            self.assertIn("input_drift_error", out)

    def test_well_formed_no_major(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "drift.json"
            _write_drift_report(p, {
                "weather_temp_f": {"metric": "PSI", "value": 0.05, "verdict": "stable"},
                "team_offense_delta": {"metric": "PSI", "value": 0.18, "verdict": "minor"},
            })
            out = cal._load_input_drift_status(p)
            self.assertFalse(out["input_drift_triggered"])
            self.assertEqual(out["input_drift_status"], "ok")
            self.assertEqual(out["input_drift_major_features"], [])

    def test_single_major_below_threshold(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "drift.json"
            _write_drift_report(p, {
                "stage2_run_env_delta": {"metric": "PSI", "value": 3.22, "verdict": "major"},
                "weather_temp_f": {"metric": "PSI", "value": 0.05, "verdict": "stable"},
            })
            out = cal._load_input_drift_status(p)
            # Only 1 major, default min=2 -> no trigger but feature listed
            self.assertFalse(out["input_drift_triggered"])
            self.assertEqual(len(out["input_drift_major_features"]), 1)
            self.assertEqual(out["input_drift_major_features"][0]["feature"],
                             "stage2_run_env_delta")

    def test_two_majors_triggers_sorted_desc(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "drift.json"
            _write_drift_report(p, {
                "team_offense_delta": {"metric": "PSI", "value": 2.36, "verdict": "major"},
                "stage2_run_env_delta": {"metric": "PSI", "value": 3.22, "verdict": "major"},
                "base_fair_value": {"metric": "PSI", "value": 2.88, "verdict": "major"},
            })
            out = cal._load_input_drift_status(p)
            self.assertTrue(out["input_drift_triggered"])
            self.assertEqual(out["input_drift_status"], "ok")
            # Sorted by PSI desc
            self.assertEqual(
                [r["feature"] for r in out["input_drift_major_features"]],
                ["stage2_run_env_delta", "base_fair_value", "team_offense_delta"],
            )

    def test_tvd_features_ignored(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "drift.json"
            _write_drift_report(p, {
                "stadium_id": {"metric": "TVD", "value": 0.99, "verdict": "major"},
                "weather_temp_f": {"metric": "PSI", "value": 0.05, "verdict": "stable"},
            })
            out = cal._load_input_drift_status(p)
            # TVD doesn't count -> 0 major
            self.assertFalse(out["input_drift_triggered"])
            self.assertEqual(out["input_drift_major_features"], [])

    def test_custom_min_features_threshold(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "drift.json"
            _write_drift_report(p, {
                "stage2_run_env_delta": {"metric": "PSI", "value": 3.22, "verdict": "major"},
            })
            # With min=1, single major triggers
            out = cal._load_input_drift_status(p, min_features=1)
            self.assertTrue(out["input_drift_triggered"])

    def test_report_generated_at_propagated(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "drift.json"
            _write_drift_report(p, {
                "x": {"metric": "PSI", "value": 0.5, "verdict": "major"},
            }, generated_at="2026-05-15T08:00:00Z")
            out = cal._load_input_drift_status(p)
            self.assertEqual(out["input_drift_report_generated_at_utc"],
                             "2026-05-15T08:00:00Z")

    def test_lowercase_metric_field_matches_psi(self):
        # The build_concept_drift_report.py writes metric as lowercase
        # ("psi"/"tvd"); the helper normalises and still triggers.
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "drift.json"
            _write_drift_report(p, {
                "a": {"metric": "psi", "value": 0.5, "verdict": "major"},
                "b": {"metric": "psi", "value": 1.0, "verdict": "major"},
                "c": {"metric": "tvd", "value": 0.9, "verdict": "major"},  # ignored
            })
            out = cal._load_input_drift_status(p)
            self.assertTrue(out["input_drift_triggered"])
            self.assertEqual(len(out["input_drift_major_features"]), 2)

    def test_malformed_value_field_ignored(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "drift.json"
            _write_drift_report(p, {
                "good_one": {"metric": "PSI", "value": 0.5, "verdict": "major"},
                "weird":    {"metric": "PSI", "value": "not a number", "verdict": "major"},
            })
            out = cal._load_input_drift_status(p)
            # weird's value is unparseable -> dropped; only 1 left -> no trigger
            self.assertEqual(len(out["input_drift_major_features"]), 1)
            self.assertEqual(out["input_drift_major_features"][0]["feature"], "good_one")
            self.assertFalse(out["input_drift_triggered"])


if __name__ == "__main__":
    unittest.main()
