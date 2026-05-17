import unittest

from scripts.analysis import build_signal_training_table as bstt


class BuildSignalTrainingTableTests(unittest.TestCase):
    def test_allocate_split_counts(self) -> None:
        self.assertEqual(bstt._allocate_split_counts(0, 0.15, 0.15), (0, 0, 0))
        self.assertEqual(bstt._allocate_split_counts(1, 0.15, 0.15), (1, 0, 0))
        self.assertEqual(bstt._allocate_split_counts(2, 0.15, 0.15), (1, 0, 1))
        self.assertEqual(bstt._allocate_split_counts(3, 0.15, 0.15), (1, 1, 1))
        self.assertEqual(bstt._allocate_split_counts(10, 0.15, 0.15), (6, 2, 2))

    def test_build_date_split_map_temporal_order(self) -> None:
        split_map, date_rank, split_dates = bstt._build_date_split_map(
            dates=["2026-04-03", "2026-04-01", "2026-04-02", "2026-04-03"],
            val_frac=0.2,
            test_frac=0.2,
        )
        self.assertEqual(date_rank["2026-04-01"], 0)
        self.assertEqual(date_rank["2026-04-02"], 1)
        self.assertEqual(date_rank["2026-04-03"], 2)
        self.assertEqual(split_dates["train"], ["2026-04-01"])
        self.assertEqual(split_dates["validation"], ["2026-04-02"])
        self.assertEqual(split_dates["test"], ["2026-04-03"])
        self.assertEqual(split_map["2026-04-01"], "train")
        self.assertEqual(split_map["2026-04-02"], "validation")
        self.assertEqual(split_map["2026-04-03"], "test")

    def test_build_training_rows_labels(self) -> None:
        rows = [
            {
                "mode": "live",
                "bet_id": "2026-04-20_a",
                "session_date": "2026-04-20",
                "game_pk": 1,
                "line": "8.5",
                "side": "OVER",
                "settled": True,
                "realized_executed": True,
                "realized_profit": 12.5,
                "realized_win": True,
                "entry_ask": 0.7,
                "signal_model_family": "score_event_transition",
                "state_value_strategy": "score_event_transition",
                "current_state_value_edge": 0.06,
                "shadow_phantom_risk_score": 0.25,
                "shadow_fv_inferred_lift": 0.08,
                "stadium_weather_exposure": "open_air",
                "weather_air_density_index": 0.985,
                "weather_wind_out_component_mph": 5.0,
                "ask_1s": 0.71,
                "param_edge_threshold": 0.15,
            },
            {
                "mode": "live",
                "bet_id": "2026-04-21_b",
                "session_date": "2026-04-21",
                "game_pk": 2,
                "line": "9.5",
                "side": "OVER",
                "settled": False,
                "realized_executed": False,
                "realized_profit": None,
                "realized_win": None,
                "entry_ask": 0.6,
                "signal_model_family": "score_event_transition",
                "state_value_strategy": "score_event_transition",
                "current_state_value_edge": -0.04,
                "shadow_phantom_risk_score": 0.72,
                "shadow_fv_inferred_lift": 0.22,
                "stadium_weather_exposure": "retractable_roof",
                "weather_air_density_index": 1.012,
                "weather_wind_out_component_mph": -2.0,
                "ask_1s": 0.62,
                "param_edge_threshold": 0.15,
            },
        ]
        split_map = {"2026-04-20": "train", "2026-04-21": "test"}
        date_rank = {"2026-04-20": 0, "2026-04-21": 1}
        out = bstt.build_training_rows(
            rows=rows,
            pre_signal_columns=[
                "entry_ask",
                "signal_model_family",
                "state_value_strategy",
                "current_state_value_edge",
                "shadow_phantom_risk_score",
                "shadow_fv_inferred_lift",
                "stadium_weather_exposure",
                "weather_air_density_index",
                "weather_wind_out_component_mph",
            ],
            post_signal_columns=["ask_1s"],
            param_columns=["param_edge_threshold"],
            split_map=split_map,
            date_rank=date_rank,
            drop_unsettled=False,
        )
        self.assertEqual(len(out), 2)
        settled_row = next(r for r in out if r["bet_id"] == "2026-04-20_a")
        unsettled_row = next(r for r in out if r["bet_id"] == "2026-04-21_b")

        self.assertEqual(settled_row["split"], "train")
        self.assertEqual(settled_row["target_filled"], 1)
        self.assertEqual(settled_row["target_win"], 1)
        self.assertAlmostEqual(settled_row["target_profit"], 12.5, places=6)
        self.assertEqual(settled_row["state_value_strategy"], "score_event_transition")
        self.assertEqual(settled_row["signal_model_family"], "score_event_transition")
        self.assertAlmostEqual(settled_row["current_state_value_edge"], 0.06, places=6)
        self.assertAlmostEqual(settled_row["shadow_phantom_risk_score"], 0.25, places=6)
        self.assertAlmostEqual(settled_row["shadow_fv_inferred_lift"], 0.08, places=6)
        self.assertEqual(settled_row["stadium_weather_exposure"], "open_air")
        self.assertAlmostEqual(settled_row["weather_air_density_index"], 0.985, places=6)
        self.assertAlmostEqual(settled_row["weather_wind_out_component_mph"], 5.0, places=6)

        self.assertEqual(unsettled_row["split"], "test")
        self.assertFalse(unsettled_row["label_available"])
        self.assertIsNone(unsettled_row["target_filled"])
        self.assertIsNone(unsettled_row["target_win"])
        self.assertIsNone(unsettled_row["target_profit"])

    def test_state_value_fields_are_pre_signal_features(self) -> None:
        self.assertIn("signal_model_family", bstt.PRE_SIGNAL_COLUMNS)
        self.assertIn("state_value_strategy", bstt.PRE_SIGNAL_COLUMNS)
        self.assertIn("home_leading_late", bstt.PRE_SIGNAL_COLUMNS)
        self.assertIn("expected_remaining_half_innings", bstt.PRE_SIGNAL_COLUMNS)
        self.assertIn("home_skip_bottom9_risk", bstt.PRE_SIGNAL_COLUMNS)
        self.assertIn("current_state_value_edge", bstt.PRE_SIGNAL_COLUMNS)
        self.assertIn("current_state_value_base_empirical", bstt.PRE_SIGNAL_COLUMNS)
        self.assertIn("shadow_phantom_risk_score", bstt.PRE_SIGNAL_COLUMNS)
        self.assertIn("shadow_fv_inferred_lift", bstt.PRE_SIGNAL_COLUMNS)
        self.assertIn("shadow_ltp_ask_gap", bstt.PRE_SIGNAL_COLUMNS)
        self.assertIn("stadium_weather_exposure", bstt.PRE_SIGNAL_COLUMNS)
        self.assertIn("weather_air_density_index", bstt.PRE_SIGNAL_COLUMNS)
        self.assertIn("weather_wind_out_component_mph", bstt.PRE_SIGNAL_COLUMNS)
        self.assertIn("weather_effective_wind_out_component_mph", bstt.PRE_SIGNAL_COLUMNS)
        self.assertIn("weather_effective_wind_out_component_mph", bstt.PRE_SIGNAL_MODEL_COLUMNS)
        self.assertNotIn("weather_cache_date", bstt.PRE_SIGNAL_MODEL_COLUMNS)
        self.assertNotIn("weather_selected_time_utc", bstt.PRE_SIGNAL_MODEL_COLUMNS)
        self.assertNotIn("stadium_id", bstt.PRE_SIGNAL_MODEL_COLUMNS)


if __name__ == "__main__":
    unittest.main()
