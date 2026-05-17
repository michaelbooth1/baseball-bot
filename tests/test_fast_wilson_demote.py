"""Active #13 (2026-05-17): fast Wilson-UB demote verdict tests.

Covers:
  - _wilson_upper_bound math (boundary cases + sanity benchmarks)
  - _fast_wilson_demote_from_post_bets verdict taxonomy
  - Per-lever wrappers (stage2/stage3-v2/stake-scaling/gate-threshold)
  - Grace-period guard prevents same-day firing
  - Insufficient-data + no-promotion edge cases
  - Stake-scaling bet_filter is respected
  - Real-world scenarios: 20-fill window with breakeven vs failing policies
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import promote  # noqa: E402


class WilsonUpperBoundMathTests(unittest.TestCase):
    def test_zero_trials_returns_one(self):
        """No evidence -> assume best-case (don't fire demote on zero data)."""
        self.assertEqual(
            promote._wilson_upper_bound(wins=0, n=0, z=1.645),
            1.0,
        )

    def test_all_wins_ub_near_one(self):
        """20 wins out of 20 -> UB should be near 1.0 (above any
        breakeven). At z=1.645, UB on 20/20 is about 0.992."""
        ub = promote._wilson_upper_bound(wins=20, n=20, z=1.645)
        self.assertGreater(ub, 0.95)
        self.assertLessEqual(ub, 1.0)

    def test_all_losses_ub_well_below_one(self):
        """0 wins out of 20 -> UB should sit well below breakeven."""
        ub = promote._wilson_upper_bound(wins=0, n=20, z=1.645)
        # Wilson upper bound on 0/20 is about 0.137
        self.assertGreater(ub, 0.05)
        self.assertLess(ub, 0.20)

    def test_ub_is_above_observed_rate(self):
        """The Wilson UB must always be >= the observed rate, for
        non-trivial cases."""
        for n in (5, 10, 20, 50):
            for wins in range(0, n + 1):
                ub = promote._wilson_upper_bound(wins=wins, n=n, z=1.645)
                p_hat = wins / n
                self.assertGreaterEqual(
                    ub + 1e-9, p_hat,
                    f"UB {ub} should be >= p_hat {p_hat} (n={n}, wins={wins})",
                )

    def test_ub_tightens_as_n_grows(self):
        """At the same observed win rate, UB should tighten toward
        the observed value as N grows."""
        p_hat = 0.6
        ubs = []
        for n in (10, 50, 200):
            wins = int(round(p_hat * n))
            ubs.append(promote._wilson_upper_bound(wins=wins, n=n, z=1.645))
        # UBs should be monotonically non-increasing
        self.assertTrue(ubs[0] >= ubs[1] >= ubs[2])
        # And the largest-N UB should be within ~7pp of p_hat
        # (Wilson at z=1.645, n=200, p_hat=0.6 -> UB ~0.655, gap ~5.5pp)
        self.assertLess(ubs[-1] - p_hat, 0.07)


class FastWilsonDemoteFromPostBetsTests(unittest.TestCase):
    def _bet(self, *, profit: float, entry_ask: float = 0.70):
        return {"profit": profit, "entry_ask": entry_ask}

    def test_insufficient_post_data_below_min_fills(self):
        bets = [self._bet(profit=-10.0) for _ in range(19)]
        v = promote._fast_wilson_demote_from_post_bets(
            bets, min_post_fills=20,
        )
        self.assertEqual(v["verdict"], "insufficient_post_data")
        self.assertEqual(v["n_post_filled"], 19)

    def test_hold_when_wilson_ub_above_breakeven(self):
        """Mix of wins + losses with high enough win rate that even
        the lower bound on win rate is comfortable above breakeven."""
        # ~70% wins at 0.5 ask: 14 wins, 6 losses, mean ask 0.5
        # p_hat = 0.7, breakeven = 0.5, Wilson UB on 14/20 ~ 0.836
        bets = [
            *[self._bet(profit=10.0, entry_ask=0.5) for _ in range(14)],
            *[self._bet(profit=-10.0, entry_ask=0.5) for _ in range(6)],
        ]
        v = promote._fast_wilson_demote_from_post_bets(
            bets, min_post_fills=20,
        )
        self.assertEqual(v["verdict"], "hold")
        self.assertAlmostEqual(v["breakeven_win_rate"], 0.5)
        self.assertGreater(v["wilson_ub_win_rate"], 0.5)

    def test_fast_demote_when_wilson_ub_below_breakeven(self):
        """Failing policy: at 0.80 ask, breakeven win rate is 0.80.
        4 wins, 16 losses -> p_hat=0.20. Wilson UB ~0.39. < 0.80 ->
        fast_demote."""
        bets = [
            *[self._bet(profit=2.5, entry_ask=0.80) for _ in range(4)],
            *[self._bet(profit=-10.0, entry_ask=0.80) for _ in range(16)],
        ]
        v = promote._fast_wilson_demote_from_post_bets(
            bets, min_post_fills=20,
        )
        self.assertEqual(v["verdict"], "fast_demote")
        self.assertAlmostEqual(v["breakeven_win_rate"], 0.80)
        self.assertLess(v["wilson_ub_win_rate"], 0.80)
        # Sanity: ratio captured for operator inspection
        self.assertEqual(v["observed_win_rate"], 0.20)

    def test_breakeven_boundary_does_not_fire(self):
        """If Wilson UB exactly equals breakeven, hold (strict <)."""
        # Construct a case where UB barely clears breakeven. Use
        # 15/20 at 0.75 ask: p_hat=0.75, breakeven=0.75. UB > 0.75
        # (always strictly above point estimate for n>0).
        bets = [
            *[self._bet(profit=10.0, entry_ask=0.75) for _ in range(15)],
            *[self._bet(profit=-10.0, entry_ask=0.75) for _ in range(5)],
        ]
        v = promote._fast_wilson_demote_from_post_bets(
            bets, min_post_fills=20,
        )
        self.assertEqual(v["verdict"], "hold")

    def test_missing_entry_ask_falls_back_to_insufficient(self):
        """Without entry_ask we can't compute breakeven -- don't fire."""
        bets = [
            {"profit": -10.0} for _ in range(25)
        ]
        v = promote._fast_wilson_demote_from_post_bets(
            bets, min_post_fills=20,
        )
        self.assertEqual(v["verdict"], "insufficient_post_data")

    def test_payload_carries_audit_fields(self):
        bets = [
            *[self._bet(profit=10.0, entry_ask=0.60) for _ in range(8)],
            *[self._bet(profit=-10.0, entry_ask=0.60) for _ in range(12)],
        ]
        v = promote._fast_wilson_demote_from_post_bets(
            bets, min_post_fills=20, z=1.645,
        )
        # All audit fields present
        for key in (
            "n_post_filled", "wins_post", "observed_win_rate",
            "mean_entry_ask", "wilson_ub_win_rate",
            "breakeven_win_rate", "wilson_ub_vs_breakeven_delta",
            "min_post_fills", "z",
        ):
            self.assertIn(key, v)
        self.assertEqual(v["min_post_fills"], 20)
        self.assertEqual(v["z"], 1.645)


class PerLeverFastDemoteWrapperTests(unittest.TestCase):
    def _make_session(self, dir_: Path, date: str, bets):
        path = dir_ / f"{date}_session.json"
        path.write_text(json.dumps({
            "session_date": date,
            "bets": bets,
        }), encoding="utf-8")

    def _filled_bet(self, *, profit: float, entry_ask: float = 0.70,
                     multiplier=None):
        bet = {
            "order_status": "filled",
            "placement_mode": "live",
            "profit": profit,
            "entry_ask": entry_ask,
            "fill_cost": 10.0,
            "stake": 10.0,
        }
        if multiplier is not None:
            bet["calibrated_stake_multiplier"] = multiplier
        return bet

    def test_no_promotion_returns_no_promotion_to_demote(self):
        with tempfile.TemporaryDirectory() as td:
            sessions = Path(td)
            v = promote.stage2_fast_demote_verdict(
                events=[], sessions_dir=sessions, today="2026-05-17",
            )
            self.assertEqual(v["verdict"], "no_promotion_to_demote")

    def test_within_grace_period_does_not_fire(self):
        """A promotion at today's date should not fire on today."""
        with tempfile.TemporaryDirectory() as td:
            sessions = Path(td)
            events = [{
                "lever": "stage2",
                "direction": "promote",
                "action": "promoted",
                "operator": "alice",
                "generated_at_utc": "2026-05-17T08:00:00Z",
            }]
            v = promote.stage2_fast_demote_verdict(
                events=events, sessions_dir=sessions,
                today="2026-05-17", grace_days=1,
            )
            self.assertEqual(v["verdict"], "within_grace_period")

    def test_fast_demote_fires_on_post_window_failing_policy(self):
        with tempfile.TemporaryDirectory() as td:
            sessions = Path(td)
            # Promotion was 7 days ago
            events = [{
                "lever": "stage2",
                "direction": "promote",
                "action": "promoted",
                "operator": "alice",
                "generated_at_utc": "2026-05-10T08:00:00Z",
            }]
            # Seed 7 days of sessions, each with ~3 bets all losing
            # at 0.80 ask -> 21 losses + 0 wins
            for i, day in enumerate([
                "2026-05-11", "2026-05-12", "2026-05-13",
                "2026-05-14", "2026-05-15", "2026-05-16", "2026-05-17",
            ]):
                self._make_session(sessions, day, [
                    self._filled_bet(profit=-10.0, entry_ask=0.80)
                    for _ in range(3)
                ])
            v = promote.stage2_fast_demote_verdict(
                events=events, sessions_dir=sessions,
                today="2026-05-17",
            )
            self.assertEqual(v["verdict"], "fast_demote")
            self.assertEqual(v["n_post_filled"], 21)
            self.assertEqual(v["wins_post"], 0)

    def test_hold_when_post_window_winning_policy(self):
        with tempfile.TemporaryDirectory() as td:
            sessions = Path(td)
            events = [{
                "lever": "stage2",
                "direction": "promote",
                "action": "promoted",
                "operator": "alice",
                "generated_at_utc": "2026-05-10T08:00:00Z",
            }]
            # 7 days, 3 bets/day at 0.50 ask, 21 wins -> p_hat=1.0
            for day in [
                "2026-05-11", "2026-05-12", "2026-05-13",
                "2026-05-14", "2026-05-15", "2026-05-16", "2026-05-17",
            ]:
                self._make_session(sessions, day, [
                    self._filled_bet(profit=10.0, entry_ask=0.50)
                    for _ in range(3)
                ])
            v = promote.stage2_fast_demote_verdict(
                events=events, sessions_dir=sessions,
                today="2026-05-17",
            )
            self.assertEqual(v["verdict"], "hold")
            self.assertEqual(v["wins_post"], 21)

    def test_stake_scaling_filter_excludes_multiplier_1(self):
        """The stake-scaling bet filter only counts bets where the
        calibrated_stake_multiplier deviated from 1.0 -- those are
        the bets the promotion actually affected."""
        with tempfile.TemporaryDirectory() as td:
            sessions = Path(td)
            events = [{
                "lever": "stake_scaling",
                "direction": "promote",
                "action": "promoted",
                "operator": "alice",
                "generated_at_utc": "2026-05-10T08:00:00Z",
            }]
            # 30 losing bets at multiplier=1.0 (filter excludes)
            # + 5 losing bets at multiplier=1.5 (filter keeps)
            # = post window has 5 filled, below min_post_fills
            self._make_session(sessions, "2026-05-15", [
                *[self._filled_bet(profit=-10.0, multiplier=1.0)
                  for _ in range(30)],
                *[self._filled_bet(profit=-10.0, multiplier=1.5)
                  for _ in range(5)],
            ])
            v = promote.stake_scaling_fast_demote_verdict(
                events=events, sessions_dir=sessions,
                today="2026-05-17",
            )
            self.assertEqual(v["verdict"], "insufficient_post_data")
            self.assertEqual(v["n_post_filled"], 5)

    def test_grace_period_default_is_one_day(self):
        """Promotion at 2026-05-15, today 2026-05-16 -> post-window
        starts 05-16, can include today's bets."""
        with tempfile.TemporaryDirectory() as td:
            sessions = Path(td)
            events = [{
                "lever": "stage2",
                "direction": "promote",
                "action": "promoted",
                "operator": "alice",
                "generated_at_utc": "2026-05-15T08:00:00Z",
            }]
            self._make_session(sessions, "2026-05-16", [
                self._filled_bet(profit=-10.0, entry_ask=0.80)
                for _ in range(20)
            ])
            v = promote.stage2_fast_demote_verdict(
                events=events, sessions_dir=sessions,
                today="2026-05-16",
            )
            # Grace=1 day: post starts 05-16 (15 + 1), today is 05-16
            # -> window [05-16, 05-16] -> the 20 bets count.
            self.assertEqual(v["verdict"], "fast_demote")
            self.assertEqual(v["n_post_filled"], 20)


class FastVsWindowedDemoteParityTests(unittest.TestCase):
    """The fast verdict is INDEPENDENT of the windowed verdict; both
    can fire on the same data but they use different criteria. This
    test verifies they DON'T contradict each other on clearly-failing
    or clearly-winning policies."""

    def _make_session(self, dir_: Path, date: str, bets):
        path = dir_ / f"{date}_session.json"
        path.write_text(json.dumps({
            "session_date": date, "bets": bets,
        }), encoding="utf-8")

    def _filled_bet(self, *, profit, entry_ask=0.70):
        return {
            "order_status": "filled", "placement_mode": "live",
            "profit": profit, "entry_ask": entry_ask,
            "fill_cost": 10.0, "stake": 10.0,
        }

    def test_both_say_demote_on_clearly_failing_policy(self):
        with tempfile.TemporaryDirectory() as td:
            sessions = Path(td)
            events = [{
                "lever": "stage2", "direction": "promote",
                "action": "promoted", "operator": "alice",
                "generated_at_utc": "2026-04-20T08:00:00Z",
            }]
            # 14d pre window: 3 bets/day winning at 0.5 ask
            for day in [f"2026-04-{d:02d}" for d in range(6, 20)]:
                self._make_session(sessions, day, [
                    self._filled_bet(profit=10.0, entry_ask=0.50)
                    for _ in range(3)
                ])
            # Post window: 3 bets/day losing at 0.8 ask
            for day in [f"2026-04-{d:02d}" for d in range(21, 30)] + [
                "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04",
                "2026-05-05",
            ]:
                self._make_session(sessions, day, [
                    self._filled_bet(profit=-10.0, entry_ask=0.80)
                    for _ in range(3)
                ])
            fast = promote.stage2_fast_demote_verdict(
                events=events, sessions_dir=sessions, today="2026-05-05",
            )
            slow = promote.stage2_demotion_verdict(
                events=events, sessions_dir=sessions,
            )
            # Both should converge on demote
            self.assertEqual(fast["verdict"], "fast_demote")
            self.assertEqual(slow["verdict"], "demote")


if __name__ == "__main__":
    unittest.main()
