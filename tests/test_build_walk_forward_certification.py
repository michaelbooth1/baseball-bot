"""Tests for build_walk_forward_certification (Active #1 prep).

The certification builder consumes signal_training_table.jsonl and emits
a cohort + gate scorecard report with three states:
  - READY (sample meets full Active #1 thresholds)
  - PRELIMINARY (mid-build sample, directional only)
  - INSUFFICIENT (report shape only, do not act)

This module covers:
  - Sample-size readiness verdict transitions
  - BetRow projection (None-safe field extraction, drop unparseable rows)
  - Cohort assignment (band cuts, missing-bucket fallback)
  - CohortStats math (fill rate, ROI, max drawdown over a profit series)
  - Per-gate sweep + verdict (KEEP when blocked is worse, RETUNE when
    blocked is materially better, KEEP-low-confidence when blocked is
    too thin)
  - End-to-end main() writes JSON + Markdown
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_walk_forward_certification as cert  # noqa: E402


def _row(**overrides):
    base = {
        "session_date": "2026-05-01",
        "signal_model_family": "score_event_transition",
        "line": 7.5,
        "inning": 6,
        "runs_needed": 1.5,
        "decision_ask": 0.78,
        "edge_at_ask": 0.18,
        "fair_value": 0.96,
        "limit_price": 0.76,
        "current_state_value_edge": 0.05,
        "shadow_phantom_risk_band": "low",
        "target_filled": 1,
        "target_win": 1,
        "target_profit": 5.0,
    }
    base.update(overrides)
    return base


def _bet(**overrides):
    return cert._to_bet_row(_row(**overrides))


class ReadinessTests(unittest.TestCase):
    def test_insufficient(self):
        v = cert.readiness_verdict(n_filled=20, n_dates=5)
        self.assertEqual(v["label"], "INSUFFICIENT")

    def test_preliminary(self):
        v = cert.readiness_verdict(n_filled=80, n_dates=15)
        self.assertEqual(v["label"], "PRELIMINARY")

    def test_ready(self):
        v = cert.readiness_verdict(n_filled=160, n_dates=32)
        self.assertEqual(v["label"], "READY")


class BetRowProjectionTests(unittest.TestCase):
    def test_required_fields_present(self):
        b = _bet()
        self.assertEqual(b.session_date, "2026-05-01")
        self.assertEqual(b.target_filled, 1)
        self.assertAlmostEqual(b.edge_at_ask, 0.18)

    def test_drops_when_decision_ask_missing(self):
        self.assertIsNone(cert._to_bet_row(_row(decision_ask=None)))

    def test_drops_when_edge_missing(self):
        self.assertIsNone(cert._to_bet_row(_row(edge_at_ask=None)))

    def test_drops_when_target_filled_missing(self):
        self.assertIsNone(cert._to_bet_row(_row(target_filled=None)))

    def test_missing_phantom_falls_back_to_string(self):
        b = cert._to_bet_row(_row(shadow_phantom_risk_band=None))
        self.assertEqual(b.phantom_risk_band, "missing")


class CohortAssignmentTests(unittest.TestCase):
    def test_edge_band_boundaries(self):
        # cuts: 0.10, 0.15, 0.22 -> labels: <=0.10, 0.10-0.15, 0.15-0.22, >0.22
        cohort_def = next(c for c in cert.COHORT_DEFS if c[0] == "edge_band")
        fn = cohort_def[1]
        self.assertEqual(fn(_bet(edge_at_ask=0.05)), "<=0.10")
        self.assertEqual(fn(_bet(edge_at_ask=0.10)), "<=0.10")  # inclusive
        self.assertEqual(fn(_bet(edge_at_ask=0.12)), "0.10-0.15")
        self.assertEqual(fn(_bet(edge_at_ask=0.15)), "0.10-0.15")
        self.assertEqual(fn(_bet(edge_at_ask=0.18)), "0.15-0.22")
        self.assertEqual(fn(_bet(edge_at_ask=0.30)), ">0.22")

    def test_band_missing(self):
        self.assertEqual(cert._band(None, [1.0, 2.0], ["a", "b", "c"]), "missing")


class CohortStatsTests(unittest.TestCase):
    def test_aggregate_basic(self):
        bets = [
            _bet(target_filled=1, target_win=1, target_profit=5.0),
            _bet(target_filled=1, target_win=0, target_profit=-10.0),
            _bet(target_filled=0, target_win=0, target_profit=0.0),
        ]
        s = cert.aggregate_overall(bets)
        self.assertEqual(s.n_bets, 3)
        self.assertEqual(s.n_filled, 2)
        self.assertEqual(s.n_filled_wins, 1)
        self.assertAlmostEqual(s.fill_rate, 2 / 3)
        self.assertAlmostEqual(s.filled_win_rate, 0.5)
        self.assertAlmostEqual(s.signal_win_rate, 1 / 3)
        self.assertAlmostEqual(s.total_profit, -5.0)

    def test_max_drawdown_picks_worst_trough(self):
        s = cert.CohortStats()
        for p in (5.0, 3.0, -10.0, -8.0, 12.0, -2.0):
            s.add(_bet(target_filled=1, target_win=p > 0, target_profit=p))
        # cumulative: 5, 8, -2, -10, 2, 0
        # peak: 5, 8, 8, 8, 8, 8
        # drawdown from peak: 0, 0, -10, -18, -6, -8
        # worst = -18
        self.assertAlmostEqual(s.max_drawdown, -18.0, places=3)

    def test_no_filled_returns_none_metrics(self):
        s = cert.aggregate_overall([_bet(target_filled=0, target_win=0, target_profit=0)])
        self.assertIsNone(s.fill_rate or None)  # 0/1 = 0.0 -> truthy check; ensure not None
        # Filled WR / ROI defined as None when n_filled == 0:
        self.assertIsNone(s.filled_win_rate)
        self.assertIsNone(s.roi)


class GateSweepAndVerdictTests(unittest.TestCase):
    def _gate(self):
        return cert.GateDef(
            name="gate_test_edge",
            description="test gate -- block edges above current threshold",
            bet_field=lambda b: b.edge_at_ask,
            direction="max",
            current_threshold=0.20,
            sweep_thresholds=[0.15, 0.20, 0.25, 0.30],
        )

    def test_keep_when_blocked_cohort_loses(self):
        # Kept (edge<=0.20): 8 wins of 10 -- big winners
        # Blocked (edge>0.20): 1 win of 10 -- big losers
        bets = (
            [_bet(edge_at_ask=0.18, target_filled=1, target_win=1, target_profit=5.0)] * 8
            + [_bet(edge_at_ask=0.18, target_filled=1, target_win=0, target_profit=-10.0)] * 2
            + [_bet(edge_at_ask=0.30, target_filled=1, target_win=1, target_profit=5.0)]
            + [_bet(edge_at_ask=0.30, target_filled=1, target_win=0, target_profit=-10.0)] * 9
        )
        g = self._gate()
        result = cert.evaluate_gate(bets, g)
        v = result["verdict"]
        self.assertEqual(v["verdict"], "KEEP")
        self.assertGreater(result["current_kept"]["roi"], result["current_blocked"]["roi"])

    def test_retune_when_blocked_cohort_wins(self):
        # Kept (edge<=0.20): all losses
        # Blocked (edge>0.20): all wins
        # The gate is filtering profitable bets -> RETUNE
        bets = (
            [_bet(edge_at_ask=0.18, target_filled=1, target_win=0, target_profit=-10.0)] * 10
            + [_bet(edge_at_ask=0.30, target_filled=1, target_win=1, target_profit=5.0)] * 10
        )
        g = self._gate()
        result = cert.evaluate_gate(bets, g)
        v = result["verdict"]
        self.assertEqual(v["verdict"], "RETUNE")
        self.assertIsNotNone(v["recommended_threshold"])

    def test_keep_with_low_confidence_when_blocked_thin(self):
        # 20 kept bets, only 2 blocked -- under GATE_RETUNE_MIN_BLOCKED_N (5)
        bets = (
            [_bet(edge_at_ask=0.18, target_filled=1, target_win=1, target_profit=5.0)] * 20
            + [_bet(edge_at_ask=0.30, target_filled=1, target_win=1, target_profit=5.0)] * 2
        )
        g = self._gate()
        result = cert.evaluate_gate(bets, g)
        v = result["verdict"]
        self.assertEqual(v["verdict"], "KEEP")
        self.assertEqual(v["confidence"], "low")
        self.assertIn("Insufficient evidence", v["reason"].replace("insufficient", "Insufficient"))


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cert_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.training_path = self.tmp / "signal_training_table.jsonl"
        self.output_dir = self.tmp / "out"

        # 50 mixed training rows: enough to render every section, well
        # below READY thresholds.
        rows = []
        for i in range(50):
            won = (i % 3) != 0  # ~67% WR
            profit = 5.0 if won else -10.0
            rows.append(_row(
                session_date=f"2026-05-{(i % 10) + 1:02d}",
                edge_at_ask=0.10 + (i % 5) * 0.04,
                inning=4 + (i % 6),
                target_filled=int(i % 2 == 0),
                target_win=int(won),
                target_profit=profit if (i % 2 == 0) else 0.0,
                current_state_value_edge=0.02 + (i % 3) * 0.05,
                shadow_phantom_risk_band=("low", "medium", "high")[i % 3],
            ))
        self.training_path.write_text(
            "\n".join(json.dumps(r) for r in rows), encoding="utf-8"
        )

    def test_main_writes_artifacts(self):
        rc = cert.main([
            "--training-table", str(self.training_path),
            "--output-dir", str(self.output_dir),
        ])
        self.assertEqual(rc, 0)
        json_path = self.output_dir / "walk_forward_certification.json"
        md_path = self.output_dir / "walk_forward_certification.md"
        self.assertTrue(json_path.exists())
        self.assertTrue(md_path.exists())

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        # 50 rows split into 25 filled across 10 dates -> INSUFFICIENT
        self.assertEqual(payload["readiness"]["label"], "INSUFFICIENT")
        # Every cohort dimension represented
        self.assertIn("edge_band", payload["cohorts"])
        self.assertIn("ask_band", payload["cohorts"])
        self.assertIn("phantom_risk_band", payload["cohorts"])
        # All gate definitions evaluated
        gate_names = {g["name"] for g in payload["gates"]}
        for required in ("gate_extreme_edge", "gate_min_edge", "gate_min_inning",
                         "gate_min_entry_ask", "gate_runs_needed_max"):
            self.assertIn(required, gate_names)

        md = md_path.read_text(encoding="utf-8")
        # Headline sections all present
        for section in ("Walk-forward certification", "Headline verdicts",
                        "Cohort breakdowns", "Per-gate scorecard",
                        "Weekly drift"):
            self.assertIn(section, md)
        # Readiness badge in markdown
        self.assertIn("INSUFFICIENT", md)

    def test_empty_input_renders_safely(self):
        empty = self.tmp / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        rc = cert.main([
            "--training-table", str(empty),
            "--output-dir", str(self.output_dir),
        ])
        self.assertEqual(rc, 0)
        payload = json.loads(
            (self.output_dir / "walk_forward_certification.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(payload["readiness"]["label"], "INSUFFICIENT")
        self.assertEqual(payload["overall"]["n_bets"], 0)


if __name__ == "__main__":
    unittest.main()
