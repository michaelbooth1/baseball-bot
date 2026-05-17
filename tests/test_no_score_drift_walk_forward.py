import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_no_score_drift_paper_ledger as ledger  # noqa: E402
import evaluate_no_score_drift_policy as nsd  # noqa: E402
import no_score_drift_walk_forward as wf  # noqa: E402


def _candidate(**overrides):
    base = {
        "session_date": "2026-05-01",
        "mode": "live",
        "candidate_id": "cand_1",
        "ts": "2026-05-01T17:00:00Z",
        "game_pk": 101,
        "away_abbrev": "AWY",
        "home_abbrev": "HOM",
        "line": "8.5",
        "inning": 4,
        "inning_state": "Top",
        "outs": 0,
        "runners_on": 0,
        "away_score_before": 2,
        "home_score_before": 1,
        "current_total": 3,
        "score_segment_key": "2-1",
        "decision": "shadow_no_score_drift",
        "decision_reason": "state_value_no_score_drift",
        "signal_model_family": "no_score_drift",
        "decision_ask": 0.50,
        "best_bid": 0.47,
        "spread": 0.03,
        "fair_value": 0.66,
        "edge": 0.16,
        "current_state_value_fv_raw": 0.66,
        "current_state_value_base_poisson": 0.64,
        "current_state_value_base_empirical": 0.62,
        "current_state_value_edge": 0.16,
        "current_state_value_empirical_edge": 0.09,
        "shadow_no_score_drift_trigger": "both",
        "stadium_weather_exposure": "open_air",
        "weather_air_density_index": 0.99,
        "weather_wind_out_component_mph": 4.5,
        "weather_effective_air_density_index": 0.99,
        "weather_effective_wind_out_component_mph": 4.5,
    }
    base.update(overrides)
    return base


def _training_row(row_id, date, win, ask=0.50, fv=0.66, regime="poisson_and_empirical_support"):
    profit = 10.0 if win else -10.0
    return {
        "row_id": row_id,
        "bet_id": row_id,
        "session_date": date,
        "mode": "live",
        "signal_model_family": "no_score_drift",
        "decision_ask": ask,
        "best_bid": ask - 0.03,
        "spread": 0.03,
        "fair_value": fv,
        "edge": fv - ask,
        "current_state_value_fv_raw": fv,
        "current_state_value_base_poisson": fv,
        "current_state_value_base_empirical": fv - 0.02,
        "current_state_value_edge": fv - ask,
        "current_state_value_empirical_edge": fv - 0.02 - ask,
        "support_regime": regime,
        "shadow_no_score_drift_trigger": "both",
        "inning": 4,
        "inning_state": "Top",
        "outs": 0,
        "runners_on": 0,
        "current_total": 3,
        "line": "8.5",
        "score_segment_age_secs": 240,
        "score_segment_ticks": 40,
        "score_segment_drawdown": 0.08,
        "baseline_ask": ask + 0.08,
        "ask_jump": 0.08,
        "target_win": 1 if win else 0,
        "paper_decision": "submitted",
        "paper_filled": True,
        "paper_fill_price": ask,
        "paper_profit_usdc": profit,
        "paper_stake_usdc": 10.0,
    }


class NoScoreDriftWalkForwardTests(unittest.TestCase):
    def test_build_training_rows_uses_deduped_policy_rows_and_ledger_fields(self):
        outcomes = {
            ("2026-05-01", "live", 101, "8.5"): {
                "over_hit": True,
                "final_total": 10,
                "final_away": 6,
                "final_home": 4,
            },
        }
        candidates = [
            _candidate(candidate_id="first", decision_ask=0.50),
            _candidate(candidate_id="duplicate_tick", ts="2026-05-01T17:02:00Z", decision_ask=0.49),
        ]
        policy_rows, counts = nsd.build_policy_rows(candidates, outcomes)
        ledger_rows = ledger.build_ledger_rows(
            policy_rows,
            raw_candidates=candidates,
            stake_usdc=10.0,
            daily_budget_usdc=80.0,
            per_game_budget_fraction=1.0,
            price_policy="taker",
        )
        training_rows = wf.build_training_rows(policy_rows, ledger_rows)

        self.assertEqual(counts["duplicate_rows_collapsed"], 1)
        self.assertEqual(len(training_rows), 1)
        row = training_rows[0]
        self.assertEqual(row["signal_model_family"], "no_score_drift")
        self.assertEqual(row["duplicate_candidate_rows"], 2)
        self.assertEqual(row["target_win"], 1)
        self.assertEqual(row["paper_decision"], "submitted")
        self.assertTrue(row["paper_filled"])
        self.assertAlmostEqual(row["paper_profit_usdc"], 10.0)
        self.assertEqual(row["stadium_weather_exposure"], "open_air")
        self.assertAlmostEqual(row["weather_air_density_index"], 0.99)
        self.assertAlmostEqual(row["weather_wind_out_component_mph"], 4.5)
        self.assertAlmostEqual(row["weather_effective_wind_out_component_mph"], 4.5)
        self.assertNotIn("weather_cache_date", wf.MODEL_FEATURE_COLUMNS)
        self.assertNotIn("stadium_id", wf.MODEL_FEATURE_COLUMNS)
        self.assertIn("weather_effective_air_density_index", wf.MODEL_FEATURE_COLUMNS)
        self.assertIn("weather_effective_wind_out_component_mph", wf.MODEL_FEATURE_COLUMNS)

    def test_plan_windows_keeps_prior_history(self):
        windows = wf.plan_windows(
            ["2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04"],
            start_date=None,
            end_date=None,
            train_days=2,
            val_days=1,
            test_days=1,
            min_train_dates=2,
        )
        self.assertEqual(windows[0]["test_start"], "2026-05-04")
        self.assertEqual(windows[0]["train_dates_with_data"], ["2026-05-01", "2026-05-02"])
        self.assertEqual(windows[0]["val_dates_with_data"], ["2026-05-03"])
        self.assertEqual(windows[0]["test_dates_with_data"], ["2026-05-04"])

    def test_run_window_trains_and_reports_model_policy(self):
        rows = [
            _training_row("r1", "2026-05-01", True, ask=0.45, fv=0.70),
            _training_row("r2", "2026-05-01", False, ask=0.70, fv=0.62, regime="poisson_only_support"),
            _training_row("r3", "2026-05-02", True, ask=0.48, fv=0.68),
            _training_row("r4", "2026-05-02", False, ask=0.72, fv=0.61, regime="poisson_only_support"),
            _training_row("v1", "2026-05-03", True, ask=0.46, fv=0.69),
            _training_row("v2", "2026-05-03", False, ask=0.71, fv=0.60, regime="poisson_only_support"),
            _training_row("t1", "2026-05-04", True, ask=0.47, fv=0.68),
            _training_row("t2", "2026-05-04", False, ask=0.73, fv=0.61, regime="poisson_only_support"),
        ]
        window = {
            "test_start": "2026-05-04",
            "test_end": "2026-05-04",
            "train_dates_with_data": ["2026-05-01", "2026-05-02"],
            "val_dates_with_data": ["2026-05-03"],
            "test_dates_with_data": ["2026-05-04"],
            "skip_reason": None,
        }

        result = wf.run_window(
            window,
            rows,
            thresholds=[0.0, 0.05],
            min_train_rows=4,
            strict=True,
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["row_counts"]["test"], 2)
        self.assertEqual(result["probability_metrics"]["poisson_fv"]["rows"], 2)
        self.assertEqual(result["model"]["status"], "trained")
        self.assertIn("test_summary", result["model_policy"])


if __name__ == "__main__":
    unittest.main()
