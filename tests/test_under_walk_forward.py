"""Phase A4 (2026-05-16): UNDER walk-forward + certification.

Tests cover the label flip in the runner and the certification
report's outcome derivation, ROI math, sample-readiness verdict,
and cohort breakdowns.
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

import under_walk_forward_runner as uwf  # noqa: E402
import build_under_walk_forward_certification as uwfc  # noqa: E402


class UnderRunnerLabelFlipTests(unittest.TestCase):
    """The runner flips target_win to under_target_win = 1 - target_win
    before calling train_task. target_filled and target_profit stay as
    Over-side bookkeeping (no UNDER orders have been posted, so there
    is no under-side fill model to train)."""

    def test_flip_target_win_one_to_zero(self):
        row = {"target_win": 1, "target_filled": 1, "target_profit": 5.0}
        out = uwf._flip_target_win(row)
        self.assertEqual(out["target_win"], 0)

    def test_flip_target_win_zero_to_one(self):
        row = {"target_win": 0, "target_filled": 1, "target_profit": -10.0}
        out = uwf._flip_target_win(row)
        self.assertEqual(out["target_win"], 1)

    def test_flip_target_win_none_passes_through(self):
        row = {"target_win": None, "target_filled": 0}
        out = uwf._flip_target_win(row)
        self.assertIsNone(out["target_win"])

    def test_flip_does_not_mutate_other_fields(self):
        row = {"target_win": 1, "target_filled": 1, "target_profit": 5.5,
               "split": "train", "bet_id": "abc"}
        out = uwf._flip_target_win(row)
        self.assertEqual(out["target_filled"], 1)
        self.assertEqual(out["target_profit"], 5.5)
        self.assertEqual(out["split"], "train")
        self.assertEqual(out["bet_id"], "abc")

    def test_flip_is_a_copy_not_a_mutation(self):
        row = {"target_win": 1, "target_filled": 1}
        _ = uwf._flip_target_win(row)
        self.assertEqual(row["target_win"], 1, "source row was mutated")

    def test_invalid_label_becomes_none(self):
        """A defensive: any non-numeric label should not crash; output
        None so downstream label-count filter drops the row."""
        row = {"target_win": "not_a_number"}
        out = uwf._flip_target_win(row)
        self.assertIsNone(out["target_win"])


class UnderCertReadinessTests(unittest.TestCase):
    def test_insufficient_when_low(self):
        v = uwfc.readiness_verdict(n_outcomes=50, n_dates=10)
        self.assertEqual(v["verdict"], "INSUFFICIENT")

    def test_preliminary_band(self):
        v = uwfc.readiness_verdict(n_outcomes=100, n_dates=20)
        self.assertEqual(v["verdict"], "PRELIMINARY")

    def test_ready_when_thresholds_met(self):
        v = uwfc.readiness_verdict(n_outcomes=200, n_dates=40)
        self.assertEqual(v["verdict"], "READY")

    def test_preliminary_requires_both_thresholds(self):
        """High outcomes but few dates should not promote to READY."""
        v = uwfc.readiness_verdict(n_outcomes=400, n_dates=15)
        self.assertEqual(v["verdict"], "PRELIMINARY")


class UnderCertCohortStatsTests(unittest.TestCase):
    def _row(self, **kw):
        defaults = dict(
            session_date="2026-05-01", family="score_event_transition",
            line=8.5, inning=5, runs_needed=2.0,
            decision_ask=0.70, edge_at_ask=0.10,
            current_state_edge=-0.05, phantom_risk_band="low",
            target_over_win=0,  # OVER lost -> UNDER won
            under_best_ask=0.30, under_pair_available=True,
        )
        defaults.update(kw)
        return uwfc.UnderBetRow(**defaults)

    def test_under_signal_win_is_flip_of_over_win(self):
        s = uwfc.UnderCohortStats()
        s.add(self._row(target_over_win=0))  # under wins
        s.add(self._row(target_over_win=1))  # under loses
        s.add(self._row(target_over_win=0))  # under wins
        self.assertEqual(s.n_outcomes, 3)
        self.assertEqual(s.n_under_wins, 2)
        self.assertAlmostEqual(s.under_signal_win_rate, 2 / 3, places=4)

    def test_unpaired_row_counts_n_but_excluded_from_roi(self):
        s = uwfc.UnderCohortStats()
        s.add(self._row(target_over_win=0, under_pair_available=True,
                        under_best_ask=0.30))  # paired UNDER win
        s.add(self._row(target_over_win=1, under_pair_available=False,
                        under_best_ask=None))  # unpaired UNDER loss
        self.assertEqual(s.n_bets, 2)
        self.assertEqual(s.n_with_under_ask, 1)
        # ROI is from just the one paired row: 1/0.30 - 1 = 2.333...
        self.assertAlmostEqual(s.under_taker_roi, 1.0 / 0.30 - 1.0, places=4)

    def test_roi_math_uses_under_ask_not_decision_ask(self):
        """Critical: under_taker_roi must use under_best_ask, not
        decision_ask (which is the over ask)."""
        s = uwfc.UnderCohortStats()
        s.add(self._row(
            target_over_win=0,  # under wins
            decision_ask=0.70,  # over ask (would give ROI 1/0.70 - 1 = 0.428)
            under_best_ask=0.30,  # under ask (gives ROI 1/0.30 - 1 = 2.333)
        ))
        self.assertAlmostEqual(s.under_taker_roi, 1.0 / 0.30 - 1.0, places=4)

    def test_no_outcomes_returns_none(self):
        s = uwfc.UnderCohortStats()
        s.add(uwfc.UnderBetRow(
            session_date="2026-05-01", family="x", line=8.5, inning=5,
            runs_needed=2.0, decision_ask=0.70, edge_at_ask=0.10,
            current_state_edge=-0.05, phantom_risk_band="low",
            target_over_win=None,  # unsettled
            under_best_ask=0.30, under_pair_available=True,
        ))
        self.assertIsNone(s.under_signal_win_rate)
        self.assertIsNone(s.under_taker_roi)


class UnderCertEndToEndTests(unittest.TestCase):
    """End-to-end check: feed a synthetic training table to the
    cert builder, verify the payload has the expected shape."""

    def _write_training_table(self, path: Path):
        # Build 16 rows spanning 4 dates so distinct_dates is meaningful.
        rows = []
        for i in range(16):
            rows.append({
                "session_date": f"2026-05-{(i % 4) + 1:02d}",
                "signal_model_family": (
                    "score_event_transition" if i % 2 == 0 else "no_score_drift"
                ),
                "line": 8.5,
                "inning": 5 + (i % 3),
                "runs_needed": 1.0 + 0.5 * (i % 4),
                "decision_ask": 0.65 + 0.05 * (i % 3),
                "edge_at_ask": 0.10 + 0.02 * (i % 5),
                "current_state_value_edge": -0.05 + 0.01 * (i % 4),
                "shadow_phantom_risk_band": ["low", "medium", "high"][i % 3],
                "target_win": i % 2,  # alternate over W/L
                "target_filled": 1,
                "target_profit": 5.0 if i % 2 else -10.0,
                # Half the rows have under_pair_available; tests coverage logic
                "under_pair_available": (i % 2 == 0),
                "under_best_ask": 0.30 + 0.04 * (i % 4) if (i % 2 == 0) else None,
            })
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return rows

    def test_end_to_end_payload_shape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            training_path = root / "training.jsonl"
            self._write_training_table(training_path)
            per_window = root / "per_window.jsonl"
            per_window.write_text("", encoding="utf-8")
            out = root / "out"

            uwfc.main([
                "--training-table", str(training_path),
                "--per-window-path", str(per_window),
                "--output-dir", str(out),
            ])

            json_path = out / "under_walk_forward_certification.json"
            md_path = out / "under_walk_forward_certification.md"
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())

            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["side"], "under")
            self.assertEqual(payload["phase"], "A4")
            self.assertEqual(payload["overall"]["n_bets"], 16)
            self.assertEqual(payload["overall"]["n_outcomes"], 16)
            # 8 over-wins -> 8 under-losses; 8 over-losses -> 8 under-wins
            self.assertEqual(payload["overall"]["n_under_wins"], 8)
            self.assertEqual(payload["readiness"]["verdict"], "INSUFFICIENT")
            # All cohort dimensions present
            for cohort_name in (
                "edge_band", "ask_band", "inning_band", "runs_needed_band",
                "current_state_edge_band", "phantom_risk_band", "family",
            ):
                self.assertIn(cohort_name, payload["cohort_breakdowns"])

    def test_empty_training_table_renders_clean(self):
        """Cert must render without crashing when training table is
        empty (the day-zero state)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            training_path = root / "training.jsonl"
            training_path.write_text("", encoding="utf-8")
            per_window = root / "per_window.jsonl"
            per_window.write_text("", encoding="utf-8")
            out = root / "out"
            uwfc.main([
                "--training-table", str(training_path),
                "--per-window-path", str(per_window),
                "--output-dir", str(out),
            ])
            payload = json.loads(
                (out / "under_walk_forward_certification.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["overall"]["n_bets"], 0)
            self.assertEqual(payload["readiness"]["verdict"], "INSUFFICIENT")

    def test_per_window_summary_pulls_brier_when_available(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            training_path = root / "training.jsonl"
            self._write_training_table(training_path)
            per_window = root / "per_window.jsonl"
            per_window.write_text(
                "\n".join([
                    json.dumps({
                        "completed": True,
                        "task_metrics": {
                            "signal_win": {"test": {"brier": 0.21}},
                        },
                    }),
                    json.dumps({
                        "completed": True,
                        "task_metrics": {
                            "signal_win": {"test": {"brier": 0.19}},
                        },
                    }),
                    json.dumps({"completed": False, "error": "insufficient"}),
                ]) + "\n",
                encoding="utf-8",
            )
            out = root / "out"
            uwfc.main([
                "--training-table", str(training_path),
                "--per-window-path", str(per_window),
                "--output-dir", str(out),
            ])
            payload = json.loads(
                (out / "under_walk_forward_certification.json").read_text(encoding="utf-8")
            )
            wf = payload["walk_forward"]
            self.assertEqual(wf["n_windows_total"], 3)
            self.assertEqual(wf["n_windows_completed"], 2)
            self.assertAlmostEqual(wf["test_brier_mean"], 0.20, places=4)


if __name__ == "__main__":
    unittest.main()
