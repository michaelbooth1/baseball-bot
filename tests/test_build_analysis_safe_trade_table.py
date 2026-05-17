import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_analysis_safe_trade_table as bastt  # noqa: E402


class AnalysisSafeTradeTableTests(unittest.TestCase):
    def test_excludes_error_rows_and_labels_execution_modes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sessions = root / "sessions"
            sessions.mkdir()
            live_ledger = root / "live_orders_ledger.jsonl"
            master_ledger = root / "master_ledger.jsonl"
            out = root / "out"

            session = {
                "date": "2026-05-13",
                "mode": "live",
                "bets": [
                    {
                        "bet_id": "2026-05-13_live_0001",
                        "game_pk": 1,
                        "away_abbrev": "AAA",
                        "home_abbrev": "BBB",
                        "line": "7.5",
                        "side": "over",
                        "order_id": "0xabc",
                        "order_status": "filled",
                        "stake": 8.0,
                        "fill_price": 0.80,
                        "filled_shares": 10.0,
                        "fill_cost_usdc": 8.0,
                        "payout_usdc": 10.0,
                        "profit": 2.0,
                        "won": True,
                    },
                    {
                        "bet_id": "2026-05-13_paper_0002",
                        "game_pk": 2,
                        "away_abbrev": "CCC",
                        "home_abbrev": "DDD",
                        "line": "8.5",
                        "order_id": "paper_fallback_1",
                        "order_status": "filled",
                        "placement_mode": "paper_fallback",
                        "paper_fallback_reason": "clob_balance_error",
                        "stake": 8.0,
                        "fill_price": 0.75,
                        "filled_shares": 10.6666667,
                        "fill_cost_usdc": 8.0,
                        "profit": -8.0,
                        "won": False,
                    },
                    {
                        "bet_id": "2026-05-13_error_0003",
                        "game_pk": 3,
                        "away_abbrev": "EEE",
                        "home_abbrev": "FFF",
                        "line": "9.5",
                        "order_id": "",
                        "order_status": "error",
                        "stake": 8.0,
                        "profit": 0.0,
                    },
                ],
            }
            (sessions / "2026-05-13_session.json").write_text(json.dumps(session), encoding="utf-8")
            live_ledger.write_text(
                "\n".join(
                    [
                        json.dumps({
                            "bet_id": "2026-05-13_live_0001",
                            "order_id": "0xabc",
                            "_event": "filled",
                            "filled_at": "2026-05-13T20:00:00Z",
                            "fill_cost_usdc": 8.0,
                        }),
                        json.dumps({
                            "bet_id": "2026-05-13_live_0001",
                            "order_id": "0xabc",
                            "_event": "filled",
                            "filled_at": "2026-05-13T20:01:00Z",
                            "ask_5s": 0.80,
                        }),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            master_ledger.write_text("", encoding="utf-8")

            args = Namespace(
                sessions_dir=sessions,
                live_orders_ledger=live_ledger,
                master_ledger=master_ledger,
                output_root=out,
                min_date="",
                max_date="",
                include_errors=False,
                strict=False,
            )
            rows, summary = bastt.build_analysis_safe_trade_table(args)
            bastt.write_outputs(rows, summary, out)

            self.assertEqual([row["bet_id"] for row in rows], [
                "2026-05-13_live_0001",
                "2026-05-13_paper_0002",
            ])
            by_id = {row["bet_id"]: row for row in rows}
            self.assertEqual(by_id["2026-05-13_live_0001"]["execution_mode"], "live")
            self.assertTrue(by_id["2026-05-13_live_0001"]["is_live_money"])
            self.assertEqual(by_id["2026-05-13_live_0001"]["ledger_event_count_raw"], 2)
            self.assertEqual(by_id["2026-05-13_live_0001"]["ledger_event_count_deduped"], 1)
            self.assertEqual(by_id["2026-05-13_live_0001"]["ledger_duplicate_event_count"], 1)
            self.assertEqual(by_id["2026-05-13_paper_0002"]["execution_mode"], "paper_fallback")
            self.assertTrue(by_id["2026-05-13_paper_0002"]["is_paper_fallback"])
            self.assertEqual(summary["row_counts"]["excluded"], {"order_status_error": 1})
            self.assertEqual(summary["pnl"]["live_money_profit_usdc"], 2.0)
            self.assertEqual(summary["pnl"]["paper_fallback_profit_usdc"], -8.0)

    def test_backfills_modern_fill_cost_from_shares_and_price(self):
        bet = {
            "bet_id": "2026-05-13_modern_0001",
            "order_id": "0xdef",
            "order_status": "filled",
            "stake": 8.0,
            "fill_price": 0.79,
            "order_size_shares": 10.126582278481012,
            "won": True,
            "profit": 2.13,
        }
        session_entry = {
            "mode": "live",
            "session_date": "2026-05-13",
            "session_path": "session.json",
            "bet": bet,
        }
        row = bastt.build_trade_row("2026-05-13_modern_0001", session_entry, None)
        self.assertEqual(row["filled_shares_source"], "order_size_shares_filled")
        self.assertEqual(row["fill_cost_source"], "filled_shares_x_fill_price")
        self.assertAlmostEqual(row["fill_cost_usdc"], 8.0, places=2)
        self.assertEqual(row["payout_source"], "filled_shares_if_won")
        self.assertAlmostEqual(row["payout_usdc"], 10.13, places=2)


if __name__ == "__main__":
    unittest.main()
