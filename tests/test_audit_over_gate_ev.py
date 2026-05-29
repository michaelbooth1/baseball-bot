"""Tests for scripts/analysis/audit_over_gate_ev.py."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import audit_over_gate_ev as a  # noqa: E402


class WilsonAndRoiTests(unittest.TestCase):
    def test_wilson_zero(self):
        self.assertEqual(a.wilson_interval(0, 0), (0.0, 0.0))

    def test_wilson_bounds(self):
        lo, hi = a.wilson_interval(7, 10)
        self.assertGreaterEqual(lo, 0.0)
        self.assertLessEqual(hi, 1.0)
        self.assertLess(lo, 0.7)
        self.assertGreater(hi, 0.7)

    def test_taker_roi(self):
        self.assertAlmostEqual(a.taker_roi(True, 0.5), 1.0)      # win at 0.50 -> +1.0
        self.assertAlmostEqual(a.taker_roi(False, 0.5), -1.0)    # loss -> -1.0
        self.assertAlmostEqual(a.taker_roi(True, 0.8), 0.25)


class ClassifyTests(unittest.TestCase):
    def test_insufficient_below_min_n(self):
        self.assertEqual(
            a.classify_gate(bettable_n=10, roi=-0.5, wr=0.3, breakeven=0.8, min_n=30),
            "INSUFFICIENT",
        )

    def test_plus_ev_when_blocked_loses(self):
        self.assertEqual(
            a.classify_gate(bettable_n=100, roi=-0.12, wr=0.65, breakeven=0.74, min_n=30),
            "PLUS_EV",
        )

    def test_minus_ev_when_blocked_wins(self):
        self.assertEqual(
            a.classify_gate(bettable_n=50, roi=0.106, wr=0.81, breakeven=0.72, min_n=30),
            "MINUS_EV",
        )

    def test_marginal_near_breakeven(self):
        self.assertEqual(
            a.classify_gate(bettable_n=500, roi=-0.03, wr=0.70, breakeven=0.72, min_n=30),
            "MARGINAL",
        )


class CohortAggTests(unittest.TestCase):
    def test_summarize_splits_bettable_subset(self):
        cohort = a.GateCohort("gate_x", rows=[
            (True, 0.60), (False, 0.60), (True, 0.60), (True, 0.60),  # bettable
            (True, 0.40),  # below min_ask -> excluded from bettable
            (False, None),  # no ask -> excluded from bettable
        ])
        s = a.summarize_gate(cohort, min_ask=0.55, min_n=3)
        self.assertEqual(s["n_blocked"], 6)
        self.assertEqual(s["bettable_n"], 4)
        self.assertAlmostEqual(s["bettable_avg_ask"], 0.60)
        self.assertAlmostEqual(s["bettable_over_win_rate"], 0.75)
        # roi: 3 wins @ (1/.6-1=.6667), 1 loss @ -1 -> (3*.6667-1)/4
        self.assertAlmostEqual(s["bettable_taker_roi"], (3 * (1 / 0.6 - 1) - 1) / 4, places=6)


class EndToEndTests(unittest.TestCase):
    def _write(self, path, rows):
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_main_writes_and_flags_minus_ev(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "cu"
            root.mkdir()
            # outcomes: game 1 line 7.5 over hit; game 2 line 7.5 over miss
            self._write(root / "d_outcomes.jsonl", [
                {"game_pk": 1, "line": "7.5", "over_hit": True},
                {"game_pk": 2, "line": "7.5", "over_hit": False},
            ])
            cands = []
            # a -EV gate: blocks mostly winners at ask 0.70 (40 unique states)
            for i in range(40):
                cands.append({
                    "side": "over", "decision_reason": "gate_stage2_suppression",
                    "game_pk": 1, "line": "7.5", "inning": 6, "inning_state": "Top",
                    "outs": i % 3, "away_score_before": i, "home_score_before": 0,
                    "decision_ask": 0.70,
                })
            # a +EV gate: blocks losers at ask 0.70 (40 unique states)
            for i in range(40):
                cands.append({
                    "side": "over", "decision_reason": "gate_runs_needed_max",
                    "game_pk": 2, "line": "7.5", "inning": 5, "inning_state": "Top",
                    "outs": i % 3, "away_score_before": i, "home_score_before": 0,
                    "decision_ask": 0.70,
                })
            # placed baseline
            for i in range(5):
                cands.append({
                    "side": "over", "decision_reason": "placed_bet",
                    "game_pk": 1, "line": "7.5", "inning": 7, "inning_state": "Bottom",
                    "outs": i % 3, "away_score_before": 100 + i, "home_score_before": 0,
                    "decision_ask": 0.70,
                })
            self._write(root / "d_candidates.jsonl", cands)

            out = Path(td) / "out"
            rc = a.main([
                "--roots", str(root), "--output-root", str(out),
                "--output-stem", "audit", "--min-n", "30",
            ])
            self.assertEqual(rc, 0)
            rep = json.loads((out / "audit.json").read_text(encoding="utf-8"))
            byr = {g["reason"]: g for g in rep["gates"]}
            # stage2 blocked game-1 overs that ALL hit -> -EV
            self.assertEqual(byr["gate_stage2_suppression"]["verdict"], "MINUS_EV")
            # runs_needed blocked game-2 overs that ALL missed -> +EV
            self.assertEqual(byr["gate_runs_needed_max"]["verdict"], "PLUS_EV")
            self.assertTrue((out / "audit.md").exists())

    def test_main_missing_root_is_empty_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            rc = a.main([
                "--roots", str(Path(td) / "nope"),
                "--output-root", str(out), "--output-stem", "audit",
            ])
            self.assertEqual(rc, 0)
            rep = json.loads((out / "audit.json").read_text(encoding="utf-8"))
            self.assertEqual(rep["outcomes_loaded"], 0)
            self.assertEqual(rep["gates"], [])


if __name__ == "__main__":
    unittest.main()
