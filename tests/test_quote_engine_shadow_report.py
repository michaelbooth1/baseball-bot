"""Phase C shadow ledger + summary report tests (2026-05-17).

Covers:
  - shadow_ledger_path + append_shadow_decision (writer)
  - build_report distribution math
  - skip-reason aggregation
  - hedge-opportunity aggregation
  - Empty-ledger / day-zero path
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
for d in (TRADING_DIR, ANALYSIS_DIR):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))

import live_quote_engine as lqe  # noqa: E402
import build_quote_engine_shadow_report as bqes  # noqa: E402


def _shadow_row(**kw):
    """Build a shadow-decision row matching QuoteDecision.to_dict()."""
    base = {
        "schema_version": 1,
        "game_pk": 1,
        "line": "8.5",
        "over_best_bid": 0.71,
        "over_best_ask": 0.74,
        "under_best_bid": 0.26,
        "under_best_ask": 0.29,
        "over_fair_value": 0.72,
        "under_fair_value": 0.28,
        "under_pair_available": True,
        "net_inventory_over_shares": 0.0,
        "would_quote_bid": 0.70,
        "would_quote_ask": 0.74,
        "inventory_shade": 0.0,
        "half_spread": 0.02,
        "bid_anchor_price": 0.70,
        "ask_anchor_price": 0.74,
        "bid_skipped_reason": "ok",
        "ask_skipped_reason": "ok",
        "hedge_opportunity": False,
        "hedge_side": None,
        "hedge_target_price": None,
        "hedge_max_price": None,
        "hedge_reason": "no_inventory_to_hedge",
        "config_snapshot": {"half_spread": 0.02},
    }
    base.update(kw)
    return base


class ShadowLedgerWriterTests(unittest.TestCase):
    def test_shadow_ledger_path_is_per_date(self):
        path = lqe.shadow_ledger_path(Path("/tmp/data"), "2026-05-17")
        self.assertEqual(path.name, "2026-05-17_quotes.jsonl")
        self.assertEqual(path.parent.name, "quote_engine_shadow")

    def test_append_creates_directory_and_writes_row(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = lqe.shadow_ledger_path(root, "2026-05-17")
            ctx = lqe.QuoteDecisionContext(
                game_pk=1, line="8.5",
                over_best_bid=0.71, over_best_ask=0.74,
                under_best_bid=0.26, under_best_ask=0.29,
                over_fair_value=0.72, under_fair_value=0.28,
                under_pair_available=True,
                net_inventory_over_shares=0.0,
            )
            d = lqe.compute_quote_decision(ctx)
            ok = lqe.append_shadow_decision(path, d)
            self.assertTrue(ok)
            self.assertTrue(path.exists())
            content = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(content), 1)
            row = json.loads(content[0])
            self.assertEqual(row["game_pk"], 1)
            self.assertEqual(row["bid_skipped_reason"], "ok")
            self.assertIn("ts", row)

    def test_append_multiple_decisions_accumulates(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "quotes.jsonl"
            for game in (1, 2, 3):
                ctx = lqe.QuoteDecisionContext(
                    game_pk=game, line="8.5",
                    over_best_bid=0.70, over_best_ask=0.73,
                    under_best_bid=0.27, under_best_ask=0.30,
                    over_fair_value=0.72, under_fair_value=0.28,
                    under_pair_available=True,
                    net_inventory_over_shares=0.0,
                )
                lqe.append_shadow_decision(path, lqe.compute_quote_decision(ctx))
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 3)


class ShadowReportDistributionTests(unittest.TestCase):
    def test_empty_rows_returns_zero_coverage(self):
        report = bqes.build_report([])
        self.assertEqual(report["coverage"]["n_rows_total"], 0)
        self.assertIsNone(report["quote_emission_rates"]["both_quoted_share"])
        self.assertEqual(report["spread_summary"]["n"], 0)
        self.assertEqual(report["hedge_opportunities"]["n_triggered"], 0)

    def test_both_quoted_rows_populate_spread_distribution(self):
        rows = [
            _shadow_row(would_quote_bid=0.70, would_quote_ask=0.74,
                        session_date="2026-05-17"),
            _shadow_row(would_quote_bid=0.68, would_quote_ask=0.76,
                        session_date="2026-05-17"),
            _shadow_row(would_quote_bid=0.71, would_quote_ask=0.73,
                        session_date="2026-05-16"),
        ]
        report = bqes.build_report(rows)
        # Spreads: 0.04, 0.08, 0.02
        sp = report["spread_summary"]
        self.assertEqual(sp["n"], 3)
        self.assertAlmostEqual(sp["min"], 0.02, places=3)
        self.assertAlmostEqual(sp["max"], 0.08, places=3)
        self.assertAlmostEqual(sp["p50"], 0.04, places=3)

    def test_skip_reasons_aggregated(self):
        rows = [
            _shadow_row(would_quote_bid=None, would_quote_ask=None,
                        bid_skipped_reason="missing_fair_value",
                        ask_skipped_reason="missing_fair_value"),
            _shadow_row(would_quote_bid=None, would_quote_ask=0.74,
                        bid_skipped_reason="max_inventory_long",
                        ask_skipped_reason="ok",
                        net_inventory_over_shares=60.0,
                        inventory_shade=0.05),
            _shadow_row(would_quote_bid=0.70, would_quote_ask=0.74,
                        bid_skipped_reason="ok",
                        ask_skipped_reason="ok"),
        ]
        report = bqes.build_report(rows)
        qer = report["quote_emission_rates"]
        self.assertEqual(qer["n_both_quoted"], 1)
        self.assertEqual(qer["n_ask_only"], 1)
        self.assertEqual(qer["n_neither_quoted"], 1)
        self.assertEqual(qer["bid_skip_reasons"]["missing_fair_value"], 1)
        self.assertEqual(qer["bid_skip_reasons"]["max_inventory_long"], 1)
        self.assertEqual(qer["bid_skip_reasons"]["ok"], 1)

    def test_hedge_opportunities_aggregated_by_side(self):
        rows = [
            _shadow_row(hedge_opportunity=True, hedge_side="buy_under",
                        net_inventory_over_shares=30.0),
            _shadow_row(hedge_opportunity=True, hedge_side="buy_under",
                        net_inventory_over_shares=20.0),
            _shadow_row(hedge_opportunity=True, hedge_side="buy_over",
                        net_inventory_over_shares=-25.0),
            _shadow_row(hedge_opportunity=False),
        ]
        report = bqes.build_report(rows)
        hh = report["hedge_opportunities"]
        self.assertEqual(hh["n_triggered"], 3)
        self.assertEqual(hh["by_side"]["buy_under"], 2)
        self.assertEqual(hh["by_side"]["buy_over"], 1)
        # Inventory at triggers: [30, 20, -25]
        inv_sum = hh["inventory_at_trigger_summary"]
        self.assertEqual(inv_sum["n"], 3)
        self.assertAlmostEqual(inv_sum["min"], -25.0)
        self.assertAlmostEqual(inv_sum["max"], 30.0)

    def test_inventory_summary_covers_all_rows(self):
        """Inventory distribution should include EVERY decision row,
        not just both-quoted ones -- the operator wants to see the
        distribution at all decision moments."""
        rows = [
            _shadow_row(net_inventory_over_shares=0.0,
                        would_quote_bid=None, would_quote_ask=None),
            _shadow_row(net_inventory_over_shares=10.0,
                        would_quote_bid=0.70, would_quote_ask=0.74),
            _shadow_row(net_inventory_over_shares=20.0,
                        would_quote_bid=0.69, would_quote_ask=0.75),
        ]
        report = bqes.build_report(rows)
        inv = report["inventory_summary"]
        self.assertEqual(inv["n"], 3)
        self.assertAlmostEqual(inv["mean"], 10.0, places=2)


class ShadowReportEndToEndTests(unittest.TestCase):
    def test_main_writes_outputs_on_empty_day(self):
        """Day-zero path: no shadow ledger files exist; main writes a
        valid empty payload + markdown rather than crashing."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "out"
            bqes.main([
                "--mode", "live",
                "--today", "2026-05-17",
                "--window-days", "7",
                "--output-root", str(out),
            ])
            json_path = out / "quote_engine_shadow_report.json"
            md_path = out / "quote_engine_shadow_report.md"
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["coverage"]["n_rows_total"], 0)
            self.assertEqual(payload["phase"], "C_shadow")


if __name__ == "__main__":
    unittest.main()
