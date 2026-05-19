"""Tests for build_stage1_shadow_override_report (Active #8 prep).

The shadow-override builder replays two candidate Stage-1 fixes
(empirical-when-available + block-deep-fallback) against the actual
training-table outcomes and shows their counterfactual impact.

Coverage:
  - project_bet filter (target_filled/target_win/FV chain required)
  - Alt A: empirical-when-available math (logit-additive composition;
    no-empirical row falls back to production)
  - Alt B: kept/blocked semantics by fallback_level threshold
  - aggregate_window bias deltas, counterfactual P&L math
  - Recommendation thresholds (Alt A: >=1pp + >=25% coverage; Alt B:
    >=$20 + >=3 blocked)
  - Window slicing anchored on latest session_date
  - End-to-end main + markdown render + empty input
"""

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_stage1_shadow_override_report as so  # noqa: E402


def _row(**overrides):
    base = {
        "session_date": "2026-05-15",
        "target_filled": 1,
        "target_win": 1,
        "target_profit": 5.0,
        "base_fair_value": 0.7,           # p0 poisson
        "fair_value": 0.7,                # p3 (no s2/s3/cal shift)
        "stage2_run_env_delta": 0.0,
        "team_offense_delta": 0.0,
        "inferred_state_base_empirical": 0.5,  # alt A would shift
        "inferred_state_fallback_level": 0,
        "inning": 6,
        "line": 8.5,
    }
    base.update(overrides)
    return base


def _bet(**overrides):
    return so.project_bet(_row(**overrides))


class ProjectBetTests(unittest.TestCase):
    def test_requires_target_filled(self):
        self.assertIsNone(so.project_bet(_row(target_filled=0)))

    def test_requires_target_win_bool(self):
        self.assertIsNone(so.project_bet(_row(target_win=None)))

    def test_requires_fv_chain(self):
        self.assertIsNone(so.project_bet(_row(base_fair_value=None)))
        self.assertIsNone(so.project_bet(_row(fair_value=None)))
        self.assertIsNone(so.project_bet(_row(stage2_run_env_delta=None)))
        self.assertIsNone(so.project_bet(_row(team_offense_delta=None)))

    def test_alt_a_unchanged_when_no_empirical(self):
        row = _row()
        del row["inferred_state_base_empirical"]
        bet = so.project_bet(row)
        self.assertFalse(bet.alt_a_changed)
        self.assertEqual(bet.p3_alt_a, bet.p3_prod)

    def test_alt_a_unchanged_when_empirical_out_of_range(self):
        # 0 or 1 isn't valid for the logit chain
        bet = so.project_bet(_row(inferred_state_base_empirical=0.0))
        self.assertFalse(bet.alt_a_changed)
        bet = so.project_bet(_row(inferred_state_base_empirical=1.0))
        self.assertFalse(bet.alt_a_changed)

    def test_alt_a_uses_empirical_in_logit_chain(self):
        # p0=0.8 poisson, s2=0.2, s3=-0.1; empirical=0.5
        # alt p3 should be sigmoid(logit(0.5) + 0.2 - 0.1) = sigmoid(0.1) ~ 0.525
        bet = so.project_bet(_row(
            base_fair_value=0.8,
            stage2_run_env_delta=0.2,
            team_offense_delta=-0.1,
            inferred_state_base_empirical=0.5,
            fair_value=0.85,  # production result; unused for alt
        ))
        self.assertTrue(bet.alt_a_changed)
        self.assertAlmostEqual(bet.p3_alt_a, 1.0 / (1.0 + math.exp(-0.1)), places=4)

    def test_alt_b_keeps_when_no_fallback_metadata(self):
        row = _row()
        del row["inferred_state_fallback_level"]
        bet = so.project_bet(row)
        self.assertTrue(bet.alt_b_kept)

    def test_alt_b_keeps_below_threshold(self):
        self.assertTrue(_bet(inferred_state_fallback_level=0).alt_b_kept)
        self.assertTrue(_bet(inferred_state_fallback_level=1).alt_b_kept)

    def test_alt_b_blocks_at_threshold(self):
        self.assertFalse(_bet(inferred_state_fallback_level=2).alt_b_kept)
        self.assertFalse(_bet(inferred_state_fallback_level=5).alt_b_kept)


class AggregateWindowTests(unittest.TestCase):
    def test_empty_window_returns_none_fields(self):
        agg = so.aggregate_window([])
        self.assertEqual(agg["n_bets"], 0)
        self.assertIsNone(agg["production"]["bias"])
        self.assertEqual(agg["recommendations"], [])

    def test_alt_a_bias_delta_when_empirical_helps(self):
        # 10 bets: poisson=0.9, empirical=0.5, won all 0
        # production bias = +0.9 per bet
        # alt_A bias = +0.5 per bet (5 of 5 changed)
        # improvement = +0.4 (40pp)
        bets = []
        for _ in range(10):
            bets.append(so.project_bet(_row(
                base_fair_value=0.9,
                fair_value=0.9,
                stage2_run_env_delta=0.0,
                team_offense_delta=0.0,
                inferred_state_base_empirical=0.5,
                target_win=0,
            )))
        agg = so.aggregate_window(bets)
        self.assertAlmostEqual(agg["production"]["bias"], 0.9, places=4)
        self.assertAlmostEqual(
            agg["alt_a_empirical_when_available"]["bias"], 0.5, places=4,
        )
        # Improvement = (0.9 - 0.5) * 100 = 40pp
        self.assertAlmostEqual(
            agg["alt_a_empirical_when_available"]["bias_delta_vs_prod_pp"],
            40.0, places=2,
        )

    def test_alt_b_counterfactual_positive_when_blocked_were_losers(self):
        # 8 bets: 5 kept (mix of W/L) + 3 blocked losers ($-30)
        # cf_delta = -(-30) = +30 (saved by blocking)
        kept_bets = [
            so.project_bet(_row(target_win=1, target_profit=5.0)),
            so.project_bet(_row(target_win=1, target_profit=5.0)),
            so.project_bet(_row(target_win=0, target_profit=-10.0)),
            so.project_bet(_row(target_win=1, target_profit=5.0)),
            so.project_bet(_row(target_win=0, target_profit=-10.0)),
        ]
        blocked_bets = [
            so.project_bet(_row(
                target_win=0, target_profit=-10.0,
                inferred_state_fallback_level=2,
            )),
            so.project_bet(_row(
                target_win=0, target_profit=-10.0,
                inferred_state_fallback_level=3,
            )),
            so.project_bet(_row(
                target_win=0, target_profit=-10.0,
                inferred_state_fallback_level=5,
            )),
        ]
        agg = so.aggregate_window(kept_bets + blocked_bets)
        alt_b = agg["alt_b_block_fallback_level_2plus"]
        self.assertEqual(alt_b["n_blocked"], 3)
        self.assertEqual(alt_b["n_kept"], 5)
        self.assertEqual(alt_b["blocked_n_losses"], 3)
        # Counterfactual = +30 (would have saved $30 by blocking)
        self.assertAlmostEqual(
            alt_b["counterfactual_profit_delta_usd"], 30.0, places=2,
        )

    def test_alt_b_counterfactual_negative_when_blocked_were_winners(self):
        # 5 bets blocked, all winners (+$25 total) -> blocking COSTS $25
        bets = [
            so.project_bet(_row(
                target_win=1, target_profit=5.0,
                inferred_state_fallback_level=2,
            ))
            for _ in range(5)
        ]
        agg = so.aggregate_window(bets)
        alt_b = agg["alt_b_block_fallback_level_2plus"]
        self.assertEqual(alt_b["n_blocked"], 5)
        # cf_delta = -25 (we'd have lost the winnings by blocking)
        self.assertAlmostEqual(
            alt_b["counterfactual_profit_delta_usd"], -25.0, places=2,
        )


class RecommendationsTests(unittest.TestCase):
    def test_no_recommendation_below_sample_floor(self):
        # 20 bets is below the 30-bet floor for any recommendation
        bets = [
            so.project_bet(_row(
                base_fair_value=0.9, fair_value=0.9,
                inferred_state_base_empirical=0.5, target_win=0,
            ))
            for _ in range(20)
        ]
        agg = so.aggregate_window(bets)
        self.assertEqual(agg["recommendations"], [])

    def test_alt_a_recommendation_fires_when_thresholds_met(self):
        # 30 bets, all with strong alt-A improvement
        bets = [
            so.project_bet(_row(
                base_fair_value=0.9, fair_value=0.9,
                inferred_state_base_empirical=0.5, target_win=0,
            ))
            for _ in range(30)
        ]
        agg = so.aggregate_window(bets)
        recs = [
            r for r in agg["recommendations"]
            if r["alt"] == "alt_a_empirical_when_available"
        ]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["verdict"], "promote_to_runtime_shadow")

    def test_alt_a_recommendation_suppressed_when_coverage_too_low(self):
        # 30 bets, only 5 have empirical (16% coverage < 25% floor)
        bets_changed = [
            so.project_bet(_row(
                base_fair_value=0.9, fair_value=0.9,
                inferred_state_base_empirical=0.5, target_win=0,
            )) for _ in range(5)
        ]
        bets_no_change = []
        for _ in range(25):
            row = _row(base_fair_value=0.9, fair_value=0.9, target_win=0)
            del row["inferred_state_base_empirical"]
            bets_no_change.append(so.project_bet(row))
        agg = so.aggregate_window(bets_changed + bets_no_change)
        recs = [
            r for r in agg["recommendations"]
            if r["alt"] == "alt_a_empirical_when_available"
        ]
        self.assertEqual(recs, [])

    def test_alt_b_recommendation_fires_at_threshold(self):
        # 25 kept + 5 blocked losers ($-50) -> cf_delta = +50 >= $20
        kept = [
            so.project_bet(_row(target_win=0, target_profit=-1.0))
            for _ in range(25)
        ]
        blocked = [
            so.project_bet(_row(
                target_win=0, target_profit=-10.0,
                inferred_state_fallback_level=2,
            )) for _ in range(5)
        ]
        agg = so.aggregate_window(kept + blocked)
        recs = [
            r for r in agg["recommendations"]
            if r["alt"] == "alt_b_block_fallback_level_2plus"
        ]
        self.assertEqual(len(recs), 1)

    def test_alt_b_recommendation_suppressed_with_few_blocks(self):
        # 27 kept + 3 blocked but $5 cf delta (< $20 floor)
        kept = [
            so.project_bet(_row(target_win=1, target_profit=2.0))
            for _ in range(27)
        ]
        blocked = [
            so.project_bet(_row(
                target_win=1, target_profit=2.0,
                inferred_state_fallback_level=2,
            )),
        ]
        agg = so.aggregate_window(kept + blocked)
        recs = [
            r for r in agg["recommendations"]
            if r["alt"] == "alt_b_block_fallback_level_2plus"
        ]
        # cf_delta = -2 (negative; recommendation never fires)
        self.assertEqual(recs, [])


class RuntimeAltSourcePreferenceTests(unittest.TestCase):
    """When the live engine logged fair_value_alt_empirical (via the
    --stage1-shadow-empirical-override shadow flag), the report
    prefers that runtime-computed value over its own offline
    logit-additive fallback. The source breakdown surfaces how many
    bets came from each path."""

    def test_runtime_alt_preferred_over_offline(self):
        # Runtime alt = 0.42 (logged by live engine, includes the
        # production calibrator). Offline computation would give
        # sigmoid(logit(0.5)) = 0.5. Report should use 0.42.
        row = _row(
            base_fair_value=0.8, fair_value=0.85,
            stage2_run_env_delta=0.0, team_offense_delta=0.0,
            inferred_state_base_empirical=0.5,
            target_win=0,
        )
        row["fair_value_alt_empirical"] = 0.42
        row["fair_value_alt_empirical_used_empirical"] = True
        bet = so.project_bet(row)
        self.assertEqual(bet.alt_a_source, "runtime")
        self.assertAlmostEqual(bet.p3_alt_a, 0.42, places=4)

    def test_offline_fallback_when_runtime_not_logged(self):
        # No runtime alt field; offline fallback computes from chain.
        bet = so.project_bet(_row(
            base_fair_value=0.8, fair_value=0.85,
            stage2_run_env_delta=0.0, team_offense_delta=0.0,
            inferred_state_base_empirical=0.5,
            target_win=0,
        ))
        self.assertEqual(bet.alt_a_source, "offline")
        # Offline: sigmoid(logit(0.5)) = 0.5
        self.assertAlmostEqual(bet.p3_alt_a, 0.5, places=4)

    def test_no_change_when_runtime_not_used_empirical(self):
        # Runtime ran in shadow but didn't have empirical (used=False)
        # AND offline has no empirical either: no_change source.
        row = _row(
            base_fair_value=0.8, fair_value=0.85, target_win=0,
        )
        del row["inferred_state_base_empirical"]
        row["fair_value_alt_empirical_used_empirical"] = False
        bet = so.project_bet(row)
        self.assertEqual(bet.alt_a_source, "no_change")
        self.assertFalse(bet.alt_a_changed)
        self.assertAlmostEqual(bet.p3_alt_a, bet.p3_prod, places=4)

    def test_runtime_alt_ignored_when_used_empirical_false(self):
        # The flag value matters: even if fair_value_alt_empirical is
        # present, it's only trusted when fair_value_alt_empirical_used_empirical
        # is True. Otherwise fall back to offline.
        row = _row(
            base_fair_value=0.8, fair_value=0.85,
            stage2_run_env_delta=0.0, team_offense_delta=0.0,
            inferred_state_base_empirical=0.5, target_win=0,
        )
        row["fair_value_alt_empirical"] = 0.42
        row["fair_value_alt_empirical_used_empirical"] = False
        bet = so.project_bet(row)
        self.assertEqual(bet.alt_a_source, "offline")
        self.assertAlmostEqual(bet.p3_alt_a, 0.5, places=4)

    def test_source_breakdown_in_aggregate(self):
        # Mix: 2 runtime, 2 offline, 1 no_change
        runtime_row = _row(target_win=0, fair_value=0.85)
        runtime_row["fair_value_alt_empirical"] = 0.4
        runtime_row["fair_value_alt_empirical_used_empirical"] = True
        offline_row = _row(target_win=0, fair_value=0.85)  # has empirical default
        no_change_row = _row(target_win=0, fair_value=0.85)
        del no_change_row["inferred_state_base_empirical"]

        bets = [
            so.project_bet(runtime_row),
            so.project_bet(runtime_row),
            so.project_bet(offline_row),
            so.project_bet(offline_row),
            so.project_bet(no_change_row),
        ]
        agg = so.aggregate_window(bets)
        breakdown = agg["alt_a_empirical_when_available"]["alt_source_breakdown"]
        self.assertEqual(breakdown["runtime"], 2)
        self.assertEqual(breakdown["offline"], 2)
        self.assertEqual(breakdown["no_change"], 1)


class WindowSlicingTests(unittest.TestCase):
    def test_anchors_on_latest_session_date(self):
        bets = [
            so.project_bet(_row(session_date="2026-04-10")),
            so.project_bet(_row(session_date="2026-04-25")),
            so.project_bet(_row(session_date="2026-05-15")),
        ]
        windows = so.slice_windows(bets)
        d7 = {b.session_date for b in windows["trailing_7d"]}
        self.assertEqual(d7, {"2026-05-15"})

    def test_empty_input(self):
        windows = so.slice_windows([])
        for w in ("all", "trailing_30d", "trailing_7d"):
            self.assertEqual(windows[w], [])


class PayloadAndMarkdownTests(unittest.TestCase):
    def test_payload_has_required_keys(self):
        payload = so.build_payload([_bet()])
        for k in (
            "schema_version", "generated_at_utc",
            "n_bets", "date_span", "config", "windows",
        ):
            self.assertIn(k, payload)
        for w in ("all", "trailing_30d", "trailing_7d"):
            self.assertIn(w, payload["windows"])

    def test_markdown_lists_both_alts(self):
        payload = so.build_payload([_bet() for _ in range(3)])
        md = so.render_markdown(payload)
        self.assertIn("Alt A", md)
        self.assertIn("Alt B", md)
        self.assertIn("empirical-when-available", md)

    def test_empty_payload_does_not_crash_markdown(self):
        md = so.render_markdown(so.build_payload([]))
        self.assertIn("Stage-1 shadow-override report", md)


class CohortBreakdownTests(unittest.TestCase):
    """2026-05-19 follow-up: cohort breakdown of Alt A across 5
    dimensions (edge / inning / line / ask / current_state_edge).

    Validates:
      - cohort bucketers map ranges correctly
      - aggregate_cohort computes per-cohort bias delta + Alt B P&L
      - build_cohort_breakdown groups bets per dimension + per bucket
      - top_cohorts surfaces most_improved / regressions /
        highest_coverage / largest_alt_b_savings sorted correctly
      - cohorts below min_n_per_cohort are aggregated but excluded
        from the top_cohorts summary
    """

    def test_edge_bucketer_ranges(self):
        self.assertEqual(so._edge_bucket(None), "missing")
        self.assertEqual(so._edge_bucket(0.10), "<0.15")
        self.assertEqual(so._edge_bucket(0.16), "0.15-0.18")
        self.assertEqual(so._edge_bucket(0.20), "0.18-0.22")
        self.assertEqual(so._edge_bucket(0.25), ">=0.22")

    def test_inning_bucketer_ranges(self):
        self.assertEqual(so._inning_bucket(None), "missing")
        self.assertEqual(so._inning_bucket(3), "<=5")
        self.assertEqual(so._inning_bucket(5), "<=5")
        self.assertEqual(so._inning_bucket(6), "6")
        self.assertEqual(so._inning_bucket(7), "7")
        self.assertEqual(so._inning_bucket(9), ">=8")

    def test_line_bucketer_ranges(self):
        self.assertEqual(so._line_bucket(None), "missing")
        self.assertEqual(so._line_bucket(6.5), "<=7.5")
        self.assertEqual(so._line_bucket(8.5), "8.5")
        self.assertEqual(so._line_bucket(9.5), "9.5")
        self.assertEqual(so._line_bucket(11.5), ">=10.5")

    def test_ask_bucketer_ranges(self):
        self.assertEqual(so._ask_bucket(None), "missing")
        self.assertEqual(so._ask_bucket(0.40), "<0.55")
        self.assertEqual(so._ask_bucket(0.65), "0.55-0.70")
        self.assertEqual(so._ask_bucket(0.80), "0.70-0.85")
        self.assertEqual(so._ask_bucket(0.90), ">=0.85")

    def test_current_state_edge_bucketer_ranges(self):
        self.assertEqual(so._current_state_edge_bucket(None), "missing")
        self.assertEqual(so._current_state_edge_bucket(-0.01), "<0.00")
        self.assertEqual(so._current_state_edge_bucket(0.02), "0.00-0.03")
        self.assertEqual(so._current_state_edge_bucket(0.04), "0.03-0.06")
        self.assertEqual(so._current_state_edge_bucket(0.08), ">=0.06")

    def test_project_bet_carries_new_cohort_fields(self):
        bet = so.project_bet(_row(
            edge_at_ask=0.20, decision_ask=0.70,
            current_state_value_edge=0.05,
        ))
        self.assertAlmostEqual(bet.edge_at_ask, 0.20)
        self.assertAlmostEqual(bet.decision_ask, 0.70)
        self.assertAlmostEqual(bet.current_state_value_edge, 0.05)

    def test_project_bet_falls_back_to_alias_columns(self):
        """`edge` (alias) + `entry_ask` (alias) propagate when the
        canonical column names are missing. Important for backward-
        compat with older training-table rows."""
        row = _row()
        # Drop canonical fields; populate aliases.
        for k in (
            "edge_at_ask", "decision_ask", "current_state_value_edge",
        ):
            row.pop(k, None)
        row["edge"] = 0.18
        row["entry_ask"] = 0.65
        bet = so.project_bet(row)
        self.assertAlmostEqual(bet.edge_at_ask, 0.18)
        self.assertAlmostEqual(bet.decision_ask, 0.65)
        self.assertIsNone(bet.current_state_value_edge)

    def test_aggregate_cohort_empty_returns_none_fields(self):
        c = so._aggregate_cohort([])
        self.assertEqual(c["n_bets"], 0)
        self.assertIsNone(c["production"]["bias"])
        self.assertIsNone(c["alt_a"]["bias_delta_vs_prod_pp"])

    def test_aggregate_cohort_computes_alt_a_delta(self):
        """5 bets: poisson=0.9, empirical=0.5, won=0 -> bias_prod=+0.9,
        bias_alt_a=+0.5, delta=+0.40 (= +40pp).
        """
        bets = [
            so.project_bet(_row(
                base_fair_value=0.9, fair_value=0.9,
                inferred_state_base_empirical=0.5, target_win=0,
            ))
            for _ in range(5)
        ]
        c = so._aggregate_cohort(bets)
        self.assertEqual(c["n_bets"], 5)
        self.assertAlmostEqual(c["production"]["bias"], 0.9, places=4)
        self.assertAlmostEqual(c["alt_a"]["bias"], 0.5, places=4)
        self.assertAlmostEqual(
            c["alt_a"]["bias_delta_vs_prod_pp"], 40.0, places=2,
        )

    def test_build_cohort_breakdown_groups_by_each_dimension(self):
        bets = [
            so.project_bet(_row(edge_at_ask=0.10, inning=3, line=6.5)),
            so.project_bet(_row(edge_at_ask=0.20, inning=6, line=8.5)),
            so.project_bet(_row(edge_at_ask=0.25, inning=8, line=10.5)),
        ]
        breakdown = so.build_cohort_breakdown(
            bets, min_n_per_cohort=1,
        )
        # All 5 dimensions present
        for dim in (
            "edge_bucket", "inning_bucket", "line_bucket",
            "ask_bucket", "current_state_edge_bucket",
        ):
            self.assertIn(dim, breakdown["by_dimension"])
        # Per-bucket aggregates
        edge_buckets = breakdown["by_dimension"]["edge_bucket"]
        self.assertEqual(edge_buckets["<0.15"]["n_bets"], 1)
        self.assertEqual(edge_buckets["0.18-0.22"]["n_bets"], 1)
        self.assertEqual(edge_buckets[">=0.22"]["n_bets"], 1)

    def test_top_cohorts_most_improved_sorted_descending(self):
        """One cohort with big Alt A improvement, one with small,
        one with regression. `most_improved` should rank biggest first.
        """
        # Cohort 1 (edge<0.15): 5 bets, big Alt A win
        cohort_1 = [
            so.project_bet(_row(
                edge_at_ask=0.10,
                base_fair_value=0.90, fair_value=0.90,
                inferred_state_base_empirical=0.50, target_win=0,
            ))
            for _ in range(5)
        ]
        # Cohort 2 (edge>=0.22): 5 bets, small Alt A delta
        cohort_2 = [
            so.project_bet(_row(
                edge_at_ask=0.25,
                base_fair_value=0.90, fair_value=0.90,
                inferred_state_base_empirical=0.85, target_win=0,
            ))
            for _ in range(5)
        ]
        breakdown = so.build_cohort_breakdown(
            cohort_1 + cohort_2, min_n_per_cohort=5,
        )
        most_improved = breakdown["top_cohorts"]["most_improved"]
        self.assertGreater(len(most_improved), 0)
        # First entry should be cohort_1 (larger delta)
        first = most_improved[0]
        self.assertEqual(first["bucket"], "<0.15")
        # Cohort 1: bias 0.9 -> 0.5 = 40pp; cohort 2: 0.9 -> 0.85 = 5pp
        self.assertGreater(first["bias_delta_vs_prod_pp"], 30.0)

    def test_top_cohorts_regressions_only_negative_deltas(self):
        """A cohort where Alt A makes bias WORSE shows up in
        `regressions`. Bias direction matters: when bias_prod is
        negative (model UNDER-predicts), an empirical that is MORE
        negative makes bias more negative. The signed delta should
        flag it as a regression.
        """
        # 5 bets: poisson=0.30, empirical=0.10, won=1 (every bet wins)
        # bias_prod = 0.30 - 1.00 = -0.70 (under-prediction)
        # bias_alt_a = 0.10 - 1.00 = -0.90 (MORE under-prediction)
        # signed delta = bias_alt_a - bias_prod = -0.20 -> *100 = -20pp
        # (negative because bias_prod < 0 path: delta = alt_a - prod)
        bets = [
            so.project_bet(_row(
                edge_at_ask=0.10,
                base_fair_value=0.30, fair_value=0.30,
                inferred_state_base_empirical=0.10, target_win=1,
            ))
            for _ in range(5)
        ]
        breakdown = so.build_cohort_breakdown(bets, min_n_per_cohort=5)
        regressions = breakdown["top_cohorts"]["regressions"]
        # The same 5 bets fall into ONE bucket per dimension (5 dims),
        # so all 5 dimension entries will appear as regressions. The
        # top_cohorts list is capped at 5 entries.
        self.assertEqual(len(regressions), 5)
        for r in regressions:
            self.assertLess(r["bias_delta_vs_prod_pp"], 0.0)

    def test_top_cohorts_excludes_small_samples(self):
        """Cohort with n < min_n_per_cohort is in `by_dimension`
        but NOT in `top_cohorts`."""
        bets = [
            so.project_bet(_row(
                edge_at_ask=0.10,
                base_fair_value=0.90, fair_value=0.90,
                inferred_state_base_empirical=0.50, target_win=0,
            ))
            for _ in range(3)
        ]
        breakdown = so.build_cohort_breakdown(bets, min_n_per_cohort=5)
        edge_buckets = breakdown["by_dimension"]["edge_bucket"]
        # Bucket exists in per-dimension table
        self.assertEqual(edge_buckets["<0.15"]["n_bets"], 3)
        # But not in top_cohorts (3 < 5 = min_n)
        most_improved = breakdown["top_cohorts"]["most_improved"]
        self.assertEqual(
            [e for e in most_improved if e["dimension"] == "edge_bucket"],
            [],
        )

    def test_top_cohorts_largest_alt_b_savings_only_positive(self):
        """Cohorts with $0 or negative Alt B savings are excluded
        from the `largest_alt_b_savings` summary view in the markdown
        (but still present in the raw flat list)."""
        # All bets at fallback_level=2 are losers => Alt B saves money
        bets = [
            so.project_bet(_row(
                edge_at_ask=0.20,
                inferred_state_fallback_level=2,
                target_win=0, target_profit=-10.0,
            ))
            for _ in range(5)
        ]
        breakdown = so.build_cohort_breakdown(bets, min_n_per_cohort=5)
        savings = breakdown["top_cohorts"]["largest_alt_b_savings"]
        self.assertGreater(len(savings), 0)
        for e in savings:
            # All entries in the summary have positive savings
            # OR are 0 (when no bets were blocked).
            self.assertGreaterEqual(e["alt_b_counterfactual_usd"], 0.0)

    def test_payload_includes_cohort_breakdown_trailing_30d(self):
        payload = so.build_payload(
            [_bet() for _ in range(3)],
        )
        self.assertIn("cohort_breakdown_trailing_30d", payload)
        ct = payload["cohort_breakdown_trailing_30d"]
        self.assertIn("by_dimension", ct)
        self.assertIn("top_cohorts", ct)
        self.assertEqual(ct["n_bets_total"], 3)

    def test_markdown_renders_cohort_section_when_present(self):
        payload = so.build_payload(
            [_bet() for _ in range(3)],
        )
        md = so.render_markdown(payload)
        self.assertIn("Cohort breakdown", md)
        self.assertIn("Per-dimension detail", md)


class EndToEndTests(unittest.TestCase):
    def test_main_writes_json_and_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            jsonl = tmp_dir / "training.jsonl"
            with open(jsonl, "w", encoding="utf-8") as f:
                for i in range(5):
                    f.write(json.dumps(_row(target_win=i % 2)) + "\n")
            out_dir = tmp_dir / "out"
            rc = so.main([
                "--training-table", str(jsonl),
                "--output-dir", str(out_dir),
            ])
            self.assertEqual(rc, 0)
            j = json.loads(
                (out_dir / "stage1_shadow_override_report.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(j["n_bets"], 5)
            self.assertTrue(
                (out_dir / "stage1_shadow_override_report.md").exists()
            )

    def test_main_empty_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "training.jsonl"
            jsonl.write_text("", encoding="utf-8")
            out_dir = Path(tmp) / "out"
            rc = so.main([
                "--training-table", str(jsonl),
                "--output-dir", str(out_dir),
            ])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
