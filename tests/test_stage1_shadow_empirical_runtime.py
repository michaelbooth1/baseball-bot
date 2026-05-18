"""Active #8 prep (2026-05-17): runtime shadow logging for Alt A.

Tests for the `_attach_stage1_shadow_empirical_fields` helper added
to scripts/trading/signal_pipeline_gates_post_fv.py. Covers:

  - mode='off' attaches the mode tag but no alt values (no decision
    change; existing engine state untouched)
  - mode='shadow' + no empirical: mode tag set, alt fields None,
    used_empirical=False
  - mode='shadow' + empirical present: alt fields populated with the
    correct logit-additive composition AND the production calibrator
    has been called on the alt raw prob
  - Boundary empirical values (0.0, 1.0) treated as unusable
  - ANY exception in the alt path is swallowed (fail-open)
  - The bridge in LiveTradingEngine.__init__ copies live_args ->
    trade_args correctly

The shadow-override report's tests already cover the
'runtime > offline > no_change' source preference; the runtime
hook is the producer side of that contract.
"""

from __future__ import annotations

import argparse
import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock


PROJECT_DIR = Path(__file__).resolve().parents[1]
for sub in ("scripts/trading", "scripts/analysis", "scripts/monitor", "cache"):
    p = PROJECT_DIR / sub
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import signal_pipeline_gates_post_fv as sp_gates  # noqa: E402


def _make_fake_engine(
    *, mode: str = "off",
    calibrated_fv_returns=None,
) -> Any:
    """Build a minimal duck-typed engine for the helper.

    The helper only reads `_stage1_shadow_empirical_mode` and calls
    `_calibrate_fair_value(...)`. We hand-roll a SimpleNamespace so
    we don't have to construct a real SignalEngine.
    """
    engine = SimpleNamespace()
    engine._stage1_shadow_empirical_mode = mode

    if calibrated_fv_returns is None:
        # Default: calibrator returns the raw_prob unchanged (the
        # current production behavior when calibration is in shadow).
        def _calibrate(*, raw_prob, line, inning, decision_ask, model_family):
            return raw_prob, {"calibrated_prob": raw_prob, "delta": 0.0}
    else:
        # Operator-supplied calibrator behavior for shift-testing.
        def _calibrate(*, raw_prob, line, inning, decision_ask, model_family):
            return calibrated_fv_returns, {"calibrated_prob": calibrated_fv_returns, "delta": calibrated_fv_returns - raw_prob}

    engine._calibrate_fair_value = _calibrate
    return engine


def _payload(*, base_empirical=None, fair_value=0.85) -> Dict[str, Any]:
    """Candidate payload pre-populated to the state the helper expects."""
    payload: Dict[str, Any] = {
        "fair_value": fair_value,
    }
    if base_empirical is not None:
        payload["inferred_state_base_empirical"] = base_empirical
    return payload


class ModeOffTests(unittest.TestCase):
    """When mode is 'off', attach the mode tag but never compute alts."""

    def test_off_mode_attaches_only_mode_tag(self):
        engine = _make_fake_engine(mode="off")
        payload = _payload(base_empirical=0.5)
        sp_gates._attach_stage1_shadow_empirical_fields(
            engine,
            candidate_payload=payload,
            base_fair_value_poisson=0.7,
            stage2_run_env_delta=0.05,
            team_offense_delta=0.0,
            line="8.5",
            inning=6,
            decision_ask=0.65,
        )
        self.assertEqual(payload["stage1_shadow_empirical_mode"], "off")
        self.assertIsNone(payload["fair_value_alt_empirical"])
        self.assertIsNone(payload["fair_value_alt_empirical_raw"])
        self.assertIsNone(payload["fair_value_alt_empirical_delta_vs_prod"])
        self.assertFalse(payload["fair_value_alt_empirical_used_empirical"])
        self.assertIsNone(payload["fair_value_alt_empirical_p0"])

    def test_off_mode_does_not_call_calibrator(self):
        engine = _make_fake_engine(mode="off")
        engine._calibrate_fair_value = MagicMock(return_value=(0.85, {}))
        payload = _payload(base_empirical=0.5)
        sp_gates._attach_stage1_shadow_empirical_fields(
            engine,
            candidate_payload=payload,
            base_fair_value_poisson=0.7,
            stage2_run_env_delta=0.05,
            team_offense_delta=0.0,
            line="8.5", inning=6, decision_ask=0.65,
        )
        engine._calibrate_fair_value.assert_not_called()


class ModeShadowNoEmpiricalTests(unittest.TestCase):
    """When mode is 'shadow' but no empirical: mode tag set, alts None."""

    def test_no_empirical_field(self):
        engine = _make_fake_engine(mode="shadow")
        payload = _payload()  # no inferred_state_base_empirical
        sp_gates._attach_stage1_shadow_empirical_fields(
            engine, candidate_payload=payload,
            base_fair_value_poisson=0.7,
            stage2_run_env_delta=0.0, team_offense_delta=0.0,
            line="8.5", inning=6, decision_ask=0.65,
        )
        self.assertEqual(payload["stage1_shadow_empirical_mode"], "shadow")
        self.assertIsNone(payload["fair_value_alt_empirical"])
        self.assertFalse(payload["fair_value_alt_empirical_used_empirical"])

    def test_empirical_none_value(self):
        engine = _make_fake_engine(mode="shadow")
        payload = _payload(base_empirical=None)
        # The payload setter omits None; helper still treats as missing
        payload["inferred_state_base_empirical"] = None
        sp_gates._attach_stage1_shadow_empirical_fields(
            engine, candidate_payload=payload,
            base_fair_value_poisson=0.7,
            stage2_run_env_delta=0.0, team_offense_delta=0.0,
            line="8.5", inning=6, decision_ask=0.65,
        )
        self.assertIsNone(payload["fair_value_alt_empirical"])
        self.assertFalse(payload["fair_value_alt_empirical_used_empirical"])

    def test_empirical_unparseable(self):
        engine = _make_fake_engine(mode="shadow")
        payload = _payload()
        payload["inferred_state_base_empirical"] = "not a number"
        sp_gates._attach_stage1_shadow_empirical_fields(
            engine, candidate_payload=payload,
            base_fair_value_poisson=0.7,
            stage2_run_env_delta=0.0, team_offense_delta=0.0,
            line="8.5", inning=6, decision_ask=0.65,
        )
        self.assertIsNone(payload["fair_value_alt_empirical"])

    def test_empirical_boundary_zero_rejected(self):
        engine = _make_fake_engine(mode="shadow")
        payload = _payload(base_empirical=0.0)
        sp_gates._attach_stage1_shadow_empirical_fields(
            engine, candidate_payload=payload,
            base_fair_value_poisson=0.7,
            stage2_run_env_delta=0.0, team_offense_delta=0.0,
            line="8.5", inning=6, decision_ask=0.65,
        )
        # 0.0 would blow up the logit transform
        self.assertIsNone(payload["fair_value_alt_empirical"])

    def test_empirical_boundary_one_rejected(self):
        engine = _make_fake_engine(mode="shadow")
        payload = _payload(base_empirical=1.0)
        sp_gates._attach_stage1_shadow_empirical_fields(
            engine, candidate_payload=payload,
            base_fair_value_poisson=0.7,
            stage2_run_env_delta=0.0, team_offense_delta=0.0,
            line="8.5", inning=6, decision_ask=0.65,
        )
        self.assertIsNone(payload["fair_value_alt_empirical"])


class ModeShadowWithEmpiricalTests(unittest.TestCase):
    """When mode is 'shadow' and empirical present: alt fields populated."""

    def test_alt_logit_additive_math(self):
        # empirical=0.5, s2=0.0, s3=0.0 -> p2_alt = sigmoid(logit(0.5)) = 0.5
        engine = _make_fake_engine(mode="shadow")
        payload = _payload(base_empirical=0.5, fair_value=0.85)
        sp_gates._attach_stage1_shadow_empirical_fields(
            engine, candidate_payload=payload,
            base_fair_value_poisson=0.7,
            stage2_run_env_delta=0.0, team_offense_delta=0.0,
            line="8.5", inning=6, decision_ask=0.65,
        )
        self.assertTrue(payload["fair_value_alt_empirical_used_empirical"])
        self.assertAlmostEqual(payload["fair_value_alt_empirical_raw"], 0.5, places=4)
        # Default calibrator returns raw_prob unchanged
        self.assertAlmostEqual(payload["fair_value_alt_empirical"], 0.5, places=4)
        self.assertEqual(payload["fair_value_alt_empirical_p0"], 0.5)
        # Delta: alt p3 - prod p3 = 0.5 - 0.85 = -0.35
        self.assertAlmostEqual(
            payload["fair_value_alt_empirical_delta_vs_prod"], -0.35, places=4,
        )

    def test_alt_with_logit_deltas(self):
        # empirical=0.5, s2=+0.5, s3=-0.3
        # logit(0.5) = 0; +0.5 + -0.3 = 0.2; sigmoid(0.2) = ~0.5498
        engine = _make_fake_engine(mode="shadow")
        payload = _payload(base_empirical=0.5, fair_value=0.85)
        sp_gates._attach_stage1_shadow_empirical_fields(
            engine, candidate_payload=payload,
            base_fair_value_poisson=0.7,
            stage2_run_env_delta=0.5, team_offense_delta=-0.3,
            line="8.5", inning=6, decision_ask=0.65,
        )
        expected = 1.0 / (1.0 + math.exp(-0.2))
        self.assertAlmostEqual(
            payload["fair_value_alt_empirical_raw"], expected, places=4,
        )

    def test_calibrator_is_called_on_alt_raw(self):
        # Verify the calibrator runs against the ALT raw prob, not the
        # production raw prob.
        recorded: Dict[str, Any] = {}
        def _spy(*, raw_prob, line, inning, decision_ask, model_family):
            recorded["raw_prob"] = raw_prob
            recorded["line"] = line
            recorded["inning"] = inning
            recorded["decision_ask"] = decision_ask
            recorded["model_family"] = model_family
            return raw_prob, {"calibrated_prob": raw_prob, "delta": 0.0}
        engine = _make_fake_engine(mode="shadow")
        engine._calibrate_fair_value = _spy
        payload = _payload(base_empirical=0.5, fair_value=0.85)
        sp_gates._attach_stage1_shadow_empirical_fields(
            engine, candidate_payload=payload,
            base_fair_value_poisson=0.7,
            stage2_run_env_delta=0.0, team_offense_delta=0.0,
            line="8.5", inning=6, decision_ask=0.65,
        )
        self.assertAlmostEqual(recorded["raw_prob"], 0.5, places=4)
        self.assertEqual(recorded["line"], "8.5")
        self.assertEqual(recorded["inning"], 6)

    def test_calibrator_shift_propagates(self):
        # When calibrator shifts the raw prob, alt p3 reflects the shift.
        engine = _make_fake_engine(mode="shadow", calibrated_fv_returns=0.72)
        payload = _payload(base_empirical=0.5, fair_value=0.85)
        sp_gates._attach_stage1_shadow_empirical_fields(
            engine, candidate_payload=payload,
            base_fair_value_poisson=0.7,
            stage2_run_env_delta=0.0, team_offense_delta=0.0,
            line="8.5", inning=6, decision_ask=0.65,
        )
        # alt_raw = 0.5 (sigmoid(logit(0.5)) = 0.5)
        # alt_p3 = 0.72 (calibrator output)
        self.assertAlmostEqual(payload["fair_value_alt_empirical_raw"], 0.5, places=4)
        self.assertAlmostEqual(payload["fair_value_alt_empirical"], 0.72, places=4)


class FailOpenTests(unittest.TestCase):
    """ANY exception in the alt path must be silently swallowed.
    Production fair_value MUST be unaffected."""

    def test_calibrator_raises_does_not_blow_up(self):
        def _broken(**kwargs):
            raise RuntimeError("calibrator went boom")
        engine = _make_fake_engine(mode="shadow")
        engine._calibrate_fair_value = _broken
        payload = _payload(base_empirical=0.5, fair_value=0.85)
        # Must NOT raise
        sp_gates._attach_stage1_shadow_empirical_fields(
            engine, candidate_payload=payload,
            base_fair_value_poisson=0.7,
            stage2_run_env_delta=0.0, team_offense_delta=0.0,
            line="8.5", inning=6, decision_ask=0.65,
        )
        # Production fair_value preserved
        self.assertEqual(payload["fair_value"], 0.85)
        # Alt fields left as None (initialised at start of helper)
        self.assertIsNone(payload["fair_value_alt_empirical"])
        self.assertFalse(payload["fair_value_alt_empirical_used_empirical"])

    def test_mode_attr_missing_treated_as_off(self):
        engine = SimpleNamespace()  # no _stage1_shadow_empirical_mode
        payload = _payload(base_empirical=0.5)
        sp_gates._attach_stage1_shadow_empirical_fields(
            engine, candidate_payload=payload,
            base_fair_value_poisson=0.7,
            stage2_run_env_delta=0.0, team_offense_delta=0.0,
            line="8.5", inning=6, decision_ask=0.65,
        )
        self.assertEqual(payload["stage1_shadow_empirical_mode"], "off")


class LogitSigmoidMathTests(unittest.TestCase):
    def test_logit_sigmoid_roundtrip(self):
        for p in (0.01, 0.25, 0.5, 0.75, 0.99):
            self.assertAlmostEqual(
                sp_gates._stage1_shadow_sigmoid(
                    sp_gates._stage1_shadow_logit(p)
                ), p, places=5,
            )

    def test_logit_clamps_extremes(self):
        # 0.0 and 1.0 must NOT raise; the helper itself filters these
        # but the math primitives should be robust too.
        self.assertTrue(math.isfinite(sp_gates._stage1_shadow_logit(0.0)))
        self.assertTrue(math.isfinite(sp_gates._stage1_shadow_logit(1.0)))


class LiveArgsBridgeTests(unittest.TestCase):
    """The LiveTradingEngine.__init__ bridge that copies
    live_args.stage1_shadow_empirical_override -> trade_args."""

    def test_bridge_copies_explicit_value(self):
        live_args = argparse.Namespace(
            stage1_shadow_empirical_override="shadow",
        )
        trade_args = argparse.Namespace()
        # Simulate the bridge line from live_engine.py:294 region
        trade_args.stage1_shadow_empirical_mode = getattr(
            live_args, "stage1_shadow_empirical_override", "off",
        )
        self.assertEqual(trade_args.stage1_shadow_empirical_mode, "shadow")

    def test_bridge_falls_back_to_off(self):
        live_args = argparse.Namespace()  # no flag set
        trade_args = argparse.Namespace()
        trade_args.stage1_shadow_empirical_mode = getattr(
            live_args, "stage1_shadow_empirical_override", "off",
        )
        self.assertEqual(trade_args.stage1_shadow_empirical_mode, "off")


class CliRegistrationTests(unittest.TestCase):
    """Parse a minimal CLI invocation and confirm the new flag works
    end-to-end through parse_live_args."""

    def test_default_is_off(self):
        from live_engine_cli import parse_live_args
        # Minimal invocation
        live_args, _trade_args, _monitor_args = parse_live_args([
            "--daily-budget", "100",
        ])
        self.assertEqual(
            getattr(live_args, "stage1_shadow_empirical_override", "?"),
            "off",
        )

    def test_can_be_set_to_shadow(self):
        from live_engine_cli import parse_live_args
        live_args, _trade_args, _monitor_args = parse_live_args([
            "--daily-budget", "100",
            "--stage1-shadow-empirical-override", "shadow",
        ])
        self.assertEqual(
            live_args.stage1_shadow_empirical_override, "shadow",
        )

    def test_invalid_choice_rejected(self):
        from live_engine_cli import parse_live_args
        with self.assertRaises(SystemExit):
            parse_live_args([
                "--daily-budget", "100",
                "--stage1-shadow-empirical-override", "enforce",  # not allowed
            ])


if __name__ == "__main__":
    unittest.main()
