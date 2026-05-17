import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import walk_forward_runner as wfr  # noqa: E402


def _write_signal_rows(path: Path, dates):
    with path.open("w", encoding="utf-8") as f:
        for idx, session_date in enumerate(dates, start=1):
            f.write(
                json.dumps(
                    {
                        "mode": "live",
                        "session_date": session_date,
                        "bet_id": f"bet_{idx}",
                    }
                )
                + "\n"
            )


class WalkForwardRunnerTests(unittest.TestCase):
    def test_plan_windows_rolls_train_val_test_dates(self):
        windows = wfr.plan_windows(
            available_dates=[
                "2026-04-17",
                "2026-04-18",
                "2026-04-19",
                "2026-04-20",
                "2026-04-21",
            ],
            start_date=None,
            end_date=wfr._parse_date("2026-04-21"),
            train_days=2,
            val_days=1,
            test_days=1,
            min_train_dates=2,
        )

        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]["train_dates_with_data"], ["2026-04-17", "2026-04-18"])
        self.assertEqual(windows[0]["val_dates_with_data"], ["2026-04-19"])
        self.assertEqual(windows[0]["test_dates_with_data"], ["2026-04-20"])
        self.assertIsNone(windows[0]["skip_reason"])
        self.assertEqual(windows[1]["test_dates_with_data"], ["2026-04-21"])

    def test_start_date_keeps_prior_training_history(self):
        dates = [
            "2026-04-17",
            "2026-04-18",
            "2026-04-19",
            "2026-04-20",
            "2026-04-21",
        ]
        with tempfile.TemporaryDirectory() as td:
            input_path = Path(td) / "signals_master.jsonl"
            _write_signal_rows(input_path, dates)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                wfr.main(
                    [
                        "--input-path", str(input_path),
                        "--mode", "live",
                        "--train-days", "2",
                        "--val-days", "1",
                        "--start-date", "2026-04-20",
                        "--end-date", "2026-04-21",
                        "--min-train-dates", "2",
                        "--plan-only",
                    ]
                )

            plans = [
                json.loads(line)
                for line in stdout.getvalue().splitlines()
                if line.strip()
            ]
            self.assertEqual(plans[0]["test_start"], "2026-04-20")
            self.assertEqual(plans[0]["n_train_dates_with_data"], 2)
            self.assertIsNone(plans[0]["skip_reason"])

    def test_strict_mode_fails_on_skipped_window(self):
        dates = [
            "2026-04-17",
            "2026-04-18",
            "2026-04-19",
            "2026-04-20",
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_path = root / "signals_master.jsonl"
            _write_signal_rows(input_path, dates)

            with self.assertRaisesRegex(SystemExit, "Strict mode failed"):
                wfr.main(
                    [
                        "--input-path", str(input_path),
                        "--output-root", str(root / "out"),
                        "--mode", "live",
                        "--train-days", "2",
                        "--val-days", "1",
                        "--min-train-dates", "99",
                        "--strict",
                    ]
                )

    def test_aggregate_uses_accuracy_0p5_metric(self):
        summary = wfr.aggregate(
            [
                {
                    "completed": True,
                    "task_metrics": {
                        "signal_win": {
                            "test": {
                                "brier": 0.2,
                                "logloss": 0.5,
                                "auc": 0.75,
                                "accuracy_0p5": 0.8,
                            }
                        }
                    },
                    "baseline_live_engine_results": {"baseline_realized_profit": 1.0},
                    "model_policy_results": {"observed_realized_profit": 0.5},
                },
                {
                    "completed": True,
                    "task_metrics": {
                        "signal_win": {
                            "test": {
                                "brier": 0.4,
                                "logloss": 0.7,
                                "auc": 0.25,
                                "accuracy_0p5": 0.6,
                            }
                        }
                    },
                    "baseline_live_engine_results": {"baseline_realized_profit": -2.0},
                    "model_policy_results": {"observed_realized_profit": -1.0},
                },
            ]
        )

        self.assertAlmostEqual(
            summary["tasks"]["signal_win"]["test_accuracy_0p5"]["mean"],
            0.7,
        )

    def test_observed_order_model_policy_can_skip_negative_ev_orders(self):
        rows = [
            {
                "bet_id": "good",
                "limit_price": 0.50,
                "target_filled": 1,
                "target_win": 1,
                "target_profit": 10.0,
            },
            {
                "bet_id": "bad",
                "limit_price": 0.80,
                "target_filled": 1,
                "target_win": 0,
                "target_profit": -8.0,
            },
        ]
        results = wfr._observed_order_model_policy_results(
            rows,
            {
                "signal_win": {"good": 0.70, "bad": 0.60},
                "execution_fill": {"good": 0.80, "bad": 0.50},
            },
        )

        self.assertEqual(results["status"], "simulated_observed_orders")
        self.assertEqual(results["n_scored_orders"], 2)
        self.assertEqual(results["n_selected"], 1)
        self.assertAlmostEqual(results["observed_realized_profit"], 10.0)

    def test_aggregate_compares_baseline_and_model_policy_results(self):
        summary = wfr.aggregate(
            [
                {
                    "completed": True,
                    "task_metrics": {},
                    "baseline_live_engine_results": {
                        "n_placed": 2,
                        "n_filled": 1,
                        "n_won": 1,
                        "fill_rate": 0.5,
                        "win_rate_filled": 1.0,
                        "baseline_realized_profit": 4.0,
                    },
                    "model_policy_results": {
                        "n_scored_orders": 2,
                        "n_selected": 1,
                        "n_filled_observed": 1,
                        "n_won_observed": 1,
                        "selection_rate": 0.5,
                        "observed_fill_rate": 1.0,
                        "observed_win_rate_filled": 1.0,
                        "observed_realized_profit": 4.0,
                    },
                },
                {
                    "completed": True,
                    "task_metrics": {},
                    "baseline_live_engine_results": {
                        "n_placed": 1,
                        "n_filled": 1,
                        "n_won": 0,
                        "fill_rate": 1.0,
                        "win_rate_filled": 0.0,
                        "baseline_realized_profit": -5.0,
                    },
                    "model_policy_results": {
                        "n_scored_orders": 1,
                        "n_selected": 0,
                        "n_filled_observed": 0,
                        "n_won_observed": 0,
                        "selection_rate": 0.0,
                        "observed_fill_rate": None,
                        "observed_win_rate_filled": None,
                        "observed_realized_profit": 0.0,
                    },
                },
            ]
        )

        self.assertNotIn("trade_outcomes", summary)
        self.assertEqual(summary["model_policy_results"]["status"], "simulated_observed_orders")
        self.assertAlmostEqual(
            summary["baseline_live_engine_results"]["cumulative_baseline_realized_profit"],
            -1.0,
        )
        self.assertAlmostEqual(
            summary["baseline_live_engine_results"]["max_baseline_drawdown_across_test_windows"],
            -5.0,
        )
        self.assertAlmostEqual(
            summary["model_policy_results"]["cumulative_observed_realized_profit"],
            4.0,
        )
        self.assertAlmostEqual(
            summary["model_policy_results"]["incremental_profit_vs_baseline"],
            5.0,
        )

    def test_execution_fill_auc_is_small_sample_warning(self):
        summary = wfr.aggregate(
            [
                {
                    "completed": True,
                    "task_metrics": {
                        "execution_fill": {
                            "test": {
                                "brier": 0.6,
                                "logloss": 1.8,
                                "auc": 0.31,
                                "accuracy_0p5": 0.4,
                            }
                        }
                    },
                    "baseline_live_engine_results": {
                        "n_placed": 4,
                        "n_filled": 2,
                        "n_won": 1,
                        "fill_rate": 0.5,
                        "win_rate_filled": 0.5,
                        "baseline_realized_profit": -5.0,
                    },
                    "model_policy_results": {
                        "n_scored_orders": 4,
                        "n_selected": 1,
                        "n_filled_observed": 0,
                        "n_won_observed": 0,
                        "selection_rate": 0.25,
                        "observed_realized_profit": 0.0,
                    },
                }
            ]
        )

        warnings = summary["warnings"]
        self.assertEqual(warnings[0]["code"], "small_sample_execution_fill_auc")
        self.assertIn("do not enforce yet", warnings[0]["message"])


if __name__ == "__main__":
    unittest.main()
