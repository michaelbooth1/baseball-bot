"""Phase C C2 (2026-05-17): inventory_tracker tests.

Verifies per-game net exposure math on synthetic ledger rows. Covers:
- Filled bets add shares on the right side
- Settled / cancelled / error bets are excluded
- Multiple events per bet collapse to the latest
- Open orders (placed-not-yet-filled) contribute open_*_shares
- Net inventory math: net_over = (filled_over + open_over) - (filled_under + open_under)
- Fallback share computation: stake / fill_price when fill_size absent
- Optional in-memory open_orders stack on top of ledger
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
if str(TRADING_DIR) not in sys.path:
    sys.path.insert(0, str(TRADING_DIR))

import inventory_tracker as it  # noqa: E402


def _row(**kw):
    base = {
        "bet_id": "b1",
        "game_pk": 1,
        "line": "8.5",
        "side": "over",
        "stake": 20.0,
        "fill_price": 0.80,
        "fill_size": 25.0,  # 20 / 0.80
        "_event": "filled",
        "ts": "2026-05-17T20:00:00Z",
        "placed_at": "2026-05-17T19:55:00Z",
    }
    base.update(kw)
    return base


class FilledBetInventoryTests(unittest.TestCase):
    def test_single_filled_over_bet_adds_shares(self):
        snap = it.build_inventory_snapshot([_row()])
        gi = snap.for_game(1)
        self.assertAlmostEqual(gi.filled_over_shares, 25.0)
        self.assertEqual(gi.filled_under_shares, 0.0)
        self.assertEqual(gi.net_over_shares, 25.0)
        self.assertEqual(gi.n_filled_open, 1)

    def test_single_filled_under_bet_adds_under_shares(self):
        snap = it.build_inventory_snapshot([
            _row(side="under", fill_size=30.0, stake=20.0, fill_price=0.667),
        ])
        gi = snap.for_game(1)
        self.assertEqual(gi.filled_over_shares, 0.0)
        self.assertAlmostEqual(gi.filled_under_shares, 30.0)
        # Net OVER = -30 (long Under = short Over)
        self.assertAlmostEqual(gi.net_over_shares, -30.0)

    def test_multiple_filled_bets_same_game_sum(self):
        snap = it.build_inventory_snapshot([
            _row(bet_id="b1", fill_size=25.0),
            _row(bet_id="b2", fill_size=15.0),
        ])
        gi = snap.for_game(1)
        self.assertEqual(gi.filled_over_shares, 40.0)
        self.assertEqual(gi.n_filled_open, 2)

    def test_filled_then_settled_removes_from_inventory(self):
        """Bet lifecycle: filled then settled. The LATEST event
        wins (settled), so the inventory is 0."""
        snap = it.build_inventory_snapshot([
            _row(bet_id="b1", _event="filled", ts="2026-05-17T20:00:00Z"),
            _row(bet_id="b1", _event="settled", ts="2026-05-17T23:00:00Z"),
        ])
        gi = snap.for_game(1)
        self.assertEqual(gi.filled_over_shares, 0.0)
        self.assertEqual(gi.n_filled_open, 0)

    def test_filled_then_cancelled_removes_from_inventory(self):
        snap = it.build_inventory_snapshot([
            _row(bet_id="b1", _event="filled", ts="2026-05-17T20:00:00Z"),
            _row(bet_id="b1", _event="cancelled", ts="2026-05-17T20:05:00Z"),
        ])
        self.assertEqual(snap.for_game(1).filled_over_shares, 0.0)

    def test_error_event_excluded(self):
        snap = it.build_inventory_snapshot([_row(_event="error")])
        self.assertNotIn(1, snap.by_game)

    def test_reconciled_filled_counts_as_filled(self):
        """A bet recovered by the orphan-fill reconciler should
        contribute inventory just like a normal filled bet."""
        snap = it.build_inventory_snapshot([_row(_event="reconciled_filled")])
        self.assertEqual(snap.for_game(1).filled_over_shares, 25.0)


class OpenOrderInventoryTests(unittest.TestCase):
    def test_live_order_with_limit_price_contributes_open_shares(self):
        snap = it.build_inventory_snapshot([
            _row(_event="live", fill_size=None, limit_price=0.80,
                 stake=20.0, fill_price=None),
        ])
        gi = snap.for_game(1)
        self.assertAlmostEqual(gi.open_over_shares, 25.0)
        self.assertEqual(gi.filled_over_shares, 0.0)
        self.assertEqual(gi.n_orders_open, 1)

    def test_in_memory_open_orders_stack_on_top_of_ledger(self):
        """Open orders passed in via the `open_orders` kwarg add to
        whatever the ledger already contained."""
        snap = it.build_inventory_snapshot(
            [_row(fill_size=25.0)],  # 1 filled bet from ledger
            open_orders=[
                {"game_pk": 1, "side": "over", "stake": 10.0, "limit_price": 0.50},
            ],
        )
        gi = snap.for_game(1)
        self.assertEqual(gi.filled_over_shares, 25.0)
        self.assertAlmostEqual(gi.open_over_shares, 20.0)  # 10/0.50
        self.assertAlmostEqual(gi.net_over_shares, 45.0)

    def test_open_order_without_limit_price_uses_entry_ask(self):
        """Fallback: when limit_price absent, use entry_ask."""
        snap = it.build_inventory_snapshot([
            _row(_event="live", fill_size=None, limit_price=None,
                 stake=10.0, entry_ask=0.50, fill_price=None),
        ])
        self.assertAlmostEqual(snap.for_game(1).open_over_shares, 20.0)


class NetInventoryMathTests(unittest.TestCase):
    def test_net_is_filled_minus_under_plus_open_diff(self):
        """The math: net_over = filled_over - filled_under
                                + open_over - open_under"""
        snap = it.build_inventory_snapshot([
            _row(bet_id="b1", side="over", fill_size=30.0),
            _row(bet_id="b2", side="under", fill_size=10.0,
                 stake=4.0, fill_price=0.4),
            _row(bet_id="b3", side="over", _event="live",
                 fill_size=None, stake=5.0, limit_price=0.50,
                 fill_price=None),
        ])
        gi = snap.for_game(1)
        # filled_over=30, filled_under=10, open_over=10, open_under=0
        # net_over = 30 + 10 - 10 - 0 = 30
        self.assertEqual(gi.filled_over_shares, 30.0)
        self.assertEqual(gi.filled_under_shares, 10.0)
        self.assertAlmostEqual(gi.open_over_shares, 10.0)
        self.assertAlmostEqual(gi.net_over_shares, 30.0)

    def test_missing_game_pk_returns_zeroed_row(self):
        snap = it.build_inventory_snapshot([])
        gi = snap.for_game(999)
        self.assertEqual(gi.net_over_shares, 0.0)
        self.assertEqual(gi.game_pk, 999)

    def test_helper_net_inventory_for_game_matches_snapshot(self):
        snap = it.build_inventory_snapshot([_row(fill_size=25.0)])
        self.assertEqual(it.net_inventory_for_game(snap, 1), 25.0)
        self.assertEqual(it.net_inventory_for_game(snap, 999), 0.0)


class ShareComputationFallbackTests(unittest.TestCase):
    def test_prefers_fill_size_when_present(self):
        snap = it.build_inventory_snapshot([
            _row(fill_size=42.0, stake=20.0, fill_price=0.80),  # would compute 25
        ])
        self.assertEqual(snap.for_game(1).filled_over_shares, 42.0)

    def test_falls_back_to_stake_div_price_when_fill_size_missing(self):
        snap = it.build_inventory_snapshot([
            _row(fill_size=None, stake=20.0, fill_price=0.50),
        ])
        self.assertAlmostEqual(snap.for_game(1).filled_over_shares, 40.0)

    def test_zero_share_filled_row_is_skipped(self):
        snap = it.build_inventory_snapshot([
            _row(fill_size=0.0, stake=0.0, fill_price=0.0,
                 actual_fill_price=None),
        ])
        self.assertNotIn(1, snap.by_game)
        self.assertGreaterEqual(snap.n_skipped, 1)


class LegacyEventTagTests(unittest.TestCase):
    """Real production ledger has thousands of rows without `_event`
    (legacy format predating the tag). They fall back to
    `order_status` -- a row with order_status='filled' but no
    _event should still count as a fill."""

    def test_legacy_order_status_filled_counts_as_fill(self):
        snap = it.build_inventory_snapshot([{
            "bet_id": "legacy",
            "game_pk": 1,
            "side": "over",
            "order_status": "filled",  # NO _event field
            "fill_size": 25.0,
            "stake": 20.0,
            "fill_price": 0.80,
            "ts": "2026-04-15T20:00:00Z",
        }])
        self.assertEqual(snap.for_game(1).filled_over_shares, 25.0)


class LoadLedgerRowsTests(unittest.TestCase):
    def test_returns_empty_when_path_missing(self):
        with tempfile.TemporaryDirectory() as td:
            rows = it.load_ledger_rows(Path(td) / "missing.jsonl")
            self.assertEqual(rows, [])

    def test_skips_malformed_lines(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.jsonl"
            path.write_text(
                "\n".join([
                    json.dumps({"bet_id": "a", "_event": "filled"}),
                    "{not valid json}",
                    "",
                    json.dumps({"bet_id": "b", "_event": "live"}),
                ]) + "\n",
                encoding="utf-8",
            )
            rows = it.load_ledger_rows(path)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["bet_id"], "a")
            self.assertEqual(rows[1]["bet_id"], "b")


class InventorySnapshotSerializationTests(unittest.TestCase):
    def test_to_dict_round_trip(self):
        snap = it.build_inventory_snapshot([_row(fill_size=25.0)])
        d = snap.to_dict()
        self.assertIn("by_game", d)
        self.assertIn("1", d["by_game"])  # str key for JSON safety
        self.assertEqual(d["by_game"]["1"]["filled_over_shares"], 25.0)
        self.assertEqual(d["n_games_with_inventory"], 1)


if __name__ == "__main__":
    unittest.main()
