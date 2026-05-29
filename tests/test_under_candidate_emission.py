"""Phase A5 (2026-05-19): UNDER candidate emission tests.

Tests for `_maybe_emit_under_candidate` in scripts/trading/
signal_pipeline.py + the live_engine_cli.py -> live_engine.py ->
SignalEngine bridge for `--under-emission-mode`.

The helper emits a sibling UNDER candidate row alongside every OVER
candidate that reaches the FV phase. Coverage:

  - mode='off' is a no-op (default; existing OVER-only behavior)
  - mode='shadow' calls the engine's _record_candidate_decision
    with a payload carrying side='under', bet_id ending in
    `_under_shadow`, calibrated UNDER FV, UNDER-side ask
  - UNDER FV uses (1 - over_fv_raw) through the UNDER calibrator
  - UNDER-side market: under_best_ask + under_best_bid + under_pair_available
  - When UNDER ask is missing, payload is recorded with decision='skip'
    and decision_reason='gate_no_under_liquidity' (coverage diagnostic)
  - Stage-2 / Stage-3 deltas carry the SIGNED inverse (logit-additive
    on OVER => opposite sign on UNDER)
  - ANY exception in the helper is swallowed (fail-open contract)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List


PROJECT_DIR = Path(__file__).resolve().parents[1]
for sub in ("scripts/trading", "scripts/analysis", "scripts/monitor", "cache"):
    p = PROJECT_DIR / sub
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


import signal_pipeline as sp  # noqa: E402


def _make_fake_engine(
    *,
    mode: str = "off",
    under_calibrator_returns: float | None = None,
    edge_threshold: float = 0.10,
    under_gate_overrides: Dict[str, Any] | None = None,
) -> Any:
    """Build a duck-typed engine for the helper.

    Records every `_record_candidate_decision(payload)` call into
    `engine.recorded_decisions` so tests can assert on the emitted
    row directly.

    Phase C-paper (2026-05-27): UNDER gate thresholds default to
    permissive values so existing shadow-only tests are unaffected
    by the 5 new symmetric gates. Per-test overrides via
    `under_gate_overrides` exercise individual gate behavior.
    """
    engine = SimpleNamespace()
    # Carry both the legacy and new mode attrs so the helper's
    # `getattr(engine, "_under_mode", ...) or getattr(engine,
    # "_under_emission_mode", "off")` resolves either way.
    engine._under_mode = mode
    engine._under_emission_mode = mode if mode != "paper" else "shadow"

    if under_calibrator_returns is None:
        under_cal = None
    else:
        under_cal = SimpleNamespace(
            method="platt",
            calibrate=lambda raw, model_family=None: under_calibrator_returns,
        )
    engine._under_prob_calibrator = under_cal

    # Permissive UNDER gate defaults: tuned so the existing tests
    # (which only assert on gate_min_edge / no_liquidity) are not
    # accidentally tripped by the new 5-gate stack. Each test that
    # exercises a specific gate overrides the relevant attr.
    under_gate_defaults = {
        "edge_threshold": edge_threshold,
        "high_line_cutoff": 8.5,
        "under_min_inning": 1,
        "under_min_inning_high_line": 1,
        "under_min_entry_ask": 0.0,
        "under_min_entry_ask_high_line": 0.0,
        "under_max_base_fv": 1.01,
        "under_fv_ask_gap_max": 1.0,
        "under_fv_ask_gap_min_inning": 99,
        "under_extreme_edge_max": 1.0,
        "stake": 10.0,
        "config_label": "default",
    }
    if under_gate_overrides:
        under_gate_defaults.update(under_gate_overrides)
    engine.trade_args = SimpleNamespace(**under_gate_defaults)
    engine.recorded_decisions: List[Dict[str, Any]] = []
    engine._record_candidate_decision = lambda p: engine.recorded_decisions.append(dict(p))
    # Phase C-paper paper placement collects BetRecords here.
    engine._bets: List[Any] = []
    engine.date_str = "2026-05-27"
    engine._under_bet_counter = 0
    engine._save_session = lambda: None
    return engine


def _make_fake_ctx(
    *,
    game_pk: int = 12345,
    line: str = "8.5",
    inning: int = 5,
    inning_state: str = "Top",
    outs: int = 1,
    away_score: int = 3,
    home_score: int = 2,
    under_best_ask: float | None = 0.35,
    under_best_bid: float | None = 0.32,
    under_pair_available: bool = True,
    ask: float = 0.65,
    best_bid: float = 0.63,
) -> Any:
    ctx = SimpleNamespace()
    ctx.game = SimpleNamespace(
        game_pk=game_pk,
        away_abbrev="AAA",
        home_abbrev="BBB",
        venue_name="Test Park",
        game_date="2026-05-19T19:05:00Z",
    )
    ctx.market = SimpleNamespace(
        line=line, over_token_id="0xover", under_token_id="0xunder",
    )
    ctx.inning = inning
    ctx.inning_state = inning_state
    ctx.outs = outs
    ctx.away_score = away_score
    ctx.home_score = home_score
    ctx.current_total = away_score + home_score
    ctx.runners_on = 0
    ctx.ask = ask
    ctx.best_bid = best_bid
    ctx.line_val = float(line)
    ctx.book = {
        "best_bid": best_bid, "best_ask": ask,
        "under_best_bid": under_best_bid,
        "under_best_ask": under_best_ask,
        "under_pair_available": under_pair_available,
        "ltp": 0.50,
    }
    ctx.now = 1779200000.0
    return ctx


def _make_fv_phase(
    *,
    fair_value: float = 0.80,
    fair_value_raw: float = 0.78,
    base_fair_value: float = 0.75,
    stage2_run_env_delta: float = 0.05,
    team_offense_delta: float = -0.03,
) -> Any:
    return SimpleNamespace(
        stopped=False,
        fair_value=fair_value,
        fair_value_raw=fair_value_raw,
        base_fair_value=base_fair_value,
        stage2_run_env_delta=stage2_run_env_delta,
        team_offense_delta=team_offense_delta,
        inferred_runs=1,
        batting_is_away=True,
        half="T",
        edge=fair_value - 0.65,
        min_edge_base=0.10,
        ask_edge_boost=0.0,
        min_edge=0.10,
    )


class UnderCandidateEmissionTests(unittest.TestCase):

    def test_mode_off_is_no_op(self):
        engine = _make_fake_engine(mode="off")
        ctx = _make_fake_ctx()
        over_payload = {"bet_id": "test_0001"}
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), over_payload,
        )
        self.assertEqual(engine.recorded_decisions, [])

    def test_mode_shadow_emits_under_candidate_with_side_field(self):
        engine = _make_fake_engine(
            mode="shadow", under_calibrator_returns=0.18,
        )
        ctx = _make_fake_ctx(under_best_ask=0.35)
        over_payload = {
            "bet_id": "test_0001",
            "fair_value": 0.80,
            "base_fair_value": 0.75,
            "stage2_run_env_delta": 0.05,
            "team_offense_delta": -0.03,
        }
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), over_payload,
        )
        self.assertEqual(len(engine.recorded_decisions), 1)
        under = engine.recorded_decisions[0]
        self.assertEqual(under["side"], "under")
        self.assertEqual(under["bet_id"], "test_0001_under_shadow")
        self.assertEqual(under["over_bet_id"], "test_0001")
        self.assertEqual(under["entry_ask"], 0.35)
        self.assertEqual(under["fair_value"], 0.18)
        self.assertEqual(under["fair_value_raw"], 1.0 - 0.78)

    def test_under_calibrator_is_called_with_complement_of_over_raw(self):
        # Identity calibrator: under_fv should equal 1 - over_fv_raw
        # (= 0.22) when over_fv_raw=0.78.
        engine = _make_fake_engine(
            mode="shadow", under_calibrator_returns=0.22,
        )
        ctx = _make_fake_ctx()
        over_payload = {"bet_id": "id1"}
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(fair_value_raw=0.78), over_payload,
        )
        under = engine.recorded_decisions[0]
        self.assertAlmostEqual(under["fair_value_raw"], 0.22)
        self.assertAlmostEqual(under["fair_value"], 0.22)

    def test_missing_under_ask_records_no_liquidity_skip(self):
        engine = _make_fake_engine(
            mode="shadow", under_calibrator_returns=0.30,
        )
        ctx = _make_fake_ctx(under_best_ask=None)
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), {"bet_id": "x"},
        )
        self.assertEqual(len(engine.recorded_decisions), 1)
        under = engine.recorded_decisions[0]
        self.assertEqual(under["decision"], "skip")
        self.assertEqual(under["decision_reason"], "gate_no_under_liquidity")
        self.assertIsNone(under["edge"])
        self.assertIsNone(under["entry_ask"])

    def test_under_edge_above_threshold_yields_shadow_under_decision(self):
        # under_fv=0.55, under_ask=0.35 -> edge=0.20 > 0.10 threshold
        engine = _make_fake_engine(
            mode="shadow",
            under_calibrator_returns=0.55,
            edge_threshold=0.10,
        )
        ctx = _make_fake_ctx(under_best_ask=0.35)
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), {"bet_id": "x"},
        )
        under = engine.recorded_decisions[0]
        self.assertEqual(under["decision"], "shadow_under")
        self.assertEqual(under["decision_reason"], "shadow_under_gates_pass")
        self.assertAlmostEqual(under["edge"], 0.20, places=5)

    def test_under_edge_below_threshold_yields_skip_min_edge(self):
        # under_fv=0.40, under_ask=0.38 -> edge=0.02 < 0.10 threshold
        engine = _make_fake_engine(
            mode="shadow",
            under_calibrator_returns=0.40,
            edge_threshold=0.10,
        )
        ctx = _make_fake_ctx(under_best_ask=0.38)
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), {"bet_id": "x"},
        )
        under = engine.recorded_decisions[0]
        self.assertEqual(under["decision"], "skip")
        self.assertEqual(under["decision_reason"], "gate_min_edge")

    def test_stage2_and_stage3_deltas_propagate_with_inverted_sign(self):
        """Stage-2 / Stage-3 logit-additive deltas on OVER are
        equivalent magnitude opposite sign on UNDER. Carrying the
        signed inverse on the UNDER row lets cohort analysis compare
        side-by-side without having to re-derive."""
        engine = _make_fake_engine(
            mode="shadow", under_calibrator_returns=0.30,
        )
        ctx = _make_fake_ctx()
        sp._maybe_emit_under_candidate(
            engine, ctx,
            _make_fv_phase(
                stage2_run_env_delta=0.08, team_offense_delta=-0.04,
            ),
            {"bet_id": "x"},
        )
        under = engine.recorded_decisions[0]
        self.assertAlmostEqual(under["stage2_run_env_delta"], -0.08)
        self.assertAlmostEqual(under["team_offense_delta"], 0.04)
        # base_fv inversion: over base_fv=0.75 -> under base_fv=0.25
        self.assertAlmostEqual(under["base_fair_value"], 0.25)

    def test_under_pair_available_propagates(self):
        engine = _make_fake_engine(
            mode="shadow", under_calibrator_returns=0.30,
        )
        ctx = _make_fake_ctx(under_pair_available=False)
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), {"bet_id": "x"},
        )
        under = engine.recorded_decisions[0]
        self.assertEqual(under["under_pair_available"], False)

    def test_helper_is_failopen_on_internal_exception(self):
        """If the engine's _record_candidate_decision raises, the
        helper swallows the exception so OVER's pipeline continues.
        """
        engine = _make_fake_engine(
            mode="shadow", under_calibrator_returns=0.30,
        )
        engine._record_candidate_decision = lambda p: (_ for _ in ()).throw(
            RuntimeError("simulated downstream failure"),
        )
        ctx = _make_fake_ctx()
        # Must NOT raise.
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(), {"bet_id": "x"},
        )

    def test_helper_no_op_when_over_fv_raw_is_none(self):
        """The FV phase may emit fair_value_raw=None on some edge
        cases (calibration off, missing stage3). UNDER complement is
        undefined; emit nothing rather than guessing."""
        engine = _make_fake_engine(
            mode="shadow", under_calibrator_returns=0.30,
        )
        ctx = _make_fake_ctx()
        sp._maybe_emit_under_candidate(
            engine, ctx,
            _make_fv_phase(fair_value_raw=None),
            {"bet_id": "x"},
        )
        self.assertEqual(engine.recorded_decisions, [])

    def test_no_calibrator_falls_back_to_identity_complement(self):
        engine = _make_fake_engine(
            mode="shadow", under_calibrator_returns=None,
        )
        ctx = _make_fake_ctx()
        sp._maybe_emit_under_candidate(
            engine, ctx, _make_fv_phase(fair_value_raw=0.78),
            {"bet_id": "x"},
        )
        under = engine.recorded_decisions[0]
        # No calibrator -> fair_value == fair_value_raw == 1 - 0.78
        self.assertAlmostEqual(under["fair_value"], 0.22)
        self.assertAlmostEqual(under["fair_value_raw"], 0.22)


class LiveEngineUnderEmissionBridgeTests(unittest.TestCase):
    """Verify the live_engine -> trade_args bridge for --under-mode
    (and the legacy --under-emission-mode alias) is wired correctly.
    Mirrors the existing bridge test for
    --stage1-shadow-empirical-override.
    """

    def test_bridge_resolves_under_mode_and_alias(self):
        # Read the source directly to avoid having to instantiate
        # the full LiveTradingEngine.
        src = (PROJECT_DIR / "scripts" / "trading" / "live_engine.py").read_text(
            encoding="utf-8",
        )
        # New flag bridge.
        self.assertIn(
            'under_mode = getattr(live_args, "under_mode", "off")', src,
            "Missing the trade_args.under_mode bridge in live_engine.py.",
        )
        # Legacy alias merge.
        self.assertIn(
            'getattr(\n            live_args, "under_emission_mode_legacy", None',
            src,
            "Missing the legacy --under-emission-mode alias bridge.",
        )
        # Legacy mirror so any downstream reader still works for one
        # transition cycle.
        self.assertIn("trade_args.under_emission_mode = under_mode", src)
        # 5 symmetric UNDER gate thresholds wired through.
        self.assertIn('"under_min_inning"', src)
        self.assertIn('"under_min_entry_ask"', src)
        self.assertIn('"under_max_base_fv"', src)
        self.assertIn('"under_fv_ask_gap_max"', src)
        self.assertIn('"under_extreme_edge_max"', src)


if __name__ == "__main__":
    unittest.main()
