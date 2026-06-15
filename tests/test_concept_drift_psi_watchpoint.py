"""T7 (2026-06-15): concept-drift PSI watchpoint -- a one-shot nudge the
first day PSI becomes computable again after a live-root gap, auto-re-armed
when PSI goes insufficient again.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR / "scripts" / "analysis") not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR / "scripts" / "analysis"))

from human_review.drift_health import _concept_drift_health  # noqa: E402


def _report(features_verdict, n_rows):
    return {
        "generated_at_utc": "2026-06-20T16:00:00Z",
        "active_date": "2026-06-20",
        "current_window": {"start": "2026-06-14", "end": "2026-06-20", "n_rows": n_rows},
        "baseline_window": {"start": "2026-05-14", "end": "2026-06-13"},
        "thresholds": {"min_rows_per_feature": 30, "psi_major": 0.25},
        "alerts": [],
        "features": {
            f"feat_{i}": {
                "kind": "continuous", "metric": "psi", "value": 0.05,
                "verdict": v, "current_n": n_rows, "baseline_n": 200,
            }
            for i, v in enumerate(features_verdict)
        },
    }


class PsiWatchpointTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "concept_drift_report.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, report, date="2026-06-20"):
        self.path.write_text(json.dumps(report), encoding="utf-8")
        return _concept_drift_health(report_path=self.path, session_date=date)

    def _fired(self, out):
        return any("computable again" in a for a in out.get("alerts", []))

    def test_fires_once_then_suppressed_by_marker(self):
        rep = _report(["ok", "minor", "ok"], n_rows=42)
        first = self._run(rep)
        self.assertTrue(first["psi_watchpoint"]["psi_computable"])
        self.assertTrue(self._fired(first))
        # Marker now present -> second run does NOT re-fire.
        second = self._run(rep)
        self.assertFalse(self._fired(second))

    def test_insufficient_does_not_fire(self):
        rep = _report(["insufficient_data", "insufficient_data"], n_rows=8)
        out = self._run(rep)
        self.assertFalse(out["psi_watchpoint"]["psi_computable"])
        self.assertFalse(self._fired(out))

    def test_rearms_after_going_insufficient_again(self):
        # Computable -> fire + marker.
        self.assertTrue(self._fired(self._run(_report(["ok"], 40))))
        # Gap: insufficient -> re-arm (marker removed), no fire.
        self.assertFalse(self._fired(self._run(_report(["insufficient_data"], 5))))
        # Recovery -> fires again.
        self.assertTrue(self._fired(self._run(_report(["ok", "ok"], 38))))


if __name__ == "__main__":
    unittest.main()
