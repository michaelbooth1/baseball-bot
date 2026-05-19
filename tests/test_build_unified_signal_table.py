import unittest

from scripts.analysis import build_unified_signal_table as bust


class BuildUnifiedSignalTableTests(unittest.TestCase):
    def _capture(self) -> bust.CaptureData:
        snapshots = [
            {
                "type": "snapshot",
                "seq": 0,
                "elapsed_s": 0.0,
                "book": {"best_bid": 0.78, "best_ask": 0.80},
            },
            {
                "type": "snapshot",
                "seq": 1,
                "elapsed_s": 1.0,
                "book": {"best_bid": 0.79, "best_ask": 0.82, "spread": 0.03},
            },
            {
                "type": "snapshot",
                "seq": 2,
                "elapsed_s": 2.0,
                "book": {"best_bid": 0.77, "best_ask": 0.79, "spread": 0.02},
            },
            {
                "type": "snapshot",
                "seq": 5,
                "elapsed_s": 5.0,
                "book": {"best_bid": 0.74, "best_ask": 0.76, "spread": 0.02},
            },
            {
                "type": "snapshot",
                "seq": 10,
                "elapsed_s": 10.0,
                "book": {"best_bid": 0.73, "best_ask": 0.75, "spread": 0.02},
            },
            {
                "type": "snapshot",
                "seq": 30,
                "elapsed_s": 30.0,
                "book": {"best_bid": 0.68, "best_ask": 0.70, "spread": 0.02},
            },
        ]
        return bust.CaptureData(
            bet_id="test-bet",
            path="capture.jsonl",
            header={"type": "signal", "bet_id": "test-bet"},
            t0_book={"best_bid": 0.78, "best_ask": 0.80},
            snapshots=snapshots,
        )

    def test_parse_horizons_csv(self) -> None:
        self.assertEqual(bust._parse_horizons_csv("1,2,5,10,30"), [1, 2, 5, 10, 30])
        self.assertEqual(bust._parse_horizons_csv(" 5, 2,5 "), [2, 5])
        with self.assertRaises(SystemExit):
            bust._parse_horizons_csv("")
        with self.assertRaises(SystemExit):
            bust._parse_horizons_csv("1,0")
        with self.assertRaises(SystemExit):
            bust._parse_horizons_csv("1,abc")

    def test_build_master_columns_dynamic(self) -> None:
        cols = bust._build_master_columns([1, 3, 7], 12)
        self.assertIn("ask_1s", cols)
        self.assertIn("bid_3s", cols)
        self.assertIn("ask_7s", cols)
        self.assertIn("sim_fill_time_12s", cols)
        self.assertIn("sim_filled_12s", cols)
        self.assertIn("sim_cents_saved_vs_taker_12s", cols)
        self.assertIn("param_edge_threshold", cols)
        self.assertIn("state_value_strategy", cols)
        self.assertIn("signal_model_family", cols)
        self.assertIn("home_leading_late", cols)
        self.assertIn("expected_remaining_half_innings", cols)
        self.assertIn("home_skip_bottom9_risk", cols)
        self.assertIn("stadium_weather_exposure", cols)
        self.assertIn("weather_air_density_index", cols)
        self.assertIn("weather_wind_out_component_mph", cols)
        self.assertIn("current_state_value_edge", cols)
        self.assertIn("current_state_value_base_poisson", cols)
        self.assertIn("inferred_state_base_empirical", cols)
        self.assertIn("inferred_state_poisson_minus_empirical", cols)
        self.assertIn("inferred_state_empirical_line_source_key", cols)
        self.assertIn("shadow_phantom_risk_score", cols)
        self.assertIn("shadow_fv_inferred_lift", cols)
        self.assertIn("shadow_ltp_ask_gap", cols)
        self.assertEqual(len(cols), len(set(cols)))

    def test_build_master_rows_propagates_state_value_candidate_fields(self) -> None:
        master_columns = bust._build_master_columns([1], 30)
        session_rows = {
            "bet-1": {
                "mode": "live",
                "session_date": "2026-04-30",
                "session_path": "session.json",
                "session_params": {},
                "bet": {
                    "bet_id": "bet-1",
                    "placed_at": "2026-04-30T12:00:00Z",
                    "game_pk": 1,
                    "away_abbrev": "AAA",
                    "home_abbrev": "BBB",
                    "line": "8.5",
                    "side": "over",
                    "inning": 6,
                    "inning_state": "Top",
                    "outs": 1,
                    "runners_on": 3,
                    "away_score_before": 4,
                    "home_score_before": 3,
                    "inferred_runs": 1,
                    "entry_ask": 0.65,
                    "decision_ask": 0.65,
                    "fair_value": 0.82,
                    "base_fair_value": 0.80,
                    "edge": 0.17,
                    "limit_price": 0.61,
                    "stake": 10.0,
                    "order_status": "filled",
                    "settled": True,
                    "won": True,
                    "profit": 6.39,
                    "payout": 16.39,
                    "final_away": 6,
                    "final_home": 4,
                    "final_total": 10,
                    "state_value_strategy": "score_event_transition",
                    "current_state_value_edge": 0.04,
                    "current_state_value_fv_raw": 0.69,
                    "shadow_phantom_risk_score": 0.30,
                    "shadow_phantom_risk_band": "low",
                },
            }
        }
        candidate_rows = {
            "bet-1": {
                "bet_id": "bet-1",
                "decision": "trade",
                "current_state_value_base_poisson": 0.71,
                "current_state_value_base_empirical": 0.62,
                "current_state_value_line_key_poisson": "po85",
                "current_state_value_used_fallback": False,
                "current_state_value_state_cell_key": "4_3_6_T_1_3",
                "current_state_value_stage2_run_env_delta": 0.02,
                "current_state_value_team_offense_delta": -0.01,
                "inferred_state_base_poisson": 0.80,
                "inferred_state_base_empirical": 0.73,
                "inferred_state_poisson_minus_empirical": 0.07,
                "inferred_state_n": 92,
                "inferred_state_line_key_empirical": "o85",
                "inferred_state_empirical_line_source_key": "o85",
                "shadow_fv_current_state": 0.69,
                "shadow_fv_after_inferred_score": 0.82,
                "shadow_fv_inferred_lift": 0.13,
                "shadow_no_event_edge": 0.04,
                "shadow_after_event_edge": 0.17,
                "shadow_p_score_event_proxy": 0.40,
                "shadow_transition_inferred_runs": 1,
                "shadow_ltp_ask_gap": 0.28,
                "shadow_ltp_ask_gap_exceeded": False,
                "stadium_weather_exposure": "open_air",
                "weather_air_density_index": 0.987,
                "weather_wind_out_component_mph": 6.5,
            }
        }
        warnings: list[str] = []
        hard_errors: list[str] = []

        rows = bust.build_master_rows_for_mode(
            mode="live",
            session_rows=session_rows,
            ledger_events={},
            captures={},
            candidate_rows=candidate_rows,
            min_date=None,
            max_date=None,
            horizons=[1],
            fill_window_secs=30,
            master_columns=master_columns,
            warnings=warnings,
            hard_errors=hard_errors,
        )

        self.assertEqual(hard_errors, [])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["signal_model_family"], "score_event_transition")
        self.assertFalse(row["home_leading_late"])
        self.assertFalse(row["batting_team_is_home"])
        self.assertTrue(row["bottom9_available_if_needed"])
        self.assertAlmostEqual(row["expected_remaining_half_innings"], 8.0)
        self.assertEqual(row["expected_remaining_pa_bucket"], "19+")
        self.assertAlmostEqual(row["home_skip_bottom9_risk"], 0.0)
        self.assertEqual(row["state_value_strategy"], "score_event_transition")
        self.assertAlmostEqual(row["current_state_value_edge"], 0.04)
        self.assertAlmostEqual(row["current_state_value_base_poisson"], 0.71)
        self.assertEqual(row["current_state_value_state_cell_key"], "4_3_6_T_1_3")
        self.assertAlmostEqual(row["inferred_state_base_empirical"], 0.73)
        self.assertAlmostEqual(row["inferred_state_poisson_minus_empirical"], 0.07)
        self.assertEqual(row["inferred_state_empirical_line_source_key"], "o85")
        self.assertAlmostEqual(row["shadow_fv_inferred_lift"], 0.13)
        self.assertAlmostEqual(row["shadow_phantom_risk_score"], 0.30)
        self.assertEqual(row["shadow_phantom_risk_band"], "low")
        self.assertAlmostEqual(row["shadow_ltp_ask_gap"], 0.28)
        self.assertFalse(row["shadow_ltp_ask_gap_exceeded"])
        self.assertEqual(row["stadium_weather_exposure"], "open_air")
        self.assertAlmostEqual(row["weather_air_density_index"], 0.987)
        self.assertAlmostEqual(row["weather_wind_out_component_mph"], 6.5)
        # Forward-compat alias (Fix #2, 2026-05-15): unified rows must
        # carry both `edge_at_ask` (canonical column name) and `edge` (the
        # name source bet/candidate rows use) so ad-hoc consumers reading
        # `row.get("edge")` don't silently get None/0.
        self.assertAlmostEqual(row["edge_at_ask"], 0.17)
        self.assertIn("edge", row, "edge alias missing from unified row")
        self.assertEqual(row["edge"], row["edge_at_ask"])

    def test_build_master_rows_propagates_stage1_alt_a_shadow_fields(self) -> None:
        """Active #8 (2026-05-19): the 6 Stage-1 Alt-A shadow fields the
        runtime writes onto every score-event-family candidate MUST be
        carried through into the unified signal master.

        Caught during 2026-05-18 paper-trading audit: the training table
        declared the columns but populated 0 rows because the
        intermediate unified-signal builder dropped them. Without this
        propagation the loss-attribution + shadow-override reports keep
        recomputing Alt-A offline instead of reading the runtime
        decision.
        """
        master_columns = bust._build_master_columns([1], 30)
        session_rows = {
            "bet-alt-a": {
                "mode": "paper",
                "session_date": "2026-05-18",
                "session_path": "session.json",
                "session_params": {},
                "bet": {
                    "bet_id": "bet-alt-a",
                    "placed_at": "2026-05-18T17:00:00Z",
                    "game_pk": 823549,
                    "away_abbrev": "TOR",
                    "home_abbrev": "NYY",
                    "line": "7.5",
                    "side": "over",
                    "inning": 4,
                    "inning_state": "Top",
                    "outs": 2,
                    "runners_on": 0,
                    "away_score_before": 3,
                    "home_score_before": 1,
                    "inferred_runs": 1,
                    "entry_ask": 0.75,
                    "decision_ask": 0.75,
                    "fair_value": 0.922,
                    "base_fair_value": 0.912,
                    "edge": 0.17,
                    "limit_price": 0.74,
                    "stake": 10.0,
                    "order_status": "filled",
                    "settled": True,
                    "won": True,
                    "profit": 3.33,
                    "payout": 13.33,
                    "final_away": 6,
                    "final_home": 7,
                    "final_total": 13,
                    "state_value_strategy": "score_event_transition",
                    # Note: session_bet does NOT carry Alt-A fields by
                    # design; only the candidate row does. The schema
                    # extractor must pull them from candidate_row.
                },
            }
        }
        candidate_rows = {
            "bet-alt-a": {
                "bet_id": "bet-alt-a",
                "decision": "trade",
                "inferred_state_base_empirical": 0.786,
                "inferred_state_base_poisson": 0.912,
                # The 6 Alt-A fields that must propagate:
                "stage1_shadow_empirical_mode": "shadow",
                "fair_value_alt_empirical": 0.821,
                "fair_value_alt_empirical_raw": 0.821,
                "fair_value_alt_empirical_delta_vs_prod": -0.101,
                "fair_value_alt_empirical_used_empirical": True,
                "fair_value_alt_empirical_p0": 0.786,
            }
        }
        rows = bust.build_master_rows_for_mode(
            mode="paper",
            session_rows=session_rows,
            ledger_events={},
            captures={},
            candidate_rows=candidate_rows,
            min_date=None,
            max_date=None,
            horizons=[1],
            fill_window_secs=30,
            master_columns=master_columns,
            warnings=[],
            hard_errors=[],
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["stage1_shadow_empirical_mode"], "shadow")
        self.assertAlmostEqual(row["fair_value_alt_empirical"], 0.821)
        self.assertAlmostEqual(row["fair_value_alt_empirical_raw"], 0.821)
        self.assertAlmostEqual(
            row["fair_value_alt_empirical_delta_vs_prod"], -0.101,
        )
        self.assertTrue(row["fair_value_alt_empirical_used_empirical"])
        self.assertAlmostEqual(row["fair_value_alt_empirical_p0"], 0.786)
        # Schema sanity: the 6 fields are declared in master_columns
        # too, so the CSV writer's DictWriter(extrasaction="ignore")
        # passes them through to disk.
        for field in (
            "stage1_shadow_empirical_mode",
            "fair_value_alt_empirical",
            "fair_value_alt_empirical_raw",
            "fair_value_alt_empirical_delta_vs_prod",
            "fair_value_alt_empirical_used_empirical",
            "fair_value_alt_empirical_p0",
        ):
            self.assertIn(field, master_columns)

    def test_build_master_rows_alt_a_fields_default_to_none_when_off(self) -> None:
        """When stage1_shadow_empirical_mode is 'off' (or absent from
        the candidate row), the alt fields propagate as None / False
        without raising. Mirrors the runtime's behavior on rows
        emitted before Alt-A shadow was active (pre-2026-05-17) or
        when the operator didn't pass --stage1-shadow-empirical-mode
        shadow.
        """
        master_columns = bust._build_master_columns([1], 30)
        session_rows = {
            "bet-no-alt-a": {
                "mode": "paper",
                "session_date": "2026-05-10",
                "session_path": "session.json",
                "session_params": {},
                "bet": {
                    "bet_id": "bet-no-alt-a",
                    "placed_at": "2026-05-10T17:00:00Z",
                    "game_pk": 1,
                    "away_abbrev": "AAA",
                    "home_abbrev": "BBB",
                    "line": "8.5",
                    "side": "over",
                    "inning": 6,
                    "inning_state": "Top",
                    "outs": 1,
                    "runners_on": 0,
                    "away_score_before": 4,
                    "home_score_before": 3,
                    "inferred_runs": 1,
                    "entry_ask": 0.65,
                    "decision_ask": 0.65,
                    "fair_value": 0.80,
                    "base_fair_value": 0.78,
                    "edge": 0.15,
                    "stake": 10.0,
                },
            }
        }
        # Pre-Alt-A candidate row: no shadow fields at all.
        candidate_rows = {
            "bet-no-alt-a": {
                "bet_id": "bet-no-alt-a",
                "decision": "trade",
            }
        }
        rows = bust.build_master_rows_for_mode(
            mode="paper",
            session_rows=session_rows,
            ledger_events={},
            captures={},
            candidate_rows=candidate_rows,
            min_date=None,
            max_date=None,
            horizons=[1],
            fill_window_secs=30,
            master_columns=master_columns,
            warnings=[],
            hard_errors=[],
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIsNone(row["stage1_shadow_empirical_mode"])
        self.assertIsNone(row["fair_value_alt_empirical"])
        self.assertIsNone(row["fair_value_alt_empirical_p0"])
        # _safe_bool(None) returns None, not False. Downstream
        # consumers must treat None as "no shadow data" not "False".
        self.assertIsNone(row["fair_value_alt_empirical_used_empirical"])

    def test_compute_phase2_capture_features(self) -> None:
        cap = self._capture()
        feats = bust._compute_phase2_capture_features(
            cap=cap,
            horizons=[1, 2, 5, 10, 30],
            fill_window_secs=30,
            t0_best_ask=0.80,
            entry_ask=0.80,
            limit_price=0.77,
        )

        self.assertAlmostEqual(feats["ask_1s"], 0.82, places=6)
        self.assertAlmostEqual(feats["bid_1s"], 0.79, places=6)
        self.assertAlmostEqual(feats["ask_2s"], 0.79, places=6)
        self.assertAlmostEqual(feats["ask_5s"], 0.76, places=6)
        self.assertAlmostEqual(feats["ask_10s"], 0.75, places=6)
        self.assertAlmostEqual(feats["ask_30s"], 0.70, places=6)

        self.assertAlmostEqual(feats["ask_move_2s"], -0.01, places=6)
        self.assertAlmostEqual(feats["ask_move_5s"], -0.04, places=6)
        # Positive means ask dropped over 5 seconds.
        self.assertAlmostEqual(feats["ask_velocity_5s_cents"], 0.8, places=6)

        self.assertAlmostEqual(feats["min_ask_30s"], 0.70, places=6)
        self.assertAlmostEqual(feats["max_ask_30s"], 0.82, places=6)
        self.assertAlmostEqual(feats["min_spread_30s"], 0.02, places=6)
        self.assertAlmostEqual(feats["max_spread_30s"], 0.03, places=6)

        self.assertTrue(feats["sim_filled_30s"])
        self.assertAlmostEqual(feats["sim_fill_time_30s"], 5.0, places=6)
        self.assertAlmostEqual(feats["sim_cents_saved_vs_taker_30s"], 3.0, places=6)

    def test_horizon_extrapolation_guard(self) -> None:
        cap = bust.CaptureData(
            bet_id="short-cap",
            path="short.jsonl",
            header={"type": "signal", "bet_id": "short-cap"},
            t0_book={"best_bid": 0.48, "best_ask": 0.50},
            snapshots=[
                {
                    "type": "snapshot",
                    "seq": 0,
                    "elapsed_s": 0.0,
                    "book": {"best_bid": 0.48, "best_ask": 0.50},
                },
                {
                    "type": "snapshot",
                    "seq": 5,
                    "elapsed_s": 5.0,
                    "book": {"best_bid": 0.47, "best_ask": 0.49},
                },
            ],
        )
        feats = bust._compute_phase2_capture_features(
            cap=cap,
            horizons=[1, 5, 30],
            fill_window_secs=30,
            t0_best_ask=0.50,
            entry_ask=0.50,
            limit_price=0.49,
        )
        self.assertAlmostEqual(feats["ask_1s"], 0.50, places=6)
        self.assertAlmostEqual(feats["ask_5s"], 0.49, places=6)
        self.assertIsNone(feats["ask_30s"])
        self.assertIsNone(feats["bid_30s"])


if __name__ == "__main__":
    unittest.main()
