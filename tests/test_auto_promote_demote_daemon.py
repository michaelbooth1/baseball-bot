"""Tests for the auto-promotion/demotion daemon.

The daemon reads the same verdicts promote.py uses and (in `act` mode)
invokes promote.py for actionable verdicts on file-swap levers. Tests
focus on the safety contract:

  - Mode preview: never invokes subprocess; just logs decisions
  - Mode act: invokes subprocess for actionable verdicts; skips
    cooldown-blocked levers
  - Mode off: returns early; no verdict reads, no actions
  - Cooldown: any prior action (manual or daemon) within N days blocks
    new auto-action
  - Per-lever opt-outs: --no-auto-promote-stage2 etc. skip the lever
  - Preview-only levers (gate-threshold): always skipped with note,
    even when verdict actionable (operator-in-loop value selection)
  - Subprocess failure: recorded distinctly, doesn't crash daemon
  - Schema consistency: decisions are well-formed dicts
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import auto_promote_demote_daemon as daemon  # noqa: E402
import promote  # noqa: E402


def _write_event_log(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _stage2_history_row(date: str, delta: float) -> dict:
    return {
        "generated_at_utc": f"{date}T12:00:00Z",
        "data_max_date": date,
        "production_brier": 0.22,
        "staging_brier": 0.22 + delta,
        "delta": delta,
    }


def _stage3_history_row(date: str, max_abs_delta: float) -> dict:
    return {
        "generated_at_utc": f"{date}T12:00:00Z",
        "data_max_date": date,
        "research_betas": {"prior_season": -0.16, "season_to_date": +0.14, "momentum_10": +0.16},
        "active_betas": {"prior_season": -0.1514, "season_to_date": +0.1407, "momentum_10": +0.1503},
        "active_source": "compiled_defaults",
        "max_abs_delta": max_abs_delta,
    }


def _build_args(td: Path, **overrides) -> SimpleNamespace:
    defaults = SimpleNamespace(
        mode="preview",
        active_date="2026-05-15",
        cooldown_days=14,
        operator="auto_daemon",
        event_log_path=td / "events.jsonl",
        sessions_dir=td / "sessions",
        stage2_brier_history_path=td / "stage2_brier_history.jsonl",
        stage3_v2_drift_history_path=td / "stage3_v2_drift_history.jsonl",
        stake_scaling_report_path=td / "stake_scaling.json",
        walk_forward_cert_path=td / "wfc.json",
        promote_script=td / "promote.py",
        no_auto_promote_stage2=False,
        no_auto_demote_stage2=False,
        no_auto_promote_stage3_v2=False,
        no_auto_demote_stage3_v2=False,
        no_auto_promote_stake_scaling=False,
        no_auto_demote_stake_scaling=False,
    )
    for k, v in overrides.items():
        setattr(defaults, k, v)
    return defaults


def _seed_promote_verdict(td: Path, lever: str) -> None:
    """Seed history that makes the promotion verdict say 'promote'."""
    if lever == "stage2":
        _write_event_log(
            td / "stage2_brier_history.jsonl",
            [_stage2_history_row(f"2026-05-0{d}", -0.005) for d in range(1, 7)],
        )
    elif lever == "stage3-v2":
        _write_event_log(
            td / "stage3_v2_drift_history.jsonl",
            [_stage3_history_row(f"2026-05-0{d}", 0.025) for d in range(1, 7)],
        )


def _seed_event_log(td: Path, rows: list) -> None:
    _write_event_log(td / "events.jsonl", rows)


def _seed_stake_scaling_report(td: Path, verdict: str = "need_more_data") -> None:
    (td / "stake_scaling.json").write_text(
        json.dumps({
            "verdict": verdict, "verdict_reason": "test",
            "n_sessions": 3, "thresholds": {"min_sessions": 30},
        }),
        encoding="utf-8",
    )


def _seed_walk_forward_cert(td: Path, gates: list) -> None:
    (td / "wfc.json").write_text(
        json.dumps({
            "readiness": {"label": "READY", "n_filled": 200, "n_dates": 35},
            "gates": gates,
        }),
        encoding="utf-8",
    )


class CooldownTests(unittest.TestCase):
    def test_no_history_returns_ok(self):
        ok, reason = daemon._cooldown_ok(
            lever_key="stage2", events=[], today="2026-05-15", cooldown_days=14,
        )
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_recent_promotion_blocks(self):
        events = [{
            "lever": "stage2", "action": "promoted", "direction": "promote",
            "generated_at_utc": "2026-05-10T12:00:00Z", "operator": "tester",
        }]
        ok, reason = daemon._cooldown_ok(
            lever_key="stage2", events=events, today="2026-05-15", cooldown_days=14,
        )
        self.assertFalse(ok)
        self.assertIn("5.0d ago", reason)

    def test_old_promotion_does_not_block(self):
        events = [{
            "lever": "stage2", "action": "promoted", "direction": "promote",
            "generated_at_utc": "2026-04-15T12:00:00Z", "operator": "tester",
        }]
        ok, _ = daemon._cooldown_ok(
            lever_key="stage2", events=events, today="2026-05-15", cooldown_days=14,
        )
        self.assertTrue(ok)

    def test_demote_action_also_triggers_cooldown(self):
        events = [{
            "lever": "stage2", "action": "demoted", "direction": "demote",
            "generated_at_utc": "2026-05-12T12:00:00Z", "operator": "tester",
        }]
        ok, _ = daemon._cooldown_ok(
            lever_key="stage2", events=events, today="2026-05-15", cooldown_days=14,
        )
        self.assertFalse(ok)

    def test_blocked_action_does_not_trigger_cooldown(self):
        # Blocked attempts aren't state changes; no reason to cool down.
        events = [{
            "lever": "stage2", "action": "blocked", "direction": "promote",
            "generated_at_utc": "2026-05-14T12:00:00Z", "operator": "tester",
        }]
        ok, _ = daemon._cooldown_ok(
            lever_key="stage2", events=events, today="2026-05-15", cooldown_days=14,
        )
        self.assertTrue(ok)

    def test_lever_name_hyphen_maps_to_underscore_in_log(self):
        # The daemon uses "stage3-v2"; promote_events uses "stage3_v2".
        events = [{
            "lever": "stage3_v2", "action": "promoted", "direction": "promote",
            "generated_at_utc": "2026-05-12T12:00:00Z", "operator": "tester",
        }]
        ok, _ = daemon._cooldown_ok(
            lever_key="stage3-v2", events=events, today="2026-05-15", cooldown_days=14,
        )
        self.assertFalse(ok, "cooldown must respect the hyphen->underscore mapping")


class EvaluateLeverTests(unittest.TestCase):
    def test_no_action_when_verdict_not_actionable(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "sessions").mkdir()
            args = _build_args(td)
            d = daemon.evaluate_lever(
                lever="stage2", args=args, events=[], today="2026-05-15",
            )
            self.assertEqual(d["decision"], "no_action")

    def test_would_promote_in_preview_mode(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "sessions").mkdir()
            _seed_promote_verdict(td, "stage2")
            args = _build_args(td, mode="preview")
            d = daemon.evaluate_lever(
                lever="stage2", args=args, events=[], today="2026-05-15",
            )
            self.assertEqual(d["decision"], "would_promote")
            self.assertEqual(d["direction"], "promote")

    def test_promoting_in_act_mode(self):
        # In act mode the evaluator sets decision="promoting"; actuation
        # happens in actuate(). Cooldown OK since events=[].
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "sessions").mkdir()
            _seed_promote_verdict(td, "stage2")
            args = _build_args(td, mode="act")
            d = daemon.evaluate_lever(
                lever="stage2", args=args, events=[], today="2026-05-15",
            )
            self.assertEqual(d["decision"], "promoting")

    def test_cooldown_blocks_actionable_verdict(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "sessions").mkdir()
            _seed_promote_verdict(td, "stage2")  # promote verdict
            events = [{
                "lever": "stage2", "action": "promoted", "direction": "promote",
                "generated_at_utc": "2026-05-10T12:00:00Z", "operator": "manual",
            }]
            args = _build_args(td, mode="act")
            d = daemon.evaluate_lever(
                lever="stage2", args=args, events=events, today="2026-05-15",
            )
            self.assertEqual(d["decision"], "skipped_cooldown")
            self.assertIn("cooldown is 14d", d["reason"])

    def test_opt_out_skips_lever(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "sessions").mkdir()
            _seed_promote_verdict(td, "stage2")
            args = _build_args(td, mode="act", no_auto_promote_stage2=True)
            d = daemon.evaluate_lever(
                lever="stage2", args=args, events=[], today="2026-05-15",
            )
            self.assertEqual(d["decision"], "skipped_opt_out")

    def test_stake_scaling_with_promote_verdict_actuates(self):
        # As of 2026-05-16, stake-scaling actuates through the runtime
        # overrides file: "promote" verdict + no cooldown -> would_promote
        # (preview) / promoting (act).
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "sessions").mkdir()
            _seed_stake_scaling_report(td, verdict="promote")
            args = _build_args(td, mode="preview")
            d = daemon.evaluate_lever(
                lever="stake-scaling", args=args, events=[], today="2026-05-15",
            )
            self.assertEqual(d["decision"], "would_promote")
            self.assertEqual(d["direction"], "promote")
            self.assertEqual(d["verdict_label"], "promote")

    def test_stake_scaling_with_hold_verdict_no_action(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "sessions").mkdir()
            _seed_stake_scaling_report(td, verdict="hold")
            args = _build_args(td, mode="act")
            d = daemon.evaluate_lever(
                lever="stake-scaling", args=args, events=[], today="2026-05-15",
            )
            self.assertEqual(d["decision"], "no_action")

    def test_stake_scaling_opt_out_skips_lever(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "sessions").mkdir()
            _seed_stake_scaling_report(td, verdict="promote")
            args = _build_args(td, mode="act", no_auto_promote_stake_scaling=True)
            d = daemon.evaluate_lever(
                lever="stake-scaling", args=args, events=[], today="2026-05-15",
            )
            self.assertEqual(d["decision"], "skipped_opt_out")

    def test_gate_threshold_with_no_actionable_gates_skipped(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "sessions").mkdir()
            _seed_walk_forward_cert(td, gates=[
                {"name": "gate_extreme_edge",
                 "verdict": {"verdict": "KEEP", "recommended_threshold": None}},
            ])
            args = _build_args(td, mode="act")
            d = daemon.evaluate_lever(
                lever="gate-threshold", args=args, events=[], today="2026-05-15",
            )
            self.assertEqual(d["decision"], "skipped_preview_only_lever")
            self.assertEqual(d["verdict_label"], "hold")

    def test_gate_threshold_with_retune_gate_still_preview_only(self):
        # Even with an actionable RETUNE verdict the daemon refuses to
        # auto-pick a threshold value -- it's operator-in-loop.
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "sessions").mkdir()
            _seed_walk_forward_cert(td, gates=[
                {"name": "gate_extreme_edge",
                 "verdict": {"verdict": "RETUNE", "recommended_threshold": 0.20}},
            ])
            args = _build_args(td, mode="act")
            d = daemon.evaluate_lever(
                lever="gate-threshold", args=args, events=[], today="2026-05-15",
            )
            self.assertEqual(d["decision"], "skipped_preview_only_lever")
            self.assertEqual(d["verdict_label"], "promote")


class ActuationTests(unittest.TestCase):
    def test_preview_mode_does_not_invoke_subprocess(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            args = _build_args(td, mode="preview")
            decision = {"lever": "stage2", "decision": "would_promote",
                        "direction": "promote", "verdict_label": "promote", "reason": ""}
            with patch.object(daemon.subprocess, "run") as mock_run:
                daemon.actuate(decision, args)
            self.assertEqual(mock_run.call_count, 0)
            # Decision unchanged
            self.assertEqual(decision["decision"], "would_promote")

    def test_act_mode_invokes_subprocess_for_promoting(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            args = _build_args(td, mode="act")
            decision = {"lever": "stage2", "decision": "promoting",
                        "direction": "promote", "verdict_label": "promote", "reason": ""}
            with patch.object(daemon.subprocess, "run") as mock_run:
                mock_run.return_value = SimpleNamespace(returncode=0, stdout="PROMOTED\n", stderr="")
                daemon.actuate(decision, args)
            self.assertEqual(mock_run.call_count, 1)
            self.assertEqual(decision["decision"], "auto_promoted")
            self.assertEqual(decision["subprocess"]["returncode"], 0)
            # Subprocess command includes promote.py + lever + operator label
            cmd = mock_run.call_args[0][0]
            self.assertIn("stage2", cmd)
            self.assertIn("--operator", cmd)
            self.assertIn("auto_daemon", cmd)

    def test_act_mode_invokes_subprocess_for_demoting(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            args = _build_args(td, mode="act")
            decision = {"lever": "stage2", "decision": "demoting",
                        "direction": "demote", "verdict_label": "demote", "reason": ""}
            with patch.object(daemon.subprocess, "run") as mock_run:
                mock_run.return_value = SimpleNamespace(returncode=0, stdout="DEMOTED\n", stderr="")
                daemon.actuate(decision, args)
            cmd = mock_run.call_args[0][0]
            self.assertIn("demote", cmd)
            self.assertIn("stage2", cmd)
            self.assertEqual(decision["decision"], "auto_demoted")

    def test_subprocess_failure_marks_decision_failed(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            args = _build_args(td, mode="act")
            decision = {"lever": "stage2", "decision": "promoting",
                        "direction": "promote", "verdict_label": "promote", "reason": ""}
            with patch.object(daemon.subprocess, "run") as mock_run:
                mock_run.return_value = SimpleNamespace(returncode=2, stdout="boom\n", stderr="")
                daemon.actuate(decision, args)
            self.assertEqual(decision["decision"], "failed")
            self.assertEqual(decision["subprocess"]["returncode"], 2)

    def test_act_mode_no_op_for_non_actionable_decisions(self):
        # Skip-decisions stay skip; daemon doesn't try to actuate them.
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            args = _build_args(td, mode="act")
            for skip_label in ("no_action", "skipped_cooldown", "skipped_opt_out",
                               "skipped_preview_only_lever"):
                decision = {"lever": "stage2", "decision": skip_label,
                            "direction": None, "verdict_label": "x", "reason": ""}
                with patch.object(daemon.subprocess, "run") as mock_run:
                    daemon.actuate(decision, args)
                self.assertEqual(mock_run.call_count, 0,
                                 f"actuate must not invoke subprocess for decision={skip_label}")
                self.assertEqual(decision["decision"], skip_label)


class MainEntryPointTests(unittest.TestCase):
    def test_mode_off_skips_everything(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "sessions").mkdir()
            rc = daemon.main([
                "--mode", "off",
                "--active-date", "2026-05-15",
                "--event-log-path", str(td / "events.jsonl"),
                "--sessions-dir", str(td / "sessions"),
                "--stage2-brier-history-path", str(td / "stage2_brier_history.jsonl"),
                "--stage3-v2-drift-history-path", str(td / "stage3_v2_drift_history.jsonl"),
                "--stake-scaling-report-path", str(td / "stake_scaling.json"),
                "--walk-forward-cert-path", str(td / "wfc.json"),
            ])
            self.assertEqual(rc, 0)

    def test_preview_mode_runs_without_subprocess(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "sessions").mkdir()
            # Seed promote verdict for stage2 -> evaluator returns "would_promote"
            _seed_promote_verdict(td, "stage2")
            _seed_stake_scaling_report(td, verdict="hold")
            _seed_walk_forward_cert(td, gates=[])
            with patch.object(daemon.subprocess, "run") as mock_run:
                rc = daemon.main([
                    "--mode", "preview",
                    "--active-date", "2026-05-15",
                    "--event-log-path", str(td / "events.jsonl"),
                    "--sessions-dir", str(td / "sessions"),
                    "--stage2-brier-history-path", str(td / "stage2_brier_history.jsonl"),
                    "--stage3-v2-drift-history-path", str(td / "stage3_v2_drift_history.jsonl"),
                    "--stake-scaling-report-path", str(td / "stake_scaling.json"),
                    "--walk-forward-cert-path", str(td / "wfc.json"),
                ])
            self.assertEqual(rc, 0)
            self.assertEqual(mock_run.call_count, 0,
                             "preview mode must never invoke subprocess")

    def test_act_mode_invokes_subprocess_for_actionable_lever(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "sessions").mkdir()
            _seed_promote_verdict(td, "stage2")
            _seed_stake_scaling_report(td, verdict="hold")
            _seed_walk_forward_cert(td, gates=[])
            with patch.object(daemon.subprocess, "run") as mock_run:
                mock_run.return_value = SimpleNamespace(returncode=0, stdout="PROMOTED\n", stderr="")
                rc = daemon.main([
                    "--mode", "act",
                    "--active-date", "2026-05-15",
                    "--event-log-path", str(td / "events.jsonl"),
                    "--sessions-dir", str(td / "sessions"),
                    "--stage2-brier-history-path", str(td / "stage2_brier_history.jsonl"),
                    "--stage3-v2-drift-history-path", str(td / "stage3_v2_drift_history.jsonl"),
                    "--stake-scaling-report-path", str(td / "stake_scaling.json"),
                    "--walk-forward-cert-path", str(td / "wfc.json"),
                ])
            self.assertEqual(rc, 0)
            # stage2 had a promote verdict -> one subprocess invocation
            self.assertGreaterEqual(mock_run.call_count, 1)


class RenderSummaryTests(unittest.TestCase):
    def test_summary_includes_mode_and_cooldown(self):
        decisions = [
            {"lever": "stage2", "decision": "would_promote",
             "direction": "promote", "verdict_label": "promote", "reason": "test"},
        ]
        args = SimpleNamespace(mode="preview", cooldown_days=14, active_date="2026-05-15")
        out = daemon.render_summary(decisions, args=args)
        self.assertIn("mode=preview", out)
        self.assertIn("cooldown_days=14", out)
        self.assertIn("ALERT", out)
        self.assertIn("stage2", out)

    def test_summary_for_mode_off_is_compact(self):
        args = SimpleNamespace(mode="off", cooldown_days=14, active_date="2026-05-15")
        out = daemon.render_summary([], args=args)
        self.assertIn("DAEMON DISABLED", out)


if __name__ == "__main__":
    unittest.main()
