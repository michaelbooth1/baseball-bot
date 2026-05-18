"""Active #14 (2026-05-17): backup retention + PSI-history GC tests.

Two pieces under test:

  1. promote.py archives the prior `.prior_promote.json` backup into
     a sibling `<file>.prior_promote_archive/` directory + GCs that
     archive to BACKUP_ARCHIVE_KEEP (default 5) most-recent entries
     on each new promotion. The "current backup" semantic (single
     `.prior_promote.json` file that demote reads) is preserved --
     all the existing tests + the audit-row contract still work.

  2. build_concept_drift_report._write_history_rows trims
     psi_history.jsonl to PSI_HISTORY_RETENTION_DAYS (default 365)
     of rows on every append. Drift-in-drift only consumes the
     trailing 30d so older rows are pure storage cost; trimming
     keeps the file bounded.

Both are best-effort fail-open: any GC failure must NOT block the
underlying promotion / history-append pipeline.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import promote  # noqa: E402
import build_concept_drift_report as bcdr  # noqa: E402


# ---------------------------------------------------------------------------
# Backup retention tests
# ---------------------------------------------------------------------------


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class ArchiveHelperTests(unittest.TestCase):
    def test_archive_dir_is_sibling_with_suffix(self):
        prod = Path("/tmp/cache/file.json")
        self.assertEqual(
            promote._archive_dir_for(prod).name,
            "file.json.prior_promote_archive",
        )

    def test_list_archive_backups_empty_when_dir_missing(self):
        with tempfile.TemporaryDirectory() as td:
            prod = Path(td) / "file.json"
            self.assertEqual(promote._list_archive_backups(prod), [])

    def test_list_archive_backups_sorted_by_mtime_asc(self):
        with tempfile.TemporaryDirectory() as td:
            prod = Path(td) / "file.json"
            arc = promote._archive_dir_for(prod)
            arc.mkdir()
            files = []
            for i, name in enumerate(("a.json", "b.json", "c.json")):
                p = arc / name
                _write_text(p, str(i))
                # Stagger mtimes so the sort is deterministic
                ts = time.time() - (10 - i)
                os.utime(p, (ts, ts))
                files.append(p)
            listed = promote._list_archive_backups(prod)
            self.assertEqual(
                [p.name for p in listed],
                ["a.json", "b.json", "c.json"],
            )

    def test_list_archive_backups_ignores_non_json_files(self):
        with tempfile.TemporaryDirectory() as td:
            prod = Path(td) / "file.json"
            arc = promote._archive_dir_for(prod)
            arc.mkdir()
            _write_text(arc / "should_keep.json", "ok")
            _write_text(arc / "should_ignore.txt", "ignore")
            _write_text(arc / "junk.tmp", "ignore")
            listed = promote._list_archive_backups(prod)
            self.assertEqual([p.name for p in listed], ["should_keep.json"])


class GCArchiveBackupsTests(unittest.TestCase):
    def test_gc_keeps_n_most_recent(self):
        with tempfile.TemporaryDirectory() as td:
            prod = Path(td) / "file.json"
            arc = promote._archive_dir_for(prod)
            arc.mkdir()
            # Create 8 files with monotonically increasing mtimes
            for i in range(8):
                p = arc / f"backup_{i:02d}.json"
                _write_text(p, str(i))
                ts = time.time() - (100 - i)
                os.utime(p, (ts, ts))
            deleted = promote._gc_archive_backups(prod, keep=3)
            remaining = sorted(
                [p.name for p in arc.iterdir() if p.suffix == ".json"],
            )
            # 8 files, keep 3 -> 5 deleted, 3 remaining (the most recent)
            self.assertEqual(len(deleted), 5)
            self.assertEqual(
                remaining, ["backup_05.json", "backup_06.json", "backup_07.json"],
            )

    def test_gc_noop_when_under_keep(self):
        with tempfile.TemporaryDirectory() as td:
            prod = Path(td) / "file.json"
            arc = promote._archive_dir_for(prod)
            arc.mkdir()
            for i in range(3):
                _write_text(arc / f"b_{i}.json", str(i))
            deleted = promote._gc_archive_backups(prod, keep=5)
            self.assertEqual(deleted, [])
            self.assertEqual(len(list(arc.iterdir())), 3)

    def test_gc_noop_when_archive_missing(self):
        with tempfile.TemporaryDirectory() as td:
            prod = Path(td) / "file.json"
            self.assertEqual(
                promote._gc_archive_backups(prod, keep=5), [],
            )

    def test_gc_keep_zero_deletes_all(self):
        with tempfile.TemporaryDirectory() as td:
            prod = Path(td) / "file.json"
            arc = promote._archive_dir_for(prod)
            arc.mkdir()
            for i in range(3):
                _write_text(arc / f"b_{i}.json", str(i))
            deleted = promote._gc_archive_backups(prod, keep=0)
            self.assertEqual(len(deleted), 3)


class RotateExistingBackupTests(unittest.TestCase):
    def test_no_op_when_no_existing_backup(self):
        with tempfile.TemporaryDirectory() as td:
            prod = Path(td) / "file.json"
            self.assertIsNone(
                promote._rotate_existing_backup_to_archive(prod),
            )

    def test_moves_existing_backup_to_archive(self):
        with tempfile.TemporaryDirectory() as td:
            prod = Path(td) / "file.json"
            backup = promote._backup_path(prod)
            _write_text(backup, "old_backup_contents")
            archived = promote._rotate_existing_backup_to_archive(prod)
            self.assertIsNotNone(archived)
            self.assertTrue(archived.exists())
            # Original backup path no longer exists
            self.assertFalse(backup.exists())
            # Archive path is under the archive directory
            self.assertEqual(archived.parent, promote._archive_dir_for(prod))
            # Contents preserved
            self.assertEqual(
                archived.read_text(encoding="utf-8"), "old_backup_contents",
            )
            # Archive filename is timestamped (YYYYMMDDT...Z.json)
            self.assertRegex(archived.name, r"^\d{8}T\d{6}Z(?:_\d+)?\.json$")

    def test_disambiguates_collision_within_same_second(self):
        # Two rotations in quick succession should produce distinct paths
        with tempfile.TemporaryDirectory() as td:
            prod = Path(td) / "file.json"
            backup = promote._backup_path(prod)
            # First rotation
            _write_text(backup, "v1")
            arc1 = promote._rotate_existing_backup_to_archive(prod)
            # Second rotation -- force same-second by setting backup mtime
            # explicitly to the same value
            _write_text(backup, "v2")
            try:
                ts = arc1.stat().st_mtime
                os.utime(backup, (ts, ts))
            except OSError:
                pass
            arc2 = promote._rotate_existing_backup_to_archive(prod)
            self.assertIsNotNone(arc2)
            self.assertNotEqual(arc1, arc2)


class BackupPriorProductionTests(unittest.TestCase):
    """End-to-end: simulate N sequential promotions and verify
    the archive accumulates to BACKUP_ARCHIVE_KEEP entries."""

    def test_first_promotion_no_archive_entry(self):
        with tempfile.TemporaryDirectory() as td:
            prod = Path(td) / "file.json"
            _write_text(prod, "production_v1")
            backup = promote._backup_prior_production(prod)
            self.assertIsNotNone(backup)
            self.assertEqual(backup, promote._backup_path(prod))
            # No archive yet (nothing to rotate)
            archive_entries = promote._list_archive_backups(prod)
            self.assertEqual(archive_entries, [])

    def test_subsequent_promotion_rotates_to_archive(self):
        with tempfile.TemporaryDirectory() as td:
            prod = Path(td) / "file.json"
            _write_text(prod, "production_v1")
            # First promotion
            promote._backup_prior_production(prod)
            # Now production has been swapped to v2 (simulated)
            _write_text(prod, "production_v2")
            # Second promotion
            promote._backup_prior_production(prod)
            # Archive should now have 1 entry (the v1 backup)
            archive_entries = promote._list_archive_backups(prod)
            self.assertEqual(len(archive_entries), 1)
            # The current backup is v2
            self.assertEqual(
                promote._backup_path(prod).read_text(encoding="utf-8"),
                "production_v2",
            )
            # The archive holds the previous backup
            self.assertEqual(
                archive_entries[0].read_text(encoding="utf-8"),
                "production_v1",
            )

    def test_n_promotions_archive_capped_at_keep(self):
        with tempfile.TemporaryDirectory() as td:
            prod = Path(td) / "file.json"
            # Simulate 10 sequential promotions
            for i in range(10):
                _write_text(prod, f"production_v{i}")
                promote._backup_prior_production(prod)
                # Force unique timestamps for the rotations
                if i > 0:
                    arc = promote._list_archive_backups(prod)
                    if arc:
                        ts_ref = time.time() - (10 - i)
                        os.utime(arc[-1], (ts_ref, ts_ref))
            archive_entries = promote._list_archive_backups(prod)
            # Should be capped at BACKUP_ARCHIVE_KEEP
            self.assertLessEqual(
                len(archive_entries),
                promote.BACKUP_ARCHIVE_KEEP,
            )

    def test_backup_failure_does_not_block_when_prod_missing(self):
        # When production doesn't exist, returns None (no-op)
        with tempfile.TemporaryDirectory() as td:
            prod = Path(td) / "file.json"
            self.assertIsNone(promote._backup_prior_production(prod))
            # No archive directory created
            self.assertFalse(promote._archive_dir_for(prod).exists())


# ---------------------------------------------------------------------------
# PSI-history GC tests
# ---------------------------------------------------------------------------


def _write_psi_rows(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _read_psi_rows(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _psi_row(active_date: str, feature: str = "f") -> Dict[str, Any]:
    return {
        "generated_at_utc": "2026-05-17T00:00:00Z",
        "active_date": active_date,
        "feature": feature,
        "kind": "continuous",
        "metric": "PSI",
        "value": 0.1,
        "verdict": "minor",
        "current_n": 100,
        "baseline_n": 1000,
    }


class TrimPsiHistoryTests(unittest.TestCase):
    def test_missing_file_no_op(self):
        with tempfile.TemporaryDirectory() as td:
            bcdr._trim_psi_history(
                Path(td) / "missing.jsonl", retention_days=30,
            )
            # No error

    def test_empty_file_no_op(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "psi.jsonl"
            p.write_text("", encoding="utf-8")
            bcdr._trim_psi_history(p, retention_days=30)
            self.assertEqual(p.read_text(encoding="utf-8"), "")

    def test_within_retention_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "psi.jsonl"
            rows = [
                _psi_row("2026-05-15"),
                _psi_row("2026-05-16"),
                _psi_row("2026-05-17"),
            ]
            _write_psi_rows(p, rows)
            bcdr._trim_psi_history(p, retention_days=30)
            self.assertEqual(len(_read_psi_rows(p)), 3)

    def test_drops_rows_older_than_cutoff(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "psi.jsonl"
            rows = [
                _psi_row("2024-01-01"),
                _psi_row("2025-01-01"),
                _psi_row("2026-05-01"),
                _psi_row("2026-05-15"),
                _psi_row("2026-05-17"),
            ]
            _write_psi_rows(p, rows)
            # Latest = 2026-05-17; retention=30 -> cutoff 2026-04-18
            bcdr._trim_psi_history(p, retention_days=30)
            kept = _read_psi_rows(p)
            kept_dates = sorted({r["active_date"] for r in kept})
            self.assertEqual(
                kept_dates, ["2026-05-01", "2026-05-15", "2026-05-17"],
            )

    def test_anchor_on_latest_date_not_today(self):
        # Cutoff is computed from the LATEST date in the file, not from
        # today's wall-clock. Ensures the trim is stable when the
        # refresh runs on a stale corpus.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "psi.jsonl"
            rows = [
                _psi_row("2024-05-15"),
                _psi_row("2024-05-16"),
                _psi_row("2024-05-17"),  # latest
            ]
            _write_psi_rows(p, rows)
            bcdr._trim_psi_history(p, retention_days=30)
            # All 3 within 30d of latest -> all kept
            self.assertEqual(len(_read_psi_rows(p)), 3)

    def test_skips_corrupted_lines_does_not_drop_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "psi.jsonl"
            with open(p, "w", encoding="utf-8") as f:
                f.write("not json\n")
                f.write(json.dumps(_psi_row("2026-05-17")) + "\n")
                f.write("also not json\n")
            bcdr._trim_psi_history(p, retention_days=30)
            kept = _read_psi_rows(p)
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0]["active_date"], "2026-05-17")

    def test_zero_retention_no_op(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "psi.jsonl"
            _write_psi_rows(p, [_psi_row("2026-05-17")])
            bcdr._trim_psi_history(p, retention_days=0)
            # File unchanged
            self.assertEqual(len(_read_psi_rows(p)), 1)

    def test_unparseable_active_date_kept(self):
        # Rows with garbage active_date should be kept (don't drop data
        # we can't classify).
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "psi.jsonl"
            rows = [
                _psi_row("not-a-date"),
                _psi_row("2026-05-17"),
                _psi_row("2024-01-01"),
            ]
            _write_psi_rows(p, rows)
            bcdr._trim_psi_history(p, retention_days=30)
            kept = _read_psi_rows(p)
            kept_dates = sorted({r["active_date"] for r in kept})
            # 2024-01-01 dropped; unparseable preserved; 2026-05-17 kept
            self.assertIn("not-a-date", kept_dates)
            self.assertIn("2026-05-17", kept_dates)
            self.assertNotIn("2024-01-01", kept_dates)


class WriteHistoryRowsIntegrationTest(unittest.TestCase):
    """_write_history_rows now trims on every append. End-to-end: an
    append against a file with old rows leaves only retention-day rows."""

    def test_append_triggers_trim(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "psi.jsonl"
            # Pre-existing old rows
            old_rows = [_psi_row("2024-01-01"), _psi_row("2024-06-15")]
            _write_psi_rows(p, old_rows)
            # Append a fresh row via the real entry point
            payload = {
                "active_date": "2026-05-17",
                "generated_at_utc": "2026-05-17T00:00:00Z",
                "features": {
                    "feature_A": {
                        "kind": "continuous",
                        "metric": "PSI",
                        "value": 0.05,
                        "verdict": "stable",
                        "current_n": 100,
                        "baseline_n": 1000,
                    }
                },
            }
            bcdr._write_history_rows(p, payload)
            kept = _read_psi_rows(p)
            # Old 2024 rows should be trimmed; only the freshly-appended
            # row remains within the 365d window of 2026-05-17.
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0]["active_date"], "2026-05-17")
            self.assertEqual(kept[0]["feature"], "feature_A")


if __name__ == "__main__":
    unittest.main()
