"""Tests for live_reconciliation.reconcile_orphan_fills.

Covers the 2026-05-10 MIN@CLE 7.5 incident: bot logged a `MISSED` because
both `get_order` (returned `live` forever) and `get_trades_for_order`
(filtered by maker_address, returned 0) failed to surface a real fill,
yet the share was in the wallet at the bot's limit price.

The reconciler queries the public Polymarket data-api by *wallet address*,
which is independent of those failure modes.
"""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_DIR = Path(__file__).resolve().parents[1]
# live_order_lifecycle imports stage2_run_env_model from cache/, monitor types
# from monitor/, etc. Mirror live_engine.py's sys.path setup so test imports
# resolve the same way the runtime does.
for _dir in (
    PROJECT_DIR / "scripts" / "trading",
    PROJECT_DIR / "scripts" / "monitor",
    PROJECT_DIR / "scripts" / "analysis",
    PROJECT_DIR / "cache",
):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import live_reconciliation as lr  # noqa: E402
import live_order_lifecycle as lol  # noqa: E402
from models import LiveBetRecord  # noqa: E402


def _bet(
    *,
    bet_id="2026-05-10_824440_7.5_0001",
    order_id="0xfd2fe2fc",
    over_token_id="5321611806427048",
    order_status="cancelled",
    settled=True,
    limit_price=0.79,
    placed_at="2026-05-10T19:09:11Z",
):
    return LiveBetRecord(
        bet_id=bet_id,
        placed_at=placed_at,
        game_pk=824440,
        away_abbrev="MIN",
        home_abbrev="CLE",
        line="7.5",
        side="over",
        entry_ask=0.81,
        fair_value=0.985,
        base_fair_value=0.988,
        stage2_run_env_delta=-0.262,
        team_offense_delta=0.055,
        edge=0.175,
        inferred_runs=1,
        inning=5,
        inning_state="Bot",
        outs=0,
        away_score_before=5,
        home_score_before=1,
        inferred_away_after=5,
        inferred_home_after=2,
        stake=10.0,
        runners_on=0,
        limit_price=limit_price,
        order_id=order_id,
        order_placed_at=placed_at,
        order_status=order_status,
        over_token_id=over_token_id,
        settled=settled,
    )


class _FakeCLOB:
    """Stand-in for CLOBOrderClient that controls reconciliation API responses."""

    def __init__(self, *, positions=None, trades=None, positions_error=False):
        self._positions = positions if positions is not None else {}
        self._trades = trades if trades is not None else []
        self._positions_error = positions_error
        self.calls = {"positions": 0, "trades": 0}

    def get_user_positions(self):
        self.calls["positions"] += 1
        if self._positions_error:
            return None
        return dict(self._positions)

    def get_user_trades_for_market(self, token_id, *, after_ts=None):
        self.calls["trades"] += 1
        return [t for t in self._trades if t.get("market_token") == token_id]


class _FakeEngine:
    def __init__(self, *, bets, clob):
        self._bets = list(bets)
        self._clob = clob
        self.ledger_rows = []

    def _write_live_ledger_row(self, row):
        self.ledger_rows.append(row)
        return True


class ReconcileOrphanFillsTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # Happy path: orphan fill detected via position + trade history
    # ------------------------------------------------------------------

    def test_orphan_fill_recovered_with_full_trade_detail(self):
        bet = _bet()
        clob = _FakeCLOB(
            positions={"5321611806427048": 12.6582},
            trades=[{
                "market_token": "5321611806427048",
                "trade_id": "trade_abc",
                "price": 0.79,
                "size": 12.6582,
                "timestamp_iso": "2026-05-10T19:11:42Z",
            }],
        )
        engine = _FakeEngine(bets=[bet], clob=clob)

        summary = lr.reconcile_orphan_fills(engine)

        self.assertEqual(summary, {"checked": 1, "orphans": 1, "errors": 0})
        self.assertEqual(bet.order_status, "filled")
        self.assertEqual(bet.fill_price, 0.79)
        self.assertEqual(bet.fill_size, 12.6582)
        self.assertEqual(bet.fill_cost, 10.0)
        self.assertEqual(bet.actual_fill_price, 0.79)
        self.assertEqual(bet.filled_shares, 12.6582)
        self.assertEqual(bet.filled_at, "2026-05-10T19:11:42Z")
        # Provenance metadata stamped on the bet.
        self.assertEqual(getattr(bet, "reconciliation_source"), "data_api_trades")
        self.assertEqual(getattr(bet, "reconciliation_trade_id"), "trade_abc")
        # Cancellation cleared so downstream tooling sees a clean filled bet.
        self.assertEqual(bet.cancel_reason, "")
        self.assertIsNone(bet.cancelled_at)
        # One corrected ledger row written, tagged so analysis tooling can spot it.
        self.assertEqual(len(engine.ledger_rows), 1)
        self.assertEqual(engine.ledger_rows[0]["_event"], "reconciled_filled")
        self.assertEqual(engine.ledger_rows[0]["bet_id"], bet.bet_id)

    # ------------------------------------------------------------------
    # Position only: trade history empty, fall back to limit_price synth
    # ------------------------------------------------------------------

    def test_position_only_synthesizes_fill_at_limit_price(self):
        bet = _bet(limit_price=0.79)
        clob = _FakeCLOB(positions={"5321611806427048": 12.6582}, trades=[])
        engine = _FakeEngine(bets=[bet], clob=clob)

        summary = lr.reconcile_orphan_fills(engine)

        self.assertEqual(summary["orphans"], 1)
        self.assertEqual(bet.order_status, "filled")
        self.assertEqual(bet.fill_price, 0.79)
        self.assertEqual(bet.fill_size, 12.6582)
        self.assertEqual(getattr(bet, "reconciliation_source"), "data_api_position_only")

    # ------------------------------------------------------------------
    # No-op: wallet has no position on this token
    # ------------------------------------------------------------------

    def test_no_position_means_no_orphan_no_mutation(self):
        bet = _bet()
        clob = _FakeCLOB(positions={"some_other_token": 50.0})
        engine = _FakeEngine(bets=[bet], clob=clob)

        summary = lr.reconcile_orphan_fills(engine)

        self.assertEqual(summary, {"checked": 1, "orphans": 0, "errors": 0})
        self.assertEqual(bet.order_status, "cancelled")
        self.assertIsNone(bet.fill_price)
        self.assertEqual(engine.ledger_rows, [])

    # ------------------------------------------------------------------
    # Filled bets are not candidates -- never re-checked / re-written
    # ------------------------------------------------------------------

    def test_already_filled_bets_are_not_candidates(self):
        bet = _bet(order_status="filled", settled=True)
        bet.fill_price = 0.78
        bet.fill_size = 10.0
        clob = _FakeCLOB(positions={"5321611806427048": 999.0})
        engine = _FakeEngine(bets=[bet], clob=clob)

        summary = lr.reconcile_orphan_fills(engine)

        self.assertEqual(summary["checked"], 0)
        self.assertEqual(clob.calls["positions"], 0)
        self.assertEqual(bet.fill_price, 0.78)

    # ------------------------------------------------------------------
    # Bets without an order_id (skips, dry-runs) are not candidates
    # ------------------------------------------------------------------

    def test_bet_without_order_id_is_not_a_candidate(self):
        bet = _bet(order_id=None)
        clob = _FakeCLOB(positions={"5321611806427048": 12.6582})
        engine = _FakeEngine(bets=[bet], clob=clob)

        summary = lr.reconcile_orphan_fills(engine)
        self.assertEqual(summary["checked"], 0)

    # ------------------------------------------------------------------
    # Tiny dust positions are ignored
    # ------------------------------------------------------------------

    def test_dust_position_below_threshold_is_ignored(self):
        bet = _bet()
        clob = _FakeCLOB(positions={"5321611806427048": 0.001})
        engine = _FakeEngine(bets=[bet], clob=clob)

        summary = lr.reconcile_orphan_fills(engine)
        self.assertEqual(summary["orphans"], 0)
        self.assertEqual(bet.order_status, "cancelled")

    # ------------------------------------------------------------------
    # Data-api unreachable: degrades gracefully, no mutation
    # ------------------------------------------------------------------

    def test_data_api_failure_increments_error_no_mutation(self):
        bet = _bet()
        clob = _FakeCLOB(positions_error=True)
        engine = _FakeEngine(bets=[bet], clob=clob)

        summary = lr.reconcile_orphan_fills(engine)

        self.assertEqual(summary, {"checked": 1, "orphans": 0, "errors": 1})
        self.assertEqual(bet.order_status, "cancelled")

    # ------------------------------------------------------------------
    # CLOB client missing the new methods (older builds): no crash
    # ------------------------------------------------------------------

    def test_engine_with_legacy_clob_client_is_skipped(self):
        bet = _bet()
        engine = _FakeEngine(bets=[bet], clob=SimpleNamespace())

        summary = lr.reconcile_orphan_fills(engine)
        self.assertEqual(summary, {"checked": 1, "orphans": 0, "errors": 0})

    # ------------------------------------------------------------------
    # Trade-detail price filter: only fills at/below limit_price are taken
    # ------------------------------------------------------------------

    def test_trade_above_limit_price_is_rejected_falls_back_to_position(self):
        bet = _bet(limit_price=0.79)
        # The trade history shows a fill at 0.85 (above our limit) -- not ours.
        # Reconciler should ignore it and fall back to position-only synth.
        clob = _FakeCLOB(
            positions={"5321611806427048": 12.6582},
            trades=[{
                "market_token": "5321611806427048",
                "trade_id": "wrong_order",
                "price": 0.85,
                "size": 5.0,
                "timestamp_iso": "2026-05-10T19:30:00Z",
            }],
        )
        engine = _FakeEngine(bets=[bet], clob=clob)

        summary = lr.reconcile_orphan_fills(engine)
        self.assertEqual(summary["orphans"], 1)
        self.assertEqual(bet.fill_price, 0.79)
        self.assertEqual(getattr(bet, "reconciliation_source"), "data_api_position_only")


class NormalizeOrderStatusTests(unittest.TestCase):
    """Verify the canceled<->cancelled normalization fix.

    Previously `_normalize_order_status` returned the raw string lowercased,
    so a CLOB response of `CANCELED` produced `canceled` -- not in the
    `("cancelled", "expired", "error")` set in `check_open_orders`, so the
    order would silently stay in `_open_orders` until stale-order timeout.
    """

    def test_canceled_normalizes_to_cancelled(self):
        self.assertEqual(lol._normalize_order_status("CANCELED"), "cancelled")
        self.assertEqual(lol._normalize_order_status("Canceled"), "cancelled")
        self.assertEqual(lol._normalize_order_status("canceled"), "cancelled")

    def test_existing_spellings_unchanged(self):
        self.assertEqual(lol._normalize_order_status("cancelled"), "cancelled")
        self.assertEqual(lol._normalize_order_status("FILLED"), "filled")
        self.assertEqual(lol._normalize_order_status("matched"), "matched")
        self.assertEqual(lol._normalize_order_status("live"), "live")
        self.assertEqual(lol._normalize_order_status(None), "")
        self.assertEqual(lol._normalize_order_status(""), "")


if __name__ == "__main__":
    unittest.main()
