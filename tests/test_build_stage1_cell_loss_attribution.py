"""Tests for build_stage1_cell_loss_attribution.

This builder drills today's Active #10 finding (Stage-1 owns ~100% of
the aggregate bias) across Stage-1-internal cohort dimensions
(fallback level, line fallback mode, used_fallback, sample-size
bucket, Poisson-empirical gap).

Coverage:
  - project_bet filter (target_filled/target_win required;
    base_fair_value out-of-range rejected)
  - Cohort buckets (fallback level / line fallback / used_fallback /
    n / poisson-empirical gap)
  - aggregate math (bias, fallback_rate, mean poisson-empirical gap,
    n_with_empirical counter)
  - build_top_culprits (min n, min |bias|, min share, sort order;
    helpful cohorts excluded)
  - Window slicing (anchor on latest)
  - End-to-end main writes JSON + Markdown + handles empty input
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_stage1_cell_loss_attribution as s1la  # noqa: E402


def _row(**overrides):
    base = {
        "session_date": "2026-05-15",
        "bet_id": "bet-1",
        "target_filled": 1,
        "target_win": 1,
        "base_fair_value": 0.7,
        "inferred_state_fallback_level": 0,
        "inferred_state_line_fallback_mode": "exact",
        "inferred_state_used_fallback": False,
        "inferred_state_n": 200,
        "inferred_state_poisson_minus_empirical": 0.02,
        "inferred_state_base_empirical": 0.68,
        "inferred_state_cell_key": "0_0_5_T_0_0",
    }
    base.update(overrides)
    return base


def _bet(**overrides):
    return s1la.project_bet(_row(**overrides))


class ProjectBetTests(unittest.TestCase):
    def test_requires_target_filled(self):
        self.assertIsNone(s1la.project_bet(_row(target_filled=0)))

    def test_requires_target_win_bool(self):
        self.assertIsNone(s1la.project_bet(_row(target_win=None)))

    def test_requires_base_fair_value(self):
        self.assertIsNone(s1la.project_bet(_row(base_fair_value=None)))

    def test_rejects_base_fair_value_out_of_range(self):
        # base_fv outside [0, 1]
        self.assertIsNone(s1la.project_bet(_row(base_fair_value=1.5)))
        self.assertIsNone(s1la.project_bet(_row(base_fair_value=-0.1)))

    def test_accepts_with_missing_stage1_metadata(self):
        # Older rows from before inferred_state_* fields shipped
        # should still project; they'll surface as "missing" in
        # cohort cuts.
        row = _row()
        for k in (
            "inferred_state_fallback_level",
            "inferred_state_line_fallback_mode",
            "inferred_state_used_fallback",
            "inferred_state_n",
            "inferred_state_poisson_minus_empirical",
        ):
            del row[k]
        bet = s1la.project_bet(row)
        self.assertIsNotNone(bet)
        self.assertIsNone(bet.fallback_level)
        self.assertIsNone(bet.used_fallback)
        self.assertIsNone(bet.poisson_minus_empirical)

    def test_bias_signed_correctly(self):
        # p0=0.7, won=0 -> bias=+0.7 (over-predicted)
        self.assertAlmostEqual(
            _bet(target_win=0, base_fair_value=0.7).bias, 0.7, places=5,
        )
        # p0=0.7, won=1 -> bias=-0.3 (under-predicted)
        self.assertAlmostEqual(
            _bet(target_win=1, base_fair_value=0.7).bias, -0.3, places=5,
        )


class CohortBucketTests(unittest.TestCase):
    def test_fallback_level_buckets(self):
        self.assertEqual(s1la._bucket_fallback_level(
            _bet(inferred_state_fallback_level=0)),
            "level_0_exact")
        self.assertEqual(s1la._bucket_fallback_level(
            _bet(inferred_state_fallback_level=1)),
            "level_1_fallback")
        self.assertEqual(s1la._bucket_fallback_level(
            _bet(inferred_state_fallback_level=2)),
            "level_2plus_fallback")
        self.assertEqual(s1la._bucket_fallback_level(
            _bet(inferred_state_fallback_level=99)),
            "level_2plus_fallback")
        # Missing field
        row = _row()
        del row["inferred_state_fallback_level"]
        self.assertEqual(
            s1la._bucket_fallback_level(s1la.project_bet(row)),
            "missing",
        )

    def test_used_fallback_buckets(self):
        self.assertEqual(s1la._bucket_used_fallback(
            _bet(inferred_state_used_fallback=True)),
            "fallback_used")
        self.assertEqual(s1la._bucket_used_fallback(
            _bet(inferred_state_used_fallback=False)),
            "exact_match")

    def test_n_buckets(self):
        self.assertEqual(s1la._bucket_n(_bet(inferred_state_n=10)), "<50")
        self.assertEqual(s1la._bucket_n(_bet(inferred_state_n=50)), "50-200")
        self.assertEqual(s1la._bucket_n(_bet(inferred_state_n=199)), "50-200")
        self.assertEqual(s1la._bucket_n(_bet(inferred_state_n=200)), "200-1000")
        self.assertEqual(s1la._bucket_n(_bet(inferred_state_n=1500)), ">=1000")

    def test_poisson_empirical_gap_uses_absolute(self):
        # Symmetric: |0.15| and |-0.15| both bucket to 0.10-0.20
        self.assertEqual(
            s1la._bucket_poisson_empirical_gap(
                _bet(inferred_state_poisson_minus_empirical=0.15),
            ), "0.10-0.20")
        self.assertEqual(
            s1la._bucket_poisson_empirical_gap(
                _bet(inferred_state_poisson_minus_empirical=-0.15),
            ), "0.10-0.20")

    def test_line_fallback_mode_passes_through(self):
        self.assertEqual(s1la._bucket_line_fallback_mode(
            _bet(inferred_state_line_fallback_mode="exact")),
            "exact")
        self.assertEqual(s1la._bucket_line_fallback_mode(
            _bet(inferred_state_line_fallback_mode="extrapolate_low")),
            "extrapolate_low")


class AggregateTests(unittest.TestCase):
    def test_empty_returns_none_fields(self):
        agg = s1la.aggregate([])
        self.assertEqual(agg["n"], 0)
        for k in (
            "mean_p0", "mean_won", "stage1_bias",
            "mean_poisson_minus_empirical",
            "mean_inferred_state_n", "fallback_rate",
        ):
            self.assertIsNone(agg[k])

    def test_over_prediction_bias_positive(self):
        # p0=0.8, won mix: 2 wins / 3 losses -> mean_won=0.4
        bets = (
            [_bet(target_win=1, base_fair_value=0.8)] * 2
            + [_bet(target_win=0, base_fair_value=0.8)] * 3
        )
        agg = s1la.aggregate(bets)
        self.assertEqual(agg["mean_p0"], 0.8)
        self.assertEqual(agg["mean_won"], 0.4)
        self.assertEqual(agg["stage1_bias"], 0.4)

    def test_fallback_rate_computed_only_on_known(self):
        # 3 bets with known fallback (2 True, 1 False) + 1 missing
        b1 = _bet(inferred_state_used_fallback=True)
        b2 = _bet(inferred_state_used_fallback=True)
        b3 = _bet(inferred_state_used_fallback=False)
        # Missing one
        row = _row()
        del row["inferred_state_used_fallback"]
        b4 = s1la.project_bet(row)
        agg = s1la.aggregate([b1, b2, b3, b4])
        # 2 of 3 known are fallback
        self.assertAlmostEqual(agg["fallback_rate"], 2 / 3, places=4)

    def test_n_with_empirical_counts_only_present(self):
        b1 = _bet(inferred_state_poisson_minus_empirical=0.05)
        b2 = _bet(inferred_state_poisson_minus_empirical=0.10)
        row = _row()
        del row["inferred_state_poisson_minus_empirical"]
        b3 = s1la.project_bet(row)
        agg = s1la.aggregate([b1, b2, b3])
        self.assertEqual(agg["n_with_empirical"], 2)
        self.assertAlmostEqual(
            agg["mean_poisson_minus_empirical"], 0.075, places=4,
        )


class TopCulpritsTests(unittest.TestCase):
    def _by_cohort(self, *cohorts):
        """Build a by_cohort dict from (dim, bucket, agg) tuples."""
        out = {}
        for dim, bucket, agg in cohorts:
            out.setdefault(dim, {})[bucket] = agg
        return out

    @staticmethod
    def _agg(*, n=10, bias=0.30, mean_p0=0.85, mean_won=0.55,
             pe_gap=None, n_with_emp=0,
             cell_n=200.0, fallback_rate=0.5):
        return {
            "n": n,
            "mean_p0": mean_p0,
            "mean_won": mean_won,
            "stage1_bias": bias,
            "abs_stage1_bias": abs(bias),
            "mean_poisson_minus_empirical": pe_gap,
            "n_with_empirical": n_with_emp,
            "mean_inferred_state_n": cell_n,
            "fallback_rate": fallback_rate,
        }

    def test_empty_aggregate_returns_empty(self):
        out = s1la.build_top_culprits({}, aggregate_bias=None)
        self.assertEqual(out, [])

    def test_aggregate_bias_below_floor_returns_empty(self):
        cohorts = self._by_cohort(
            ("dim", "bucket", self._agg(n=20, bias=0.10)),
        )
        out = s1la.build_top_culprits(cohorts, aggregate_bias=0.01)
        self.assertEqual(out, [])

    def test_cohort_below_min_n_excluded(self):
        cohorts = self._by_cohort(
            ("dim", "thin", self._agg(n=3, bias=0.40)),
            ("dim", "thick", self._agg(n=20, bias=0.30)),
        )
        out = s1la.build_top_culprits(cohorts, aggregate_bias=0.20)
        names = [(c["dimension"], c["bucket"]) for c in out]
        self.assertNotIn(("dim", "thin"), names)
        self.assertIn(("dim", "thick"), names)

    def test_missing_bucket_excluded(self):
        cohorts = self._by_cohort(
            ("dim", "missing", self._agg(n=20, bias=0.40)),
            ("dim", "real", self._agg(n=20, bias=0.30)),
        )
        out = s1la.build_top_culprits(cohorts, aggregate_bias=0.20)
        names = [c["bucket"] for c in out]
        self.assertNotIn("missing", names)

    def test_helpful_cohort_excluded(self):
        # Aggregate bias is +0.30 (over-predicting), but this cohort
        # has bias=-0.20 (under-predicting). Cohort is HELPING --
        # not a culprit.
        cohorts = self._by_cohort(
            ("dim", "helpful", self._agg(n=20, bias=-0.20)),
            ("dim", "culprit", self._agg(n=20, bias=0.40)),
        )
        out = s1la.build_top_culprits(cohorts, aggregate_bias=0.30)
        names = [c["bucket"] for c in out]
        self.assertNotIn("helpful", names)
        self.assertIn("culprit", names)

    def test_ratio_can_exceed_one_when_cohort_amplifies(self):
        # Cohort bias > aggregate bias -> ratio > 1.0
        cohorts = self._by_cohort(
            ("dim", "amp", self._agg(n=20, bias=0.40)),
        )
        out = s1la.build_top_culprits(cohorts, aggregate_bias=0.20)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["stage1_bias_vs_aggregate_ratio"], 2.0)

    def test_sorted_by_bias_desc(self):
        cohorts = self._by_cohort(
            ("dim", "a", self._agg(n=20, bias=0.40)),
            ("dim", "b", self._agg(n=20, bias=0.30)),
            ("dim", "c", self._agg(n=20, bias=0.50)),
        )
        out = s1la.build_top_culprits(cohorts, aggregate_bias=0.30)
        self.assertEqual(
            [c["bucket"] for c in out], ["c", "a", "b"],
        )

    def test_rationale_text_includes_dimension_and_bucket(self):
        cohorts = self._by_cohort(
            ("stage1_used_fallback_bucket", "fallback_used",
             self._agg(n=20, bias=0.40)),
        )
        out = s1la.build_top_culprits(cohorts, aggregate_bias=0.30)
        self.assertEqual(len(out), 1)
        text = out[0]["rationale"]
        self.assertIn("stage1_used_fallback_bucket", text)
        self.assertIn("fallback_used", text)
        self.assertIn("over-predicting", text)

    def test_rationale_flags_poisson_smoothing_when_gap_present(self):
        cohorts = self._by_cohort(
            ("dim", "high_gap", self._agg(
                n=20, bias=0.30, pe_gap=0.15, n_with_emp=15,
            )),
        )
        out = s1la.build_top_culprits(cohorts, aggregate_bias=0.30)
        text = out[0]["rationale"]
        self.assertIn("Poisson smoothing inflates", text)


class WindowSlicingTests(unittest.TestCase):
    def test_anchor_on_latest(self):
        bets = [
            _bet(session_date="2026-04-20"),
            _bet(session_date="2026-05-01"),
            _bet(session_date="2026-05-15"),
        ]
        windows = s1la.slice_windows(bets)
        d7 = {b.session_date for b in windows["trailing_7d"]}
        self.assertEqual(d7, {"2026-05-15"})

    def test_empty(self):
        windows = s1la.slice_windows([])
        for w in ("all", "trailing_30d", "trailing_7d"):
            self.assertEqual(windows[w], [])


class PayloadBuildTests(unittest.TestCase):
    def test_payload_has_required_keys(self):
        payload = s1la.build_payload([_bet()])
        for k in (
            "schema_version", "generated_at_utc",
            "n_bets", "date_span", "config", "windows",
        ):
            self.assertIn(k, payload)
        for w in ("all", "trailing_30d", "trailing_7d"):
            self.assertIn(w, payload["windows"])
            self.assertIn("aggregate", payload["windows"][w])
            self.assertIn("by_cohort", payload["windows"][w])
            self.assertIn("top_culprits", payload["windows"][w])

    def test_payload_n_bets_matches(self):
        payload = s1la.build_payload([_bet() for _ in range(7)])
        self.assertEqual(payload["n_bets"], 7)


class MarkdownTests(unittest.TestCase):
    def test_markdown_includes_dimension_names(self):
        payload = s1la.build_payload([_bet()])
        md = s1la.render_markdown(payload)
        for dim, _ in s1la.COHORT_DIMENSIONS:
            self.assertIn(dim, md)

    def test_empty_payload_does_not_crash(self):
        md = s1la.render_markdown(s1la.build_payload([]))
        self.assertIn("Stage-1 cell loss attribution", md)


class EndToEndTests(unittest.TestCase):
    def test_main_writes_json_and_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            jsonl = tmp_dir / "training.jsonl"
            with open(jsonl, "w", encoding="utf-8") as f:
                for i in range(3):
                    f.write(json.dumps(_row(target_win=i % 2)) + "\n")
            out_dir = tmp_dir / "out"
            rc = s1la.main([
                "--training-table", str(jsonl),
                "--output-dir", str(out_dir),
            ])
            self.assertEqual(rc, 0)
            j = json.loads(
                (out_dir / "stage1_cell_loss_attribution.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(j["n_bets"], 3)
            self.assertTrue(
                (out_dir / "stage1_cell_loss_attribution.md").exists()
            )

    def test_main_empty_input_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "training.jsonl"
            jsonl.write_text("", encoding="utf-8")
            out_dir = Path(tmp) / "out"
            rc = s1la.main([
                "--training-table", str(jsonl),
                "--output-dir", str(out_dir),
            ])
            self.assertEqual(rc, 0)
            j = json.loads(
                (out_dir / "stage1_cell_loss_attribution.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(j["n_bets"], 0)


if __name__ == "__main__":
    unittest.main()
