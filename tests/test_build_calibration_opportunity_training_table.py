import json
import tempfile
import unittest
from pathlib import Path

from scripts.analysis import build_calibration_opportunity_training_table as cot


def _opportunity(**overrides):
    base = {
        "schema_version": 1,
        "session_date": "2026-05-07",
        "mode": "live",
        "candidate_id": "cand_score_1",
        "outcome_join_key": "2026-05-07|101|8.5",
        "game_pk": 101,
        "away_abbrev": "AWY",
        "home_abbrev": "HOM",
        "line": "8.5",
        "side": "over",
        "decision": "skip_with_features",
        "decision_reason": "gate_min_edge",
        "signal_model_family": "score_event_transition",
        "state_value_strategy": "score_event_transition",
        "inning": 6,
        "inning_state": "Top",
        "outs": 1,
        "runners_on": 3,
        "away_score_before": 4,
        "home_score_before": 3,
        "current_total": 7,
        "runs_needed": 1.5,
        "decision_ask": 0.74,
        "best_bid": 0.70,
        "spread": 0.04,
        "fair_value": 0.90,
        "edge": 0.16,
        "current_state_value_edge": 0.04,
        "shadow_phantom_risk_score": 0.35,
        "weather_cache_available": True,
        "stadium_weather_exposure": "open",
        "weather_effective_air_density_index": 0.99,
        "weather_effective_wind_out_component_mph": 5.0,
        "execution_policy_current_limit_price": 0.72,
    }
    base.update(overrides)
    return base


class CalibrationOpportunityTrainingTableTests(unittest.TestCase):
    def test_build_training_rows_joins_final_and_score_confirmation_labels(self):
        rows = [_opportunity()]
        confirmations = {
            "cand_score_1": {
                "candidate_id": "cand_score_1",
                "confirmation_status": "score_changed",
                "score_confirmation_latency_secs": 17.5,
                "observed_away_score": 5,
                "observed_home_score": 3,
                "observed_total": 8,
                "score_delta_away": 1,
                "score_delta_home": 0,
                "score_delta_total": 1,
                "score_confirmed_within_10s": False,
                "score_confirmed_within_30s": True,
                "score_confirmed_within_60s": True,
            }
        }
        finals = {101: {"final_away": 6, "final_home": 4, "final_total": 10}}
        out = cot.build_training_rows(
            rows,
            confirmations_by_candidate_id=confirmations,
            final_scores_by_game_pk=finals,
            split_map={"2026-05-07": "train"},
            date_rank={"2026-05-07": 0},
        )

        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertEqual(row["signal_model_family"], "score_event_transition")
        self.assertTrue(row["label_final_available"])
        self.assertEqual(row["final_total"], 10)
        self.assertEqual(row["target_over_win"], 1)
        self.assertAlmostEqual(row["target_taker_profit_units"], 1.0 / 0.74 - 1.0)
        self.assertAlmostEqual(row["target_limit_profit_units"], 1.0 / 0.72 - 1.0)
        self.assertTrue(row["label_score_confirmation_available"])
        self.assertEqual(row["target_score_changed_any"], 1)
        self.assertEqual(row["target_score_confirmed_10s"], 0)
        self.assertEqual(row["target_score_confirmed_30s"], 1)
        self.assertEqual(row["target_score_confirmed_60s"], 1)
        self.assertEqual(row["target_no_score_change_60s"], 0)
        self.assertEqual(row["target_phantom_no_score_60s"], 0)
        self.assertEqual(row["split"], "train")
        self.assertEqual(row["shadow_current_state_edge_bucket"], "current_edge_0.03-0.08")
        self.assertEqual(row["weather_effective_air_density_index"], 0.99)

    def test_no_score_drift_rows_keep_final_label_without_phantom_target(self):
        row = _opportunity(
            candidate_id="cand_nsd_1",
            decision="shadow_no_score_drift",
            decision_reason="state_value_no_score_drift",
            signal_model_family="no_score_drift",
            state_value_strategy="no_score_drift",
            decision_ask=0.55,
            line="9.5",
            game_pk=102,
        )
        out = cot.build_training_rows(
            [row],
            confirmations_by_candidate_id={},
            final_scores_by_game_pk={102: {"final_away": 4, "final_home": 4, "final_total": 8}},
            split_map={"2026-05-07": "train"},
            date_rank={"2026-05-07": 0},
        )

        self.assertEqual(out[0]["signal_model_family"], "no_score_drift")
        self.assertEqual(out[0]["target_over_win"], 0)
        self.assertEqual(out[0]["target_taker_profit_units"], -1.0)
        self.assertFalse(out[0]["label_score_confirmation_available"])
        self.assertIsNone(out[0]["target_phantom_no_score_60s"])

    def test_score_event_repeat_policy_dedupes_same_state_reason(self):
        rows = cot.build_training_rows(
            [
                _opportunity(candidate_id="cand_a", signal_ts_epoch=10.0, decision_ask=0.72),
                _opportunity(candidate_id="cand_b", signal_ts_epoch=11.0, decision_ask=0.76),
                _opportunity(
                    candidate_id="cand_c",
                    signal_ts_epoch=12.0,
                    outs=2,
                    decision_ask=0.76,
                ),
            ],
            confirmations_by_candidate_id={},
            final_scores_by_game_pk={101: {"final_away": 6, "final_home": 4, "final_total": 10}},
            split_map={"2026-05-07": "train"},
            date_rank={"2026-05-07": 0},
        )

        deduped, stats = cot.apply_score_event_repeat_policy(rows, policy="dedupe")

        self.assertEqual(len(deduped), 2)
        self.assertEqual(stats["score_event_repeated_groups"], 1)
        self.assertEqual(stats["score_event_collapsed_rows"], 1)
        representative = next(r for r in deduped if r["outs"] == 1)
        self.assertEqual(representative["candidate_id"], "cand_a")
        self.assertEqual(representative["calibration_repeat_group_size"], 2)
        self.assertEqual(representative["calibration_row_weight"], 1.0)

    def test_score_event_repeat_policy_weight_mode_keeps_rows_with_inverse_group_weight(self):
        rows = cot.build_training_rows(
            [
                _opportunity(candidate_id="cand_a", signal_ts_epoch=10.0),
                _opportunity(candidate_id="cand_b", signal_ts_epoch=11.0),
            ],
            confirmations_by_candidate_id={},
            final_scores_by_game_pk={101: {"final_away": 6, "final_home": 4, "final_total": 10}},
            split_map={"2026-05-07": "train"},
            date_rank={"2026-05-07": 0},
        )

        weighted, stats = cot.apply_score_event_repeat_policy(rows, policy="weight")

        self.assertEqual(len(weighted), 2)
        self.assertEqual(stats["score_event_collapsed_rows"], 0)
        self.assertTrue(all(r["calibration_repeat_group_size"] == 2 for r in weighted))
        self.assertTrue(all(abs(r["calibration_row_weight"] - 0.5) < 1e-9 for r in weighted))

    def test_backfills_selected_inferred_state_from_raw_candidate_and_panel(self):
        row = _opportunity(
            candidate_id="cand_backfill",
            inferred_state_base_poisson=None,
            inferred_state_base_empirical=None,
            inferred_state_n=None,
            inference_panel_selected_runs=2,
            inference_run2_base_poisson=0.81,
            inference_run2_base_empirical=0.74,
            inference_run2_poisson_minus_empirical=0.07,
            inference_run2_n_samples=64,
            inference_run2_fallback_level=2,
            inference_run2_line_source_key="O8.5",
            inference_run2_empirical_line_source_key="O8.5",
        )
        raw = {
            "cand_backfill": {
                "inferred_state_n": 44,
                "inferred_state_n_samples": 88,
                "inferred_state_cell_key": "raw-cell",
            }
        }

        stats = cot.backfill_selected_inferred_state_fields(
            [row],
            raw_backfill_by_candidate_id=raw,
        )

        self.assertEqual(row["inferred_state_base_poisson"], 0.81)
        self.assertEqual(row["inferred_state_base_empirical"], 0.74)
        self.assertAlmostEqual(row["inferred_state_empirical_edge"], 0.0)
        self.assertEqual(row["inferred_state_n"], 44)
        self.assertEqual(row["inferred_state_n_samples"], 88)
        self.assertEqual(row["inferred_state_cell_key"], "raw-cell")
        self.assertEqual(row["inferred_state_fallback_level"], 2)
        self.assertAlmostEqual(row["inferred_state_effective_n_proxy"], 15.4)
        self.assertEqual(row["inferred_state_stage1_support_bucket"], "<20")
        self.assertEqual(row["inferred_state_empirical_sample_support"], 88)
        self.assertEqual(row["inferred_state_empirical_sample_bucket"], "50-100")
        self.assertTrue(row["inferred_state_used_fallback"])
        self.assertEqual(row["inferred_state_base_source"], "poisson_runtime")
        self.assertEqual(stats["rows_with_raw_backfill"], 1)
        self.assertEqual(stats["rows_with_panel_derivation"], 1)
        self.assertEqual(stats["rows_with_selected_summary_after"], 1)

    def test_backfills_current_stage1_support_from_cache_cell_key(self):
        row = _opportunity(
            candidate_id="cand_current_support",
            current_state_value_state_cell_key="cell-current",
            current_state_value_state_fallback_level=0,
            current_state_value_line_fallback_mode="exact",
            current_state_value_line_key_empirical="o85",
        )
        stats = cot.backfill_stage1_support_fields(
            [row],
            cells={"cell-current": {"n": 160, "n_samples": 320, "o85": 0.62, "po85": 0.66}},
        )

        self.assertEqual(stats["rows_with_current_support_backfill"], 1)
        self.assertEqual(row["current_state_value_effective_n_proxy"], 160)
        self.assertGreater(row["current_state_value_stage1_trust_weight"], 0.8)
        self.assertEqual(row["current_state_value_stage1_support_bucket"], "100-250")
        self.assertTrue(row["current_state_value_exact_cell_support"])
        self.assertTrue(row["current_state_value_poisson_line_exact"])
        self.assertTrue(row["current_state_value_empirical_line_exact"])
        self.assertEqual(row["current_state_value_empirical_sample_support"], 320)

    def test_main_writes_table_and_manifest_from_temp_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live_root = root / "live"
            games_root = root / "games"
            out_root = root / "out"
            live_root.mkdir(parents=True)
            game_dir = games_root / "2026" / "2026-05-07"
            game_dir.mkdir(parents=True)

            calibration_row = _opportunity()
            calibration_row["inferred_state_base_poisson"] = 0.91
            (live_root / "2026-05-07_calibration_opportunities.jsonl").write_text(
                json.dumps(calibration_row) + "\n",
                encoding="utf-8",
            )
            (live_root / "2026-05-07_candidates.jsonl").write_text(
                json.dumps(
                    {
                        "candidate_id": "cand_score_1",
                        "session_date": "2026-05-07",
                        "mode": "live",
                        "inferred_state_base_poisson": 0.91,
                        "inferred_state_base_empirical": 0.82,
                        "inferred_state_n": 55,
                        "inferred_state_n_samples": 155,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (live_root / "2026-05-07_score_confirmations.jsonl").write_text(
                json.dumps(
                    {
                        "candidate_id": "cand_score_1",
                        "confirmation_status": "no_score_change_within_60s",
                        "score_confirmed_within_10s": False,
                        "score_confirmed_within_30s": False,
                        "score_confirmed_within_60s": False,
                        "score_delta_total": 0,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (game_dir / "101.json").write_text(
                json.dumps(
                    {
                        "gamePk": 101,
                        "liveData": {
                            "linescore": {
                                "teams": {
                                    "away": {"runs": 6},
                                    "home": {"runs": 4},
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            cot.main(
                [
                    "--mode",
                    "live",
                    "--min-date",
                    "2026-05-07",
                    "--max-date",
                    "2026-05-07",
                    "--live-root",
                    str(live_root),
                    "--games-root",
                    str(games_root),
                    "--output-root",
                    str(out_root),
                    "--strict",
                ]
            )

            table_path = out_root / "calibration_opportunity_training_table.jsonl"
            manifest_path = out_root / "calibration_opportunity_training_table_manifest.json"
            rows = [
                json.loads(line)
                for line in table_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            family_path = (
                out_root
                / "by_family"
                / "calibration_opportunity_training_table_score_event_transition.jsonl"
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["target_over_win"], 1)
            self.assertEqual(rows[0]["target_no_score_change_60s"], 1)
            self.assertEqual(rows[0]["target_phantom_no_score_60s"], 1)
            self.assertEqual(rows[0]["inferred_state_base_poisson"], 0.91)
            self.assertEqual(rows[0]["inferred_state_base_empirical"], 0.82)
            self.assertEqual(rows[0]["inferred_state_n"], 55)
            self.assertEqual(manifest["counts"]["training_rows_total"], 1)
            self.assertEqual(
                manifest["counts"]["selected_inferred_state_backfill"]["rows_with_raw_backfill"],
                1,
            )
            self.assertEqual(
                manifest["counts"]["score_event_repeat_policy"]["policy"],
                "dedupe",
            )
            self.assertIn(
                "weather_effective_air_density_index",
                manifest["column_groups"]["pre_signal_model_features"],
            )
            self.assertIn(
                "target_phantom_no_score_60s",
                manifest["column_groups"]["label_columns"],
            )
            self.assertTrue(family_path.exists())
            self.assertEqual(
                manifest["family_outputs"]["score_event_transition"]["rows"],
                1,
            )


if __name__ == "__main__":
    unittest.main()
