"""Phase C-paper (2026-05-27): UNDER paper-bet placement tests.

Covers the four pieces of Phase C-paper:

1. **5 symmetric UNDER gates** (`gate_under_min_inning`,
   `gate_under_min_entry_ask`, `gate_under_max_base_fv`,
   `gate_under_fv_ask_gap`, `gate_under_extreme_edge`) each fire
   independently when their threshold is exceeded.

2. **Paper placement**: when `--under-mode paper` AND all 5 gates +
   `gate_min_edge` pass, `_maybe_emit_under_candidate` records the
   candidate row AND appends a `BetRecord(side="under")` to
   `engine._bets`. Shadow mode records the row but does NOT place.

3. **Settlement**: `_settle_finished_games` settles UNDER bets so
   `won = (final_total < line)` (vs `> line` for OVER). MLB OU lines
   end in .5 so pushes are structurally impossible.

4. **Live-engine safety**: `LiveTradingEngine._is_bet_executable`
   returns True for UNDER paper bets (settle as filled-at-limit
   even when the live engine hosts them via `--under-mode paper`).
   The CLOB `place_bet` is OVER-only by construction; UNDER paper
   bets never reach it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_DIR = Path(__file__).resolve().parents[1]
for sub in ("scripts/trading", "scripts/analysis", "scripts/monitor", "cache"):
    p = PROJECT_DIR / sub
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


import signal_pipeline as sp  # noqa: E402
from test_under_candidate_emission import (  # noqa: E402
    _make_fake_ctx, _make_fake_engine, _make_fv_phase,
)


# ----------------------------------------------------------------------
# UNDER gate stack (5 symmetric gates)
# ----------------------------------------------------------------------


class UnderGateStackTests(unittest.TestCase):

    def _emit_with_overrides(self, **trade_arg_overrides):
        """Build a permissive engine in paper mode + apply overrides
        to test a specific gate. Returns engine for assertions."""
        engine = _make_fake_engine(
            mode="paper",
            under_calibrator_returns=0.65,
            under_gate_overrides=trade_arg_overrides,
        )
        # ask=0.40, fv=0.65 -> edge=0.25 (passes default 0.10 min_edge
        # but trips extreme_edge unless overridden).
        ctx = _make_fake_ctx(
            inning=6,
            line="8.5",
            under_best_ask=0.40,
        )
        # base_fair_value default in _make_fv_phase = 0.75 -> under
        # base = 0.25.
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), {"bet_id": "x"},
        )
        return engine

    def test_gate_under_min_inning_fires_when_inning_below_threshold(self):
        # Standard line (< high_line_cutoff): inning=6, threshold=8.
        engine = _make_fake_engine(
            mode="paper",
            under_calibrator_returns=0.65,
            under_gate_overrides={"under_min_inning": 8},
        )
        ctx = _make_fake_ctx(inning=6, line="7.5", under_best_ask=0.50)
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), {"bet_id": "x"},
        )
        under = engine.recorded_decisions[0]
        self.assertEqual(under["decision"], "skip")
        self.assertEqual(under["decision_reason"], "gate_under_min_inning")
        self.assertEqual(engine._bets, [])

    def test_gate_under_min_inning_high_line_used_when_line_at_cutoff(self):
        # line=8.5 >= 8.5 high_line_cutoff -> uses high-line attr.
        engine = _make_fake_engine(
            mode="paper",
            under_calibrator_returns=0.65,
            under_gate_overrides={
                "under_min_inning": 1,  # standard floor permissive
                "under_min_inning_high_line": 9,  # high-line strict
            },
        )
        ctx = _make_fake_ctx(inning=6, line="8.5", under_best_ask=0.50)
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), {"bet_id": "x"},
        )
        under = engine.recorded_decisions[0]
        self.assertEqual(under["decision_reason"], "gate_under_min_inning")

    def test_gate_under_min_entry_ask_fires_when_under_ask_below_floor(self):
        engine = _make_fake_engine(
            mode="paper",
            under_calibrator_returns=0.50,
            under_gate_overrides={"under_min_entry_ask": 0.55},
        )
        ctx = _make_fake_ctx(inning=6, line="7.5", under_best_ask=0.40)
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), {"bet_id": "x"},
        )
        under = engine.recorded_decisions[0]
        self.assertEqual(under["decision_reason"], "gate_under_min_entry_ask")
        self.assertEqual(engine._bets, [])

    def test_gate_under_max_base_fv_fires_when_under_base_fv_at_or_above(self):
        # over_base = 0.05 -> under_base = 0.95 >= 0.90 threshold.
        engine = _make_fake_engine(
            mode="paper",
            under_calibrator_returns=0.99,
            under_gate_overrides={"under_max_base_fv": 0.90},
        )
        ctx = _make_fake_ctx(inning=6, line="7.5", under_best_ask=0.50)
        fv_phase = _make_fv_phase(base_fair_value=0.05)
        sp._maybe_emit_under_candidate(
            engine, ctx, fv_phase, {"bet_id": "x"},
        )
        under = engine.recorded_decisions[0]
        self.assertEqual(under["decision_reason"], "gate_under_max_base_fv")
        self.assertEqual(engine._bets, [])

    def test_gate_under_extreme_edge_fires_when_edge_above_threshold(self):
        # under_fv=0.80, under_ask=0.50 -> edge=0.30 > 0.22 threshold.
        engine = _make_fake_engine(
            mode="paper",
            under_calibrator_returns=0.80,
            under_gate_overrides={"under_extreme_edge_max": 0.22},
        )
        ctx = _make_fake_ctx(inning=6, line="7.5", under_best_ask=0.50)
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), {"bet_id": "x"},
        )
        under = engine.recorded_decisions[0]
        self.assertEqual(under["decision_reason"], "gate_under_extreme_edge")
        self.assertEqual(engine._bets, [])

    def test_gate_under_fv_ask_gap_fires_only_when_inning_at_threshold(self):
        # Gate fires only when edge > gap_max AND inning >=
        # gap_min_inning. With inning=8 + threshold inning=7 +
        # edge=0.25 > 0.20 it fires.
        engine = _make_fake_engine(
            mode="paper",
            under_calibrator_returns=0.75,
            under_gate_overrides={
                "under_fv_ask_gap_max": 0.20,
                "under_fv_ask_gap_min_inning": 7,
            },
        )
        ctx = _make_fake_ctx(inning=8, line="7.5", under_best_ask=0.50)
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), {"bet_id": "x"},
        )
        under = engine.recorded_decisions[0]
        self.assertEqual(under["decision_reason"], "gate_under_fv_ask_gap")

    def test_gate_under_fv_ask_gap_does_not_fire_when_inning_below_threshold(self):
        # Same edge as the test above, but inning=4 < 7 -> gate dormant.
        engine = _make_fake_engine(
            mode="paper",
            under_calibrator_returns=0.75,
            under_gate_overrides={
                "under_fv_ask_gap_max": 0.20,
                "under_fv_ask_gap_min_inning": 7,
            },
        )
        ctx = _make_fake_ctx(inning=4, line="7.5", under_best_ask=0.50)
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), {"bet_id": "x"},
        )
        under = engine.recorded_decisions[0]
        # No gap-based skip; another gate or pass-through.
        self.assertNotEqual(under["decision_reason"], "gate_under_fv_ask_gap")

    def test_gates_run_in_documented_order_min_inning_before_min_ask(self):
        # When two gates would both fire, min_inning is checked first.
        engine = _make_fake_engine(
            mode="paper",
            under_calibrator_returns=0.65,
            under_gate_overrides={
                "under_min_inning": 9,
                "under_min_entry_ask": 0.55,
            },
        )
        # inning=6 trips min_inning; ask=0.40 trips min_entry_ask.
        ctx = _make_fake_ctx(inning=6, line="7.5", under_best_ask=0.40)
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), {"bet_id": "x"},
        )
        under = engine.recorded_decisions[0]
        self.assertEqual(under["decision_reason"], "gate_under_min_inning")


# ----------------------------------------------------------------------
# Paper placement
# ----------------------------------------------------------------------


class UnderPaperPlacementTests(unittest.TestCase):

    def test_paper_mode_places_bet_record_with_side_under(self):
        # All UNDER gates permissive (set in _make_fake_engine).
        # under_fv=0.65, ask=0.50 -> edge=0.15 > 0.10 min_edge.
        engine = _make_fake_engine(
            mode="paper", under_calibrator_returns=0.65,
        )
        ctx = _make_fake_ctx(inning=6, line="7.5", under_best_ask=0.50)
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), {"bet_id": "over_x"},
        )

        # Candidate row tagged paper_under
        under = engine.recorded_decisions[0]
        self.assertEqual(under["decision"], "paper_under")
        self.assertEqual(under["decision_reason"], "paper_under_gates_pass")

        # Exactly one BetRecord appended.
        self.assertEqual(len(engine._bets), 1)
        bet = engine._bets[0]
        self.assertEqual(bet.side, "under")
        self.assertEqual(bet.line, "7.5")
        self.assertAlmostEqual(bet.entry_ask, 0.50)
        self.assertAlmostEqual(bet.fair_value, 0.65)
        self.assertAlmostEqual(bet.edge, 0.15)

    def test_shadow_mode_records_candidate_but_does_not_place(self):
        engine = _make_fake_engine(
            mode="shadow", under_calibrator_returns=0.65,
        )
        ctx = _make_fake_ctx(inning=6, line="7.5", under_best_ask=0.50)
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), {"bet_id": "over_x"},
        )
        under = engine.recorded_decisions[0]
        self.assertEqual(under["decision"], "shadow_under")
        self.assertEqual(under["decision_reason"], "shadow_under_gates_pass")
        # No paper bet recorded in shadow mode.
        self.assertEqual(engine._bets, [])

    def test_off_mode_emits_nothing_and_places_nothing(self):
        engine = _make_fake_engine(
            mode="off", under_calibrator_returns=0.65,
        )
        ctx = _make_fake_ctx(inning=6, line="7.5", under_best_ask=0.50)
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), {"bet_id": "over_x"},
        )
        self.assertEqual(engine.recorded_decisions, [])
        self.assertEqual(engine._bets, [])

    def test_paper_mode_skipped_gate_does_not_place(self):
        # Trip the extreme-edge gate AND assert no BetRecord recorded.
        engine = _make_fake_engine(
            mode="paper",
            under_calibrator_returns=0.85,
            under_gate_overrides={"under_extreme_edge_max": 0.10},
        )
        ctx = _make_fake_ctx(inning=6, line="7.5", under_best_ask=0.50)
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), {"bet_id": "x"},
        )
        self.assertEqual(engine.recorded_decisions[0]["decision"], "skip")
        self.assertEqual(engine._bets, [])

    def test_paper_bet_record_carries_inferred_state_from_payload(self):
        engine = _make_fake_engine(
            mode="paper", under_calibrator_returns=0.65,
        )
        ctx = _make_fake_ctx(inning=6, line="7.5", under_best_ask=0.50)
        # Synthesize a richer OVER payload that carries inferred score
        # state. _place_under_paper_bet should mirror these onto the
        # UNDER bet (same observed game state; just trading the
        # complement side).
        over_payload = {
            "bet_id": "over_y",
            "fair_value": 0.85,
            "base_fair_value": 0.80,
            "stage2_run_env_delta": 0.05,
            "team_offense_delta": -0.02,
            "inferred_runs": 2,
            "inning": 6,
            "inning_state": "Top",
            "outs": 1,
            "away_score_before": 3,
            "home_score_before": 4,
            "runners_on": 2,
        }
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), over_payload,
        )
        bet = engine._bets[0]
        self.assertEqual(bet.inferred_runs, 2)
        self.assertEqual(bet.inning, 6)
        self.assertEqual(bet.inning_state, "Top")
        self.assertEqual(bet.outs, 1)
        self.assertEqual(bet.away_score_before, 3)
        self.assertEqual(bet.home_score_before, 4)
        self.assertEqual(bet.runners_on, 2)
        # Top-of-inning -> batting is away -> +2 runs to away side.
        self.assertEqual(bet.inferred_away_after, 5)
        self.assertEqual(bet.inferred_home_after, 4)

    def test_paper_bet_record_carries_inverted_stage_deltas(self):
        engine = _make_fake_engine(
            mode="paper", under_calibrator_returns=0.65,
        )
        ctx = _make_fake_ctx(inning=6, line="7.5", under_best_ask=0.50)
        sp._maybe_emit_under_candidate(
            engine, ctx,
            _make_fv_phase(
                stage2_run_env_delta=0.08, team_offense_delta=-0.04,
                base_fair_value=0.70,
            ),
            {"bet_id": "x"},
        )
        bet = engine._bets[0]
        # Logit-additive on OVER -> inverted on UNDER.
        self.assertAlmostEqual(bet.stage2_run_env_delta, -0.08)
        self.assertAlmostEqual(bet.team_offense_delta, 0.04)
        self.assertAlmostEqual(bet.base_fair_value, 0.30)  # 1 - 0.70


# ----------------------------------------------------------------------
# Settlement
# ----------------------------------------------------------------------


def _make_settle_engine(bets):
    """Minimal duck-typed engine for SignalEngine._settle_finished_games.

    Provides only the attributes the method touches. _is_bet_executable
    returns True (paper-mode semantics: all bets are filled at limit).
    """
    engine = SimpleNamespace()
    engine._bets = list(bets)
    # Build a games map from the bets' game_pks.
    engine.games = {}
    engine.matches = {}
    engine._line_states = {}
    engine._outcome_games_written = set()
    engine._is_bet_executable = lambda bet: True
    engine._append_to_ledger = lambda bet: None
    engine._write_outcome_record = (
        lambda *, game_pk, line, final_away, final_home: None
    )
    engine._save_session = lambda: None
    return engine


def _make_settle_game(*, game_pk, final_away, final_home):
    score = SimpleNamespace(away=final_away, home=final_home)
    return SimpleNamespace(
        game_pk=game_pk,
        score=score,
        is_final=lambda: True,
    )


def _make_settled_bet(*, side, line, game_pk=12345, entry_ask=0.50,
                       stake=10.0):
    import sys as _sys
    from pathlib import Path as _Path
    _trad = _Path(__file__).resolve().parents[1] / "scripts" / "trading"
    if str(_trad) not in _sys.path:
        _sys.path.insert(0, str(_trad))
    from models import BetRecord  # noqa: E402
    return BetRecord(
        bet_id=f"x_{side}",
        placed_at="2026-05-27T20:00:00Z",
        game_pk=game_pk,
        away_abbrev="AAA", home_abbrev="BBB",
        line=line, side=side,
        entry_ask=entry_ask,
        fair_value=0.65, base_fair_value=0.60,
        stage2_run_env_delta=0.0, team_offense_delta=0.0,
        edge=0.15,
        inferred_runs=0, inning=6, inning_state="Top", outs=1,
        away_score_before=0, home_score_before=0,
        inferred_away_after=0, inferred_home_after=0,
        stake=stake, runners_on=0,
    )


class UnderSettlementTests(unittest.TestCase):

    def setUp(self):
        import signal_engine as se  # noqa: E402
        self._settle = se.SignalEngine._settle_finished_games

    def test_under_bet_wins_when_final_total_below_line(self):
        bet = _make_settled_bet(side="under", line="8.5")
        engine = _make_settle_engine([bet])
        engine.games[12345] = _make_settle_game(
            game_pk=12345, final_away=3, final_home=4,  # total=7 < 8.5
        )
        self._settle(engine)
        self.assertTrue(bet.settled)
        self.assertTrue(bet.won)
        # Paper-mode payout: stake/entry_ask = 10/0.50 = 20.0;
        # profit = 20 - 10 = 10.
        self.assertAlmostEqual(bet.payout, 20.0)
        self.assertAlmostEqual(bet.profit, 10.0)

    def test_under_bet_loses_when_final_total_above_line(self):
        bet = _make_settled_bet(side="under", line="8.5")
        engine = _make_settle_engine([bet])
        engine.games[12345] = _make_settle_game(
            game_pk=12345, final_away=6, final_home=5,  # total=11 > 8.5
        )
        self._settle(engine)
        self.assertTrue(bet.settled)
        self.assertFalse(bet.won)
        self.assertAlmostEqual(bet.payout, 0.0)
        self.assertAlmostEqual(bet.profit, -10.0)

    def test_over_bet_settlement_logic_unchanged(self):
        # Backward-compatibility: OVER bets still win when total > line.
        bet = _make_settled_bet(side="over", line="8.5")
        engine = _make_settle_engine([bet])
        engine.games[12345] = _make_settle_game(
            game_pk=12345, final_away=6, final_home=5,  # total=11 > 8.5
        )
        self._settle(engine)
        self.assertTrue(bet.won)

    def test_under_bet_loses_when_final_total_equals_line_high_side(self):
        # MLB OU lines end in .5 so true ties are impossible, but
        # confirm the strict inequality semantics by setting the line
        # below the integer total (e.g. line=7.5, total=8 -> UNDER
        # loses because 8 > 7.5).
        bet = _make_settled_bet(side="under", line="7.5")
        engine = _make_settle_engine([bet])
        engine.games[12345] = _make_settle_game(
            game_pk=12345, final_away=4, final_home=4,  # total=8 > 7.5
        )
        self._settle(engine)
        self.assertFalse(bet.won)

    def test_under_and_over_in_same_session_settle_independently(self):
        over_bet = _make_settled_bet(side="over", line="8.5",
                                       game_pk=11111)
        under_bet = _make_settled_bet(side="under", line="8.5",
                                        game_pk=22222)
        engine = _make_settle_engine([over_bet, under_bet])
        # OVER game finishes 10 > 8.5 -> OVER wins.
        engine.games[11111] = _make_settle_game(
            game_pk=11111, final_away=5, final_home=5,
        )
        # UNDER game finishes 6 < 8.5 -> UNDER wins.
        engine.games[22222] = _make_settle_game(
            game_pk=22222, final_away=3, final_home=3,
        )
        self._settle(engine)
        self.assertTrue(over_bet.won)
        self.assertTrue(under_bet.won)


# ----------------------------------------------------------------------
# Live-engine safety
# ----------------------------------------------------------------------


class LiveEngineUnderSafetyTests(unittest.TestCase):

    def test_live_is_bet_executable_returns_true_for_under_paper(self):
        """LiveTradingEngine override must still settle UNDER paper
        bets even though they have no order_status='filled' (they
        never went to CLOB)."""
        import live_engine as le  # noqa: E402
        # Build a fake live engine instance via __new__ to bypass
        # __init__ (which needs CLOB creds + many other things).
        fake_self = le.LiveTradingEngine.__new__(le.LiveTradingEngine)

        under_bet = _make_settled_bet(side="under", line="8.5")
        # UNDER paper bet has no order_status; live override must
        # still return True.
        self.assertTrue(
            le.LiveTradingEngine._is_bet_executable(fake_self, under_bet),
        )

    def test_live_is_bet_executable_unchanged_for_over_bets(self):
        import live_engine as le  # noqa: E402
        fake_self = le.LiveTradingEngine.__new__(le.LiveTradingEngine)

        over_bet_filled = _make_settled_bet(side="over", line="8.5")
        over_bet_filled.order_status = "filled"
        over_bet_unfilled = _make_settled_bet(side="over", line="8.5")
        over_bet_unfilled.order_status = "cancelled"

        self.assertTrue(
            le.LiveTradingEngine._is_bet_executable(
                fake_self, over_bet_filled,
            ),
        )
        self.assertFalse(
            le.LiveTradingEngine._is_bet_executable(
                fake_self, over_bet_unfilled,
            ),
        )

    def test_place_bet_is_side_parameterized(self):
        """2026-05-28: live UNDER shipped. place_bet is no longer OVER-only;
        it accepts a `side` kwarg (defaulting to 'over') and routes to the
        side-correct token. Pins the new contract."""
        import inspect
        import live_engine_placement as lep  # noqa: E402
        sig = inspect.signature(lep.place_bet)
        self.assertIn("side", sig.parameters)
        self.assertEqual(sig.parameters["side"].default, "over")

    def test_live_under_requires_real_fill_no_fabricated_pnl(self):
        """CRITICAL safety: a REAL (placement_mode='live') UNDER order must
        NOT be treated as executable unless it actually filled. The prior
        blanket `side==under -> True` would have fabricated P&L on unfilled
        live UNDER orders."""
        import live_engine as le  # noqa: E402
        from models import LiveBetRecord  # noqa: E402
        fake_self = le.LiveTradingEngine.__new__(le.LiveTradingEngine)

        live_under_unfilled = LiveBetRecord(
            bet_id="lu1", placed_at="2026-05-28T20:00:00Z", game_pk=1,
            away_abbrev="AAA", home_abbrev="BBB", line="8.5", side="under",
            entry_ask=0.55, fair_value=0.65, base_fair_value=0.60,
            stage2_run_env_delta=0.0, team_offense_delta=0.0, edge=0.10,
            inferred_runs=0, inning=6, inning_state="Top", outs=1,
            away_score_before=2, home_score_before=2,
            inferred_away_after=2, inferred_home_after=2, stake=10.0,
            runners_on=0, placement_mode="live", order_status="cancelled",
        )
        self.assertFalse(
            le.LiveTradingEngine._is_bet_executable(fake_self, live_under_unfilled),
            "unfilled live UNDER must not be executable",
        )
        live_under_filled = LiveBetRecord(
            bet_id="lu2", placed_at="2026-05-28T20:00:00Z", game_pk=1,
            away_abbrev="AAA", home_abbrev="BBB", line="8.5", side="under",
            entry_ask=0.55, fair_value=0.65, base_fair_value=0.60,
            stage2_run_env_delta=0.0, team_offense_delta=0.0, edge=0.10,
            inferred_runs=0, inning=6, inning_state="Top", outs=1,
            away_score_before=2, home_score_before=2,
            inferred_away_after=2, inferred_home_after=2, stake=10.0,
            runners_on=0, placement_mode="live", order_status="filled",
        )
        self.assertTrue(
            le.LiveTradingEngine._is_bet_executable(fake_self, live_under_filled),
            "filled live UNDER must be executable",
        )

    def test_bet_traded_token_id_routes_by_side(self):
        """Lifecycle must manage UNDER orders against the under_no token."""
        from models import LiveBetRecord, bet_traded_token_id  # noqa: E402
        over = LiveBetRecord(
            bet_id="o", placed_at="t", game_pk=1, away_abbrev="A",
            home_abbrev="B", line="8.5", side="over", entry_ask=0.6,
            fair_value=0.7, base_fair_value=0.65, stage2_run_env_delta=0.0,
            team_offense_delta=0.0, edge=0.1, inferred_runs=0, inning=6,
            inning_state="Top", outs=1, away_score_before=0,
            home_score_before=0, inferred_away_after=0, inferred_home_after=0,
            stake=10.0, runners_on=0,
            over_token_id="OVER_TOK", under_token_id="UNDER_TOK",
        )
        under = LiveBetRecord(
            bet_id="u", placed_at="t", game_pk=1, away_abbrev="A",
            home_abbrev="B", line="8.5", side="under", entry_ask=0.6,
            fair_value=0.7, base_fair_value=0.65, stage2_run_env_delta=0.0,
            team_offense_delta=0.0, edge=0.1, inferred_runs=0, inning=6,
            inning_state="Top", outs=1, away_score_before=0,
            home_score_before=0, inferred_away_after=0, inferred_home_after=0,
            stake=10.0, runners_on=0,
            over_token_id="OVER_TOK", under_token_id="UNDER_TOK",
        )
        self.assertEqual(bet_traded_token_id(over), "OVER_TOK")
        self.assertEqual(bet_traded_token_id(under), "UNDER_TOK")


# ----------------------------------------------------------------------
# CLI flag rename + backward-compat alias
# ----------------------------------------------------------------------


class UnderModeFlagTests(unittest.TestCase):

    def _parse(self, *argv):
        import signal_config  # noqa: E402
        trade_args, _ = signal_config.parse_trade_args(list(argv))
        return trade_args

    def test_default_under_mode_is_off(self):
        ta = self._parse()
        self.assertEqual(ta.under_mode, "off")
        # Legacy mirror keeps the old attribute name populated.
        self.assertEqual(ta.under_emission_mode, "off")

    def test_under_mode_paper_is_accepted(self):
        ta = self._parse("--under-mode", "paper")
        self.assertEqual(ta.under_mode, "paper")
        self.assertEqual(ta.under_emission_mode, "paper")

    def test_legacy_under_emission_mode_alias_still_works(self):
        ta = self._parse("--under-emission-mode", "shadow")
        self.assertEqual(ta.under_mode, "shadow")

    def test_new_flag_wins_when_both_supplied(self):
        ta = self._parse(
            "--under-mode", "paper",
            "--under-emission-mode", "shadow",
        )
        self.assertEqual(ta.under_mode, "paper")

    def test_under_gate_thresholds_have_sensible_defaults(self):
        ta = self._parse()
        self.assertEqual(ta.under_min_inning, 4)
        self.assertEqual(ta.under_min_inning_high_line, 5)
        # 2026-05-30: NOT mirrored to OVER. UNDER ask floor lives in
        # the complementary-token price coordinate; mirroring 0.55/0.60
        # blocked ~877/877 UNDER candidates on the 2026-05-29 paper
        # session. Lowered 0.30 -> 0.20 after the M_under_paper day-1
        # distribution still pancaked at 0.30 (median ask 0.26 / p25 0.20).
        # See signal_config.py block above DEFAULT_UNDER_MIN_ENTRY_ASK.
        self.assertAlmostEqual(ta.under_min_entry_ask, 0.20)
        self.assertAlmostEqual(ta.under_min_entry_ask_high_line, 0.20)
        self.assertAlmostEqual(ta.under_max_base_fv, 0.99)
        self.assertAlmostEqual(ta.under_fv_ask_gap_max, 0.26)
        self.assertEqual(ta.under_fv_ask_gap_min_inning, 7)
        self.assertAlmostEqual(ta.under_extreme_edge_max, 0.22)
        # 2026-05-30: UNDER calibration mode defaults to `enforce` for
        # back-compat; M_under_paper preset overrides to `off`.
        self.assertEqual(ta.under_calibration_mode, "enforce")

    def test_under_calibration_mode_flag_accepts_three_modes(self):
        for mode in ("off", "shadow", "enforce"):
            ta = self._parse("--under-calibration-mode", mode)
            self.assertEqual(ta.under_calibration_mode, mode)

    def test_under_mode_live_is_accepted(self):
        """2026-05-28: real-money UNDER trading."""
        ta = self._parse("--under-mode", "live")
        self.assertEqual(ta.under_mode, "live")


# ----------------------------------------------------------------------
# Live UNDER placement (2026-05-28): real CLOB order on the under token
# ----------------------------------------------------------------------


class LiveUnderPlacementTests(unittest.TestCase):

    def _live_engine(self, clob):
        import live_engine as le  # noqa: E402
        eng = le.LiveTradingEngine.__new__(le.LiveTradingEngine)
        eng.date_str = "2026-05-28"
        eng._bets = []
        eng._open_orders = {}
        eng._bet_counter = 0
        eng._last_place_bet_skip_reason = None
        eng._dry_run = False
        eng._ev_policy_mode = "off"
        eng._under_mode = "live"
        eng._clob = clob
        eng.trade_args = SimpleNamespace(
            capture_depth=5, max_spread=0.20, edge_threshold=0.15,
            edge_threshold_high_line=0.16, high_line_cutoff=8.5,
            config_label="live", stake=10.0,
        )
        eng.live_args = SimpleNamespace(
            spread_factor=0.65, stake_mode="flat", min_order_size=5.0,
            daily_budget=80.0, per_game_budget_fraction=0.40,
            max_open_orders=7, calibrated_stake_scale_mode="off",
            kelly_max_edge=0.25, kelly_fraction=0.25,
            kelly_max_bet_fraction=0.33,
            max_correlated_over_lines_per_game=2,
            min_correlated_line_gap=1.5,
        )
        eng._fetch_depth_snapshot = lambda token_id, depth: {
            "ok": True, "best_bid": 0.55, "best_ask": 0.60,
        }
        eng._compute_limit_price = (
            lambda *, ask, bid, fair_value, line_val: 0.58
        )
        eng._compute_stake = lambda fv, lp: 10.0
        eng._kelly_components = lambda fv, lp: (0.0, 0.0, 0.0)
        eng._filled_notional = lambda b: 0.0
        eng._append_to_live_ledger = lambda b: None
        eng._save_session = lambda: None
        return eng

    def test_live_under_routes_order_to_under_token(self):
        import live_engine_placement as lep  # noqa: E402
        from models import OrderResult  # noqa: E402

        calls = {}

        class FakeClob:
            def place_limit_buy(self, *, token_id, price, size_usdc):
                calls["token_id"] = token_id
                calls["price"] = price
                return OrderResult(
                    success=True, order_id="oid_under", status="live",
                    size_shares=size_usdc / price,
                )

        eng = self._live_engine(FakeClob())
        market = SimpleNamespace(
            line="8.5", over_token_id="OVER_TOK",
            under_token_id="UNDER_TOK", venue_name="Park",
        )
        game = SimpleNamespace(
            game_pk=99, away_abbrev="AAA", home_abbrev="BBB",
            venue_name="Park", game_date="2026-05-28",
        )
        bet = lep.place_bet(
            eng, game=game, market=market, best_ask=0.60,
            fair_value=0.78, base_fair_value=0.40,
            stage2_run_env_delta=0.0, team_offense_delta=0.0,
            edge=0.18, inferred_runs=0, inning=6, inning_state="Top",
            outs=1, away_score_before=2, home_score_before=2,
            batting_is_away=True, runners_on=0, decision_bid=0.55,
            side="under",
        )
        self.assertIsNotNone(bet)
        self.assertEqual(bet.side, "under")
        # The CLOB order was posted on the UNDER token, not OVER.
        self.assertEqual(calls["token_id"], "UNDER_TOK")
        self.assertEqual(bet.under_token_id, "UNDER_TOK")
        self.assertEqual(bet.over_token_id, "OVER_TOK")
        self.assertEqual(bet.order_id, "oid_under")
        self.assertEqual(bet.placement_mode, "live")
        # Registered for lifecycle management.
        self.assertIn("oid_under", eng._open_orders)

    def test_over_path_unchanged_routes_to_over_token(self):
        import live_engine_placement as lep  # noqa: E402
        from models import OrderResult  # noqa: E402

        calls = {}

        class FakeClob:
            def place_limit_buy(self, *, token_id, price, size_usdc):
                calls["token_id"] = token_id
                return OrderResult(
                    success=True, order_id="oid_over", status="live",
                    size_shares=size_usdc / price,
                )

        eng = self._live_engine(FakeClob())
        eng._under_mode = "off"
        # OVER needs the EV feature builders; stub them no-op-allow.
        eng._build_ev_feature_row = lambda **kw: {}
        eng._evaluate_ev_policy = lambda row, stake, lp: (True, None)
        market = SimpleNamespace(
            line="8.5", over_token_id="OVER_TOK",
            under_token_id="UNDER_TOK", venue_name="Park",
        )
        game = SimpleNamespace(
            game_pk=99, away_abbrev="AAA", home_abbrev="BBB",
            venue_name="Park", game_date="2026-05-28",
        )
        bet = lep.place_bet(
            eng, game=game, market=market, best_ask=0.60,
            fair_value=0.78, base_fair_value=0.72,
            stage2_run_env_delta=0.0, team_offense_delta=0.0,
            edge=0.18, inferred_runs=1, inning=6, inning_state="Top",
            outs=1, away_score_before=2, home_score_before=2,
            batting_is_away=True, runners_on=0, decision_bid=0.55,
        )  # no side kwarg -> defaults to over
        self.assertIsNotNone(bet)
        self.assertEqual(bet.side, "over")
        self.assertEqual(calls["token_id"], "OVER_TOK")
        self.assertEqual(bet.under_token_id, "")


if __name__ == "__main__":
    unittest.main()
