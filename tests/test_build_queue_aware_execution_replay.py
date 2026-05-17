import unittest

from scripts.analysis import build_queue_aware_execution_replay as replay


class QueueAwareExecutionReplayTests(unittest.TestCase):
    def test_queue_adjusted_fill_requires_ask_to_move_through_limit(self):
        signal_rows = [
            {
                "mode": "live",
                "session_date": "2026-04-30",
                "bet_id": "bet_win",
                "game_pk": 1,
                "away_abbrev": "AWY",
                "home_abbrev": "HOM",
                "line": "8.5",
                "inning": 6,
                "settled": True,
                "won_counterfactual": True,
                "stake": 10.0,
                "decision_ask": 0.63,
                "entry_ask": 0.63,
                "posted_limit": 0.60,
                "fair_value": 0.72,
                "state_value_strategy": "score_event_transition",
            },
        ]
        snapshots = {
            ("live", "bet_win"): [
                {"elapsed_s": 0.0, "seq": 0, "best_ask": 0.63},
                {"elapsed_s": 5.0, "seq": 1, "best_ask": 0.60},
                {"elapsed_s": 10.0, "seq": 2, "best_ask": 0.59},
            ]
        }

        rows = replay.build_replay_rows(
            signal_rows,
            snapshots,
            queue_buffer_cents=1.0,
            max_fill_seconds=120.0,
        )
        by_key = {(row["policy"], row["fill_model"]): row for row in rows}

        current_touch = by_key[("current_limit", "touch")]
        self.assertTrue(current_touch["filled"])
        self.assertAlmostEqual(current_touch["fill_seconds"], 5.0)
        self.assertAlmostEqual(current_touch["fill_price"], 0.60)
        self.assertAlmostEqual(current_touch["realized_profit"], 6.666667, places=6)

        current_queue = by_key[("current_limit", "queue_adjusted")]
        self.assertTrue(current_queue["filled"])
        self.assertAlmostEqual(current_queue["fill_seconds"], 10.0)
        self.assertAlmostEqual(current_queue["fill_price"], 0.60)

        p1_queue = by_key[("limit_p1c", "queue_adjusted")]
        self.assertTrue(p1_queue["filled"])
        self.assertAlmostEqual(p1_queue["fill_seconds"], 5.0)
        self.assertAlmostEqual(p1_queue["fill_price"], 0.61)

        taker = by_key[("taker_like", "queue_adjusted")]
        self.assertTrue(taker["filled"])
        self.assertTrue(taker["marketable_at_entry"])
        self.assertAlmostEqual(taker["fill_seconds"], 0.0)
        self.assertAlmostEqual(taker["fill_price"], 0.63)

    def test_summary_compares_profit_not_just_fill_rate(self):
        signal_rows = [
            {
                "mode": "live",
                "session_date": "2026-04-30",
                "bet_id": "bet_win",
                "game_pk": 1,
                "line": "8.5",
                "settled": True,
                "won_counterfactual": True,
                "stake": 10.0,
                "decision_ask": 0.63,
                "posted_limit": 0.60,
                "fair_value": 0.72,
            },
            {
                "mode": "live",
                "session_date": "2026-04-30",
                "bet_id": "bet_loss",
                "game_pk": 2,
                "line": "9.5",
                "settled": True,
                "won_counterfactual": False,
                "stake": 10.0,
                "decision_ask": 0.62,
                "posted_limit": 0.60,
                "fair_value": 0.76,
            },
        ]
        snapshots = {
            ("live", "bet_win"): [
                {"elapsed_s": 0.0, "seq": 0, "best_ask": 0.63},
                {"elapsed_s": 5.0, "seq": 1, "best_ask": 0.60},
            ],
            ("live", "bet_loss"): [
                {"elapsed_s": 0.0, "seq": 0, "best_ask": 0.62},
                {"elapsed_s": 5.0, "seq": 1, "best_ask": 0.62},
            ],
        }

        rows = replay.build_replay_rows(
            signal_rows,
            snapshots,
            queue_buffer_cents=1.0,
            max_fill_seconds=120.0,
        )
        summary = replay.build_summary(rows, config={}, warnings=[])

        current = summary["by_policy"]["current_limit"]["touch"]
        p2 = summary["by_policy"]["limit_p2c"]["touch"]
        taker = summary["by_policy"]["taker_like"]["touch"]

        self.assertEqual(current["filled"], 1)
        self.assertEqual(p2["filled"], 2)
        self.assertEqual(taker["filled"], 2)
        self.assertGreater(current["realized_profit"], p2["realized_profit"])
        self.assertGreater(current["realized_profit"], taker["realized_profit"])
        self.assertLess(p2["profit_delta_vs_current_limit"], 0)
        self.assertLess(taker["profit_delta_vs_current_limit"], 0)


if __name__ == "__main__":
    unittest.main()
