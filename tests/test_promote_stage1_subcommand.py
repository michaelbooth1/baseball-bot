"""Tests for the `promote.py stage1` + `promote.py demote stage1`
subcommands (2026-05-17).

Stage-1 is the only major cache without promote/demote tooling
before today. The new subcommand pair mirrors the Stage-2 pattern:
atomic file swap with backup on promote; restore-from-backup on
demote. Verdict gates on source-file existence + lineage freshness
(rather than Stage-2's Brier-history-driven verdict, because the
Stage-1 cache has no validation Brier).

Coverage:
  - _stage1_promotion_verdict semantics (5 verdict outcomes)
  - cmd_stage1: dry-run, blocked-without-force, forced override,
    successful promotion (atomic swap + backup + audit row +
    source-artifact lineage stamping)
  - cmd_demote_stage1: restore-from-backup, fallback-delete when no
    backup, dry-run
  - stage1 demotion + fast-demote verdict helpers (smoke that they
    return the standard verdict shape and degrade cleanly on no
    promotion event)
  - subcommand registration: parse_args accepts both `stage1` and
    `demote stage1`
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import promote  # noqa: E402


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_cache(
    path: Path, *,
    lineage_built_at: str | None = None,
    payload_extras: Dict[str, Any] | None = None,
) -> None:
    payload: Dict[str, Any] = {"cells": {}, "schema_version": 1}
    if payload_extras:
        payload.update(payload_extras)
    if lineage_built_at is not None:
        payload["lineage"] = {
            "schema_version": 1,
            "built_at_utc": lineage_built_at,
            "builder_path": "cache/build_mlb_ou_cache.py",
            "git_sha": "abc1234567",
            "git_branch": "main",
            "git_dirty": False,
            "input_hashes": {},
            "input_dir_summaries": {},
            "python_version": "3.11",
        }
    path.write_text(json.dumps(payload), encoding="utf-8")


class Stage1PromotionVerdictTests(unittest.TestCase):
    def test_staging_missing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            prod = tmp / "prod.json"
            _write_cache(prod, lineage_built_at=_now_iso())
            verdict = promote._stage1_promotion_verdict(
                source_path=tmp / "missing.json",
                production_path=prod,
            )
            self.assertEqual(verdict["verdict"], "staging_missing")
            self.assertIn("does not exist", verdict["verdict_reason"])

    def test_production_missing_first_promotion(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "src.json"
            _write_cache(src, lineage_built_at=_now_iso())
            verdict = promote._stage1_promotion_verdict(
                source_path=src,
                production_path=tmp / "missing_prod.json",
            )
            # Bootstrap path: production didn't exist; promote allowed
            self.assertEqual(verdict["verdict"], "promote")
            self.assertIn("first-time", verdict["verdict_reason"])

    def test_no_lineage_comparison(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "src.json"
            prod = tmp / "prod.json"
            _write_cache(src, lineage_built_at=None)
            _write_cache(prod, lineage_built_at=None)
            verdict = promote._stage1_promotion_verdict(
                source_path=src, production_path=prod,
            )
            self.assertEqual(verdict["verdict"], "no_lineage_comparison")

    def test_source_older_than_production(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "src.json"
            prod = tmp / "prod.json"
            _write_cache(src, lineage_built_at="2026-05-01T00:00:00Z")
            _write_cache(prod, lineage_built_at="2026-05-15T00:00:00Z")
            verdict = promote._stage1_promotion_verdict(
                source_path=src, production_path=prod,
            )
            self.assertEqual(verdict["verdict"], "source_older_than_production")
            self.assertIn("downgrade", verdict["verdict_reason"].lower())

    def test_promote_when_source_newer(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "src.json"
            prod = tmp / "prod.json"
            _write_cache(src, lineage_built_at="2026-05-15T00:00:00Z")
            _write_cache(prod, lineage_built_at="2026-05-01T00:00:00Z")
            verdict = promote._stage1_promotion_verdict(
                source_path=src, production_path=prod,
            )
            self.assertEqual(verdict["verdict"], "promote")

    def test_equal_built_at_treated_as_promote(self):
        # Same timestamp -> still allowed (operator is rebuilding with
        # identical lineage, e.g. small fix to the cache contents but
        # same builder run).
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = tmp / "src.json"
            prod = tmp / "prod.json"
            ts = _now_iso()
            _write_cache(src, lineage_built_at=ts)
            _write_cache(prod, lineage_built_at=ts)
            verdict = promote._stage1_promotion_verdict(
                source_path=src, production_path=prod,
            )
            self.assertEqual(verdict["verdict"], "promote")


class CmdStage1Tests(unittest.TestCase):
    def _setup_workspace(
        self, *, with_source=True, with_production=True,
        source_newer=True,
    ) -> tuple[Path, Path, Path]:
        """Return (src_path, prod_path, event_log_path) in a temp dir.
        Caller is responsible for cleaning up the parent directory."""
        td = Path(tempfile.mkdtemp())
        src = td / "src.json"
        prod = td / "prod.json"
        log = td / "events.jsonl"
        if with_source:
            built = (
                "2026-05-15T00:00:00Z" if source_newer
                else "2026-05-01T00:00:00Z"
            )
            _write_cache(src, lineage_built_at=built,
                         payload_extras={"marker": "source"})
        if with_production:
            built = (
                "2026-05-01T00:00:00Z" if source_newer
                else "2026-05-15T00:00:00Z"
            )
            _write_cache(prod, lineage_built_at=built,
                         payload_extras={"marker": "production"})
        return src, prod, log

    def _make_args(
        self, *, src: Path, prod: Path, log: Path,
        dry_run=False, force=False,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            stage1_source_path=src,
            stage1_cache_path=prod,
            event_log_path=log,
            dry_run=dry_run,
            force=force,
            operator="test-operator",
        )

    def test_dry_run_does_not_mutate_production(self):
        src, prod, log = self._setup_workspace()
        try:
            args = self._make_args(src=src, prod=prod, log=log, dry_run=True)
            rc = promote.cmd_stage1(args)
            self.assertEqual(rc, 0)
            # Production unchanged
            prod_payload = json.loads(prod.read_text(encoding="utf-8"))
            self.assertEqual(prod_payload["marker"], "production")
            # Event log has a dry_run row
            rows = [
                json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()
                if l.strip()
            ]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["lever"], "stage1")
            self.assertEqual(rows[0]["action"], "dry_run")
            self.assertEqual(rows[0]["direction"], "promote")
        finally:
            import shutil
            shutil.rmtree(src.parent, ignore_errors=True)

    def test_blocked_when_source_older_no_force(self):
        src, prod, log = self._setup_workspace(source_newer=False)
        try:
            args = self._make_args(src=src, prod=prod, log=log)
            rc = promote.cmd_stage1(args)
            self.assertEqual(rc, 1)
            # Production unchanged
            prod_payload = json.loads(prod.read_text(encoding="utf-8"))
            self.assertEqual(prod_payload["marker"], "production")
            # Event log carries a blocked row
            rows = [
                json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()
                if l.strip()
            ]
            self.assertEqual(rows[0]["action"], "blocked")
            self.assertIn("source_older", rows[0]["block_reason"])
        finally:
            import shutil
            shutil.rmtree(src.parent, ignore_errors=True)

    def test_blocked_when_staging_missing(self):
        src, prod, log = self._setup_workspace(with_source=False)
        try:
            args = self._make_args(src=src, prod=prod, log=log)
            rc = promote.cmd_stage1(args)
            self.assertEqual(rc, 1)
        finally:
            import shutil
            shutil.rmtree(src.parent, ignore_errors=True)

    def test_successful_promotion_swaps_and_backs_up(self):
        src, prod, log = self._setup_workspace()
        try:
            args = self._make_args(src=src, prod=prod, log=log)
            rc = promote.cmd_stage1(args)
            self.assertEqual(rc, 0)
            # Production now matches source
            prod_payload = json.loads(prod.read_text(encoding="utf-8"))
            self.assertEqual(prod_payload["marker"], "source")
            # Backup file exists with prior production content
            backup = promote._backup_path(prod)
            self.assertTrue(backup.exists())
            backup_payload = json.loads(backup.read_text(encoding="utf-8"))
            self.assertEqual(backup_payload["marker"], "production")
            # Audit row stamped
            rows = [
                json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()
                if l.strip()
            ]
            self.assertEqual(rows[0]["action"], "promoted")
            self.assertEqual(rows[0]["lever"], "stage1")
            self.assertEqual(rows[0]["direction"], "promote")
            self.assertEqual(rows[0]["backup_path"], str(backup))
            # Source lineage stamped on the audit row
            self.assertIn("source_artifact_lineage", rows[0])
        finally:
            import shutil
            shutil.rmtree(src.parent, ignore_errors=True)

    def test_force_overrides_blocked_verdict(self):
        src, prod, log = self._setup_workspace(source_newer=False)
        try:
            args = self._make_args(
                src=src, prod=prod, log=log, force=True,
            )
            rc = promote.cmd_stage1(args)
            self.assertEqual(rc, 0)
            # Production swapped despite verdict=source_older
            prod_payload = json.loads(prod.read_text(encoding="utf-8"))
            self.assertEqual(prod_payload["marker"], "source")
            rows = [
                json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()
                if l.strip()
            ]
            self.assertEqual(rows[0]["action"], "forced")
        finally:
            import shutil
            shutil.rmtree(src.parent, ignore_errors=True)

    def test_first_promotion_no_backup(self):
        # No production file -> no backup, but promotion succeeds
        src, prod, log = self._setup_workspace(with_production=False)
        try:
            args = self._make_args(src=src, prod=prod, log=log)
            rc = promote.cmd_stage1(args)
            self.assertEqual(rc, 0)
            self.assertTrue(prod.exists())
            backup = promote._backup_path(prod)
            self.assertFalse(backup.exists())  # no backup created
            rows = [
                json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()
                if l.strip()
            ]
            # PromotionEvent.to_dict omits backup_path when None, so
            # the absence is itself the assertion that no backup was
            # taken on the bootstrap path.
            self.assertNotIn("backup_path", rows[0])
        finally:
            import shutil
            shutil.rmtree(src.parent, ignore_errors=True)


class CmdDemoteStage1Tests(unittest.TestCase):
    def _setup_workspace_with_promotion(
        self, *, write_backup=True,
    ) -> tuple[Path, Path, Path]:
        """Set up a workspace that already has a promotion event in
        the log + (optionally) a backup file from that prior
        promotion.

        Returns (prod_path, backup_path, event_log_path).
        """
        td = Path(tempfile.mkdtemp())
        prod = td / "prod.json"
        backup = promote._backup_path(prod)
        log = td / "events.jsonl"

        # Write current production (the one we want to demote from)
        _write_cache(prod, lineage_built_at="2026-05-15T00:00:00Z",
                     payload_extras={"marker": "current_production"})

        # Optionally write the backup of the prior production
        if write_backup:
            _write_cache(backup, lineage_built_at="2026-05-01T00:00:00Z",
                         payload_extras={"marker": "prior_production"})

        # Write a synthetic promotion event so latest_promotion_event_for_lever
        # finds it
        event = {
            "lever": "stage1",
            "action": "promoted",
            "direction": "promote",
            "operator": "test-operator",
            "side": "both",
            "generated_at_utc": "2026-05-15T00:00:00Z",
            "backup_path": str(backup) if write_backup else None,
            "verdict_snapshot": {"verdict": "promote"},
        }
        log.write_text(json.dumps(event) + "\n", encoding="utf-8")
        return prod, backup, log

    def _make_demote_args(
        self, *, prod: Path, log: Path,
        sessions_dir: Path | None = None,
        dry_run=False, force=False,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            stage1_cache_path=prod,
            event_log_path=log,
            sessions_dir=sessions_dir or prod.parent,
            dry_run=dry_run,
            force=force,
            operator="test-operator",
        )

    def test_dry_run_does_not_mutate_production(self):
        prod, backup, log = self._setup_workspace_with_promotion()
        try:
            args = self._make_demote_args(
                prod=prod, log=log, dry_run=True, force=True,
            )
            rc = promote.cmd_demote_stage1(args)
            self.assertEqual(rc, 0)
            # Production unchanged
            self.assertEqual(
                json.loads(prod.read_text(encoding="utf-8"))["marker"],
                "current_production",
            )
        finally:
            import shutil
            shutil.rmtree(prod.parent, ignore_errors=True)

    def test_force_demote_restores_from_backup(self):
        prod, backup, log = self._setup_workspace_with_promotion()
        try:
            args = self._make_demote_args(prod=prod, log=log, force=True)
            rc = promote.cmd_demote_stage1(args)
            self.assertEqual(rc, 0)
            # Production now matches the prior backup
            self.assertEqual(
                json.loads(prod.read_text(encoding="utf-8"))["marker"],
                "prior_production",
            )
            # Audit row marked as forced (verdict isn't 'demote' with no
            # real session data) and direction=demote
            rows = [
                json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()
                if l.strip()
            ]
            demote_rows = [
                r for r in rows if r.get("direction") == "demote"
            ]
            self.assertEqual(len(demote_rows), 1)
            self.assertIn(demote_rows[0]["action"], ("demoted", "forced"))
        finally:
            import shutil
            shutil.rmtree(prod.parent, ignore_errors=True)

    def test_force_demote_with_no_backup_deletes_production(self):
        prod, backup, log = self._setup_workspace_with_promotion(
            write_backup=False,
        )
        try:
            args = self._make_demote_args(prod=prod, log=log, force=True)
            rc = promote.cmd_demote_stage1(args)
            self.assertEqual(rc, 0)
            # Production deleted (next refresh will rebuild)
            self.assertFalse(prod.exists())
        finally:
            import shutil
            shutil.rmtree(prod.parent, ignore_errors=True)


class Stage1DemotionVerdictTests(unittest.TestCase):
    """Smoke tests on the verdict helpers (delegate to shared
    _per_lever_*_demote_verdict; here we just verify the wiring)."""

    def test_no_promotion_to_demote(self):
        with tempfile.TemporaryDirectory() as td:
            verdict = promote.stage1_demotion_verdict(
                events=[], sessions_dir=Path(td),
            )
            self.assertEqual(verdict["verdict"], "no_promotion_to_demote")
            self.assertEqual(verdict["lever"], "stage1")

    def test_fast_demote_no_promotion(self):
        with tempfile.TemporaryDirectory() as td:
            verdict = promote.stage1_fast_demote_verdict(
                events=[], sessions_dir=Path(td),
            )
            self.assertEqual(verdict["verdict"], "no_promotion_to_demote")
            self.assertEqual(verdict["lever"], "stage1")


class SubcommandRegistrationTests(unittest.TestCase):
    """Confirm parse_args accepts the new subcommand pair."""

    def test_promote_stage1_parses(self):
        args = promote.parse_args([
            "stage1",
            "--stage1-source-path", "/tmp/src.json",
            "--stage1-cache-path", "/tmp/prod.json",
            "--dry-run",
        ])
        self.assertEqual(args.lever, "stage1")
        self.assertEqual(args.handler.__name__, "cmd_stage1")

    def test_demote_stage1_parses(self):
        args = promote.parse_args([
            "demote", "stage1",
            "--stage1-cache-path", "/tmp/prod.json",
            "--dry-run",
        ])
        self.assertEqual(args.demote_lever, "stage1")
        self.assertEqual(args.handler.__name__, "cmd_demote_stage1")

    def test_status_includes_stage1_args(self):
        # Status's own argparse should accept the new stage1 paths.
        args = promote.parse_args([
            "status",
            "--stage1-source-path", "/tmp/src.json",
            "--stage1-cache-path", "/tmp/prod.json",
        ])
        self.assertEqual(args.lever, "status")
        self.assertEqual(args.handler.__name__, "cmd_status")


if __name__ == "__main__":
    unittest.main()
