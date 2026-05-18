"""Active #16 v3 (2026-05-17): startup-time lineage logging tests.

V3 added two pieces:
  1. `_log_artifact_lineage_summary` helper in signal_engine.py that
     logs a one-line INFO summary per cache artifact at boot.
  2. `format_lineage_summary_line` + `_read_lineage_from_path` +
     `_age_days` helpers in artifact_lineage.py for both startup
     logging and the daily-review cache_lineage_freshness block.

These tests verify the helpers' output format + fail-open guarantees.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
MONITOR_DIR = PROJECT_DIR / "scripts" / "monitor"
CACHE_DIR_PKG = PROJECT_DIR / "cache"
for d in (ANALYSIS_DIR, TRADING_DIR, MONITOR_DIR, CACHE_DIR_PKG):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))

import artifact_lineage as al  # noqa: E402


# Standalone helper tests (no need to import signal_engine module).
class FormatLineageSummaryLineTests(unittest.TestCase):
    @staticmethod
    def _lineage(**overrides):
        base = {
            "schema_version": 1,
            "built_at_utc": "2026-05-17T10:00:00Z",
            "builder_path": "cache/build_mlb_ou_cache.py",
            "git_sha": "abc1234567ef",
            "git_branch": "main",
            "git_dirty": False,
            "input_hashes": {
                "cache/mlb_ou_cache.json": "sha256:abcdef0123456789",
            },
            "input_dir_summaries": {
                "data/games/regular": {"n_files": 2400},
            },
            "python_version": "3.11.0",
        }
        base.update(overrides)
        return base

    def test_includes_label_and_built_at(self):
        line = al.format_lineage_summary_line("stage1", self._lineage())
        self.assertIn("stage1:", line)
        self.assertIn("built=2026-05-17T10:00:00Z", line)

    def test_includes_git_sha(self):
        line = al.format_lineage_summary_line("stage1", self._lineage(
            git_sha="deadbeef",
        ))
        self.assertIn("git=deadbeef", line)

    def test_dirty_flag_visible_in_output(self):
        line_clean = al.format_lineage_summary_line(
            "stage1", self._lineage(git_dirty=False),
        )
        line_dirty = al.format_lineage_summary_line(
            "stage1", self._lineage(git_dirty=True),
        )
        self.assertNotIn("(dirty)", line_clean)
        self.assertIn("(dirty)", line_dirty)

    def test_age_ago_when_built_at_present(self):
        # Build a lineage 5 days in the past
        five_days_ago = (
            datetime.now(timezone.utc) - timedelta(days=5)
        ).isoformat().replace("+00:00", "Z")
        line = al.format_lineage_summary_line("stage1", self._lineage(
            built_at_utc=five_days_ago,
        ))
        self.assertIn("d ago", line)

    def test_none_lineage_reports_no_lineage(self):
        line = al.format_lineage_summary_line("stage1", None)
        self.assertIn("no lineage", line)
        self.assertIn("stage1:", line)

    def test_inputs_truncated_by_max_input_summary(self):
        many_inputs = {f"path/{i}.json": f"sha256:{i:016x}" for i in range(10)}
        line = al.format_lineage_summary_line(
            "stage1", self._lineage(input_hashes=many_inputs),
            max_input_summary=2,
        )
        self.assertIn("path/0.json", line)
        self.assertIn("path/1.json", line)
        self.assertNotIn("path/9.json", line)

    def test_dir_summary_shows_n_files(self):
        line = al.format_lineage_summary_line("stage1", self._lineage(
            input_hashes={},
            input_dir_summaries={"data/games/regular": {"n_files": 1234}},
        ))
        self.assertIn("n=1234", line)


class ReadLineageFromPathTests(unittest.TestCase):
    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(
                al._read_lineage_from_path(Path(td) / "missing.json"),
            )

    def test_unreadable_json_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("not json", encoding="utf-8")
            self.assertIsNone(al._read_lineage_from_path(p))

    def test_lineage_block_present_returns_dict(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "good.json"
            p.write_text(
                json.dumps({"lineage": {"git_sha": "abc"}}),
                encoding="utf-8",
            )
            out = al._read_lineage_from_path(p)
            self.assertEqual(out, {"git_sha": "abc"})

    def test_missing_lineage_block_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "no_lineage.json"
            p.write_text(
                json.dumps({"cells": {}, "other": 1}),
                encoding="utf-8",
            )
            self.assertIsNone(al._read_lineage_from_path(p))

    def test_non_dict_payload_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "list.json"
            p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
            self.assertIsNone(al._read_lineage_from_path(p))


class AgeDaysTests(unittest.TestCase):
    def test_none_input_returns_none(self):
        self.assertIsNone(al._age_days(None))
        self.assertIsNone(al._age_days(""))

    def test_unparseable_returns_none(self):
        self.assertIsNone(al._age_days("not-a-date"))

    def test_positive_for_past_date(self):
        five_days_ago = (
            datetime.now(timezone.utc) - timedelta(days=5)
        ).isoformat().replace("+00:00", "Z")
        age = al._age_days(five_days_ago)
        self.assertIsNotNone(age)
        self.assertGreater(age, 4.9)
        self.assertLess(age, 5.1)

    def test_negative_for_future_date(self):
        future = (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).isoformat().replace("+00:00", "Z")
        age = al._age_days(future)
        self.assertIsNotNone(age)
        self.assertLess(age, 0)


class StartupHelperFailOpenTests(unittest.TestCase):
    """The startup helper MUST never raise -- any error reading the
    artifact must be caught and silently downgraded. This is a
    structural contract: a broken lineage stamp cannot block engine
    boot."""

    def _get_helper(self):
        # Import signal_engine lazily; the import succeeds in the test
        # environment because we've already inserted the trading dir
        # on sys.path at module load time.
        import signal_engine  # noqa: WPS433
        return signal_engine._log_artifact_lineage_summary

    def test_helper_does_not_raise_on_missing_required_artifact(self):
        helper = self._get_helper()
        # Should NOT raise even on non-existent path with expected=True
        helper("stage1_cache", "/path/that/does/not/exist.json", expected=True)

    def test_helper_does_not_raise_on_missing_optional_artifact(self):
        helper = self._get_helper()
        helper("stage3_v2", "/path/that/does/not/exist.json", expected=False)

    def test_helper_does_not_raise_on_corrupt_json(self):
        helper = self._get_helper()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("not json {{{", encoding="utf-8")
            helper("calibrator", str(p))

    def test_helper_does_not_raise_on_none_path(self):
        helper = self._get_helper()
        helper("stage1_cache", None)

    def test_helper_emits_info_log_when_lineage_present(self):
        helper = self._get_helper()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "good.json"
            p.write_text(
                json.dumps({
                    "lineage": {
                        "built_at_utc": "2026-05-17T10:00:00Z",
                        "git_sha": "abc1234567ef",
                        "git_dirty": False,
                    },
                }),
                encoding="utf-8",
            )
            with self.assertLogs("signal_engine", level="INFO") as cm:
                helper("stage1_cache", str(p))
            output = "\n".join(cm.output)
            self.assertIn("Artifact lineage", output)
            self.assertIn("stage1_cache", output)
            self.assertIn("git=abc1234567ef", output)


if __name__ == "__main__":
    unittest.main()
