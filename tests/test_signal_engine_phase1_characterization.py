import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_DIR = Path(__file__).resolve().parents[1]
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(TRADING_DIR) not in sys.path:
    sys.path.insert(0, str(TRADING_DIR))
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_candidate_universe_table as candidate_table  # noqa: E402
import live_engine as le  # noqa: E402
import runtime_log_rollups as rlr  # noqa: E402
import shadow_post_tr20 as sp20  # noqa: E402
import signal_engine as se  # noqa: E402
import signal_pipeline as sp  # noqa: E402
import signal_pipeline_payload as spp  # noqa: E402


def _make_trade_args(**overrides):
    base = {
        "min_current_total_relax_enabled": True,
        "min_current_total_relax_inning": 4,
        "min_current_total_relax_floor": 3,
        "min_current_total_relax_ask_min": 0.60,
        "min_current_total_relax_max_lead": 4,
        "min_current_total_relax_max_runs_needed": 4.5,
        "blowout_relax_max_inning": 7,
        "blowout_relax_min_ask": 0.74,
        "blowout_relax_max_runs_needed": 2.5,
        "blowout_relax_near_line_runs_needed": 0.5,
        "blowout_relax_near_line_min_ask": 0.55,
        "gate_relax_ab_fraction": 0.5,
        "shadow_relaxed_enabled": True,
        "high_line_cutoff": 8.5,
        "min_inning": 4,
        "min_inning_high_line": 5,
        "shadow_relaxed_min_inning_offset": 1,
        "shadow_relaxed_min_inning_high_line_offset": 1,
        "min_entry_ask": 0.55,
        "min_entry_ask_high_line": 0.60,
        "max_spread": 0.20,
        "jump_threshold": 0.06,
        "confirmation_ticks": 3,
        "lookback_ticks": 5,
        "spread_factor": 0.65,
        "capture_depth": 5,
        "min_current_total": 4,
        "runs_needed_max": 3.5,
        "min_close_game_rn": 4.0,
        "inn5_rn_max": 2.5,
        "inn6_rn_max": 2.5,
        "blowout_lead_min": 6,
        "blowout_adj_lead_min": 4,
        "max_base_fv": 0.99,
        "s2_suppress_max": -0.20,
        "s2_suppress_min_inning": 6,
        "edge_threshold": 0.15,
        "edge_threshold_high_line": 0.16,
        "ask_edge_ramp_enabled": True,
        "ask_edge_ramp_start": 0.75,
        "ask_edge_ramp_end": 0.90,
        "ask_edge_ramp_max_boost": 0.05,
        "fv_ask_gap_max": 0.28,
        "fv_ask_gap_min_inning": 7,
        "sp_era_threshold": 3.75,
        "sp_era_max_inning": 6,
        "sp_era_edge_boost": 0.03,
        "event_dedup_secs": 60.0,
        "inning_dedup_gap": 2,
        "inning_dedup_edge_gap": 0.05,
        "shadow_no_score_drift_enabled": True,
        "shadow_no_score_drift_min_inning": 3,
        "shadow_no_score_drift_max_inning": 8,
        "shadow_no_score_drift_min_segment_age_secs": 180.0,
        "shadow_no_score_drift_min_segment_ticks": 20,
        "shadow_no_score_drift_min_drawdown": 0.06,
        "shadow_no_score_drift_min_ask": 0.35,
        "shadow_no_score_drift_max_ask": 0.85,
        "shadow_no_score_drift_max_spread": 0.12,
        "shadow_no_score_drift_min_po_edge": 0.10,
        "shadow_no_score_drift_min_emp_edge": 0.08,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_engine_stub(trade_args=None):
    engine = se.SignalEngine.__new__(se.SignalEngine)
    engine.trade_args = trade_args or _make_trade_args()
    engine.date_str = "2026-04-26"
    engine._line_states = {}
    engine._candidate_seq = 0
    engine._candidate_mode = "paper"
    engine._candidate_rows_written = 0
    engine._candidate_rows_dedup_suppressed = 0
    engine._candidate_null_fields_omitted = 0
    engine._candidate_skip_dedup_seen = set()
    engine._skip_features_seen = set()
    engine._skip_features_captured = 0
    engine._skip_features_dedup_suppressed = 0
    engine._skip_debug_logs_emitted = 0
    engine._skip_debug_logs_dedup_suppressed = 0
    engine._skip_debug_seen = set()
    engine._shadow_gate1_blocked = 0
    engine._shadow_gate1_would_pass = 0
    engine._shadow_gate3_blocked = 0
    engine._shadow_gate3_would_pass = 0
    engine._prob_calibration_mode = "off"
    # Stub-only: keep the scope feature off so legacy characterization
    # tests don't need to predict per-cohort decisions. Tests that
    # specifically exercise Scoped Alt-A (Active #17) set this to
    # "shadow" or "enforce" explicitly.
    engine._stage1_alt_a_scope_mode = "off"
    engine._prob_calibrator = None
    # Stub default 0.0 = no band gate. Tests that need to characterize
    # band-gated enforce override this attribute explicitly.
    engine._prob_calibration_enforce_min_raw = 0.0
    engine._prob_calibration_stats = {
        "scored": 0,
        "applied": 0,
        "shadow_scored": 0,
        "below_min_raw_kept_raw": 0,
        "disabled_or_missing": 0,
    }
    return engine


def _read_jsonl(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _stable_candidate_row(row):
    dynamic = {"candidate_id", "ts", "recorded_at", "signal_ts_epoch"}
    return {k: v for k, v in row.items() if k not in dynamic}


def _game_market(game_pk=1, line="8.5"):
    game = SimpleNamespace(
        game_pk=game_pk,
        away_abbrev="NYY",
        home_abbrev="BOS",
        game_date="2026-04-26",
        venue_name="Fenway Park",
        current_pitcher_era=4.20,
    )
    market = SimpleNamespace(line=line, over_token_id="token_over")
    return game, market


def _payload(**overrides):
    base = {
        "inning": 6,
        "inning_state": "Top",
        "away_score": 3,
        "home_score": 2,
        "outs": 1,
        "runners_on": 1,
    }
    base.update(overrides)
    return base


def _state_with_baseline(game_pk=1, line="8.5", baseline=0.60):
    state = se.LineState(game_pk=game_pk, line=line)
    state.baseline_ask = baseline
    state.baseline_candidate = baseline
    state.stable_count = 5
    return state


class _FakeCalibrator:
    method = "fake"

    def calibrate(self, p: float) -> float:
        return max(min(float(p) - 0.10, 0.99999999), 1e-8)


class _ConstantCache:
    def __init__(self, fv: float) -> None:
        self.fv = fv

    def lookup(self, **kwargs) -> float:
        return self.fv


class _IdentityOffenseModel:
    mlb_avg_total = 9.0

    def adjust_fv(self, **kwargs) -> float:
        return float(kwargs["base_fv"])

    def get_matchup_delta(self, *args) -> float:
        return 0.0

    def get_rpg(self, *args) -> float:
        return 4.5

    def get_expected_total(self, *args) -> float:
        return 9.0


class SignalEngineGateCharacterizationTests(unittest.TestCase):
    def test_ask_edge_boost_bounds_and_monotonic(self) -> None:
        start = 0.75
        end = 0.90
        max_boost = 0.05
        asks = [0.65, 0.75, 0.80, 0.85, 0.90, 0.95]
        boosts = [
            se._ask_edge_boost(ask=a, start=start, end=end, max_boost=max_boost)
            for a in asks
        ]

        self.assertAlmostEqual(boosts[0], 0.0, places=8)
        self.assertAlmostEqual(boosts[1], 0.0, places=8)
        self.assertAlmostEqual(boosts[-1], max_boost, places=8)
        self.assertAlmostEqual(boosts[-2], max_boost, places=8)
        self.assertTrue(all(b2 >= b1 for b1, b2 in zip(boosts, boosts[1:])))

    def test_resolve_relax_mode_action_contract(self) -> None:
        engine = _make_engine_stub(_make_trade_args(gate_relax_ab_fraction=1.0))

        apply_relax, arm = engine._resolve_relax_mode_action(
            mode="shadow",
            gate_name="gate_blowout",
            game_pk=1,
            line="8.5",
            relax_pass=False,
        )
        self.assertFalse(apply_relax)
        self.assertEqual(arm, "strict_block")

        apply_relax, arm = engine._resolve_relax_mode_action(
            mode="shadow",
            gate_name="gate_blowout",
            game_pk=1,
            line="8.5",
            relax_pass=True,
        )
        self.assertFalse(apply_relax)
        self.assertEqual(arm, "shadow")

        apply_relax, arm = engine._resolve_relax_mode_action(
            mode="enforce",
            gate_name="gate_blowout",
            game_pk=1,
            line="8.5",
            relax_pass=True,
        )
        self.assertTrue(apply_relax)
        self.assertEqual(arm, "enforce")

        apply_relax, arm = engine._resolve_relax_mode_action(
            mode="ab",
            gate_name="gate_blowout",
            game_pk=1,
            line="8.5",
            relax_pass=True,
        )
        self.assertTrue(apply_relax)
        self.assertEqual(arm, "ab_treatment")

        engine.trade_args = _make_trade_args(gate_relax_ab_fraction=0.0)
        apply_relax, arm = engine._resolve_relax_mode_action(
            mode="ab",
            gate_name="gate_blowout",
            game_pk=1,
            line="8.5",
            relax_pass=True,
        )
        self.assertFalse(apply_relax)
        self.assertEqual(arm, "ab_control")

    def test_min_current_total_relax_pass_characterization(self) -> None:
        engine = _make_engine_stub()
        self.assertTrue(
            engine._min_current_total_relax_pass(
                inning=4,
                current_total=3,
                ask=0.62,
                lead_abs=2,
                runs_needed=3.0,
            )
        )
        self.assertFalse(
            engine._min_current_total_relax_pass(
                inning=5,  # wrong inning for this relax rule
                current_total=3,
                ask=0.62,
                lead_abs=2,
                runs_needed=3.0,
            )
        )
        self.assertFalse(
            engine._min_current_total_relax_pass(
                inning=4,
                current_total=2,  # below relax floor
                ask=0.62,
                lead_abs=2,
                runs_needed=3.0,
            )
        )
        self.assertFalse(
            engine._min_current_total_relax_pass(
                inning=4,
                current_total=3,
                ask=0.59,  # below ask floor
                lead_abs=2,
                runs_needed=3.0,
            )
        )

    def test_post_tr20_shadow_fields_are_observability_only(self) -> None:
        trade_args = _make_trade_args()
        likely_pass = {
            "line": "8.5",
            "inning": 4,
            "decision_ask": 0.68,
            "current_total": 3,
            "away_score_before": 2,
            "home_score_before": 1,
            "runs_needed": 3.5,
            "edge": 0.19,
            "min_edge_base": 0.16,
        }
        sp20.attach_post_tr20_shadow_fields(likely_pass, trade_args)

        self.assertTrue(likely_pass["shadow_post_tr20_extreme_020_pass"])
        self.assertTrue(likely_pass["shadow_post_tr20_ask_ramp_v2_pass"])
        self.assertTrue(likely_pass["shadow_post_tr20_gate6_relax_enforce_pass"])
        self.assertTrue(likely_pass["shadow_post_tr20_combined_pass"])

        high_price_fail = {
            "line": "8.5",
            "inning": 6,
            "decision_ask": 0.82,
            "current_total": 7,
            "away_score_before": 4,
            "home_score_before": 3,
            "runs_needed": 1.5,
            "edge": 0.21,
            "min_edge_base": 0.16,
        }
        sp20.attach_post_tr20_shadow_fields(high_price_fail, trade_args)

        self.assertFalse(high_price_fail["shadow_post_tr20_extreme_020_pass"])
        self.assertFalse(high_price_fail["shadow_post_tr20_ask_ramp_v2_pass"])
        self.assertTrue(high_price_fail["shadow_post_tr20_gate6_relax_enforce_pass"])
        self.assertFalse(high_price_fail["shadow_post_tr20_combined_pass"])

    def test_blowout_relax_pass_characterization(self) -> None:
        engine = _make_engine_stub()
        self.assertTrue(
            engine._blowout_relax_pass(
                inning=7,
                ask=0.76,
                runs_needed=2.0,
            )
        )
        self.assertFalse(
            engine._blowout_relax_pass(
                inning=8,  # above max inning
                ask=0.76,
                runs_needed=2.0,
            )
        )
        self.assertFalse(
            engine._blowout_relax_pass(
                inning=7,
                ask=0.73,  # below ask threshold
                runs_needed=2.0,
            )
        )
        self.assertFalse(
            engine._blowout_relax_pass(
                inning=7,
                ask=0.76,
                runs_needed=2.6,  # above runs-needed threshold
            )
        )
        self.assertTrue(
            engine._blowout_relax_pass(
                inning=9,
                ask=0.56,
                runs_needed=0.5,  # near-line escape still passes late
            )
        )
        self.assertFalse(
            engine._blowout_relax_pass(
                inning=9,
                ask=0.54,  # below near-line ask floor
                runs_needed=0.5,
            )
        )

    def test_probability_calibration_modes_characterization(self) -> None:
        engine = _make_engine_stub()
        engine._prob_calibrator = _FakeCalibrator()

        engine._prob_calibration_mode = "off"
        final_prob, diag = engine._calibrate_fair_value(raw_prob=0.80)
        self.assertAlmostEqual(final_prob, 0.80, places=8)
        self.assertEqual(diag["mode"], "off")
        self.assertFalse(diag["applied"])
        self.assertEqual(engine._prob_calibration_stats["disabled_or_missing"], 1)

        engine._prob_calibration_mode = "shadow"
        final_prob, diag = engine._calibrate_fair_value(raw_prob=0.80)
        self.assertAlmostEqual(final_prob, 0.80, places=8)
        self.assertAlmostEqual(diag["calibrated_prob"], 0.70, places=8)
        self.assertFalse(diag["applied"])
        self.assertEqual(engine._prob_calibration_stats["scored"], 1)
        self.assertEqual(engine._prob_calibration_stats["shadow_scored"], 1)

        engine._prob_calibration_mode = "enforce"
        final_prob, diag = engine._calibrate_fair_value(raw_prob=0.80)
        self.assertAlmostEqual(final_prob, 0.70, places=8)
        self.assertTrue(diag["applied"])
        self.assertEqual(engine._prob_calibration_stats["scored"], 2)
        self.assertEqual(engine._prob_calibration_stats["applied"], 1)

    def test_probability_calibration_band_gated_enforce(self) -> None:
        """Band-gated enforce (2026-05-19 audit fix): under enforce, only
        overwrite raw FV when raw >= threshold. Below threshold the
        calibrator is still scored for observability but raw is kept.
        Default threshold 0.90 captures the [0.95,1.00) +28pp
        overconfidence band while leaving the mid-band alone where the
        Platt calibrator otherwise over-pulls.
        """
        engine = _make_engine_stub()
        engine._prob_calibrator = _FakeCalibrator()
        engine._prob_calibration_mode = "enforce"
        engine._prob_calibration_enforce_min_raw = 0.90

        # raw=0.80 < 0.90 threshold: raw kept, calibrator scored.
        final_prob, diag = engine._calibrate_fair_value(raw_prob=0.80)
        self.assertAlmostEqual(final_prob, 0.80, places=8)
        self.assertAlmostEqual(diag["calibrated_prob"], 0.70, places=8)
        self.assertFalse(diag["applied"])
        self.assertTrue(diag["below_min_raw_kept_raw"])
        self.assertAlmostEqual(diag["enforce_min_raw_threshold"], 0.90, places=8)
        self.assertEqual(engine._prob_calibration_stats["scored"], 1)
        self.assertEqual(engine._prob_calibration_stats["applied"], 0)
        self.assertEqual(engine._prob_calibration_stats["below_min_raw_kept_raw"], 1)

        # raw=0.95 >= 0.90: enforce applies normally.
        final_prob, diag = engine._calibrate_fair_value(raw_prob=0.95)
        self.assertAlmostEqual(final_prob, 0.85, places=8)
        self.assertTrue(diag["applied"])
        self.assertFalse(diag["below_min_raw_kept_raw"])
        self.assertEqual(engine._prob_calibration_stats["applied"], 1)
        self.assertEqual(engine._prob_calibration_stats["below_min_raw_kept_raw"], 1)

    def test_probability_calibration_missing_family_shadow_warns_enforce_fails_closed(self) -> None:
        payload = {
            "schema_version": 2,
            "family_mode": "separate",
            "default_family": "score_event_transition",
            "families": {
                "score_event_transition": {
                    "selected_method": "platt",
                    "methods": {"platt": {"params": {"a": 1.0, "b": 0.0}}},
                },
            },
        }
        engine = _make_engine_stub()
        engine._prob_calibrator = se.ProbabilityCalibrator.from_payload(payload)

        engine._prob_calibration_mode = "shadow"
        final_prob, diag = engine._calibrate_fair_value(
            raw_prob=0.80,
            model_family="no_score_drift",
        )
        self.assertAlmostEqual(final_prob, 0.80, places=8)
        self.assertTrue(diag["family_missing"])
        self.assertFalse(diag["family_fallback_used"])
        self.assertEqual(diag["method"], "missing_family_identity")
        self.assertAlmostEqual(diag["calibrated_prob"], 0.80, places=8)
        self.assertEqual(engine._prob_calibration_stats["family_missing"], 1)

        engine._prob_calibration_mode = "enforce"
        final_prob, diag = engine._calibrate_fair_value(
            raw_prob=0.80,
            model_family="no_score_drift",
        )
        self.assertAlmostEqual(final_prob, 1e-8, places=12)
        self.assertTrue(diag["family_missing"])
        self.assertTrue(diag["fail_closed"])
        self.assertEqual(diag["method"], "missing_family_fail_closed")
        self.assertEqual(engine._prob_calibration_stats["family_missing"], 2)
        self.assertEqual(engine._prob_calibration_stats["family_missing_fail_closed"], 1)


class SignalEngineCandidateLoggingCharacterizationTests(unittest.TestCase):
    def test_repeated_early_skip_raw_sampling_writes_single_row(self) -> None:
        engine = _make_engine_stub()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "candidates.jsonl"
            engine._candidate_log_path = lambda: path

            payload = {
                "decision": "skip",
                "decision_reason": "gate_min_edge",
                "game_pk": 1,
                "line": "8.5",
                "inning": 6,
                "inning_state": "Top",
                "outs": 1,
                "current_total": 7,
                "away_score_before": 4,
                "home_score_before": 3,
            }
            engine._record_candidate_decision(payload)
            engine._record_candidate_decision(payload)

            rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(engine._candidate_rows_written, 1)
            self.assertEqual(engine._candidate_rows_dedup_suppressed, 0)
            self.assertEqual(engine._candidate_raw_sample_suppressed, 1)
            self.assertEqual(rows[0]["session_date"], "2026-04-26")
            self.assertEqual(rows[0]["mode"], "paper")
            self.assertEqual(rows[0]["side"], "over")
            self.assertEqual(rows[0]["schema_version"], 1)
            self.assertIn("recorded_at", rows[0])

    def test_candidate_jsonl_omits_null_fields_only_on_disk(self) -> None:
        engine = _make_engine_stub()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "candidates.jsonl"
            engine._candidate_log_path = lambda: path

            engine._record_candidate_decision(
                {
                    "decision": "skip",
                    "decision_reason": "gate_min_inning",
                    "game_pk": 1,
                    "line": "8.5",
                    "inning": 3,
                    "fair_value": None,
                    "shadow_relaxed_would_pass": False,
                    "nested": {"keep": 1, "drop": None},
                }
            )

            rows = _read_jsonl(path)
            self.assertEqual(len(rows), 1)
            self.assertNotIn("fair_value", rows[0])
            self.assertEqual(rows[0]["shadow_relaxed_would_pass"], False)
            self.assertEqual(rows[0]["nested"], {"keep": 1})
            self.assertGreaterEqual(engine._candidate_null_fields_omitted, 2)

    def test_skip_dedup_preserves_diagnostic_variation(self) -> None:
        engine = _make_engine_stub()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "candidates.jsonl"
            engine._candidate_log_path = lambda: path

            base_payload = {
                "decision": "skip",
                "decision_reason": "gate_min_edge",
                "game_pk": 1,
                "line": "8.5",
                "inning": 6,
                "inning_state": "Top",
                "outs": 1,
                "current_total": 7,
                "away_score_before": 4,
                "home_score_before": 3,
                "runners_on": 0,
                "decision_ask": 0.61,
                "fair_value": 0.73,
                "edge": 0.12,
                "shadow_relaxed_reason": "gate_min_edge",
                "shadow_relaxed_would_pass": False,
                "shadow_relaxed_value": 0.12,
                "shadow_relaxed_threshold": 0.15,
            }
            changed_payload = dict(base_payload)
            changed_payload.update(
                {
                    "runners_on": 2,
                    "decision_ask": 0.66,
                    "edge": 0.07,
                    "shadow_relaxed_would_pass": True,
                }
            )

            engine._record_candidate_decision(base_payload)
            engine._record_candidate_decision(changed_payload)

            rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(engine._candidate_rows_written, 2)
            self.assertEqual(engine._candidate_rows_dedup_suppressed, 0)

    def test_non_skip_rows_are_not_deduped(self) -> None:
        engine = _make_engine_stub()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "candidates.jsonl"
            engine._candidate_log_path = lambda: path

            payload = {
                "decision": "place",
                "decision_reason": "edge_ok",
                "game_pk": 2,
                "line": "9.5",
                "inning": 7,
            }
            engine._record_candidate_decision(payload)
            engine._record_candidate_decision(payload)

            rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(engine._candidate_rows_written, 2)
            self.assertEqual(engine._candidate_rows_dedup_suppressed, 0)

    def test_early_gate_min_inning_writes_candidate_skip(self) -> None:
        engine = _make_engine_stub()
        game, market = _game_market(line="8.5")
        engine._line_states[(game.game_pk, market.line)] = _state_with_baseline(
            game_pk=game.game_pk,
            line=market.line,
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "candidates.jsonl"
            engine._candidate_log_path = lambda: path

            se.SignalEngine._process_tick(
                engine,
                game=game,
                market=market,
                payload=_payload(inning=4),
                book={"best_bid": 0.56},
                ask=0.62,
            )

            rows = _read_jsonl(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["decision"], "skip")
            self.assertEqual(rows[0]["decision_reason"], "gate_min_inning")
            self.assertEqual(rows[0]["min_inning_effective"], 5)
            self.assertTrue(rows[0]["shadow_relaxed_evaluated"])

    def test_early_gate_min_inning_golden_row(self) -> None:
        engine = _make_engine_stub()
        game, market = _game_market(line="8.5")
        engine._line_states[(game.game_pk, market.line)] = _state_with_baseline(
            game_pk=game.game_pk,
            line=market.line,
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "candidates.jsonl"
            engine._candidate_log_path = lambda: path

            se.SignalEngine._process_tick(
                engine,
                game=game,
                market=market,
                payload=_payload(inning=4),
                book={"best_bid": 0.56},
                ask=0.62,
            )

            row = _stable_candidate_row(_read_jsonl(path)[0])
            self.assertEqual(
                row,
                {
                    "game_pk": 1,
                    "away_abbrev": "NYY",
                    "home_abbrev": "BOS",
                    "line": "8.5",
                    "signal_model_family": "score_event_transition",
                    "inning": 4,
                    "inning_state": "Top",
                    "outs": 1,
                    "runners_on": 1,
                    "away_score_before": 3,
                    "home_score_before": 2,
                    "current_total": 5,
                    "home_leading_late": False,
                    "batting_team_is_home": False,
                    "bottom9_available_if_needed": True,
                    "expected_remaining_half_innings": 12.0,
                    "expected_remaining_pa_bucket": "19+",
                    "home_skip_bottom9_risk": 0.0,
                    "decision_ask": 0.62,
                    "best_bid": 0.56,
                    "spread": 0.62 - 0.56,
                    "decision": "skip",
                    "decision_reason": "gate_min_inning",
                    "fair_value_calibration_mode": "off",
                    "shadow_relaxed_evaluated": True,
                    "shadow_relaxed_would_pass": True,
                    "shadow_relaxed_reason": "gate_min_inning",
                    "shadow_relaxed_value": 4,
                    "shadow_relaxed_threshold": 4,
                    "shadow_relaxed_comparator": ">=",
                    "shadow_relaxed_secondary_evaluated": False,
                    "min_inning_effective": 5,
                    "high_line_cutoff": 8.5,
                    "schema_version": 1,
                    "session_date": "2026-04-26",
                    "mode": "paper",
                    "side": "over",
                },
            )

    def test_early_gate_min_entry_ask_writes_candidate_skip(self) -> None:
        engine = _make_engine_stub()
        game, market = _game_market(line="7.5")
        engine._line_states[(game.game_pk, market.line)] = _state_with_baseline(
            game_pk=game.game_pk,
            line=market.line,
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "candidates.jsonl"
            engine._candidate_log_path = lambda: path

            se.SignalEngine._process_tick(
                engine,
                game=game,
                market=market,
                payload=_payload(inning=4),
                book={"best_bid": 0.50},
                ask=0.53,
            )

            rows = _read_jsonl(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["decision_reason"], "gate_min_entry_ask")
            self.assertAlmostEqual(rows[0]["min_entry_ask_effective"], 0.55, places=8)
            self.assertTrue(rows[0]["shadow_relaxed_would_pass"])

    def test_early_gate_ask_jump_pending_writes_candidate_skip(self) -> None:
        engine = _make_engine_stub(
            _make_trade_args(
                jump_threshold=0.06,
                confirmation_ticks=3,
                lookback_ticks=5,
            )
        )
        game, market = _game_market(line="7.5")
        state = _state_with_baseline(game_pk=game.game_pk, line=market.line, baseline=0.60)
        for _ in range(5):
            state.ask_history.append(0.60)
        engine._line_states[(game.game_pk, market.line)] = state
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "candidates.jsonl"
            engine._candidate_log_path = lambda: path

            se.SignalEngine._process_tick(
                engine,
                game=game,
                market=market,
                payload=_payload(inning=4),
                book={"best_bid": 0.62},
                ask=0.67,
            )

            rows = _read_jsonl(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["decision_reason"], "gate_ask_jump_unconfirmed")
            self.assertAlmostEqual(rows[0]["ask_jump"], 0.07, places=8)
            self.assertAlmostEqual(rows[0]["baseline_ask"], 0.60, places=8)
            self.assertEqual(rows[0]["pending_ticks_remaining"], 2)
            self.assertEqual(rows[0]["confirmation_status"], "pending")

    def test_early_gate_ask_jump_pending_golden_row(self) -> None:
        engine = _make_engine_stub(
            _make_trade_args(
                jump_threshold=0.06,
                confirmation_ticks=3,
                lookback_ticks=5,
            )
        )
        game, market = _game_market(line="7.5")
        state = _state_with_baseline(game_pk=game.game_pk, line=market.line, baseline=0.60)
        for _ in range(5):
            state.ask_history.append(0.60)
        engine._line_states[(game.game_pk, market.line)] = state
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "candidates.jsonl"
            engine._candidate_log_path = lambda: path

            se.SignalEngine._process_tick(
                engine,
                game=game,
                market=market,
                payload=_payload(inning=4),
                book={"best_bid": 0.62},
                ask=0.67,
            )

            row = _stable_candidate_row(_read_jsonl(path)[0])
            self.assertEqual(
                row,
                {
                    "game_pk": 1,
                    "away_abbrev": "NYY",
                    "home_abbrev": "BOS",
                    "line": "7.5",
                    "signal_model_family": "score_event_transition",
                    "inning": 4,
                    "inning_state": "Top",
                    "outs": 1,
                    "runners_on": 1,
                    "away_score_before": 3,
                    "home_score_before": 2,
                    "current_total": 5,
                    "home_leading_late": False,
                    "batting_team_is_home": False,
                    "bottom9_available_if_needed": True,
                    "expected_remaining_half_innings": 12.0,
                    "expected_remaining_pa_bucket": "19+",
                    "home_skip_bottom9_risk": 0.0,
                    "decision_ask": 0.67,
                    "best_bid": 0.62,
                    "spread": 0.67 - 0.62,
                    "decision": "skip",
                    "decision_reason": "gate_ask_jump_unconfirmed",
                    "fair_value_calibration_mode": "off",
                    "shadow_relaxed_evaluated": False,
                    "shadow_relaxed_secondary_evaluated": False,
                    "ask_jump": 0.67 - 0.60,
                    "jump_threshold_effective": 0.06,
                    "lookback_ticks": 5,
                    "baseline_ask": 0.60,
                    "pending_jump_ask": 0.67,
                    "pending_ticks_remaining": 2,
                    "confirmation_ticks": 3,
                    "confirmation_status": "pending",
                    "schema_version": 1,
                    "session_date": "2026-04-26",
                    "mode": "paper",
                    "side": "over",
                },
            )

    def test_candidate_table_exports_early_gate_audit_columns(self) -> None:
        expected_columns = {
            "min_inning_effective",
            "min_entry_ask_effective",
            "ask_jump",
            "baseline_ask",
            "confirmation_status",
            "conditional_relax_gate",
            "signal_model_family",
            "fair_value_calibration_mode",
            "fair_value_calibration_family",
            "min_edge_effective",
            "home_leading_late",
            "expected_remaining_half_innings",
            "home_skip_bottom9_risk",
            "current_state_value_fv_raw",
            "shadow_fv_current_state",
            "shadow_phantom_risk_score",
            "state_value_strategy",
            "score_segment_drawdown",
            "shadow_no_score_drift_trigger",
            "shadow_post_tr20_extreme_020_pass",
            "shadow_post_tr20_ask_ramp_v2_pass",
            "shadow_post_tr20_gate6_relax_enforce_pass",
            "shadow_post_tr20_combined_pass",
            "stadium_weather_exposure",
            "weather_air_density_index",
            "weather_wind_out_component_mph",
        }
        self.assertTrue(expected_columns.issubset(set(candidate_table.OUTPUT_COLUMNS)))

    def test_base_candidate_payload_attaches_weather_cache_fields(self) -> None:
        engine = _make_engine_stub()
        engine._weather_fields_for_game = lambda game_pk: {
            "weather_cache_available": True,
            "stadium_id": "fenway_park",
            "weather_air_density_index": 0.99,
            "weather_wind_out_component_mph": -4.2,
        }
        game, market = _game_market(game_pk=42, line="8.5")
        ctx = sp.TickContext(
            game=game,
            market=market,
            state=_state_with_baseline(game_pk=42, line="8.5"),
            now=0.0,
            inning=4,
            inning_state="Top",
            away_score=1,
            home_score=1,
            outs=1,
            runners_on=1,
            current_total=2,
            line_val=8.5,
            best_bid=0.58,
            ask=0.61,
            book={"best_bid": 0.58},
        )

        row = spp.build_base_candidate_payload(engine, ctx)

        self.assertTrue(row["weather_cache_available"])
        self.assertEqual(row["stadium_id"], "fenway_park")
        self.assertAlmostEqual(row["weather_air_density_index"], 0.99)
        self.assertAlmostEqual(row["weather_wind_out_component_mph"], -4.2)

    def test_candidate_table_labels_no_score_drift_as_shadow_non_trade(self) -> None:
        rows = candidate_table._enrich_labels(
            [
                {
                    "candidate_id": "c1",
                    "decision": "shadow_no_score_drift",
                    "game_pk": 1,
                    "line": "7.5",
                }
            ],
            final_totals={1: 8},
        )

        self.assertEqual(rows[0]["target_trade"], 0)
        self.assertTrue(rows[0]["label_available"])
        self.assertEqual(rows[0]["target_counterfactual_win"], 1)

    def test_no_score_drift_shadow_candidate_logs_current_state_value(self) -> None:
        engine = _make_engine_stub(
            _make_trade_args(
                shadow_no_score_drift_min_segment_age_secs=1.0,
                shadow_no_score_drift_min_segment_ticks=5,
                shadow_no_score_drift_min_drawdown=0.06,
                shadow_no_score_drift_min_po_edge=0.10,
            )
        )
        engine.cache = _ConstantCache(0.62)
        engine.stage2_model = None
        engine.offense_model = _IdentityOffenseModel()
        game, market = _game_market(line="7.5")
        state = _state_with_baseline(game_pk=game.game_pk, line=market.line, baseline=0.55)
        state.score_segment_key = (2, 2)
        state.score_segment_started_at = 0.0
        state.score_segment_high_ask = 0.62
        state.score_segment_ticks = 25
        engine._line_states[(game.game_pk, market.line)] = state

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "candidates.jsonl"
            engine._candidate_log_path = lambda: path

            se.SignalEngine._process_tick(
                engine,
                game=game,
                market=market,
                payload=_payload(inning=4, away_score=2, home_score=2, outs=1),
                book={"best_bid": 0.47, "ltp": 0.50},
                ask=0.50,
            )

            rows = _read_jsonl(path)
            drift_rows = [r for r in rows if r.get("decision") == "shadow_no_score_drift"]
            self.assertEqual(len(drift_rows), 1)
            row = drift_rows[0]
            self.assertEqual(row["decision_reason"], "state_value_no_score_drift")
            self.assertEqual(row["state_value_strategy"], "no_score_drift")
            self.assertAlmostEqual(row["fair_value"], 0.62, places=8)
            self.assertAlmostEqual(row["base_fair_value"], 0.62, places=8)
            self.assertAlmostEqual(row["edge"], 0.12, places=8)
            self.assertAlmostEqual(row["current_state_value_fv_raw"], 0.62, places=8)
            self.assertAlmostEqual(row["current_state_value_edge"], 0.12, places=8)
            self.assertEqual(row["current_state_value_total"], 4)
            self.assertEqual(row["score_segment_key"], "2-2")
            self.assertEqual(row["score_segment_ticks"], 26)
            self.assertAlmostEqual(row["score_segment_high_ask"], 0.62, places=8)
            self.assertAlmostEqual(row["score_segment_drawdown"], 0.12, places=8)
            self.assertEqual(row["shadow_no_score_drift_trigger"], "po_edge")
            self.assertTrue(state.score_segment_shadow_logged)

    def test_no_score_drift_shadow_candidate_writes_once_per_score_segment(self) -> None:
        engine = _make_engine_stub(
            _make_trade_args(
                shadow_no_score_drift_min_segment_age_secs=1.0,
                shadow_no_score_drift_min_segment_ticks=5,
                shadow_no_score_drift_min_drawdown=0.06,
                shadow_no_score_drift_min_po_edge=0.10,
            )
        )
        engine.cache = _ConstantCache(0.62)
        engine.stage2_model = None
        engine.offense_model = _IdentityOffenseModel()
        game, market = _game_market(line="7.5")
        state = _state_with_baseline(game_pk=game.game_pk, line=market.line, baseline=0.55)
        state.score_segment_key = (2, 2)
        state.score_segment_started_at = 0.0
        state.score_segment_high_ask = 0.62
        state.score_segment_ticks = 25
        engine._line_states[(game.game_pk, market.line)] = state

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "candidates.jsonl"
            engine._candidate_log_path = lambda: path

            for _ in range(2):
                se.SignalEngine._process_tick(
                    engine,
                    game=game,
                    market=market,
                    payload=_payload(inning=4, away_score=2, home_score=2, outs=1),
                    book={"best_bid": 0.47, "ltp": 0.50},
                    ask=0.50,
                )

            rows = _read_jsonl(path)
            drift_rows = [r for r in rows if r.get("decision") == "shadow_no_score_drift"]
            self.assertEqual(len(drift_rows), 1)

    def test_skip_with_features_late_gate_uses_bound_signal_time(self) -> None:
        engine = _make_engine_stub(
            _make_trade_args(
                confirmation_ticks=1,
                shadow_relaxed_enabled=False,
            )
        )
        engine._start_tape_capture = lambda **kwargs: {
            "family": "a",
            "signal_ts": kwargs["signal_ts"],
        }
        engine._start_family_b_capture = lambda **kwargs: {
            "family": "b",
            "limit_price": kwargs["limit_price"],
        }
        engine._start_family_c_capture = lambda **kwargs: {
            "family": "c",
            "signal_ts": kwargs["signal_ts"],
        }
        game, market = _game_market(line="8.5")
        state = _state_with_baseline(game_pk=game.game_pk, line=market.line, baseline=0.60)
        for _ in range(5):
            state.ask_history.append(0.60)
        engine._line_states[(game.game_pk, market.line)] = state

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "candidates.jsonl"
            engine._candidate_log_path = lambda: path

            se.SignalEngine._process_tick(
                engine,
                game=game,
                market=market,
                payload=_payload(inning=6, away_score=2, home_score=2, outs=1),
                book={"best_bid": 0.60, "ltp": 0.61},
                ask=0.67,
            )

            rows = _read_jsonl(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["decision"], "skip_with_features")
            self.assertEqual(rows[0]["decision_reason"], "gate_runs_pace")
            self.assertEqual(engine._skip_features_captured, 1)
            self.assertAlmostEqual(rows[0]["hypothetical_limit_price"], 0.6455, places=8)
            self.assertIsInstance(rows[0]["family_a_features"]["signal_ts"], float)
            self.assertEqual(rows[0]["family_b_features"]["limit_price"], 0.6455)
            self.assertEqual(rows[0]["family_c_features"]["family"], "c")
            self.assertIn("family_d_features", rows[0])
            self.assertIn("family_e_features", rows[0])

    def test_pre_inference_gate_runs_pace_golden_row(self) -> None:
        engine = _make_engine_stub(
            _make_trade_args(
                confirmation_ticks=1,
                shadow_relaxed_enabled=False,
            )
        )
        engine._start_tape_capture = lambda **kwargs: {"fetch_ok": True}
        engine._start_family_b_capture = lambda **kwargs: {
            "book_ok": True,
            "limit_price_used": kwargs["limit_price"],
        }
        engine._start_family_c_capture = lambda **kwargs: {"buffer_n": 0}
        game, market = _game_market(line="8.5")
        state = _state_with_baseline(game_pk=game.game_pk, line=market.line, baseline=0.60)
        for _ in range(5):
            state.ask_history.append(0.60)
        engine._line_states[(game.game_pk, market.line)] = state

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "candidates.jsonl"
            engine._candidate_log_path = lambda: path

            se.SignalEngine._process_tick(
                engine,
                game=game,
                market=market,
                payload=_payload(inning=6, away_score=2, home_score=2, outs=1),
                book={"best_bid": 0.60, "ltp": 0.61},
                ask=0.67,
            )

            row = _stable_candidate_row(_read_jsonl(path)[0])
            self.assertEqual(
                row,
                {
                    "game_pk": 1,
                    "away_abbrev": "NYY",
                    "home_abbrev": "BOS",
                    "line": "8.5",
                    "signal_model_family": "score_event_transition",
                    "inning": 6,
                    "inning_state": "Top",
                    "outs": 1,
                    "runners_on": 1,
                    "away_score_before": 2,
                    "home_score_before": 2,
                    "current_total": 4,
                    "home_leading_late": False,
                    "batting_team_is_home": False,
                    "bottom9_available_if_needed": True,
                    "expected_remaining_half_innings": 8.0,
                    "expected_remaining_pa_bucket": "19+",
                    "home_skip_bottom9_risk": 0.0,
                    "decision_ask": 0.67,
                    "best_bid": 0.60,
                    "spread": 0.67 - 0.60,
                    "decision": "skip_with_features",
                    "decision_reason": "gate_runs_pace",
                    "fair_value_calibration_mode": "off",
                    "shadow_relaxed_evaluated": False,
                    "shadow_relaxed_reason": "gate_runs_pace",
                    "shadow_relaxed_secondary_evaluated": False,
                    "hypothetical_limit_price": 0.6455,
                    "skip_features_capture_id": "skip_2026-04-26_1_8.5_000001",
                    "family_a_features": {"fetch_ok": True},
                    "family_b_features": {
                        "book_ok": True,
                        "limit_price_used": 0.6455,
                    },
                    "family_c_features": {"buffer_n": 0},
                    "family_d_features": {
                        "limit_minus_bid": 0.0455,
                        "limit_minus_ask": -0.0245,
                        "spread_normalized_limit": 0.65,
                        "ltp_minus_limit": -0.0355,
                        "limit_price_used": 0.6455,
                    },
                    "family_e_features": {
                        "inning": 6,
                        "runners_on": 1,
                        "runner_on_first": True,
                        "runner_on_second": False,
                        "runner_on_third": False,
                        "runners_count": 1,
                    },
                    "schema_version": 1,
                    "session_date": "2026-04-26",
                    "mode": "paper",
                    "side": "over",
                },
            )

    def test_post_fv_gate_min_edge_golden_row(self) -> None:
        engine = _make_engine_stub(
            _make_trade_args(
                confirmation_ticks=1,
                shadow_relaxed_enabled=False,
            )
        )
        engine.cache = _ConstantCache(0.75)
        engine.stage2_model = None
        engine.offense_model = _IdentityOffenseModel()
        engine._start_tape_capture = lambda **kwargs: {"fetch_ok": True}
        engine._start_family_b_capture = lambda **kwargs: {
            "book_ok": True,
            "limit_price_used": kwargs["limit_price"],
        }
        engine._start_family_c_capture = lambda **kwargs: {"buffer_n": 0}
        game, market = _game_market(line="8.5")
        state = _state_with_baseline(game_pk=game.game_pk, line=market.line, baseline=0.60)
        for _ in range(5):
            state.ask_history.append(0.60)
        engine._line_states[(game.game_pk, market.line)] = state

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "candidates.jsonl"
            engine._candidate_log_path = lambda: path

            se.SignalEngine._process_tick(
                engine,
                game=game,
                market=market,
                payload=_payload(inning=6, away_score=4, home_score=3, outs=1),
                book={"best_bid": 0.60, "ltp": 0.61},
                ask=0.67,
            )

            row = _stable_candidate_row(_read_jsonl(path)[0])
            self.assertEqual(
                row,
                {
                    "game_pk": 1,
                    "away_abbrev": "NYY",
                    "home_abbrev": "BOS",
                    "line": "8.5",
                    "signal_model_family": "score_event_transition",
                    "inning": 6,
                    "inning_state": "Top",
                    "outs": 1,
                    "runners_on": 1,
                    "away_score_before": 4,
                    "home_score_before": 3,
                    "current_total": 7,
                    "home_leading_late": False,
                    "batting_team_is_home": False,
                    "bottom9_available_if_needed": True,
                    "expected_remaining_half_innings": 8.0,
                    "expected_remaining_pa_bucket": "19+",
                    "home_skip_bottom9_risk": 0.0,
                    "decision_ask": 0.67,
                    "best_bid": 0.60,
                    "spread": 0.67 - 0.60,
                    "over_token_id": "token_over",
                    "over_best_bid": 0.60,
                    "over_best_ask": 0.67,
                    "over_mid": 0.635,
                    "over_spread": 0.67 - 0.60,
                    "over_ltp": 0.61,
                    "decision_mid": 0.635,
                    "under_pair_available": False,
                    "decision": "skip_with_features",
                    "decision_reason": "gate_min_edge",
                    "runs_needed": 1.5,
                    "lead_abs": 1,
                    "inferred_runs": 1,
                    "fair_value": 0.75,
                    "base_fair_value": 0.75,
                    "stage2_run_env_delta": 0.0,
                    "team_offense_delta": 0.0,
                    # Active #8 prep (2026-05-17): Stage-1 shadow
                    # empirical override runs in 'off' mode for this
                    # characterization fixture so only the mode tag
                    # + the False used_empirical flag appear in the
                    # candidate payload (the other alt-FV fields stay
                    # None and are dropped by the drop_none_values
                    # pass before serialization).
                    "stage1_shadow_empirical_mode": "off",
                    "fair_value_alt_empirical_used_empirical": False,
                    # 2026-05-21 (Active #17): scoped Alt-A. Stub
                    # forces mode=off so only the mode tag is written
                    # (no per-candidate decision computed).
                    "stage1_alt_a_scope_mode": "off",
                    "stage1_alt_a_scope_decision": "mode_off",
                    # stage1_alt_a_scope_rule_matched is None in
                    # off-mode and dropped by the None-stripping pass.
                    "edge": 0.75 - 0.67,
                    "min_edge_base": 0.16,
                    "min_edge_ask_boost": 0.0,
                    "min_edge_effective": 0.16,
                    "fair_value_raw": 0.75,
                    "fair_value_calibrated": 0.75,
                    "fair_value_calibration_delta": 0.0,
                "fair_value_calibration_method": "identity",
                "fair_value_calibration_mode": "off",
                "fair_value_calibration_family": "score_event_transition",
                "fair_value_calibration_applied": False,
                "inferred_state_base_poisson": 0.75,
                "inferred_state_used_fallback": False,
                "inferred_state_base_source": "poisson_runtime",
                "inference_panel_runs_considered": "1,2,3",
                "inference_panel_selected_rule": "min_abs_poisson_minus_ask",
                "inference_panel_selected_runs": 1,
                "inference_run1_selected": True,
                "inference_run1_away_score": 5,
                "inference_run1_home_score": 3,
                "inference_run1_total": 8,
                "inference_run1_base_poisson": 0.75,
                "inference_run1_distance_to_ask": 0.75 - 0.67,
                "inference_run2_selected": False,
                "inference_run2_away_score": 6,
                "inference_run2_home_score": 3,
                "inference_run2_total": 9,
                "inference_run2_base_poisson": 0.75,
                "inference_run2_distance_to_ask": 0.75 - 0.67,
                "inference_run3_selected": False,
                "inference_run3_away_score": 7,
                "inference_run3_home_score": 3,
                "inference_run3_total": 10,
                "inference_run3_base_poisson": 0.75,
                "inference_run3_distance_to_ask": 0.75 - 0.67,
                "state_value_strategy": "score_event_transition",
                    "current_state_value_base_poisson": 0.75,
                    "current_state_value_line_key_poisson": "po85",
                    "current_state_value_line_key_empirical": "o85",
                    "current_state_value_fv_raw": 0.75,
                    "current_state_value_stage2_run_env_delta": 0.0,
                    "current_state_value_team_offense_delta": 0.0,
                    "current_state_value_edge": 0.75 - 0.67,
                    "current_state_value_away_score": 4,
                    "current_state_value_home_score": 3,
                    "current_state_value_total": 7,
                    "shadow_fv_current_state": 0.75,
                    "shadow_fv_after_inferred_score": 0.75,
                    "shadow_fv_inferred_lift": 0.0,
                    "shadow_no_event_edge": 0.75 - 0.67,
                    "shadow_after_event_edge": 0.75 - 0.67,
                    "shadow_p_score_event_proxy": 0.05000000000000032,
                    "shadow_phantom_risk_score": 0.0,
                    "shadow_phantom_risk_band": "low",
                    "shadow_transition_model": "score_event_vs_no_event_proxy_v1",
                    "shadow_transition_inferred_runs": 1,
                    "shadow_relaxed_evaluated": False,
                    "shadow_relaxed_reason": "gate_min_edge",
                    "shadow_relaxed_secondary_evaluated": False,
                    "shadow_extreme_edge": False,
                    "shadow_extreme_edge_threshold": 0.22,  # TR19 (2026-05-03) tightened 0.30 -> 0.22
                    "shadow_ltp_at_signal": 0.61,
                    "shadow_ltp_ask_gap": 0.67 - 0.61,
                    "shadow_ltp_ask_gap_threshold": 0.50,
                    "shadow_ltp_ask_gap_exceeded": False,
                    "shadow_risk_tags": "",
                    "shadow_post_tr20_gate6_relax_enforce_pass": True,
                    "shadow_post_tr20_extreme_020_pass": True,
                    "shadow_post_tr20_ask_ramp_v2_pass": False,
                    "shadow_post_tr20_combined_pass": False,
                    "hypothetical_limit_price": 0.6455,
                    "skip_features_capture_id": "skip_2026-04-26_1_8.5_000001",
                    "family_a_features": {"fetch_ok": True},
                    "family_b_features": {
                        "book_ok": True,
                        "limit_price_used": 0.6455,
                    },
                    "family_c_features": {"buffer_n": 0},
                    "family_d_features": {
                        "limit_minus_bid": 0.0455,
                        "limit_minus_ask": -0.0245,
                        "spread_normalized_limit": 0.65,
                        "ltp_minus_limit": -0.0355,
                        "limit_price_used": 0.6455,
                    },
                    "family_e_features": {
                        "fv_minus_ask": 0.08,
                        "fv_minus_ltp": 0.14,
                        "inning": 6,
                        "runs_needed": 1.5,
                        "runners_on": 1,
                        "runner_on_first": True,
                        "runner_on_second": False,
                        "runner_on_third": False,
                        "runners_count": 1,
                    },
                    "outcome_join_key": "2026-04-26|1|8.5",
                    "config_label": "default",
                    "ask_bucket": "0.65-0.75",
                    "edge_bucket": "0.00-0.10",
                    "runs_needed_bucket": "<=1.5",
                    "phantom_risk_band": "low",
                    "shadow_low_ask_high_edge": False,
                    "shadow_runs_needed_exact_3p5": False,
                    "shadow_score_event_current_edge_strong_ask_lt_085": False,
                    "shadow_current_state_edge_bucket": "current_edge_0.03-0.08",
                    "shadow_phantom_risk_bucket": "low",
                    "shadow_current_phantom_combo_bucket": "current_edge_0.03-0.08|phantom_low",
                    "shadow_inning_bucket": "6",
                    "shadow_inning_runs_needed_bucket": "inn_6|rn_<=1.5",
                    "shadow_bottom9_home_lead_context": "no_home_late_lead|pa_19+",
                    "shadow_home_skip_bottom9_risk_bucket": "no_skip_bottom9_risk",
                    "shadow_no_score_poisson_edge_bucket": "0.05-0.10",
                    "shadow_no_score_empirical_edge_bucket": "missing",
                    "shadow_no_score_ask_bucket": "0.55-0.70",
                    "shadow_no_score_drawdown_bucket": "missing",
                    "shadow_no_score_poisson_empirical_ask_drawdown_bucket": "missing",
                    "model_price_residual_version": "logit_v1",
                    "model_market_logit_residual": 0.390427,
                    "raw_model_market_logit_residual": 0.390427,
                    "current_state_market_logit_residual": 0.390427,
                    "after_event_market_logit_residual": 0.390427,
                    "execution_policy_current_limit_price": 0.6455,
                    "execution_policy_current_limit_ev_if_filled_per_share": 0.1045,
                    "execution_policy_current_limit_breakeven_prob": 0.6455,
                    "execution_policy_limit_plus_1c_price": 0.6555,
                    "execution_policy_limit_plus_1c_ev_if_filled_per_share": 0.0945,
                    "execution_policy_limit_plus_1c_breakeven_prob": 0.6555,
                    "execution_policy_limit_plus_2c_price": 0.6655,
                    "execution_policy_limit_plus_2c_ev_if_filled_per_share": 0.0845,
                    "execution_policy_limit_plus_2c_breakeven_prob": 0.6655,
                    "execution_policy_taker_like_price": 0.67,
                    "execution_policy_taker_like_ev_if_filled_per_share": 0.08,
                    "execution_policy_taker_like_breakeven_prob": 0.67,
                    "schema_version": 1,
                    "session_date": "2026-04-26",
                    "mode": "paper",
                    "side": "over",
                },
            )

    def test_late_stage_skip_gate_allowlist_matches_current_reason_names(self) -> None:
        expected_reasons = {
            "gate_runs_needed_max",
            "gate_close_game_runs_needed",
            "gate_stage2_suppression",
            "gate_sp_era",
        }
        stale_reasons = {
            "gate_runs_needed",
            "gate_close_game_high_rn",
            "gate_blowout_adj",
            "gate_s2_suppress",
        }
        self.assertTrue(expected_reasons.issubset(sp.LATE_STAGE_SKIP_GATES))
        self.assertFalse(stale_reasons.intersection(sp.LATE_STAGE_SKIP_GATES))

    def test_candidate_table_treats_skip_with_features_as_no_trade(self) -> None:
        rows = candidate_table._enrich_labels(
            [
                {
                    "decision": "skip_with_features",
                    "game_pk": 1,
                    "line": "8.5",
                }
            ],
            final_totals={1: 9},
        )

        self.assertEqual(rows[0]["target_trade"], 0)
        self.assertTrue(rows[0]["label_available"])
        self.assertEqual(rows[0]["target_counterfactual_win"], 1)

    def test_tick_buffer_health_log_summarizes_line_state_buffers_at_debug(self) -> None:
        engine = _make_engine_stub()
        now = se._now_ts()
        state_a = SimpleNamespace(
            tick_buffer=[
                {"ts": now - 20.0, "bid": 0.60, "ask": 0.64},
                {"ts": now - 5.0, "bid": 0.61, "ask": 0.65},
            ]
        )
        state_b = SimpleNamespace(tick_buffer=[])
        engine._line_states = {(1, "8.5"): state_a, (2, "7.5"): state_b}
        engine._last_tick_buffer_health_log_ts = 0.0

        with self.assertLogs("signal_engine", level="DEBUG") as captured:
            engine._maybe_log_tick_buffer_health(interval_secs=0.0)

        message = "\n".join(captured.output)
        self.assertIn("tick_buffer health: 2 line_states, 1 nonempty", message)
        self.assertGreater(engine._last_tick_buffer_health_log_ts, 0.0)

    def test_tick_buffer_health_warns_when_abnormal(self) -> None:
        engine = _make_engine_stub()
        engine._line_states = {
            (1, "8.5"): SimpleNamespace(tick_buffer=[]),
            (2, "7.5"): SimpleNamespace(tick_buffer=[]),
        }
        engine._last_tick_buffer_health_log_ts = 0.0

        with self.assertLogs("signal_engine", level="WARNING") as captured:
            engine._maybe_log_tick_buffer_health(interval_secs=0.0)

        self.assertIn("tick_buffer health: 2 line_states, 0 nonempty", "\n".join(captured.output))

    def test_runtime_debug_rollups_compact_repeated_lines(self) -> None:
        engine = _make_engine_stub()
        for _ in range(3):
            rlr.record_runtime_debug_rollup(
                engine,
                kind="stage3_adj",
                game_pk=1,
                game_label="AWY@HOM",
                line="8.5",
                inning=5,
                state="T5|outs=1|score=2-1|runners=1",
                source_key="AWY-HOM|2026-05-08",
                message="Stage-3 adj %s line=%s",
                args=("AWY@HOM", "8.5"),
            )

        with self.assertLogs("signal_engine", level="DEBUG") as captured:
            rlr.log_runtime_debug_rollups(engine, force=True)

        message = "\n".join(captured.output)
        self.assertIn("runtime debug rollup: events=3 unique_keys=1", message)
        self.assertIn("kind=stage3_adj count=3", message)
        self.assertFalse(engine._runtime_debug_rollup_counts)


class LiveEngineContractCharacterizationTests(unittest.TestCase):
    def test_parse_live_args_keeps_stake_contract(self) -> None:
        live_args, trade_args, _monitor_args = le.parse_live_args(
            [
                "--stake-mode", "flat",
                "--stake", "15",
                "--daily-budget", "100",
                "--gate-min-current-total-relax-mode", "shadow",
                "--gate-blowout-relax-mode", "shadow",
                "--prob-calibration-mode", "shadow",
                "--ask-edge-ramp-max-boost", "0.05",
            ]
        )
        self.assertEqual(live_args.stake_mode, "flat")
        self.assertAlmostEqual(trade_args.stake, 15.0, places=8)
        self.assertAlmostEqual(live_args.daily_budget, 100.0, places=8)
        self.assertEqual(trade_args.gate_min_current_total_relax_mode, "shadow")
        self.assertEqual(trade_args.gate_blowout_relax_mode, "shadow")
        self.assertEqual(trade_args.prob_calibration_mode, "shadow")
        self.assertAlmostEqual(trade_args.ask_edge_ramp_max_boost, 0.05, places=8)


class SignalPipelineDelegationCharacterizationTests(unittest.TestCase):
    def test_process_tick_delegates_to_pipeline_impl(self) -> None:
        from unittest.mock import patch

        engine = _make_engine_stub()
        game = SimpleNamespace(game_pk=123, away_abbrev="NYY", home_abbrev="BOS")
        market = SimpleNamespace(line="8.5")
        payload = {"inning": 6}
        book = {"best_bid": 0.70}
        ask = 0.72

        with patch("signal_engine._process_tick_impl") as mocked:
            se.SignalEngine._process_tick(
                engine,
                game=game,
                market=market,
                payload=payload,
                book=book,
                ask=ask,
            )
            mocked.assert_called_once()
            kwargs = mocked.call_args.kwargs
            self.assertIs(kwargs["self"], engine)
            self.assertIs(kwargs["game"], game)
            self.assertIs(kwargs["market"], market)
            self.assertIs(kwargs["payload"], payload)
            self.assertIs(kwargs["book"], book)
            self.assertEqual(kwargs["ask"], ask)
            self.assertIs(kwargs["line_state_cls"], se.LineState)


if __name__ == "__main__":
    unittest.main()
