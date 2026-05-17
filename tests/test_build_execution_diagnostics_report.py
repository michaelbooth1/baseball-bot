import unittest

from scripts.analysis import build_execution_diagnostics_report as bedr


class BuildExecutionDiagnosticsReportTests(unittest.TestCase):
    def test_first_limit_touch_detects_earliest_touch(self) -> None:
        snapshots = [
            {"seq": 0, "elapsed_s": 0.0, "best_ask": 0.72, "ts": "2026-04-23T10:00:00Z"},
            {"seq": 1, "elapsed_s": 1.0, "best_ask": 0.70, "ts": "2026-04-23T10:00:01Z"},
            {"seq": 2, "elapsed_s": 2.0, "best_ask": 0.68, "ts": "2026-04-23T10:00:02Z"},
            {"seq": 3, "elapsed_s": 3.0, "best_ask": 0.66, "ts": "2026-04-23T10:00:03Z"},
        ]
        touched, first_s, first_ask, first_ts, window_s = bedr._first_limit_touch(
            snapshots,
            posted_limit=0.69,
        )
        self.assertTrue(touched)
        self.assertAlmostEqual(first_s, 2.0, places=6)
        self.assertAlmostEqual(first_ask, 0.68, places=6)
        self.assertEqual(first_ts, "2026-04-23T10:00:02Z")
        self.assertAlmostEqual(window_s, 3.0, places=6)

    def test_build_rows_includes_cancel_reason_and_counterfactual(self) -> None:
        master_rows = [
            {
                "mode": "live",
                "session_date": "2026-04-22",
                "bet_id": "2026-04-22_test_0001",
                "game_pk": 123,
                "away_abbrev": "NYY",
                "home_abbrev": "BOS",
                "line": "8.5",
                "inning": 7,
                "order_status_final": "cancelled",
                "cancel_reason_final": "timeout",
                "settled": True,
                "won_counterfactual": True,
                "realized_executed": False,
                "realized_win": False,
                "entry_ask": 0.64,
                "posted_limit": 0.62,
                "filled_at": None,
                "cancelled_at": "2026-04-22T20:05:00Z",
                "sim_filled_30s": True,
                "sim_fill_time_30s": 4.0,
            }
        ]
        snapshot_map = {
            ("live", "2026-04-22_test_0001"): [
                {"seq": 0, "elapsed_s": 0.0, "best_ask": 0.64, "ts": "2026-04-22T20:00:00Z"},
                {"seq": 1, "elapsed_s": 4.0, "best_ask": 0.62, "ts": "2026-04-22T20:00:04Z"},
                {"seq": 2, "elapsed_s": 8.0, "best_ask": 0.61, "ts": "2026-04-22T20:00:08Z"},
            ]
        }

        rows = bedr.build_diagnostics_rows(
            master_rows=master_rows,
            snapshot_map=snapshot_map,
            mode="live",
            min_date=None,
            max_date=None,
            warnings=[],
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["cancel_reason"], "timeout")
        self.assertEqual(row["counterfactual_outcome"], "win")
        self.assertTrue(row["limit_touch"])
        self.assertAlmostEqual(row["first_touch_seconds"], 4.0, places=6)
        self.assertAlmostEqual(row["first_touch_ask"], 0.62, places=6)

    def test_build_rows_falls_back_to_limit_price(self) -> None:
        master_rows = [
            {
                "mode": "live",
                "session_date": "2026-04-22",
                "bet_id": "2026-04-22_test_0002",
                "game_pk": 456,
                "line": "9.5",
                "order_status_final": "filled",
                "settled": True,
                "won_counterfactual": False,
                "limit_price": 0.57,
                "filled_at": "2026-04-22T21:00:05Z",
                "cancelled_at": None,
            }
        ]
        snapshot_map = {
            ("live", "2026-04-22_test_0002"): [
                {"seq": 0, "elapsed_s": 0.0, "best_ask": 0.60, "ts": "2026-04-22T21:00:00Z"},
                {"seq": 1, "elapsed_s": 5.0, "best_ask": 0.57, "ts": "2026-04-22T21:00:05Z"},
            ]
        }
        rows = bedr.build_diagnostics_rows(
            master_rows=master_rows,
            snapshot_map=snapshot_map,
            mode="live",
            min_date=None,
            max_date=None,
            warnings=[],
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertAlmostEqual(row["posted_limit"], 0.57, places=6)
        self.assertTrue(row["limit_touch"])
        self.assertAlmostEqual(row["first_touch_seconds"], 5.0, places=6)
        self.assertEqual(row["counterfactual_outcome"], "loss")

    def test_counterfactual_profit_if_filled_limit(self) -> None:
        win_profit = bedr._counterfactual_profit_if_filled_limit(
            stake=20.0,
            posted_limit=0.50,
            settled=True,
            won_counterfactual=True,
        )
        self.assertAlmostEqual(win_profit, 20.0, places=6)

        loss_profit = bedr._counterfactual_profit_if_filled_limit(
            stake=20.0,
            posted_limit=0.50,
            settled=True,
            won_counterfactual=False,
        )
        self.assertAlmostEqual(loss_profit, -20.0, places=6)

        self.assertIsNone(
            bedr._counterfactual_profit_if_filled_limit(
                stake=20.0,
                posted_limit=0.50,
                settled=False,
                won_counterfactual=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
