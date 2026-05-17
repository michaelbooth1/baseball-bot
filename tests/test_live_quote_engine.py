"""Phase C C1+C3+C4 (2026-05-17): live_quote_engine tests.

Covers:
  - Bid + ask math at zero inventory (centered on FV ± half_spread)
  - Inventory shading (positive when long, negative when short)
  - Skip reasons for each gating condition (missing FV, missing book,
    no under pair, max inventory long/short, quote-through-book)
  - Hedge opportunity detection (long Over + cheap Under,
    short Over + cheap Over)
  - Quote anchor never crosses the book (min_book_offset)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
if str(TRADING_DIR) not in sys.path:
    sys.path.insert(0, str(TRADING_DIR))

import live_quote_engine as lqe  # noqa: E402


def _ctx(**kw):
    defaults = dict(
        game_pk=1,
        line="8.5",
        over_best_bid=0.71,
        over_best_ask=0.74,
        under_best_bid=0.26,
        under_best_ask=0.29,
        over_fair_value=0.72,
        under_fair_value=0.28,
        under_pair_available=True,
        net_inventory_over_shares=0.0,
        config=lqe.QuoteEngineConfig(),
    )
    defaults.update(kw)
    return lqe.QuoteDecisionContext(**defaults)


class QuoteMathAtZeroInventoryTests(unittest.TestCase):
    def test_bid_is_fv_minus_half_spread(self):
        """At zero inventory, no shading: bid = FV - 0.02."""
        d = lqe.compute_quote_decision(_ctx(over_fair_value=0.72))
        self.assertAlmostEqual(d.bid_anchor_price, 0.70)
        self.assertEqual(d.would_quote_bid, 0.70)
        self.assertEqual(d.bid_skipped_reason, "ok")

    def test_ask_is_fv_plus_half_spread(self):
        d = lqe.compute_quote_decision(_ctx(over_fair_value=0.72))
        self.assertAlmostEqual(d.ask_anchor_price, 0.74)
        # Ask anchor at 0.74 = over_best_ask. min_book_offset=0.01
        # floor = over_best_bid + 0.01 = 0.72. Anchor > floor, no clamp.
        # Final quote = 0.74.
        self.assertEqual(d.would_quote_ask, 0.74)

    def test_shade_is_zero_when_flat(self):
        d = lqe.compute_quote_decision(_ctx(net_inventory_over_shares=0.0))
        self.assertEqual(d.inventory_shade, 0.0)


class InventoryShadingTests(unittest.TestCase):
    def test_long_inventory_shades_both_quotes_down(self):
        """Half-long position (25/50 shares) shades both anchors
        down by max_shade * 0.5 = 0.025."""
        cfg = lqe.QuoteEngineConfig(
            half_spread=0.02, max_shade=0.05, max_inventory_per_game=50,
        )
        d = lqe.compute_quote_decision(_ctx(
            net_inventory_over_shares=25.0, config=cfg,
            over_fair_value=0.72,
        ))
        self.assertAlmostEqual(d.inventory_shade, 0.025)
        # Bid anchor = 0.72 - 0.02 - 0.025 = 0.675
        self.assertAlmostEqual(d.bid_anchor_price, 0.675)
        # Ask anchor = 0.72 + 0.02 - 0.025 = 0.715
        self.assertAlmostEqual(d.ask_anchor_price, 0.715)

    def test_short_inventory_shades_both_quotes_up(self):
        cfg = lqe.QuoteEngineConfig(
            half_spread=0.02, max_shade=0.05, max_inventory_per_game=50,
        )
        d = lqe.compute_quote_decision(_ctx(
            net_inventory_over_shares=-25.0, config=cfg,
            over_fair_value=0.72,
        ))
        self.assertAlmostEqual(d.inventory_shade, -0.025)
        self.assertAlmostEqual(d.bid_anchor_price, 0.725)
        self.assertAlmostEqual(d.ask_anchor_price, 0.765)

    def test_shade_clamps_at_max_when_overlimit(self):
        """Inventory above max_inventory clamps shade at max_shade."""
        cfg = lqe.QuoteEngineConfig(
            half_spread=0.02, max_shade=0.05, max_inventory_per_game=50,
        )
        d = lqe.compute_quote_decision(_ctx(
            net_inventory_over_shares=200.0, config=cfg,
        ))
        self.assertAlmostEqual(d.inventory_shade, 0.05)


class SkipReasonTests(unittest.TestCase):
    def test_missing_fair_value_skips_both_sides(self):
        d = lqe.compute_quote_decision(_ctx(over_fair_value=None))
        self.assertEqual(d.bid_skipped_reason, "missing_fair_value")
        self.assertEqual(d.ask_skipped_reason, "missing_fair_value")
        self.assertIsNone(d.would_quote_bid)
        self.assertIsNone(d.would_quote_ask)

    def test_missing_over_book_skips_both_sides(self):
        d = lqe.compute_quote_decision(_ctx(
            over_best_bid=None, over_best_ask=None,
        ))
        self.assertEqual(d.bid_skipped_reason, "missing_over_book")
        self.assertEqual(d.ask_skipped_reason, "missing_over_book")

    def test_under_pair_unavailable_skips_both_sides(self):
        d = lqe.compute_quote_decision(_ctx(under_pair_available=False))
        self.assertEqual(d.bid_skipped_reason, "under_pair_unavailable")
        self.assertEqual(d.ask_skipped_reason, "under_pair_unavailable")

    def test_max_inventory_long_blocks_bid_only(self):
        cfg = lqe.QuoteEngineConfig(max_inventory_per_game=50.0)
        d = lqe.compute_quote_decision(_ctx(
            net_inventory_over_shares=60.0, config=cfg,
        ))
        self.assertEqual(d.bid_skipped_reason, "max_inventory_long")
        # Ask still computed (we WANT to flatten by selling)
        self.assertEqual(d.ask_skipped_reason, "ok")
        self.assertIsNotNone(d.would_quote_ask)

    def test_max_inventory_short_blocks_ask_only(self):
        cfg = lqe.QuoteEngineConfig(max_inventory_per_game=50.0)
        d = lqe.compute_quote_decision(_ctx(
            net_inventory_over_shares=-60.0, config=cfg,
        ))
        self.assertEqual(d.ask_skipped_reason, "max_inventory_short")
        self.assertEqual(d.bid_skipped_reason, "ok")


class QuoteDoesNotCrossBookTests(unittest.TestCase):
    def test_bid_clamped_to_below_book_ask(self):
        """If bid anchor would be ABOVE the best ask (crossing the
        book), the engine clamps it to best_ask - min_book_offset.
        Scenario: tight book + wide spread + low FV adjustment."""
        cfg = lqe.QuoteEngineConfig(
            half_spread=0.0, min_book_offset=0.01,
        )
        d = lqe.compute_quote_decision(_ctx(
            over_best_bid=0.70, over_best_ask=0.73,
            over_fair_value=0.90,  # anchor 0.90 > best_ask 0.73
            config=cfg,
        ))
        # Bid clamped to 0.73 - 0.01 = 0.72
        self.assertEqual(d.would_quote_bid, 0.72)

    def test_ask_clamped_to_above_book_bid(self):
        cfg = lqe.QuoteEngineConfig(
            half_spread=0.0, min_book_offset=0.01,
        )
        d = lqe.compute_quote_decision(_ctx(
            over_best_bid=0.70, over_best_ask=0.73,
            over_fair_value=0.50,  # anchor 0.50 < best_bid 0.70
            config=cfg,
        ))
        # Ask clamped to 0.70 + 0.01 = 0.71
        self.assertEqual(d.would_quote_ask, 0.71)


class HedgeOpportunityTests(unittest.TestCase):
    def test_long_over_with_cheap_under_triggers_buy_under(self):
        """Long Over + Under is at-or-below (1 - FV) + premium ->
        attractive hedge."""
        cfg = lqe.QuoteEngineConfig(hedge_premium=0.01)
        # FV=0.72, fair under = 0.28. Hedge max = 0.29. Under ask=0.29.
        d = lqe.compute_quote_decision(_ctx(
            net_inventory_over_shares=30.0, over_fair_value=0.72,
            under_best_ask=0.29, config=cfg,
        ))
        self.assertTrue(d.hedge_opportunity)
        self.assertEqual(d.hedge_side, "buy_under")
        self.assertAlmostEqual(d.hedge_target_price, 0.28)
        self.assertAlmostEqual(d.hedge_max_price, 0.29)

    def test_long_over_with_expensive_under_no_hedge(self):
        d = lqe.compute_quote_decision(_ctx(
            net_inventory_over_shares=30.0, over_fair_value=0.72,
            under_best_ask=0.40,  # too expensive
        ))
        self.assertFalse(d.hedge_opportunity)
        self.assertEqual(d.hedge_side, "buy_under")  # still surfaced
        self.assertIn("above_max", d.hedge_reason)

    def test_short_over_with_cheap_over_triggers_buy_over(self):
        """Short Over (= long Under) + Over ask near fair -> cover."""
        cfg = lqe.QuoteEngineConfig(hedge_premium=0.01)
        d = lqe.compute_quote_decision(_ctx(
            net_inventory_over_shares=-30.0, over_fair_value=0.50,
            over_best_ask=0.51,  # at fair + 1c
            config=cfg,
        ))
        self.assertTrue(d.hedge_opportunity)
        self.assertEqual(d.hedge_side, "buy_over")

    def test_no_inventory_no_hedge(self):
        d = lqe.compute_quote_decision(_ctx(
            net_inventory_over_shares=0.0,
        ))
        self.assertFalse(d.hedge_opportunity)
        self.assertEqual(d.hedge_reason, "no_inventory_to_hedge")

    def test_under_book_unavailable_blocks_hedge_when_long(self):
        d = lqe.compute_quote_decision(_ctx(
            net_inventory_over_shares=30.0,
            under_pair_available=False, under_best_ask=None,
        ))
        self.assertFalse(d.hedge_opportunity)
        self.assertEqual(d.hedge_reason, "under_book_unavailable")


class DecisionRowShapeTests(unittest.TestCase):
    def test_decision_to_dict_carries_full_audit_payload(self):
        d = lqe.compute_quote_decision(_ctx())
        row = d.to_dict()
        for key in (
            "schema_version", "game_pk", "line",
            "over_best_bid", "over_best_ask",
            "would_quote_bid", "would_quote_ask",
            "inventory_shade", "bid_skipped_reason",
            "ask_skipped_reason", "hedge_opportunity",
            "config_snapshot",
        ):
            self.assertIn(key, row, f"missing key {key}")
        self.assertEqual(
            row["config_snapshot"]["half_spread"], 0.02,
        )

    def test_schema_version_set(self):
        d = lqe.compute_quote_decision(_ctx())
        self.assertEqual(d.schema_version, lqe.QUOTE_DECISION_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
