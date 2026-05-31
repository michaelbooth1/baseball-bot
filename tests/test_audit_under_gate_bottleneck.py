"""Tests for scripts/analysis/audit_under_gate_bottleneck.py."""
import datetime as _dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import audit_under_gate_bottleneck as agb  # noqa: E402


def _write_session(
    sessions_dir: Path,
    date: str,
    by_decision_reason: dict,
    mode: str = "paper",
) -> Path:
    path = sessions_dir / f"{date}_session.json"
    path.write_text(
        json.dumps(
            {
                "date": date,
                "mode": mode,
                "summary": {
                    "candidate_rollup": {
                        "by_decision_reason": by_decision_reason,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return path


class AnalyzeSessionTests(unittest.TestCase):
    def test_single_gate_owns_everything_is_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            sessions = Path(td) / "sessions"
            sessions.mkdir()
            today = _dt.date.today().isoformat()
            _write_session(
                sessions,
                today,
                {
                    "skip:gate_under_min_entry_ask": 1653,
                    "skip:gate_min_inning": 66726,  # OVER, ignored
                },
            )
            out = Path(td) / "out"
            rc = agb.main(
                [
                    "--session-root", str(sessions),
                    "--output-dir", str(out),
                    "--recent-days", "30",
                ]
            )
            self.assertEqual(rc, 0)
            payload = json.loads(
                (out / "under_gate_bottleneck_audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["status"], "bottleneck_detected")
            self.assertEqual(payload["sessions_flagged"], 1)
            [f] = payload["findings"]
            self.assertEqual(f["top_reason"], "skip:gate_under_min_entry_ask")
            self.assertEqual(f["under_skip_total"], 1653)
            self.assertAlmostEqual(f["top_reason_share"], 1.0)
            self.assertTrue(f["is_bottleneck"])

    def test_balanced_under_skip_distribution_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            sessions = Path(td) / "sessions"
            sessions.mkdir()
            today = _dt.date.today().isoformat()
            _write_session(
                sessions,
                today,
                {
                    "skip:gate_under_min_entry_ask": 200,
                    "skip:gate_under_fv_ask_gap": 180,
                    "skip:gate_under_extreme_edge": 150,
                },
            )
            out = Path(td) / "out"
            agb.main(
                [
                    "--session-root", str(sessions),
                    "--output-dir", str(out),
                    "--recent-days", "30",
                ]
            )
            payload = json.loads(
                (out / "under_gate_bottleneck_audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["status"], "clean")
            [f] = payload["findings"]
            self.assertFalse(f["is_bottleneck"])
            self.assertEqual(f["under_skip_total"], 530)

    def test_below_min_n_not_flagged_even_when_concentrated(self):
        with tempfile.TemporaryDirectory() as td:
            sessions = Path(td) / "sessions"
            sessions.mkdir()
            today = _dt.date.today().isoformat()
            # 100% concentration but n<100 -> not flagged.
            _write_session(
                sessions, today, {"skip:gate_under_min_entry_ask": 50},
            )
            out = Path(td) / "out"
            agb.main(
                [
                    "--session-root", str(sessions),
                    "--output-dir", str(out),
                    "--recent-days", "30",
                ]
            )
            payload = json.loads(
                (out / "under_gate_bottleneck_audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["status"], "clean")
            [f] = payload["findings"]
            self.assertFalse(f["is_bottleneck"])

    def test_zero_under_skip_rows_handled_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            sessions = Path(td) / "sessions"
            sessions.mkdir()
            today = _dt.date.today().isoformat()
            _write_session(
                sessions, today, {"skip:gate_min_inning": 5000},  # OVER only
            )
            out = Path(td) / "out"
            agb.main(
                [
                    "--session-root", str(sessions),
                    "--output-dir", str(out),
                    "--recent-days", "30",
                ]
            )
            payload = json.loads(
                (out / "under_gate_bottleneck_audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["status"], "clean")
            [f] = payload["findings"]
            self.assertEqual(f["under_skip_total"], 0)
            self.assertIsNone(f["top_reason"])
            self.assertFalse(f["is_bottleneck"])

    def test_older_than_window_is_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            sessions = Path(td) / "sessions"
            sessions.mkdir()
            old = (_dt.date.today() - _dt.timedelta(days=30)).isoformat()
            _write_session(
                sessions, old, {"skip:gate_under_min_entry_ask": 1000},
            )
            out = Path(td) / "out"
            agb.main(
                [
                    "--session-root", str(sessions),
                    "--output-dir", str(out),
                    "--recent-days", "7",
                ]
            )
            payload = json.loads(
                (out / "under_gate_bottleneck_audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["sessions_scanned"], 0)
            self.assertEqual(payload["status"], "clean")

    def test_markdown_output_written(self):
        with tempfile.TemporaryDirectory() as td:
            sessions = Path(td) / "sessions"
            sessions.mkdir()
            today = _dt.date.today().isoformat()
            _write_session(
                sessions, today, {"skip:gate_under_min_entry_ask": 1653},
            )
            out = Path(td) / "out"
            agb.main(
                [
                    "--session-root", str(sessions),
                    "--output-dir", str(out),
                    "--recent-days", "30",
                ]
            )
            md = (out / "under_gate_bottleneck_audit.md").read_text(encoding="utf-8")
            self.assertIn("UNDER Gate Bottleneck Audit", md)
            self.assertIn("STATUS:", md)


class DefaultSessionRootsTests(unittest.TestCase):
    """2026-05-31: the discovery function must include per-preset
    paper_<label>/sessions/ dirs (multi-engine fleet) in addition to
    the legacy paper_trading/ + live_trading/ roots. The 2026-05-30
    audit was blind to per-preset roots and missed a real bottleneck
    in paper_M_under_paper/sessions/."""

    def test_discovery_picks_up_per_preset_paper_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            project_dir = Path(td)
            data = project_dir / "data"
            # Legacy roots.
            (data / "paper_trading" / "sessions").mkdir(parents=True)
            (data / "live_trading" / "sessions").mkdir(parents=True)
            # Multi-engine per-preset roots.
            (data / "paper_A_current" / "sessions").mkdir(parents=True)
            (data / "paper_M_under_paper" / "sessions").mkdir(parents=True)
            # A paper_<label>/ directory WITHOUT a sessions/ subdir must be ignored.
            (data / "paper_no_sessions").mkdir(parents=True)
            # A non-`paper_` directory must be ignored.
            (data / "analysis_output").mkdir(parents=True)

            orig = agb.PROJECT_DIR
            agb.PROJECT_DIR = project_dir
            try:
                roots = agb._discover_default_session_roots()
            finally:
                agb.PROJECT_DIR = orig

            root_names = {Path(r).parent.name for r in roots}
            self.assertIn("paper_trading", root_names)
            self.assertIn("live_trading", root_names)
            self.assertIn("paper_A_current", root_names)
            self.assertIn("paper_M_under_paper", root_names)
            self.assertNotIn("paper_no_sessions", root_names)
            self.assertNotIn("analysis_output", root_names)


if __name__ == "__main__":
    unittest.main()
