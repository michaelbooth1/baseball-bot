"""Tests for build_loss_attribution_report (Active #10).

The loss attribution builder decomposes each filled+settled bet's
calibrated fair value (`p3`) into per-stage probability contributions
via the logit-additive chain, then aggregates across the trailing
window to surface which stage(s) own the largest share of the bias
(= mean_p3 - mean_won).

Coverage:
  - Logit / sigmoid math identity (verified on real data: chain
    matches fair_value to 0.001 on all 87 production bets)
  - decompose_bet: filter logic + 4-stage probability decomposition
  - aggregate_decompositions: stage_contributions math (in-bias
    direction projection, attribution_share via positive-sum
    normalization, top_culprits ranking)
  - slice_windows: anchored on LATEST session_date in rows
  - aggregate_by_cohort: per-(dim, bucket) projection
  - Schema completeness (all 4 stages + bias direction tag)
  - End-to-end main() writes JSON + Markdown
"""

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_loss_attribution_report as lar  # noqa: E402


def _row(**overrides) -> Dict[str, Any]:
    base = {
        "session_date": "2026-05-15",
        "bet_id": "bet-1",
        "target_filled": 1,
        "target_win": 1,
        "target_profit": 5.0,
        "base_fair_value": 0.6,
        "fair_value": 0.65,
        "stage2_run_env_delta": 0.05,
        "team_offense_delta": 0.10,
        "decision_ask": 0.55,
        "inning": 6,
        "line": 8.5,
        "edge_at_ask": 0.10,
        "current_state_value_edge": 0.05,
    }
    base.update(overrides)
    return base


def _bet(**overrides):
    return lar.decompose_bet(_row(**overrides))


class LogitMathTests(unittest.TestCase):
    def test_logit_sigmoid_roundtrip(self):
        for p in (0.001, 0.1, 0.5, 0.7, 0.95, 0.999):
            self.assertAlmostEqual(lar._sigmoid(lar._logit(p)), p, places=5)

    def test_logit_clamps_extremes(self):
        # 0.0 and 1.0 should not produce inf
        self.assertTrue(math.isfinite(lar._logit(0.0)))
        self.assertTrue(math.isfinite(lar._logit(1.0)))

    def test_logit_at_half_is_zero(self):
        self.assertAlmostEqual(lar._logit(0.5), 0.0, places=5)

    def test_logit_additive_chain_matches_construction(self):
        # If p3 is constructed from the chain, decompose_bet should
        # produce p3 == fair_value to 4 decimals.
        p0 = 0.65
        s2 = 0.20
        s3 = -0.10
        p3 = lar._sigmoid(lar._logit(p0) + s2 + s3)
        bet = _bet(base_fair_value=p0, stage2_run_env_delta=s2,
                   team_offense_delta=s3, fair_value=p3)
        self.assertAlmostEqual(bet.p3, p3, places=4)
        # Stage-3 alone shift in prob space
        p1_expected = lar._sigmoid(lar._logit(p0) + s2)
        self.assertAlmostEqual(bet.p1, p1_expected, places=4)
        # Calibration residual should be ~0 since chain is exact
        self.assertAlmostEqual(bet.stage_shift_calibration, 0.0, places=4)


class DecomposeBetFilterTests(unittest.TestCase):
    def test_requires_target_filled(self):
        self.assertIsNone(lar.decompose_bet(_row(target_filled=0)))

    def test_requires_target_win_bool(self):
        self.assertIsNone(lar.decompose_bet(_row(target_win=None)))

    def test_requires_base_fair_value(self):
        self.assertIsNone(lar.decompose_bet(_row(base_fair_value=None)))

    def test_requires_fair_value(self):
        self.assertIsNone(lar.decompose_bet(_row(fair_value=None)))

    def test_requires_stage2_delta(self):
        self.assertIsNone(lar.decompose_bet(_row(stage2_run_env_delta=None)))

    def test_requires_stage3_delta(self):
        self.assertIsNone(lar.decompose_bet(_row(team_offense_delta=None)))

    def test_rejects_fair_value_out_of_range(self):
        self.assertIsNone(lar.decompose_bet(_row(fair_value=1.0)))
        self.assertIsNone(lar.decompose_bet(_row(base_fair_value=0.0)))

    def test_accepts_target_win_zero(self):
        bet = _bet(target_win=0)
        self.assertEqual(bet.won, 0)
        # bias = p3 - 0 = +p3
        self.assertAlmostEqual(bet.bias, bet.p3, places=5)


class StageShiftDecompositionTests(unittest.TestCase):
    def test_shifts_sum_to_p3_minus_half(self):
        bet = _bet()
        s_sum = (
            bet.stage_shift_stage1
            + bet.stage_shift_stage2
            + bet.stage_shift_stage3
            + bet.stage_shift_calibration
        )
        self.assertAlmostEqual(s_sum, bet.p3 - 0.5, places=5)

    def test_stage1_shift_is_p0_minus_half(self):
        bet = _bet(base_fair_value=0.80)
        self.assertAlmostEqual(bet.stage_shift_stage1, 0.30, places=5)

    def test_stage2_shift_positive_when_s2_delta_positive(self):
        bet = _bet(base_fair_value=0.5, stage2_run_env_delta=0.5)
        # logit(0.5)=0; +0.5 => sigmoid(0.5)~0.622
        self.assertGreater(bet.p1, bet.p0)
        self.assertGreater(bet.stage_shift_stage2, 0)

    def test_stage3_shift_negative_when_s3_delta_negative(self):
        # Reset all to neutrals, then make s3 negative and check sign.
        bet = _bet(base_fair_value=0.5, stage2_run_env_delta=0.0,
                   team_offense_delta=-0.5, fair_value=0.378)
        self.assertLess(bet.p2, bet.p1)
        self.assertLess(bet.stage_shift_stage3, 0)


class AggregateDecompositionTests(unittest.TestCase):
    def test_empty_returns_none_stats(self):
        agg = lar.aggregate_decompositions([])
        self.assertEqual(agg["n"], 0)
        self.assertIsNone(agg["bias"])
        self.assertEqual(agg["top_culprits"], [])
        for stage in lar.STAGE_NAMES:
            self.assertIsNone(agg["stage_contributions"][stage]["mean_shift"])

    def test_bias_direction_positive_when_overpredict(self):
        # p3=0.8 but won=0 → bias=0.8
        bets = [_bet(base_fair_value=0.5, stage2_run_env_delta=1.39,
                     team_offense_delta=0.0, fair_value=0.8, target_win=0)] * 3
        agg = lar.aggregate_decompositions(bets)
        self.assertEqual(agg["bias_direction"], "over_predicting")
        self.assertGreater(agg["bias"], 0)

    def test_bias_direction_negative_when_underpredict(self):
        bets = [_bet(base_fair_value=0.3, stage2_run_env_delta=-1.0,
                     team_offense_delta=0.0, fair_value=0.131,
                     target_win=1)] * 3
        agg = lar.aggregate_decompositions(bets)
        self.assertEqual(agg["bias_direction"], "under_predicting")
        self.assertLess(agg["bias"], 0)

    def test_attribution_share_zero_for_helpful_stage(self):
        # Construct bets where Stage-2 PULLS DOWN (negative shift) and
        # the aggregate is over-predicting. Stage-2's contribution in
        # bias direction (positive) is negative => share should be 0.
        bets = [
            _bet(base_fair_value=0.9, stage2_run_env_delta=-0.3,
                 team_offense_delta=0.0,
                 fair_value=lar._sigmoid(lar._logit(0.9) - 0.3),
                 target_win=0),
        ] * 5
        agg = lar.aggregate_decompositions(bets)
        # Bias > 0 (over-predicting). Stage-2 mean shift is negative
        # (helping correct). Its in_bias_dir is negative => share=0.
        s2 = agg["stage_contributions"]["stage2_run_env"]
        self.assertLess(s2["mean_shift"], 0)
        self.assertEqual(s2["attribution_share"], 0.0)

    def test_attribution_share_sums_to_one(self):
        # Multiple stages all pushing same direction; positive shares
        # should sum to approximately 1.0.
        bets = [
            _bet(base_fair_value=0.65, stage2_run_env_delta=0.30,
                 team_offense_delta=0.30,
                 fair_value=lar._sigmoid(lar._logit(0.65) + 0.30 + 0.30),
                 target_win=0),
        ] * 5
        agg = lar.aggregate_decompositions(bets)
        positive_shares = sum(
            (c.get("attribution_share") or 0.0)
            for c in agg["stage_contributions"].values()
            if (c.get("attribution_share") or 0.0) > 0
        )
        self.assertAlmostEqual(positive_shares, 1.0, places=2)

    def test_top_culprits_filtered_by_min_share(self):
        # 4 stages each contributing 25% would all clear MIN_SHARE=0.25
        # exactly. Verify with 3-vs-1 split that small contributors get
        # excluded.
        bets = [
            _bet(base_fair_value=0.85, stage2_run_env_delta=0.01,
                 team_offense_delta=0.01,
                 fair_value=lar._sigmoid(lar._logit(0.85) + 0.02),
                 target_win=0),
        ] * 5
        agg = lar.aggregate_decompositions(bets)
        # Stage-1 has the big share; others are noise
        names = [c["stage"] for c in agg["top_culprits"]]
        self.assertIn("stage1_baseline", names)
        self.assertNotIn("calibration", names)


class WindowSlicingTests(unittest.TestCase):
    def test_anchored_on_latest_session_date(self):
        bets = [
            _bet(session_date="2026-04-20"),
            _bet(session_date="2026-05-01"),
            _bet(session_date="2026-05-15"),
        ]
        windows = lar.slice_windows(bets)
        # trailing_7d window = 2026-05-09 → 2026-05-15
        d7 = [b.session_date for b in windows["trailing_7d"]]
        self.assertEqual(d7, ["2026-05-15"])

    def test_empty_input(self):
        windows = lar.slice_windows([])
        for w in ("all", "trailing_30d", "trailing_7d"):
            self.assertEqual(windows[w], [])


class CohortBreakdownTests(unittest.TestCase):
    def test_per_cohort_aggregation(self):
        bets = [
            _bet(inning=5, target_win=1, base_fair_value=0.6,
                 stage2_run_env_delta=0.0, team_offense_delta=0.0,
                 fair_value=0.6),
            _bet(inning=8, target_win=0, base_fair_value=0.9,
                 stage2_run_env_delta=0.0, team_offense_delta=0.0,
                 fair_value=0.9),
        ]
        cohorts = lar.aggregate_by_cohort(bets)
        inn = cohorts["inning_bucket"]
        self.assertIn("<=5", inn)
        self.assertIn(">=8", inn)
        # >=8 bucket should show large positive bias (p3=0.9, won=0)
        self.assertGreater(inn[">=8"]["bias"], 0.5)


class PayloadBuildTests(unittest.TestCase):
    def test_payload_has_required_keys(self):
        bets = [_bet()]
        payload = lar.build_attribution_payload(bets)
        for key in (
            "schema_version", "generated_at_utc", "n_bets",
            "date_span", "config", "windows",
        ):
            self.assertIn(key, payload)
        for window in ("all", "trailing_30d", "trailing_7d"):
            self.assertIn(window, payload["windows"])
            w = payload["windows"][window]
            self.assertIn("aggregate", w)
            self.assertIn("by_cohort", w)

    def test_payload_n_bets_matches(self):
        bets = [_bet() for _ in range(5)]
        payload = lar.build_attribution_payload(bets)
        self.assertEqual(payload["n_bets"], 5)


class MarkdownRenderTests(unittest.TestCase):
    def test_markdown_includes_all_stage_names(self):
        payload = lar.build_attribution_payload([_bet()])
        md = lar.render_markdown(payload)
        for stage in lar.STAGE_NAMES:
            self.assertIn(stage, md)

    def test_markdown_includes_logit_chain_explanation(self):
        payload = lar.build_attribution_payload([_bet()])
        md = lar.render_markdown(payload)
        self.assertIn("logit", md)
        self.assertIn("sigmoid", md)

    def test_empty_payload_does_not_crash(self):
        md = lar.render_markdown(lar.build_attribution_payload([]))
        self.assertIn("Loss attribution report", md)


class EndToEndTests(unittest.TestCase):
    def test_main_writes_json_and_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            jsonl = tmp_dir / "training.jsonl"
            # Write 3 rows: 2 valid + 1 missing target_win (filter test)
            with open(jsonl, "w", encoding="utf-8") as f:
                f.write(json.dumps(_row(target_win=1)) + "\n")
                f.write(json.dumps(_row(target_win=0)) + "\n")
                f.write(json.dumps(_row(target_win=None)) + "\n")
            out_dir = tmp_dir / "out"
            rc = lar.main([
                "--training-table", str(jsonl),
                "--output-dir", str(out_dir),
            ])
            self.assertEqual(rc, 0)
            j = json.loads(
                (out_dir / "loss_attribution_report.json").read_text(
                    encoding="utf-8",
                ),
            )
            # 2 valid rows after filter
            self.assertEqual(j["n_bets"], 2)
            self.assertTrue((out_dir / "loss_attribution_report.md").exists())

    def test_main_empty_training_table_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "training.jsonl"
            jsonl.write_text("", encoding="utf-8")
            out_dir = Path(tmp) / "out"
            rc = lar.main([
                "--training-table", str(jsonl),
                "--output-dir", str(out_dir),
            ])
            self.assertEqual(rc, 0)
            j = json.loads(
                (out_dir / "loss_attribution_report.json").read_text(
                    encoding="utf-8",
                ),
            )
            self.assertEqual(j["n_bets"], 0)


if __name__ == "__main__":
    unittest.main()
