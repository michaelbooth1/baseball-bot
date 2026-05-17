"""Tests for `scripts/trading/live_engine_overrides.py`.

The runtime-overrides config file is the mechanism that lets the
auto-promote/demote daemon actuate stake-scaling (and lets promote.py
gate-threshold persist a value) without an operator typing CLI flags.

Test surface focuses on the safety properties:
  - missing file: silent no-op (no error)
  - malformed JSON / wrong version: WARN + empty dict (no exception)
  - apply_overrides respects operator's explicit CLI flag (CLI wins)
  - apply_overrides applies known keys, ignores unknown keys
  - set_override / remove_override write atomically with backup
  - restore_overrides_from_backup falls back to remove when no backup
  - Schema round-trip via load -> apply -> live_args mutation
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
if str(TRADING_DIR) not in sys.path:
    sys.path.insert(0, str(TRADING_DIR))

import live_engine_overrides as overrides  # noqa: E402


class LoadOverridesTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            self.assertEqual(overrides.load_overrides(td / "missing.json"), {})

    def test_well_formed_file_returns_payload(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "ov.json"
            p.write_text(json.dumps({
                "version": 1,
                "calibrated_stake_scale_mode": "enforce",
            }), encoding="utf-8")
            data = overrides.load_overrides(p)
            self.assertEqual(data["calibrated_stake_scale_mode"], "enforce")

    def test_malformed_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "ov.json"
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(overrides.load_overrides(p), {})

    def test_wrong_version_returns_empty(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "ov.json"
            p.write_text(json.dumps({"version": 99}), encoding="utf-8")
            self.assertEqual(overrides.load_overrides(p), {})

    def test_top_level_array_returns_empty(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "ov.json"
            p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
            self.assertEqual(overrides.load_overrides(p), {})


class PassedFlagsTests(unittest.TestCase):
    def test_basic_long_flags(self):
        self.assertEqual(
            overrides.passed_flags(["--foo", "bar", "--baz"]),
            {"--foo", "--baz"},
        )

    def test_equals_form(self):
        self.assertIn("--foo", overrides.passed_flags(["--foo=value"]))

    def test_short_flags_ignored(self):
        self.assertEqual(overrides.passed_flags(["-h", "value"]), set())

    def test_empty_argv(self):
        self.assertEqual(overrides.passed_flags([]), set())


class ApplyOverridesTests(unittest.TestCase):
    def _build_namespaces(self):
        live = argparse.Namespace(
            calibrated_stake_scale_mode="shadow",
        )
        trade = argparse.Namespace(
            extreme_edge_max=0.22,
            edge_threshold=0.10,
            min_inning=5,
            min_entry_ask=0.55,
            runs_needed_max=4.0,
        )
        return live, trade

    def test_applies_top_level_override(self):
        live, trade = self._build_namespaces()
        notes = overrides.apply_overrides(
            live_args=live, trade_args=trade,
            overrides={"version": 1, "calibrated_stake_scale_mode": "enforce"},
            passed=set(),
        )
        self.assertEqual(live.calibrated_stake_scale_mode, "enforce")
        self.assertTrue(any("calibrated_stake_scale_mode" in n for n in notes))

    def test_applies_gate_threshold_override(self):
        live, trade = self._build_namespaces()
        overrides.apply_overrides(
            live_args=live, trade_args=trade,
            overrides={
                "version": 1,
                "gate_thresholds": {"gate_extreme_edge": 0.20, "gate_min_inning": 6},
            },
            passed=set(),
        )
        self.assertEqual(trade.extreme_edge_max, 0.20)
        self.assertEqual(trade.min_inning, 6)
        # Untouched keys remain at default
        self.assertEqual(trade.edge_threshold, 0.10)

    def test_explicit_cli_flag_wins_over_override(self):
        live, trade = self._build_namespaces()
        notes = overrides.apply_overrides(
            live_args=live, trade_args=trade,
            overrides={
                "version": 1,
                "calibrated_stake_scale_mode": "enforce",
                "gate_thresholds": {"gate_extreme_edge": 0.20},
            },
            passed={"--calibrated-stake-scale-mode", "--extreme-edge-max"},
        )
        # Operator's CLI value wins
        self.assertEqual(live.calibrated_stake_scale_mode, "shadow")
        self.assertEqual(trade.extreme_edge_max, 0.22)
        # And both keys are noted as skipped
        self.assertTrue(any("operator passed --calibrated-stake-scale-mode" in n for n in notes))
        self.assertTrue(any("operator passed --extreme-edge-max" in n for n in notes))

    def test_unknown_top_level_key_warns_no_mutation(self):
        live, trade = self._build_namespaces()
        notes = overrides.apply_overrides(
            live_args=live, trade_args=trade,
            overrides={"version": 1, "not_a_real_key": 42},
            passed=set(),
        )
        self.assertTrue(any("not_a_real_key" in n for n in notes))

    def test_unknown_gate_threshold_key_warns_no_mutation(self):
        live, trade = self._build_namespaces()
        notes = overrides.apply_overrides(
            live_args=live, trade_args=trade,
            overrides={
                "version": 1,
                "gate_thresholds": {"gate_not_real": 0.5},
            },
            passed=set(),
        )
        self.assertTrue(any("gate_not_real" in n for n in notes))


class SetOverrideTests(unittest.TestCase):
    def test_writes_new_file_with_top_level_key(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "ov.json"
            backup, payload = overrides.set_override(
                path=p, operator="tester",
                top_level={"calibrated_stake_scale_mode": "enforce"},
            )
            self.assertIsNone(backup)  # no prior file
            self.assertEqual(payload["calibrated_stake_scale_mode"], "enforce")
            self.assertTrue(p.exists())
            on_disk = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["version"], 1)
            self.assertEqual(on_disk["_audit"]["last_modified_by"], "tester")

    def test_backup_pre_write_when_file_exists(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "ov.json"
            # Pre-existing file
            p.write_text(json.dumps({"version": 1, "calibrated_stake_scale_mode": "shadow"}),
                         encoding="utf-8")
            backup, _ = overrides.set_override(
                path=p, operator="tester",
                top_level={"calibrated_stake_scale_mode": "enforce"},
            )
            self.assertIsNotNone(backup)
            self.assertTrue(backup.exists())
            backup_data = json.loads(backup.read_text(encoding="utf-8"))
            self.assertEqual(backup_data["calibrated_stake_scale_mode"], "shadow")

    def test_set_override_preserves_unrelated_keys(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "ov.json"
            # Seed with stake-scaling enforce
            overrides.set_override(
                path=p, operator="t",
                top_level={"calibrated_stake_scale_mode": "enforce"},
            )
            # Now add a gate-threshold override
            _, payload = overrides.set_override(
                path=p, operator="t",
                gate_thresholds={"gate_extreme_edge": 0.20},
            )
            self.assertEqual(payload["calibrated_stake_scale_mode"], "enforce")
            self.assertEqual(payload["gate_thresholds"]["gate_extreme_edge"], 0.20)

    def test_set_override_rejects_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "ov.json"
            with self.assertRaises(ValueError):
                overrides.set_override(
                    path=p, operator="t",
                    top_level={"not_a_real_key": "x"},
                )
            with self.assertRaises(ValueError):
                overrides.set_override(
                    path=p, operator="t",
                    gate_thresholds={"gate_imaginary": 0.5},
                )


class RemoveOverrideTests(unittest.TestCase):
    def test_removes_top_level_key(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "ov.json"
            overrides.set_override(
                path=p, operator="t",
                top_level={"calibrated_stake_scale_mode": "enforce"},
                gate_thresholds={"gate_extreme_edge": 0.20},
            )
            _, payload = overrides.remove_override(
                path=p, operator="t",
                top_level_keys=["calibrated_stake_scale_mode"],
            )
            self.assertNotIn("calibrated_stake_scale_mode", payload)
            self.assertEqual(payload["gate_thresholds"]["gate_extreme_edge"], 0.20)

    def test_removes_gate_threshold_key(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "ov.json"
            overrides.set_override(
                path=p, operator="t",
                gate_thresholds={"gate_extreme_edge": 0.20, "gate_min_inning": 6},
            )
            _, payload = overrides.remove_override(
                path=p, operator="t",
                gate_threshold_keys=["gate_extreme_edge"],
            )
            self.assertNotIn("gate_extreme_edge", payload.get("gate_thresholds", {}))
            self.assertEqual(payload["gate_thresholds"]["gate_min_inning"], 6)

    def test_removing_last_override_deletes_file(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "ov.json"
            overrides.set_override(
                path=p, operator="t",
                top_level={"calibrated_stake_scale_mode": "enforce"},
            )
            self.assertTrue(p.exists())
            _, payload = overrides.remove_override(
                path=p, operator="t",
                top_level_keys=["calibrated_stake_scale_mode"],
            )
            self.assertIsNone(payload)
            self.assertFalse(p.exists())

    def test_remove_on_missing_file_no_op(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "missing.json"
            backup, payload = overrides.remove_override(
                path=p, operator="t",
                top_level_keys=["calibrated_stake_scale_mode"],
            )
            self.assertIsNone(backup)
            self.assertIsNone(payload)


class RestoreFromBackupTests(unittest.TestCase):
    def test_restores_from_backup(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "ov.json"
            # Seed initial state -> shadow
            overrides.set_override(
                path=p, operator="t",
                top_level={"calibrated_stake_scale_mode": "shadow"},
            )
            # Promote -> enforce (backup captures shadow)
            overrides.set_override(
                path=p, operator="t",
                top_level={"calibrated_stake_scale_mode": "enforce"},
            )
            self.assertEqual(
                json.loads(p.read_text(encoding="utf-8"))["calibrated_stake_scale_mode"],
                "enforce",
            )
            # Restore from backup -> back to shadow
            restored, backup = overrides.restore_overrides_from_backup(path=p)
            self.assertTrue(restored)
            self.assertEqual(
                json.loads(p.read_text(encoding="utf-8"))["calibrated_stake_scale_mode"],
                "shadow",
            )

    def test_restore_without_backup_deletes_file(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "ov.json"
            # Write file but no backup
            p.write_text(json.dumps({"version": 1, "calibrated_stake_scale_mode": "enforce"}),
                         encoding="utf-8")
            restored, backup = overrides.restore_overrides_from_backup(path=p)
            self.assertFalse(restored)
            self.assertIsNone(backup)
            self.assertFalse(p.exists())


class EndToEndTests(unittest.TestCase):
    def test_round_trip_set_load_apply(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "ov.json"
            overrides.set_override(
                path=p, operator="auto_daemon",
                top_level={"calibrated_stake_scale_mode": "enforce"},
                gate_thresholds={"gate_extreme_edge": 0.20, "gate_min_inning": 6},
            )
            data = overrides.load_overrides(p)
            live = argparse.Namespace(calibrated_stake_scale_mode="shadow")
            trade = argparse.Namespace(extreme_edge_max=0.22, min_inning=5)
            overrides.apply_overrides(
                live_args=live, trade_args=trade,
                overrides=data, passed=set(),
            )
            self.assertEqual(live.calibrated_stake_scale_mode, "enforce")
            self.assertEqual(trade.extreme_edge_max, 0.20)
            self.assertEqual(trade.min_inning, 6)


if __name__ == "__main__":
    unittest.main()
