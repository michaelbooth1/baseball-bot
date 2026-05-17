import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_DIR = Path(__file__).resolve().parents[1]
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
if str(TRADING_DIR) not in sys.path:
    sys.path.insert(0, str(TRADING_DIR))

from candidate_logging import record_candidate_decision  # noqa: E402


class CandidateLoggingCompactionTests(unittest.TestCase):
    def _engine(self, root: Path):
        (root / "candidate_universe").mkdir(parents=True, exist_ok=True)
        engine = SimpleNamespace()
        engine.date_str = "2026-05-09"
        engine._candidate_mode = "live"
        engine.trade_args = SimpleNamespace(paper_root=root)
        engine._candidate_skip_dedup_seen = set()
        engine._candidate_rows_dedup_suppressed = 0
        engine._candidate_rows_write_errors = 0
        engine._candidate_rows_written = 0
        engine._candidate_null_fields_omitted = 0
        engine._candidate_compacted_fields_omitted = 0
        engine._candidate_seq = 0
        engine._score_confirmation_pending = {}
        engine._candidate_log_path = lambda: root / "candidate_universe" / "2026-05-09_candidates.jsonl"
        engine._candidate_calibration_log_path = (
            lambda: root / "candidate_universe" / "2026-05-09_calibration_opportunities.jsonl"
        )
        return engine

    def test_early_skip_rows_drop_verbose_and_legacy_weather_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            engine = self._engine(root)
            payload = {
                "candidate_id": "early_1",
                "decision": "skip",
                "decision_reason": "gate_probable_pitcher_missing",
                "game_pk": 1,
                "line": "8.5",
                "inning": 1,
                "inning_state": "Top",
                "outs": 0,
                "current_total": 0,
                "away_score_before": 0,
                "home_score_before": 0,
                "runners_on": 0,
                "decision_ask": 0.72,
                "weather_cache_available": True,
                "weather_source_provider": "open_meteo",
                "weather_temp_f": 82.0,
                "weather_wind_out_component_mph": 9.5,
                "weather_mlb_schedule_condition": "legacy",
            }

            record_candidate_decision(engine, payload)

            path = root / "candidate_universe" / "2026-05-09_candidates.jsonl"
            row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            self.assertTrue(row["weather_cache_available"])
            self.assertEqual(row["weather_source_provider"], "open_meteo")
            self.assertNotIn("weather_temp_f", row)
            self.assertNotIn("weather_wind_out_component_mph", row)
            self.assertNotIn("weather_mlb_schedule_condition", row)
            self.assertGreater(engine._candidate_compacted_fields_omitted, 0)

    def test_model_bearing_rows_keep_full_weather_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            engine = self._engine(root)
            payload = {
                "candidate_id": "model_1",
                "decision": "skip",
                "decision_reason": "gate_stage2_suppression",
                "game_pk": 2,
                "line": "7.5",
                "inning": 5,
                "inning_state": "Bot",
                "outs": 1,
                "current_total": 4,
                "away_score_before": 2,
                "home_score_before": 2,
                "runners_on": 1,
                "decision_ask": 0.68,
                "fair_value": 0.86,
                "base_fair_value": 0.88,
                "stage2_run_env_delta": -0.02,
                "weather_cache_available": True,
                "weather_temp_f": 82.0,
                "weather_wind_out_component_mph": 9.5,
                "weather_mlb_schedule_wind": "legacy",
            }

            record_candidate_decision(engine, payload)

            path = root / "candidate_universe" / "2026-05-09_candidates.jsonl"
            row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(row["weather_temp_f"], 82.0)
            self.assertEqual(row["weather_wind_out_component_mph"], 9.5)
            self.assertNotIn("weather_mlb_schedule_wind", row)

    def test_calibration_sidecar_keeps_selected_inferred_state_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            engine = self._engine(root)
            payload = {
                "candidate_id": "model_2",
                "decision": "skip",
                "decision_reason": "gate_min_edge",
                "game_pk": 22,
                "line": "8.5",
                "inning": 6,
                "inning_state": "Top",
                "outs": 1,
                "current_total": 5,
                "away_score_before": 3,
                "home_score_before": 2,
                "runners_on": 1,
                "decision_ask": 0.72,
                "fair_value": 0.86,
                "base_fair_value": 0.84,
                "inferred_state_base_poisson": 0.84,
                "inferred_state_base_empirical": 0.77,
                "inferred_state_poisson_minus_empirical": 0.07,
                "inferred_state_n": 41,
                "inferred_state_n_samples": 123,
                "inferred_state_effective_n_proxy": 34.85,
                "inferred_state_stage1_trust_weight": 0.353,
                "inferred_state_stage1_support_bucket": "20-50",
                "inferred_state_exact_cell_support": False,
                "inferred_state_poisson_line_exact": True,
                "inferred_state_empirical_sample_support": 123,
                "inferred_state_fallback_level": 1,
                "inferred_state_base_source": "poisson_runtime",
            }

            record_candidate_decision(engine, payload)

            path = root / "candidate_universe" / "2026-05-09_calibration_opportunities.jsonl"
            row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(row["inferred_state_base_poisson"], 0.84)
            self.assertEqual(row["inferred_state_base_empirical"], 0.77)
            self.assertEqual(row["inferred_state_n"], 41)
            self.assertEqual(row["inferred_state_n_samples"], 123)
            self.assertEqual(row["inferred_state_effective_n_proxy"], 34.85)
            self.assertEqual(row["inferred_state_stage1_trust_weight"], 0.353)
            self.assertEqual(row["inferred_state_stage1_support_bucket"], "20-50")
            self.assertFalse(row["inferred_state_exact_cell_support"])
            self.assertTrue(row["inferred_state_poisson_line_exact"])
            self.assertEqual(row["inferred_state_empirical_sample_support"], 123)
            self.assertEqual(row["inferred_state_base_source"], "poisson_runtime")

    def test_repeated_early_skip_rows_are_raw_sampled_by_state_price_bucket(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            engine = self._engine(root)
            base_payload = {
                "decision": "skip",
                "decision_reason": "gate_probable_pitcher_missing",
                "game_pk": 3,
                "line": "8.5",
                "inning": 2,
                "inning_state": "Top",
                "outs": 0,
                "current_total": 0,
                "away_score_before": 0,
                "home_score_before": 0,
                "runners_on": 0,
                "decision_ask": 0.721,
                "best_bid": 0.69,
            }

            for i in range(26):
                payload = dict(base_payload)
                payload["candidate_id"] = f"early_{i}"
                record_candidate_decision(engine, payload)

            path = root / "candidate_universe" / "2026-05-09_candidates.jsonl"
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(rows), 2)
            self.assertEqual(engine._candidate_rows_written, 2)
            self.assertEqual(engine._candidate_raw_sample_suppressed, 24)
            self.assertEqual(
                engine._candidate_rollup_by_write_status["raw_sample_suppressed"],
                24,
            )


if __name__ == "__main__":
    unittest.main()
