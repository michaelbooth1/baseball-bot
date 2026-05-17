"""Tests for the unified promotion CLI (scripts/analysis/promote.py).

The CLI wraps four manual self-improvement levers (Stage-2 cache swap,
Stage-3 v2 weights swap, stake-scaling shadow->enforce, gate-threshold
RETUNE) behind one command pattern: read verdict, refuse unless verdict
says go (or --force), perform the swap, append a row to the promotion
events log, print a next-action checklist.

Tests focus on the contract:
  - Verdict-gating: each lever refuses when verdict isn't "promote"/"RETUNE".
  - --force: overrides the gate, action labelled "forced" in event log.
  - --dry-run: doesn't perform the swap, but still appends an event row.
  - Event log: every invocation appends exactly one row with the correct
    lever / action / verdict_snapshot.
  - File swap (stage2): atomic copy from staging to production.
  - Tmp routing: every test routes the event log under tmp_path so the
    canonical data/analysis_output/promotion_events.jsonl never gets
    polluted by test runs (same lesson as the stage2_brier_history
    pollution we cleaned up 2026-05-15).
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

import promote  # noqa: E402


def _read_event_log(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_history(path: Path, rows: list) -> None:
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


class PromotionEventLogTests(unittest.TestCase):
    def test_event_log_appends_one_row(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "events.jsonl"
            promote.write_promotion_event(
                promote.PromotionEvent(
                    lever="stage2",
                    action="dry_run",
                    operator="tester",
                    verdict_snapshot={"verdict": "promote"},
                ),
                log_path=log_path,
            )
            rows = _read_event_log(log_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["lever"], "stage2")
            self.assertEqual(rows[0]["action"], "dry_run")
            self.assertEqual(rows[0]["operator"], "tester")
            self.assertIn("generated_at_utc", rows[0])

    def test_event_log_is_append_only(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "events.jsonl"
            for i in range(3):
                promote.write_promotion_event(
                    promote.PromotionEvent(lever="stage2", action="dry_run", operator=f"op{i}"),
                    log_path=log_path,
                )
            rows = _read_event_log(log_path)
            self.assertEqual(len(rows), 3)
            self.assertEqual([r["operator"] for r in rows], ["op0", "op1", "op2"])

    def test_resolve_operator_falls_back_to_env(self):
        with patch.dict("os.environ", {"USER": "from_env"}, clear=False):
            self.assertEqual(promote._resolve_operator(None), "from_env")
            self.assertEqual(promote._resolve_operator("explicit"), "explicit")


class Stage2PromoteTests(unittest.TestCase):
    """Stage-2 actually swaps files. Verify gating + atomic copy + log row."""

    def _build_args(self, td: Path, **overrides):
        defaults = SimpleNamespace(
            stage2_brier_history_path=td / "history.jsonl",
            stage2_staging_path=td / "cache" / "staging.json",
            stage2_cache_path=td / "cache" / "production.json",
            event_log_path=td / "events.jsonl",
            dry_run=False,
            force=False,
            operator="tester",
        )
        for k, v in overrides.items():
            setattr(defaults, k, v)
        return defaults

    def _seed_fixtures(self, td: Path):
        (td / "cache").mkdir(parents=True, exist_ok=True)
        # Production starts as v1
        (td / "cache" / "production.json").write_text(
            json.dumps({"validation_metrics": {"7.5": {"stage2_brier": 0.220}}}),
            encoding="utf-8",
        )
        (td / "cache" / "staging.json").write_text(
            json.dumps({"validation_metrics": {"7.5": {"stage2_brier": 0.210}}}),
            encoding="utf-8",
        )

    def test_blocks_when_verdict_insufficient(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            self._seed_fixtures(td)
            # Empty history -> insufficient_history verdict
            args = self._build_args(td)
            args.stage2_brier_history_path.parent.mkdir(parents=True, exist_ok=True)
            args.stage2_brier_history_path.write_text("", encoding="utf-8")
            rc = promote.cmd_stage2(args)
            self.assertEqual(rc, 1, "should refuse when verdict isn't promote")
            # Production cache must not have changed
            prod = json.loads((td / "cache" / "production.json").read_text())
            self.assertEqual(prod["validation_metrics"]["7.5"]["stage2_brier"], 0.220)
            # Event row was logged with action=blocked
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["action"], "blocked")
            self.assertIn("not 'promote'", rows[0]["block_reason"])

    def test_promotes_when_verdict_promote(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            self._seed_fixtures(td)
            # Seed history that satisfies promote (5+ improving days)
            history = [_stage2_history_row(f"2026-05-0{d}", -0.005) for d in range(1, 7)]
            _write_history(td / "history.jsonl", history)
            args = self._build_args(td)
            rc = promote.cmd_stage2(args)
            self.assertEqual(rc, 0)
            # Production cache must now match staging (i.e. swap happened)
            prod = json.loads((td / "cache" / "production.json").read_text())
            self.assertEqual(prod["validation_metrics"]["7.5"]["stage2_brier"], 0.210)
            # Staging file still exists (we copy, not move)
            self.assertTrue((td / "cache" / "staging.json").exists())
            # Event row logged with action=promoted
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["action"], "promoted")
            self.assertEqual(rows[0]["lever"], "stage2")
            self.assertAlmostEqual(rows[0]["from_state"]["production_brier"], 0.220, places=4)
            self.assertAlmostEqual(rows[0]["to_state"]["staging_brier"], 0.210, places=4)

    def test_dry_run_does_not_swap_but_logs(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            self._seed_fixtures(td)
            history = [_stage2_history_row(f"2026-05-0{d}", -0.005) for d in range(1, 7)]
            _write_history(td / "history.jsonl", history)
            args = self._build_args(td, dry_run=True)
            rc = promote.cmd_stage2(args)
            self.assertEqual(rc, 0)
            # Production cache UNCHANGED
            prod = json.loads((td / "cache" / "production.json").read_text())
            self.assertEqual(prod["validation_metrics"]["7.5"]["stage2_brier"], 0.220)
            # But event row IS written with action=dry_run
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(rows[0]["action"], "dry_run")

    def test_force_overrides_blocked_verdict(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            self._seed_fixtures(td)
            # Empty history -> would normally block with insufficient_history
            args = self._build_args(td, force=True)
            args.stage2_brier_history_path.parent.mkdir(parents=True, exist_ok=True)
            args.stage2_brier_history_path.write_text("", encoding="utf-8")
            rc = promote.cmd_stage2(args)
            self.assertEqual(rc, 0)
            # Production was actually swapped
            prod = json.loads((td / "cache" / "production.json").read_text())
            self.assertEqual(prod["validation_metrics"]["7.5"]["stage2_brier"], 0.210)
            # Event row logged with action=forced (not promoted)
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(rows[0]["action"], "forced")

    def test_blocks_when_staging_missing(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "cache").mkdir()
            (td / "cache" / "production.json").write_text("{}", encoding="utf-8")
            # No staging file
            history = [_stage2_history_row(f"2026-05-0{d}", -0.005) for d in range(1, 7)]
            _write_history(td / "history.jsonl", history)
            args = self._build_args(td)
            rc = promote.cmd_stage2(args)
            self.assertEqual(rc, 2)
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(rows[0]["action"], "blocked")
            self.assertIn("missing", rows[0]["block_reason"])


class Stage3V2PromoteTests(unittest.TestCase):
    def _build_args(self, td: Path, **overrides):
        defaults = SimpleNamespace(
            stage3_v2_drift_history_path=td / "drift_history.jsonl",
            stage3_v2_research_fit_path=td / "phase4_models.json",
            stage3_v2_prod_weights_path=td / "team_offense_v2_weights.json",
            promote_team_offense_script=td / "promote_team_offense_v2.py",  # never runs in dry-run
            event_log_path=td / "events.jsonl",
            dry_run=True,  # default to dry-run so subprocess never fires in tests
            force=False,
            operator="tester",
        )
        for k, v in overrides.items():
            setattr(defaults, k, v)
        return defaults

    def _seed_fixtures(self, td: Path):
        # phase4_models.json with a model_3_blend section the verdict logic can read.
        (td / "phase4_models.json").write_text(
            json.dumps({
                "models": {
                    "model_3_blend": {
                        "beta_prior": -0.20,
                        "beta_season": +0.10,
                        "beta_momentum": +0.20,
                    }
                }
            }),
            encoding="utf-8",
        )

    def test_blocks_when_verdict_insufficient(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            self._seed_fixtures(td)
            args = self._build_args(td)
            args.stage3_v2_drift_history_path.write_text("", encoding="utf-8")
            rc = promote.cmd_stage3_v2(args)
            self.assertEqual(rc, 1)
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(rows[0]["action"], "blocked")

    def test_dry_run_when_verdict_promote(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            self._seed_fixtures(td)
            # 5+ days with material drift
            history = [_stage3_history_row(f"2026-05-0{d}", 0.025) for d in range(1, 7)]
            _write_history(td / "drift_history.jsonl", history)
            args = self._build_args(td)  # dry_run=True
            rc = promote.cmd_stage3_v2(args)
            self.assertEqual(rc, 0)
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(rows[0]["action"], "dry_run")
            self.assertEqual(rows[0]["lever"], "stage3_v2")

    def test_invokes_subprocess_when_real_promote(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            self._seed_fixtures(td)
            history = [_stage3_history_row(f"2026-05-0{d}", 0.025) for d in range(1, 7)]
            _write_history(td / "drift_history.jsonl", history)
            args = self._build_args(td, dry_run=False)
            with patch.object(promote.subprocess, "run") as mock_run:
                mock_run.return_value = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
                rc = promote.cmd_stage3_v2(args)
            self.assertEqual(rc, 0)
            # subprocess.run was invoked with expected args
            self.assertEqual(mock_run.call_count, 1)
            cmd = mock_run.call_args[0][0]
            self.assertIn(str(args.promote_team_offense_script), cmd)
            self.assertIn(str(args.stage3_v2_research_fit_path), cmd)
            # Event row records the subprocess returncode
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(rows[0]["action"], "promoted")
            self.assertEqual(rows[0]["subprocess_returncode"], 0)

    def test_subprocess_failure_blocks_and_records_returncode(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            self._seed_fixtures(td)
            history = [_stage3_history_row(f"2026-05-0{d}", 0.025) for d in range(1, 7)]
            _write_history(td / "drift_history.jsonl", history)
            args = self._build_args(td, dry_run=False)
            with patch.object(promote.subprocess, "run") as mock_run:
                mock_run.return_value = SimpleNamespace(returncode=2, stdout="boom\n", stderr="")
                rc = promote.cmd_stage3_v2(args)
            self.assertEqual(rc, 2)
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(rows[0]["action"], "blocked")
            self.assertEqual(rows[0]["subprocess_returncode"], 2)


class StakeScalingPromoteTests(unittest.TestCase):
    def _build_args(self, td: Path, **overrides):
        defaults = SimpleNamespace(
            stake_scaling_report_path=td / "stake_scaling.json",
            event_log_path=td / "events.jsonl",
            live_overrides_path=td / "live_engine_overrides.json",
            dry_run=False,
            force=False,
            operator="tester",
        )
        for k, v in overrides.items():
            setattr(defaults, k, v)
        return defaults

    def test_blocks_when_verdict_not_promote(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "stake_scaling.json").write_text(
                json.dumps({
                    "verdict": "need_more_data",
                    "verdict_reason": "Have 3/30 sessions",
                    "n_sessions": 3,
                    "thresholds": {"min_sessions": 30},
                }),
                encoding="utf-8",
            )
            args = self._build_args(td)
            rc = promote.cmd_stake_scaling(args)
            self.assertEqual(rc, 1)
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(rows[0]["action"], "blocked")

    def test_promotes_when_verdict_promote(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "stake_scaling.json").write_text(
                json.dumps({
                    "verdict": "promote",
                    "verdict_reason": "high beats low by 8pp WR / 17pp ROI",
                    "n_sessions": 32,
                    "thresholds": {"min_sessions": 30},
                }),
                encoding="utf-8",
            )
            args = self._build_args(td)
            rc = promote.cmd_stake_scaling(args)
            self.assertEqual(rc, 0)
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(rows[0]["action"], "promoted")
            self.assertEqual(rows[0]["from_state"]["calibrated_stake_scale_mode"], "shadow")
            self.assertEqual(rows[0]["to_state"]["calibrated_stake_scale_mode"], "enforce")

    def test_blocks_when_report_unreadable(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            args = self._build_args(td)  # report doesn't exist
            rc = promote.cmd_stake_scaling(args)
            self.assertEqual(rc, 2)


class GateThresholdPromoteTests(unittest.TestCase):
    def _build_args(self, td: Path, gate_name: str, new_value: str, **overrides):
        defaults = SimpleNamespace(
            walk_forward_cert_path=td / "wfc.json",
            event_log_path=td / "events.jsonl",
            live_overrides_path=td / "live_engine_overrides.json",
            dry_run=False,
            force=False,
            operator="tester",
            gate_name=gate_name,
            new_value=new_value,
        )
        for k, v in overrides.items():
            setattr(defaults, k, v)
        return defaults

    def _seed_wfc(self, td: Path, gate_name: str, verdict_label: str, current_threshold: float = 0.22):
        (td / "wfc.json").write_text(
            json.dumps({
                "readiness": {"label": "READY", "n_filled": 200, "n_dates": 35},
                "gates": [
                    {
                        "name": gate_name,
                        "current_threshold": current_threshold,
                        "verdict": {
                            "verdict": verdict_label,
                            "recommended_threshold": 0.20,
                            "reason": "test reason",
                        },
                    },
                ],
            }),
            encoding="utf-8",
        )

    def test_blocks_when_gate_keeps(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            self._seed_wfc(td, "gate_extreme_edge", "KEEP")
            args = self._build_args(td, "gate_extreme_edge", "0.20")
            rc = promote.cmd_gate_threshold(args)
            self.assertEqual(rc, 1)
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(rows[0]["action"], "blocked")

    def test_promotes_when_gate_retunes(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            self._seed_wfc(td, "gate_extreme_edge", "RETUNE")
            args = self._build_args(td, "gate_extreme_edge", "0.20")
            rc = promote.cmd_gate_threshold(args)
            self.assertEqual(rc, 0)
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(rows[0]["action"], "promoted")
            self.assertEqual(rows[0]["lever"], "gate_threshold")
            self.assertEqual(rows[0]["from_state"]["threshold"], 0.22)
            self.assertEqual(rows[0]["to_state"]["threshold"], "0.20")
            self.assertEqual(rows[0]["to_state"]["cli_flag"], "--extreme-edge-max")

    def test_blocks_unknown_gate(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            self._seed_wfc(td, "gate_extreme_edge", "RETUNE")
            args = self._build_args(td, "no_such_gate", "0.20")
            rc = promote.cmd_gate_threshold(args)
            self.assertEqual(rc, 2)
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(rows[0]["action"], "blocked")
            self.assertIn("not found", rows[0]["block_reason"])

    def test_blocks_gate_without_cli_flag_mapping(self):
        # A gate name walk-forward might emit but _GATE_CLI_FLAGS doesn't know.
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            self._seed_wfc(td, "gate_some_new_thing", "RETUNE")
            args = self._build_args(td, "gate_some_new_thing", "0.5")
            rc = promote.cmd_gate_threshold(args)
            self.assertEqual(rc, 2)
            rows = _read_event_log(td / "events.jsonl")
            self.assertIn("CLI flag", rows[0]["block_reason"])

    def test_force_overrides_keep(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            self._seed_wfc(td, "gate_extreme_edge", "KEEP")
            args = self._build_args(td, "gate_extreme_edge", "0.20", force=True)
            rc = promote.cmd_gate_threshold(args)
            self.assertEqual(rc, 0)
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(rows[0]["action"], "forced")


class CliEntryPointTests(unittest.TestCase):
    """Light smoke tests on parse_args + main dispatch."""

    def test_status_subcommand_parses(self):
        args = promote.parse_args(["status"])
        self.assertEqual(args.lever, "status")
        self.assertIs(args.handler, promote.cmd_status)

    def test_stage2_subcommand_parses_with_defaults(self):
        args = promote.parse_args(["stage2"])
        self.assertEqual(args.lever, "stage2")
        self.assertIs(args.handler, promote.cmd_stage2)
        self.assertFalse(args.dry_run)
        self.assertFalse(args.force)

    def test_gate_threshold_requires_two_positional_args(self):
        # Missing positional args must fail argparse, not silently succeed
        with self.assertRaises(SystemExit):
            promote.parse_args(["gate-threshold"])
        with self.assertRaises(SystemExit):
            promote.parse_args(["gate-threshold", "gate_extreme_edge"])
        # Two positionals OK
        args = promote.parse_args(["gate-threshold", "gate_extreme_edge", "0.2"])
        self.assertEqual(args.gate_name, "gate_extreme_edge")
        self.assertEqual(args.new_value, "0.2")

    def test_gate_cli_flag_mapping_exists_for_documented_gates(self):
        # The walk-forward certification report's enforced gates must all
        # have a documented CLI flag mapping; otherwise gate-threshold
        # would always block.
        for gate in ("gate_extreme_edge", "gate_min_edge", "gate_min_inning",
                     "gate_min_entry_ask", "gate_runs_needed_max"):
            self.assertIsNotNone(
                promote._gate_cli_flag(gate),
                f"missing CLI flag mapping for {gate}",
            )


def _write_session(td: Path, date: str, bets: list) -> None:
    """Write a minimal session JSON with the given bets at td/{date}_session.json."""
    p = td / f"{date}_session.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": date, "mode": "live", "bets": bets}), encoding="utf-8")


def _filled_bet(profit: float, *, stake: float = 8.0, multiplier: float = 1.0) -> dict:
    return {
        "placement_mode": "live",
        "order_status": "filled",
        "profit": profit,
        "fill_cost": stake,
        "stake": stake,
        "calibrated_stake_multiplier": multiplier,
    }


def _seed_pre_post_sessions(
    sessions_dir: Path,
    *,
    promo_date: str,
    pre_bets_per_day: list,
    post_bets_per_day: list,
    window_days: int = 14,
) -> None:
    """Write daily session JSONs with the given bet outcomes for K days
    on each side of `promo_date`. Used to drive demotion verdict tests.

    `pre_bets_per_day` and `post_bets_per_day` are lists of bet dicts
    that get written N times (one copy per day in the window).
    """
    sessions_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime as _dt, timedelta as _td
    promo_dt = _dt.strptime(promo_date, "%Y-%m-%d")
    for i in range(1, window_days + 1):
        d = (promo_dt - _td(days=i)).strftime("%Y-%m-%d")
        _write_session(sessions_dir, d, list(pre_bets_per_day))
    for i in range(0, window_days):
        d = (promo_dt + _td(days=i)).strftime("%Y-%m-%d")
        _write_session(sessions_dir, d, list(post_bets_per_day))


class BackupOnPromoteTests(unittest.TestCase):
    """When promote.py stage2 swaps files, the prior production must be
    backed up to <file>.prior_promote.json so demotion can restore it."""

    def test_backup_prior_production_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            absent = td / "nonexistent.json"
            self.assertIsNone(promote._backup_prior_production(absent))

    def test_backup_creates_sibling_with_prior_promote_suffix(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            prod = td / "mlb_stage2_run_env.json"
            prod.write_text('{"validation_metrics": {"7.5": {"stage2_brier": 0.22}}}', encoding="utf-8")
            backup = promote._backup_prior_production(prod)
            self.assertIsNotNone(backup)
            self.assertTrue(backup.exists())
            self.assertEqual(backup.name, "mlb_stage2_run_env.json.prior_promote.json")
            self.assertEqual(backup.read_text(), prod.read_text())

    def test_stage2_promote_writes_backup_and_records_path(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "cache").mkdir()
            (td / "cache" / "production.json").write_text(
                json.dumps({"validation_metrics": {"7.5": {"stage2_brier": 0.220}}}),
                encoding="utf-8",
            )
            (td / "cache" / "staging.json").write_text(
                json.dumps({"validation_metrics": {"7.5": {"stage2_brier": 0.210}}}),
                encoding="utf-8",
            )
            history = [_stage2_history_row(f"2026-05-0{d}", -0.005) for d in range(1, 7)]
            _write_history(td / "history.jsonl", history)
            args = SimpleNamespace(
                stage2_brier_history_path=td / "history.jsonl",
                stage2_staging_path=td / "cache" / "staging.json",
                stage2_cache_path=td / "cache" / "production.json",
                event_log_path=td / "events.jsonl",
                dry_run=False, force=False, operator="tester",
            )
            rc = promote.cmd_stage2(args)
            self.assertEqual(rc, 0)
            # Backup was created
            backup = td / "cache" / "production.json.prior_promote.json"
            self.assertTrue(backup.exists(), "backup file must exist after promotion")
            # Backup matches the OLD production (Brier 0.22)
            backed = json.loads(backup.read_text())
            self.assertEqual(backed["validation_metrics"]["7.5"]["stage2_brier"], 0.220)
            # New production is now the staging (Brier 0.21)
            new_prod = json.loads((td / "cache" / "production.json").read_text())
            self.assertEqual(new_prod["validation_metrics"]["7.5"]["stage2_brier"], 0.210)
            # Event row records the backup path
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(rows[0]["backup_path"], str(backup))
            self.assertEqual(rows[0]["direction"], "promote")


class DemotionVerdictTests(unittest.TestCase):
    def test_no_promotion_returns_no_promotion_to_demote(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            verdict = promote.stage2_demotion_verdict(events=[], sessions_dir=td)
            self.assertEqual(verdict["verdict"], "no_promotion_to_demote")

    def test_demote_fires_when_post_roi_regresses(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            # Pre-promotion: 14 days of profitable bets (10 per day -> 140 total).
            # Post-promotion: 14 days of losing bets.
            _seed_pre_post_sessions(
                td,
                promo_date="2026-05-01",
                pre_bets_per_day=[_filled_bet(+2.0) for _ in range(2)],   # +25% ROI
                post_bets_per_day=[_filled_bet(-8.0) for _ in range(2)],  # -100% ROI
            )
            events = [{
                "lever": "stage2",
                "action": "promoted",
                "direction": "promote",
                "generated_at_utc": "2026-05-01T12:00:00Z",
                "operator": "tester",
            }]
            verdict = promote.stage2_demotion_verdict(events=events, sessions_dir=td)
            self.assertEqual(verdict["verdict"], "demote")
            self.assertLess(verdict["roi_delta"], promote.DEMOTE_ROI_REGRESSION_THRESHOLD)

    def test_hold_when_post_outcomes_similar_or_better(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            _seed_pre_post_sessions(
                td,
                promo_date="2026-05-01",
                pre_bets_per_day=[_filled_bet(+2.0) for _ in range(2)],
                post_bets_per_day=[_filled_bet(+2.0) for _ in range(2)],  # same
            )
            events = [{
                "lever": "stage2",
                "action": "promoted",
                "direction": "promote",
                "generated_at_utc": "2026-05-01T12:00:00Z",
                "operator": "tester",
            }]
            verdict = promote.stage2_demotion_verdict(events=events, sessions_dir=td)
            self.assertEqual(verdict["verdict"], "hold")

    def test_insufficient_post_data_when_window_unfilled(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            # Only pre window has bets, post is empty
            _seed_pre_post_sessions(
                td,
                promo_date="2026-05-01",
                pre_bets_per_day=[_filled_bet(+2.0) for _ in range(2)],
                post_bets_per_day=[],
            )
            events = [{
                "lever": "stage2",
                "action": "promoted",
                "direction": "promote",
                "generated_at_utc": "2026-05-01T12:00:00Z",
                "operator": "tester",
            }]
            verdict = promote.stage2_demotion_verdict(events=events, sessions_dir=td)
            self.assertEqual(verdict["verdict"], "insufficient_post_data")

    def test_demotion_excludes_paper_fallback_bets(self):
        # Paper-fallback bets have no real-money P&L; they must not skew
        # the verdict.
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            real_bets = [_filled_bet(+2.0) for _ in range(2)]
            paper_bets = [{**_filled_bet(-8.0), "placement_mode": "paper_fallback"}]
            _seed_pre_post_sessions(
                td,
                promo_date="2026-05-01",
                pre_bets_per_day=real_bets,
                post_bets_per_day=real_bets + paper_bets,  # losses are paper -- ignore
            )
            events = [{
                "lever": "stage2",
                "action": "promoted",
                "direction": "promote",
                "generated_at_utc": "2026-05-01T12:00:00Z",
                "operator": "tester",
            }]
            verdict = promote.stage2_demotion_verdict(events=events, sessions_dir=td)
            # Both pre and post real-money bets are profitable -> hold
            self.assertEqual(verdict["verdict"], "hold")

    def test_stake_scaling_filters_to_multiplier_affected_bets(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            # Bets with multiplier=1.0 should be ignored by stake-scaling
            # filter; only multiplier!=1.0 bets count.
            ignored = [_filled_bet(+2.0, multiplier=1.0) for _ in range(2)]
            counted_pre = [_filled_bet(+2.0, multiplier=1.5) for _ in range(2)]
            counted_post = [_filled_bet(-8.0, multiplier=1.5) for _ in range(2)]
            _seed_pre_post_sessions(
                td,
                promo_date="2026-05-01",
                pre_bets_per_day=ignored + counted_pre,
                post_bets_per_day=ignored + counted_post,
            )
            events = [{
                "lever": "stake_scaling",
                "action": "promoted",
                "direction": "promote",
                "generated_at_utc": "2026-05-01T12:00:00Z",
                "operator": "tester",
            }]
            verdict = promote.stake_scaling_demotion_verdict(events=events, sessions_dir=td)
            self.assertEqual(verdict["verdict"], "demote")

    def test_latest_promotion_event_picks_most_recent_for_lever(self):
        events = [
            {"lever": "stage2", "action": "promoted", "direction": "promote",
             "generated_at_utc": "2026-04-01T12:00:00Z"},
            {"lever": "stage2", "action": "promoted", "direction": "promote",
             "generated_at_utc": "2026-05-01T12:00:00Z"},
            {"lever": "stage3_v2", "action": "promoted", "direction": "promote",
             "generated_at_utc": "2026-05-15T12:00:00Z"},
        ]
        latest = promote.latest_promotion_event_for_lever(events, "stage2")
        self.assertIsNotNone(latest)
        self.assertEqual(latest["generated_at_utc"], "2026-05-01T12:00:00Z")

    def test_latest_excludes_demote_rows(self):
        # Demote rows must not be picked as "latest promotion".
        events = [
            {"lever": "stage2", "action": "promoted", "direction": "promote",
             "generated_at_utc": "2026-05-01T12:00:00Z"},
            {"lever": "stage2", "action": "demoted", "direction": "demote",
             "generated_at_utc": "2026-05-10T12:00:00Z"},
        ]
        latest = promote.latest_promotion_event_for_lever(events, "stage2")
        self.assertEqual(latest["generated_at_utc"], "2026-05-01T12:00:00Z")
        self.assertEqual(latest["action"], "promoted")

    def test_latest_treats_missing_direction_as_promote_for_back_compat(self):
        events = [
            # Old row (pre-2026-05-15) lacks the direction field
            {"lever": "stage2", "action": "promoted",
             "generated_at_utc": "2026-05-01T12:00:00Z"},
        ]
        latest = promote.latest_promotion_event_for_lever(events, "stage2")
        self.assertIsNotNone(latest)


class DemoteSubcommandTests(unittest.TestCase):
    """End-to-end tests for the demote subcommands."""

    def _build_args(self, td: Path, **overrides):
        defaults = SimpleNamespace(
            stage2_cache_path=td / "cache" / "production.json",
            sessions_dir=td / "sessions",
            event_log_path=td / "events.jsonl",
            dry_run=False, force=False, operator="tester",
        )
        for k, v in overrides.items():
            setattr(defaults, k, v)
        return defaults

    def _seed_promotion(self, td: Path, lever: str, **extra):
        """Append a promotion event so demote subcommands have something
        to work with. Returns the event log path."""
        log = td / "events.jsonl"
        ev = {
            "lever": lever,
            "action": "promoted",
            "direction": "promote",
            "generated_at_utc": "2026-05-01T12:00:00Z",
            "operator": "tester",
            **extra,
        }
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev) + "\n")

    def test_demote_stage2_blocks_when_no_promotion(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "cache").mkdir()
            (td / "cache" / "production.json").write_text("{}", encoding="utf-8")
            (td / "sessions").mkdir()
            args = self._build_args(td)
            rc = promote.cmd_demote_stage2(args)
            self.assertEqual(rc, 1)
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(rows[0]["action"], "blocked")
            self.assertEqual(rows[0]["direction"], "demote")

    def test_demote_stage2_restores_from_backup_when_verdict_says_demote(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "cache").mkdir()
            # New production (post-promotion content)
            (td / "cache" / "production.json").write_text(
                '{"new": "weights"}', encoding="utf-8",
            )
            # Backup (the prior content we want to restore)
            backup = td / "cache" / "production.json.prior_promote.json"
            backup.write_text('{"prior": "weights"}', encoding="utf-8")
            # Seed a promotion event with the backup path
            self._seed_promotion(td, "stage2", backup_path=str(backup))
            # Seed pre/post sessions to drive verdict='demote'
            _seed_pre_post_sessions(
                td / "sessions",
                promo_date="2026-05-01",
                pre_bets_per_day=[_filled_bet(+2.0) for _ in range(2)],
                post_bets_per_day=[_filled_bet(-8.0) for _ in range(2)],
            )
            args = self._build_args(td)
            rc = promote.cmd_demote_stage2(args)
            self.assertEqual(rc, 0)
            # Production now has the prior content
            self.assertEqual(
                json.loads((td / "cache" / "production.json").read_text()),
                {"prior": "weights"},
            )
            # Event log has the demote row
            rows = _read_event_log(td / "events.jsonl")
            demote_rows = [r for r in rows if r.get("direction") == "demote"]
            self.assertEqual(len(demote_rows), 1)
            self.assertEqual(demote_rows[0]["action"], "demoted")

    def test_demote_stage2_dry_run_does_not_restore_but_logs(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "cache").mkdir()
            (td / "cache" / "production.json").write_text('{"new": "weights"}', encoding="utf-8")
            backup = td / "cache" / "production.json.prior_promote.json"
            backup.write_text('{"prior": "weights"}', encoding="utf-8")
            self._seed_promotion(td, "stage2", backup_path=str(backup))
            _seed_pre_post_sessions(
                td / "sessions",
                promo_date="2026-05-01",
                pre_bets_per_day=[_filled_bet(+2.0) for _ in range(2)],
                post_bets_per_day=[_filled_bet(-8.0) for _ in range(2)],
            )
            args = self._build_args(td, dry_run=True)
            rc = promote.cmd_demote_stage2(args)
            self.assertEqual(rc, 0)
            # Production file UNCHANGED
            self.assertEqual(
                json.loads((td / "cache" / "production.json").read_text()),
                {"new": "weights"},
            )
            rows = _read_event_log(td / "events.jsonl")
            demote_rows = [r for r in rows if r.get("direction") == "demote"]
            self.assertEqual(demote_rows[0]["action"], "dry_run")

    def test_demote_stage2_handles_missing_backup_gracefully(self):
        # First-promotion case: no backup file exists. Demote should
        # delete the production file (next refresh will rebuild from
        # staging via the sanity guard).
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "cache").mkdir()
            (td / "cache" / "production.json").write_text('{"new": "weights"}', encoding="utf-8")
            self._seed_promotion(td, "stage2", backup_path=None)
            _seed_pre_post_sessions(
                td / "sessions",
                promo_date="2026-05-01",
                pre_bets_per_day=[_filled_bet(+2.0) for _ in range(2)],
                post_bets_per_day=[_filled_bet(-8.0) for _ in range(2)],
            )
            args = self._build_args(td)
            rc = promote.cmd_demote_stage2(args)
            self.assertEqual(rc, 0)
            # Production deleted
            self.assertFalse((td / "cache" / "production.json").exists())

    def test_demote_force_overrides_no_promotion_block(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "cache").mkdir()
            (td / "cache" / "production.json").write_text('{"new": "weights"}', encoding="utf-8")
            (td / "sessions").mkdir()
            # No promotion event in the log
            args = self._build_args(td, force=True)
            rc = promote.cmd_demote_stage2(args)
            self.assertEqual(rc, 0)  # forced through despite no promotion
            rows = _read_event_log(td / "events.jsonl")
            self.assertEqual(rows[0]["action"], "forced")
            self.assertEqual(rows[0]["direction"], "demote")

    def test_demote_stake_scaling_prints_recommendation_and_logs(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "sessions").mkdir()
            self._seed_promotion(td, "stake_scaling")
            _seed_pre_post_sessions(
                td / "sessions",
                promo_date="2026-05-01",
                pre_bets_per_day=[_filled_bet(+2.0, multiplier=1.5) for _ in range(2)],
                post_bets_per_day=[_filled_bet(-8.0, multiplier=1.5) for _ in range(2)],
            )
            args = SimpleNamespace(
                sessions_dir=td / "sessions",
                event_log_path=td / "events.jsonl",
                live_overrides_path=td / "live_engine_overrides.json",
                dry_run=False, force=False, operator="tester",
            )
            rc = promote.cmd_demote_stake_scaling(args)
            self.assertEqual(rc, 0)
            rows = _read_event_log(td / "events.jsonl")
            demote_rows = [r for r in rows if r.get("direction") == "demote"]
            self.assertEqual(demote_rows[0]["action"], "demoted")
            self.assertEqual(
                demote_rows[0]["from_state"]["calibrated_stake_scale_mode"], "enforce",
            )
            self.assertEqual(
                demote_rows[0]["to_state"]["calibrated_stake_scale_mode"], "shadow",
            )

    def test_demote_gate_threshold_uses_prior_threshold_from_event(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "sessions").mkdir()
            # Promotion event records prior threshold = 0.30 (the value
            # we want to revert to).
            self._seed_promotion(
                td, "gate_threshold",
                from_state={"threshold": 0.30},
                to_state={"threshold": "0.22", "cli_flag": "--extreme-edge-max"},
            )
            _seed_pre_post_sessions(
                td / "sessions",
                promo_date="2026-05-01",
                pre_bets_per_day=[_filled_bet(+2.0) for _ in range(2)],
                post_bets_per_day=[_filled_bet(-8.0) for _ in range(2)],
            )
            args = SimpleNamespace(
                gate_name="gate_extreme_edge",
                to_value=None,  # default to prior_threshold from event
                sessions_dir=td / "sessions",
                event_log_path=td / "events.jsonl",
                live_overrides_path=td / "live_engine_overrides.json",
                dry_run=False, force=False, operator="tester",
            )
            rc = promote.cmd_demote_gate_threshold(args)
            self.assertEqual(rc, 0)
            rows = _read_event_log(td / "events.jsonl")
            demote_rows = [r for r in rows if r.get("direction") == "demote"]
            self.assertEqual(demote_rows[0]["to_state"]["threshold"], 0.30)
            self.assertEqual(demote_rows[0]["to_state"]["cli_flag"], "--extreme-edge-max")


class StatusShowsDemotionTests(unittest.TestCase):
    def test_status_includes_demotion_section_with_no_alerts_when_no_promotions(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            args = SimpleNamespace(
                stage2_brier_history_path=td / "s2.jsonl",
                stage3_v2_drift_history_path=td / "s3.jsonl",
                stake_scaling_report_path=td / "ss.json",
                walk_forward_cert_path=td / "wfc.json",
                event_log_path=td / "events.jsonl",
                sessions_dir=td / "sessions",
            )
            (td / "sessions").mkdir()
            rc = promote.cmd_status(args)
            self.assertEqual(rc, 0)


class AuditEventDirectionFieldTests(unittest.TestCase):
    def test_promotion_event_direction_defaults_to_promote(self):
        ev = promote.PromotionEvent(lever="stage2", action="promoted", operator="x")
        self.assertEqual(ev.to_row()["direction"], "promote")

    def test_promotion_event_direction_demote_explicit(self):
        ev = promote.PromotionEvent(
            lever="stage2", action="demoted", operator="x", direction="demote",
        )
        self.assertEqual(ev.to_row()["direction"], "demote")


if __name__ == "__main__":
    unittest.main()
