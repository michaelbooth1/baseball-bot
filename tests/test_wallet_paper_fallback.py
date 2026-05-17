"""Tests for wallet-aware paper-fallback placement (shipped 2026-05-13).

When the CLOB rejects a real-money order with "not enough balance /
allowance", live_engine_placement routes the bet to a synthesized
paper-fallback path: filled at the limit price, tracked through
settlement, marked `placement_mode="paper_fallback"`. A session-level
cooldown skips CLOB attempts for subsequent placements until it elapses,
then real money resumes automatically.

Tests verify:
  - Balance-error string matching (positive + negative + case)
  - The placement-side helper sets placement_mode + reason + synthesizes
    fill fields so settlement code computes P&L normally
  - Tripping the cooldown sets a future deadline + records counters
  - Cooldown remaining returns 0 when not tripped, > 0 when active
"""

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
if str(TRADING_DIR) not in sys.path:
    sys.path.insert(0, str(TRADING_DIR))

import live_engine_placement as lep  # noqa: E402
from models import LiveBetRecord  # noqa: E402


def _bet(**overrides) -> LiveBetRecord:
    base = dict(
        bet_id="bet_1",
        placed_at="2026-05-13T20:00:00Z",
        game_pk=12345,
        away_abbrev="AWY",
        home_abbrev="HOM",
        line="6.5",
        side="over",
        entry_ask=0.78,
        fair_value=0.96,
        base_fair_value=0.96,
        stage2_run_env_delta=0.0,
        team_offense_delta=-0.013,
        edge=0.18,
        inferred_runs=1,
        inning=6,
        inning_state="Bot",
        outs=0,
        away_score_before=4,
        home_score_before=0,
        inferred_away_after=4,
        inferred_home_after=1,
        stake=10.0,
        runners_on=0,
        limit_price=0.76,
    )
    base.update(overrides)
    return LiveBetRecord(**base)


def _engine() -> SimpleNamespace:
    """Minimal engine stub with the attributes the helpers touch."""
    return SimpleNamespace(
        _bets=[],
        _open_orders={},
        _wallet_exhausted_until=None,
        _paper_fallback_stats={
            "placed": 0, "wins": 0, "losses": 0, "profit": 0.0,
            "total_stake": 0.0, "wallet_exhausted_events": 0,
            "wallet_exhausted_last_at": None, "last_reason": None,
        },
        _last_place_bet_skip_reason=None,
        live_args=SimpleNamespace(wallet_exhausted_cooldown_secs=300.0),
        _append_to_live_ledger=lambda b: None,
        _save_session=lambda *a, **kw: None,
    )


class BalanceErrorMatcherTests(unittest.TestCase):
    def test_matches_balance_pattern(self):
        self.assertTrue(lep._is_balance_error(
            "{'error': 'not enough balance / allowance: the balance is "
            "not enough -> balance: 9196888, order amount: 10072480'}"
        ))

    def test_matches_allowance_pattern(self):
        self.assertTrue(lep._is_balance_error(
            "PolyApiException(status=400, error=not enough allowance ...)"
        ))

    def test_case_insensitive(self):
        self.assertTrue(lep._is_balance_error("Not Enough Balance"))

    def test_unrelated_errors_dont_match(self):
        self.assertFalse(lep._is_balance_error("token not active"))
        self.assertFalse(lep._is_balance_error("rate limited"))
        self.assertFalse(lep._is_balance_error(""))
        self.assertFalse(lep._is_balance_error(None))


class CooldownStateTests(unittest.TestCase):
    def test_remaining_zero_when_not_tripped(self):
        e = _engine()
        self.assertEqual(lep._wallet_cooldown_remaining(e), 0.0)

    def test_remaining_positive_after_trip(self):
        e = _engine()
        lep._trip_wallet_cooldown(e, reason="clob_balance_error")
        remaining = lep._wallet_cooldown_remaining(e)
        self.assertGreater(remaining, 290.0)
        self.assertLessEqual(remaining, 300.0)

    def test_remaining_zero_after_expiry(self):
        e = _engine()
        # Trip the cooldown then immediately rewind the deadline.
        lep._trip_wallet_cooldown(e, reason="clob_balance_error")
        e._wallet_exhausted_until = time.monotonic() - 1.0
        self.assertEqual(lep._wallet_cooldown_remaining(e), 0.0)

    def test_trip_records_counters(self):
        e = _engine()
        lep._trip_wallet_cooldown(e, reason="clob_balance_error")
        stats = e._paper_fallback_stats
        self.assertEqual(stats["wallet_exhausted_events"], 1)
        self.assertIsNotNone(stats["wallet_exhausted_last_at"])
        self.assertEqual(stats["last_reason"], "clob_balance_error")

    def test_zero_cooldown_disables_forward_window(self):
        """--wallet-exhausted-cooldown-secs=0 -> only trip-bet is fallback,
        no forward cooldown. Subsequent bets can still try the CLOB."""
        e = _engine()
        e.live_args.wallet_exhausted_cooldown_secs = 0.0
        lep._trip_wallet_cooldown(e, reason="clob_balance_error")
        # No deadline should be set when the cooldown is 0.
        self.assertIsNone(e._wallet_exhausted_until)
        # Counters still update so the audit trail remains.
        self.assertEqual(e._paper_fallback_stats["wallet_exhausted_events"], 1)


class PaperFallbackRecordTests(unittest.TestCase):
    def test_synthesizes_fill_at_limit_price(self):
        e = _engine()
        bet = _bet(stake=10.0, limit_price=0.76, order_size_shares=None)
        result = lep._record_paper_fallback_bet(e, bet, reason="clob_balance_error")
        self.assertIs(result, bet)
        self.assertEqual(bet.placement_mode, "paper_fallback")
        self.assertEqual(bet.paper_fallback_reason, "clob_balance_error")
        self.assertEqual(bet.clob_accept_status, "paper_fallback")
        self.assertEqual(bet.order_status, "filled")  # so _is_bet_executable picks it up
        self.assertTrue(bet.order_id.startswith("paper_fallback_"))
        # Fill synthesized at the limit price.
        self.assertAlmostEqual(bet.actual_fill_price, 0.76)
        self.assertAlmostEqual(bet.fill_price, 0.76)
        # shares = stake / limit_price = 10/0.76
        self.assertAlmostEqual(bet.order_size_shares, round(10.0 / 0.76, 4), places=4)
        self.assertAlmostEqual(bet.fill_cost, 10.0)
        self.assertAlmostEqual(bet.fill_cost_usdc, 10.0)

    def test_does_not_add_to_open_orders(self):
        """Paper-fallback bets must NOT enter _open_orders; the lifecycle
        polling code would try to fetch a fake order_id from CLOB."""
        e = _engine()
        bet = _bet()
        lep._record_paper_fallback_bet(e, bet, reason="wallet_cooldown")
        self.assertEqual(e._open_orders, {})

    def test_increments_paper_fallback_counters(self):
        e = _engine()
        for i in range(3):
            bet = _bet(bet_id=f"bet_{i}", stake=5.0)
            lep._record_paper_fallback_bet(e, bet, reason="wallet_cooldown")
        self.assertEqual(e._paper_fallback_stats["placed"], 3)
        self.assertAlmostEqual(e._paper_fallback_stats["total_stake"], 15.0)

    def test_uses_existing_order_size_shares_when_present(self):
        """If the bet already has order_size_shares (e.g. live engine
        computed a lot-rounded value), don't overwrite it."""
        e = _engine()
        bet = _bet(stake=10.0, limit_price=0.76, order_size_shares=12.5)
        lep._record_paper_fallback_bet(e, bet, reason="wallet_cooldown")
        self.assertAlmostEqual(bet.order_size_shares, 12.5)
        self.assertAlmostEqual(bet.fill_size, 12.5)

    def test_preserves_placement_error_when_supplied(self):
        """Wallet-balance-triggered fallbacks should preserve the raw SDK
        error message on the bet record so audits can recover the cause
        without grepping logs (gap discovered 2026-05-15: 62 errored
        placements on 5/12 had only order_status='error', no message).
        """
        e = _engine()
        bet = _bet()
        sdk_msg = (
            'PolyApiException[status_code=400, error_message='
            "{'error': 'not enough balance / allowance: the balance is "
            "not enough -> balance=0.42 < required=8.00'}]"
        )
        lep._record_paper_fallback_bet(
            e, bet, reason="clob_balance_error", placement_error=sdk_msg,
        )
        self.assertEqual(bet.placement_error, sdk_msg)
        self.assertEqual(bet.paper_fallback_reason, "clob_balance_error")

    def test_does_not_set_placement_error_when_not_supplied(self):
        """Cooldown-triggered fallbacks (no original SDK error) should
        leave placement_error as None -- avoids confusing audits with
        a synthetic 'unknown_error' marker on bets that never hit CLOB.
        """
        e = _engine()
        bet = _bet()
        lep._record_paper_fallback_bet(e, bet, reason="wallet_cooldown")
        self.assertIsNone(bet.placement_error)


class PlacementErrorSchemaTests(unittest.TestCase):
    """Schema-level tests for the placement_error field on LiveBetRecord
    (Fix #1, shipped 2026-05-15). The actual non-balance error branch in
    place_bet sets `bet.order_status = "error"; bet.placement_error =
    error_text` -- a regression test at the dataclass level catches
    accidental deletion of the field without requiring a full place_bet
    integration setup.
    """

    def test_placement_error_defaults_to_none(self):
        bet = _bet()
        self.assertIsNone(bet.placement_error)

    def test_placement_error_round_trips_through_asdict(self):
        # session_serialization uses asdict() to write bets; ensure the
        # new field survives serialization (and thus reaches session JSON).
        from dataclasses import asdict
        bet = _bet()
        bet.placement_error = "rate limit exceeded"
        d = asdict(bet)
        self.assertEqual(d["placement_error"], "rate limit exceeded")


class CooldownAndFallbackIntegrationTests(unittest.TestCase):
    def test_cooldown_lifecycle(self):
        """Fresh engine -> not in cooldown -> trip on first error -> in
        cooldown -> remains in cooldown for subsequent placements."""
        e = _engine()
        self.assertEqual(lep._wallet_cooldown_remaining(e), 0.0)
        # First bet hits balance error -> trip cooldown + paper-fallback
        lep._trip_wallet_cooldown(e, reason="clob_balance_error")
        bet1 = _bet(bet_id="bet_1")
        lep._record_paper_fallback_bet(e, bet1, reason="clob_balance_error")
        self.assertGreater(lep._wallet_cooldown_remaining(e), 0)
        self.assertEqual(bet1.placement_mode, "paper_fallback")
        # Second bet would skip CLOB during cooldown
        bet2 = _bet(bet_id="bet_2", game_pk=99999)
        lep._record_paper_fallback_bet(e, bet2, reason="wallet_cooldown")
        self.assertEqual(bet2.paper_fallback_reason, "wallet_cooldown")
        # Both bets counted; cooldown only counted once
        self.assertEqual(e._paper_fallback_stats["placed"], 2)
        self.assertEqual(e._paper_fallback_stats["wallet_exhausted_events"], 1)


if __name__ == "__main__":
    unittest.main()
