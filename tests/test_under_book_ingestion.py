"""Phase A1 regression: under-side book fields flow through the
signal pipeline's market-complement helper into candidate rows.

The Phase A audit (2026-05-16) found that the under-side book IS
captured by the monitor and attached to the over book payload by
signal_engine before the trading pipeline consumes it. Under-side
ingestion code already exists; A1 closes the loop by asserting the
contract so a future refactor cannot silently drop under_pair_available
or its companion under_* columns.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_DIR = Path(__file__).resolve().parents[1]
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
if str(TRADING_DIR) not in sys.path:
    sys.path.insert(0, str(TRADING_DIR))

import signal_pipeline_payload as sp_payload  # noqa: E402


def _make_ctx(book):
    market = SimpleNamespace(
        over_token_id="token_over",
        under_token_id="token_under",
    )
    return SimpleNamespace(
        market=market,
        book=book,
        best_bid=book.get("best_bid"),
        ask=book.get("best_ask", 0.65),
    )


class UnderBookIngestionContractTests(unittest.TestCase):
    """`_market_complement_fields` is the lifting point — it reads the
    under_* columns out of the book payload that signal_engine has
    pre-attached and writes them into the candidate row schema. If the
    book carries `under_pair_available=True`, every under_* field must
    propagate; if False (the ~50% production reality), under_pair_available
    must be False and the under_* numeric fields must be None.
    """

    def test_paired_book_propagates_under_fields(self):
        """When the under tick arrived in the same poll cycle, the book
        payload carries under_pair_available=True plus best_bid/ask/ltp/
        source. All of those must appear on the candidate row so the
        offline analysis pipeline (calibration table, walk-forward,
        unified signals) can consume them."""
        book = {
            "best_bid": 0.60,
            "best_ask": 0.65,
            "ltp": 0.62,
            "source": "clob",
            "under_pair_available": True,
            "under_best_bid": 0.34,
            "under_best_ask": 0.40,
            "under_ltp": 0.37,
            "under_source": "clob",
        }
        out = sp_payload._market_complement_fields(_make_ctx(book))

        self.assertTrue(out["under_pair_available"])
        self.assertEqual(out["under_best_bid"], 0.34)
        self.assertEqual(out["under_best_ask"], 0.40)
        self.assertAlmostEqual(out["under_mid"], 0.37)
        self.assertAlmostEqual(out["under_spread"], 0.06)
        self.assertEqual(out["under_ltp"], 0.37)
        self.assertEqual(out["under_book_source"], "clob")

    def test_paired_book_emits_book_pair_derived_fields(self):
        """Sum + no-vig derivations require both sides; they should
        compute when the pair is available."""
        book = {
            "best_bid": 0.60,
            "best_ask": 0.65,
            "under_pair_available": True,
            "under_best_bid": 0.34,
            "under_best_ask": 0.40,
        }
        out = sp_payload._market_complement_fields(_make_ctx(book))

        self.assertAlmostEqual(out["over_under_ask_sum"], 1.05)
        self.assertAlmostEqual(out["over_under_bid_sum"], 0.94)
        # over_mid=0.625, under_mid=0.37, sum=0.995, over_mid_no_vig=0.625/0.995
        self.assertAlmostEqual(out["over_under_mid_sum"], 0.995)
        self.assertAlmostEqual(out["over_mid_no_vig"], 0.625 / 0.995, places=4)
        self.assertAlmostEqual(out["under_mid_no_vig"], 0.37 / 0.995, places=4)
        self.assertAlmostEqual(
            out["decision_market_mid_no_vig"], 0.625 / 0.995, places=4
        )

    def test_unpaired_book_leaves_under_fields_null(self):
        """When the under tick did not arrive in the same poll cycle,
        the book carries under_pair_available=False (or absent) and the
        under_* numerics must be None. Critical: pair-derived
        columns (over_under_*_sum, *_no_vig) must also be None, NOT 0,
        so downstream consumers can distinguish 'no data' from a real
        zero-spread observation."""
        book = {"best_bid": 0.60, "best_ask": 0.65}
        out = sp_payload._market_complement_fields(_make_ctx(book))

        self.assertFalse(out["under_pair_available"])
        self.assertIsNone(out["under_best_bid"])
        self.assertIsNone(out["under_best_ask"])
        self.assertIsNone(out["under_mid"])
        self.assertIsNone(out["under_spread"])
        self.assertIsNone(out["over_under_ask_sum"])
        self.assertIsNone(out["over_under_bid_sum"])
        self.assertIsNone(out["over_under_mid_sum"])
        self.assertIsNone(out["over_mid_no_vig"])
        self.assertIsNone(out["under_mid_no_vig"])

    def test_over_side_fields_always_present(self):
        """Symmetry sanity: even when under_pair_available=False, the
        over-side fields (over_best_bid, over_best_ask, over_mid,
        over_spread, decision_mid) must be present and non-null,
        because the engine is making an Over decision using the Over
        book it's polling."""
        book = {"best_bid": 0.60, "best_ask": 0.65, "ltp": 0.61}
        out = sp_payload._market_complement_fields(_make_ctx(book))

        self.assertEqual(out["over_best_bid"], 0.60)
        self.assertEqual(out["over_best_ask"], 0.65)
        self.assertAlmostEqual(out["over_mid"], 0.625)
        self.assertAlmostEqual(out["over_spread"], 0.05)
        self.assertAlmostEqual(out["decision_mid"], 0.625)


class UnderBookDenyPrefixCommentTests(unittest.TestCase):
    """The deny-prefix comment in backtest_ev_policy was rewritten in
    Phase A1 to reflect reality: the runtime DOES capture the under
    book; the deny is now justified by the ~50% pair_available rate
    and enforce-mode fail-closed safety, not by 'under book not
    captured'. This contract test asserts the corrected rationale is
    in the comment so a future grep can verify."""

    def test_deny_prefix_comment_mentions_pair_available_rate(self):
        import importlib
        sys.path.insert(0, str(PROJECT_DIR / "scripts" / "analysis"))
        bep = importlib.import_module("backtest_ev_policy")
        source = Path(bep.__file__).read_text(encoding="utf-8")
        # Must reflect the corrected rationale; the stale "runtime
        # doesn't capture the under book" must not be present.
        deny_block = source.split("RUNTIME_FEATURE_DENY_PREFIXES")[1].split("\n)")[0]
        self.assertNotIn(
            "the runtime doesn't capture the under book",
            deny_block,
            "Stale comment still present — the runtime DOES capture the under book",
        )
        self.assertIn("under_pair_available", deny_block)


if __name__ == "__main__":
    unittest.main()
