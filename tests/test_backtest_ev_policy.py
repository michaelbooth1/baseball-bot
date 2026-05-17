import unittest

from scripts.analysis import backtest_ev_policy as bep


class BacktestEvPolicyTests(unittest.TestCase):
    def test_ev_if_filled_formula(self) -> None:
        # stake=25 at q=0.66 => win profit 12.8788, lose -25
        ev = bep._ev_if_filled(p_win_if_filled=0.70, price=0.66, stake=25.0)
        expected = 0.70 * (25.0 * ((1.0 - 0.66) / 0.66)) - 0.30 * 25.0
        self.assertAlmostEqual(ev, expected, places=8)

    def test_apply_policy_with_daily_cap(self) -> None:
        rows = [
            {"bet_id": "a1", "session_date": "2026-04-20", "ev_per_stake": 0.06, "ev_realized": 6.0, "p_fill": 0.7},
            {"bet_id": "a2", "session_date": "2026-04-20", "ev_per_stake": 0.05, "ev_realized": 5.0, "p_fill": 0.8},
            {"bet_id": "a3", "session_date": "2026-04-20", "ev_per_stake": 0.03, "ev_realized": 3.0, "p_fill": 0.9},
            {"bet_id": "b1", "session_date": "2026-04-21", "ev_per_stake": 0.07, "ev_realized": 7.0, "p_fill": 0.6},
            {"bet_id": "b2", "session_date": "2026-04-21", "ev_per_stake": 0.01, "ev_realized": 1.0, "p_fill": 0.9},
        ]
        selected = bep.apply_policy(rows, min_ev_per_stake=0.02, min_p_fill=0.0, max_per_day=2)
        self.assertEqual([r["bet_id"] for r in selected], ["a1", "a2", "b1"])

    def test_runtime_fill_features_are_decision_time_only(self) -> None:
        pre_signal = [
            "entry_ask",
            "current_state_value_edge",
            "over_token_id",
            "weather_cache_date",
            "stadium_primary_name",
            "kelly_full_fraction",
        ]
        post_signal = ["ask_1s", "bid_1s", "ask_move_2s", "sim_filled_30s"]

        runtime_features = bep._feature_list_for_runtime_fill(pre_signal)
        win_features = bep._feature_list_for_win(pre_signal)
        strict_features = bep._feature_list_for_fill(pre_signal, post_signal)

        self.assertEqual(runtime_features, ["entry_ask", "current_state_value_edge", "mode"])
        self.assertEqual(win_features, runtime_features)
        self.assertNotIn("ask_1s", runtime_features)
        self.assertNotIn("bid_1s", runtime_features)
        self.assertNotIn("over_token_id", runtime_features)
        self.assertNotIn("weather_cache_date", runtime_features)
        self.assertNotIn("stadium_primary_name", runtime_features)
        self.assertNotIn("kelly_full_fraction", runtime_features)
        self.assertIn("ask_1s", strict_features)
        self.assertIn("bid_1s", strict_features)
        self.assertIn("ask_move_2s", strict_features)
        self.assertIn("over_token_id", strict_features)
        self.assertIn("kelly_full_fraction", strict_features)
        self.assertNotIn("sim_filled_30s", strict_features)

    def test_policy_metrics_basic(self) -> None:
        rows = [
            {
                "session_date": "2026-04-20",
                "target_profit": 10.0,
                "ev_realized": 4.0,
                "ev_per_stake": 0.16,
                "stake": 25.0,
                "target_filled": 1,
                "target_win": 1,
            },
            {
                "session_date": "2026-04-20",
                "target_profit": -25.0,
                "ev_realized": 1.0,
                "ev_per_stake": 0.04,
                "stake": 25.0,
                "target_filled": 1,
                "target_win": 0,
            },
        ]
        m = bep.policy_metrics(rows)
        self.assertEqual(m["selected_trades"], 2)
        self.assertAlmostEqual(m["realized_profit_sum"], -15.0, places=6)
        self.assertAlmostEqual(m["expected_profit_sum"], 5.0, places=6)
        self.assertAlmostEqual(m["total_stake"], 50.0, places=6)
        self.assertAlmostEqual(m["roi"], -0.3, places=6)
        self.assertAlmostEqual(m["fill_rate"], 1.0, places=6)
        self.assertAlmostEqual(m["win_rate_filled_only"], 0.5, places=6)

    def test_choose_best_policy_prefers_expected_ev(self) -> None:
        # Row a has stronger expected EV but weaker realized.
        # Row b has negative expected EV but high realized.
        rows = [
            {
                "bet_id": "a",
                "session_date": "2026-04-20",
                "ev_per_stake": 0.10,
                "ev_realized": 5.0,
                "p_fill": 0.8,
                "target_profit": -1.0,
                "stake": 25.0,
                "target_filled": 1,
                "target_win": 0,
            },
            {
                "bet_id": "b",
                "session_date": "2026-04-20",
                "ev_per_stake": -0.01,
                "ev_realized": -1.0,
                "p_fill": 0.8,
                "target_profit": 10.0,
                "stake": 25.0,
                "target_filled": 1,
                "target_win": 1,
            },
        ]
        best_cfg, selected, _grid = bep.choose_best_policy(
            val_rows=rows,
            ev_thresholds=[-0.02, 0.05],
            pfill_thresholds=[0.0],
            max_per_day_options=[0],
            min_validation_trades=1,
        )
        self.assertAlmostEqual(best_cfg["min_ev_per_stake"], 0.05, places=8)
        self.assertEqual([r["bet_id"] for r in selected], ["a"])

    def test_choose_best_policy_allows_positive_any_trades_fallback(self) -> None:
        rows = [
            {
                "bet_id": "a",
                "session_date": "2026-04-20",
                "ev_per_stake": 0.05,
                "ev_realized": 1.0,
                "p_fill": 0.9,
                "target_profit": -1.0,
                "stake": 20.0,
                "target_filled": 1,
                "target_win": 0,
            },
            {
                "bet_id": "b",
                "session_date": "2026-04-20",
                "ev_per_stake": -0.05,
                "ev_realized": -2.0,
                "p_fill": 0.9,
                "target_profit": 5.0,
                "stake": 20.0,
                "target_filled": 1,
                "target_win": 1,
            },
            {
                "bet_id": "c",
                "session_date": "2026-04-20",
                "ev_per_stake": -0.05,
                "ev_realized": -2.0,
                "p_fill": 0.9,
                "target_profit": 6.0,
                "stake": 20.0,
                "target_filled": 1,
                "target_win": 1,
            },
        ]
        best_cfg, selected, _grid = bep.choose_best_policy(
            val_rows=rows,
            ev_thresholds=[-0.1, 0.0],
            pfill_thresholds=[0.0],
            max_per_day_options=[0],
            min_validation_trades=2,
        )
        self.assertEqual(best_cfg["selection_stage"], "positive_expected_any_trades")
        self.assertAlmostEqual(best_cfg["min_ev_per_stake"], 0.0, places=8)
        self.assertEqual([r["bet_id"] for r in selected], ["a"])


class RuntimeFeatureExclusionTests(unittest.TestCase):
    """Regression tests for the EV-policy feature exclusion list.

    Caught 2026-05-15: the EV-policy retrain on that day selected 295
    features including book-pair fields (`under_best_bid`,
    `over_best_ask`, `decision_mid`, etc.) the runtime doesn't supply
    at decision time. The live engine logged a missing-features warning
    at 20:15:

        EV policy artifact requires runtime features that are
        absent/null; shadow will score with artifact imputations,
        enforce will fail closed (missing={'win': ['under_token_id',
        'over_best_bid', 'over_best_ask', ...], ...})

    Impact in shadow mode: zero (silent imputation). Impact if
    promoted to enforce: every bet would fail-closed on missing
    features -- a silent blocker for Active #4 (move trade rule to
    realized EV). Fix: extend RUNTIME_FEATURE_DENY_PREFIXES to cover
    the entire `over_*`/`under_*` book-pair-side family.

    These tests pin the exclusion behaviour so a future code change
    can't silently re-include any of the problem fields.
    """

    # The full set of fields that caused the 2026-05-15 warning. Every
    # one must be excluded from runtime EV-policy training.
    PROBLEM_FIELDS = (
        "over_best_bid", "over_best_ask", "over_mid", "over_spread",
        "over_ltp", "over_book_source", "over_token_id",
        "over_under_ask_sum", "over_under_bid_sum", "over_under_mid_sum",
        "over_mid_no_vig",
        "under_best_bid", "under_best_ask", "under_mid", "under_spread",
        "under_ltp", "under_book_source", "under_token_id",
        "under_pair_available", "under_mid_no_vig",
        "decision_mid",
    )

    # Fields the live runtime DOES supply at decision time and that EV
    # policy should be free to learn from. Cross-checked against
    # build_signal_training_table.py's pre-signal feature group and the
    # candidate_universe row schema as of 2026-05-16.
    RUNTIME_SUPPLIED_FIELDS = (
        "entry_ask", "decision_ask",
        "fair_value", "base_fair_value",
        "edge", "edge_at_ask",
        "current_state_value_edge",
        "stage2_run_env_delta", "team_offense_delta",
        "inning", "outs", "runners_on",
        "away_score_before", "home_score_before",
        "weather_temp_f", "weather_wind_out_component_mph",
    )

    def test_every_2026_05_15_problem_field_is_excluded(self):
        # Direct regression test against the exact field list from the
        # production warning. If any of these passes the runtime-safe
        # filter, we re-introduce the bug.
        for col in self.PROBLEM_FIELDS:
            with self.subTest(field=col):
                self.assertFalse(
                    bep._is_runtime_safe_feature_col(col),
                    f"`{col}` is in the 2026-05-15 missing-features list "
                    "but slipped past the runtime-safe filter; the EV-policy "
                    "retrain will re-include it and the live engine will "
                    "warn on missing features at startup.",
                )

    def test_runtime_supplied_fields_not_accidentally_excluded(self):
        # Symmetric sanity: the prefix exclusions must not bleed onto
        # legitimate runtime features. If we ever accidentally excluded
        # `entry_ask` or `current_state_value_edge`, EV would be
        # crippled.
        for col in self.RUNTIME_SUPPLIED_FIELDS:
            with self.subTest(field=col):
                self.assertTrue(
                    bep._is_runtime_safe_feature_col(col),
                    f"`{col}` is a legitimate runtime-supplied field but "
                    "was excluded by RUNTIME_FEATURE_DENY -- check whether "
                    "a new prefix is overreaching.",
                )

    def test_filter_excludes_all_over_and_under_prefix_fields(self):
        # The fix is prefix-based. Any future `over_*` / `under_*`
        # field will auto-exclude.
        for hypothetical in (
            "over_some_new_field", "over_volume_imbalance",
            "under_book_depth_5", "under_anything_at_all",
        ):
            with self.subTest(field=hypothetical):
                self.assertFalse(
                    bep._is_runtime_safe_feature_col(hypothetical),
                    f"hypothetical future field `{hypothetical}` should be "
                    "auto-excluded by the over_/under_ prefix policy.",
                )

    def test_pre_signal_filter_drops_problem_fields_at_training_time(self):
        # End-to-end check via the public-ish feature-list builder:
        # given a mixed input, the runtime-fill feature list must
        # exclude every problem field and keep every runtime-supplied
        # field. Catches a bug where filter logic diverges from the
        # deny constants.
        mixed = list(self.PROBLEM_FIELDS) + list(self.RUNTIME_SUPPLIED_FIELDS)
        runtime_features = bep._feature_list_for_runtime_fill(mixed)
        # "mode" is appended unconditionally by the helper.
        self.assertIn("mode", runtime_features)
        for col in self.PROBLEM_FIELDS:
            self.assertNotIn(col, runtime_features)
        for col in self.RUNTIME_SUPPLIED_FIELDS:
            self.assertIn(col, runtime_features)

    def test_runtime_feature_exclusions_constant_is_well_formed(self):
        # The artifact writes `runtime_feature_exclusions` from
        # RUNTIME_FEATURE_DENY_EXACT (a frozenset). The list-of-strings
        # contract should hold and contain the explicit decision_mid.
        self.assertIsInstance(bep.RUNTIME_FEATURE_DENY_EXACT, frozenset)
        self.assertIn("decision_mid", bep.RUNTIME_FEATURE_DENY_EXACT)
        self.assertIn("over_", bep.RUNTIME_FEATURE_DENY_PREFIXES)
        self.assertIn("under_", bep.RUNTIME_FEATURE_DENY_PREFIXES)


if __name__ == "__main__":
    unittest.main()
