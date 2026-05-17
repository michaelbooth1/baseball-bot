"""Active #16 (2026-05-17): artifact_lineage tests.

Covers:
  - compute_lineage shape + required fields
  - File hashing: deterministic, detects content changes, handles missing
  - Directory summarization: counts + mtime bounds, empty dir handling
  - Git helpers: tolerate non-git environments
  - Relative-path resolution when project_root supplied
  - Extra fields merge without shadowing canonical keys
  - promotion_lineage stamp
  - extract_lineage_from_artifact pulls + handles missing/wrong-type
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import artifact_lineage as lineage_mod  # noqa: E402


class HashFileTests(unittest.TestCase):
    def test_hashes_existing_file_deterministically(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "input.txt"
            p.write_text("hello world", encoding="utf-8")
            h1 = lineage_mod.hash_file(p)
            h2 = lineage_mod.hash_file(p)
            self.assertEqual(h1, h2)
            self.assertTrue(h1.startswith("sha256:"))

    def test_hash_detects_content_change(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "input.txt"
            p.write_text("hello world", encoding="utf-8")
            h1 = lineage_mod.hash_file(p)
            p.write_text("hello world!", encoding="utf-8")
            h2 = lineage_mod.hash_file(p)
            self.assertNotEqual(h1, h2)

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(
                lineage_mod.hash_file(Path(td) / "missing.txt"),
            )

    def test_hash_prefix_length_truncated(self):
        """Hashes are truncated to keep audit rows compact. Default is
        16 hex chars after the 'sha256:' prefix."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "input.txt"
            p.write_text("hello", encoding="utf-8")
            h = lineage_mod.hash_file(p)
            self.assertEqual(len(h), len("sha256:") + 16)


class SummarizeDirectoryTests(unittest.TestCase):
    def test_returns_none_for_missing_dir(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(
                lineage_mod.summarize_directory(Path(td) / "missing"),
            )

    def test_empty_dir_returns_zero_counts(self):
        with tempfile.TemporaryDirectory() as td:
            out = lineage_mod.summarize_directory(Path(td))
            self.assertEqual(out["n_files"], 0)
            self.assertIsNone(out["max_mtime_utc"])

    def test_counts_files_recursively(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "subdir").mkdir()
            (root / "a.txt").write_text("a")
            (root / "b.txt").write_text("b")
            (root / "subdir" / "c.txt").write_text("c")
            out = lineage_mod.summarize_directory(root)
            self.assertEqual(out["n_files"], 3)
            self.assertIsNotNone(out["max_mtime_utc"])
            self.assertIsNotNone(out["min_mtime_utc"])

    def test_min_le_max_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "old.txt").write_text("o")
            time.sleep(0.01)
            (root / "new.txt").write_text("n")
            out = lineage_mod.summarize_directory(root)
            self.assertLessEqual(out["min_mtime_utc"], out["max_mtime_utc"])


class GitHelperTests(unittest.TestCase):
    def test_git_sha_handles_non_git_dir(self):
        """In a temp directory with no git, helpers must return None,
        not raise."""
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(lineage_mod.git_sha(cwd=Path(td)))
            self.assertIsNone(lineage_mod.git_branch(cwd=Path(td)))
            self.assertIsNone(lineage_mod.git_is_dirty(cwd=Path(td)))

    def test_git_sha_against_project_root_returns_string_or_none(self):
        """In an actual repo, git_sha returns a non-empty string.
        Outside a repo, None. We just verify shape, not the value."""
        sha = lineage_mod.git_sha(cwd=PROJECT_DIR)
        if sha is not None:
            self.assertGreater(len(sha), 0)
            self.assertLessEqual(len(sha), 16)


class ComputeLineageTests(unittest.TestCase):
    def test_required_fields_present(self):
        with tempfile.TemporaryDirectory() as td:
            out = lineage_mod.compute_lineage(
                builder_path=Path(td) / "builder.py",
            )
            for key in (
                "schema_version", "built_at_utc", "builder_path",
                "git_sha", "git_branch", "git_dirty",
                "input_hashes", "input_dir_summaries", "python_version",
            ):
                self.assertIn(key, out)

    def test_relative_paths_when_project_root_supplied(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            builder = root / "scripts" / "analysis" / "builder.py"
            builder.parent.mkdir(parents=True)
            builder.write_text("# builder")
            out = lineage_mod.compute_lineage(
                builder_path=builder, project_root=root,
            )
            # Path collapsed to repo-relative posix form
            self.assertEqual(
                out["builder_path"], "scripts/analysis/builder.py",
            )

    def test_input_files_are_hashed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            train = root / "training.jsonl"
            train.write_text("row1\nrow2\n", encoding="utf-8")
            out = lineage_mod.compute_lineage(
                builder_path=root / "builder.py",
                input_paths=[train],
                project_root=root,
            )
            self.assertIn("training.jsonl", out["input_hashes"])
            self.assertTrue(out["input_hashes"]["training.jsonl"].startswith("sha256:"))

    def test_input_dir_paths_get_summary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            games = root / "games"
            games.mkdir()
            (games / "1.json").write_text("{}")
            (games / "2.json").write_text("{}")
            out = lineage_mod.compute_lineage(
                builder_path=root / "builder.py",
                input_dir_paths=[games],
                project_root=root,
            )
            self.assertIn("games", out["input_dir_summaries"])
            self.assertEqual(out["input_dir_summaries"]["games"]["n_files"], 2)

    def test_extra_fields_merged_without_shadowing(self):
        out = lineage_mod.compute_lineage(
            builder_path="builder.py",
            extra={
                "side": "under",
                "family_mode": "separate",
                # Try to shadow a canonical key -- it should be ignored
                "git_sha": "FAKE_OVERRIDE",
            },
        )
        self.assertEqual(out.get("side"), "under")
        self.assertEqual(out.get("family_mode"), "separate")
        # Canonical git_sha NOT overridden by extra
        self.assertNotEqual(out["git_sha"], "FAKE_OVERRIDE")

    def test_compute_never_raises_on_bad_paths(self):
        """Lineage must be best-effort. A non-existent input path
        should return a None hash, not raise."""
        bad_file = Path("/nonexistent/file.jsonl")
        bad_dir = Path("/nonexistent/dir")
        out = lineage_mod.compute_lineage(
            builder_path="builder.py",
            input_paths=[bad_file],
            input_dir_paths=[bad_dir],
        )
        # Key is str(Path(...)) when no project_root supplied --
        # that differs by OS so compare via the str of the Path.
        self.assertIsNone(out["input_hashes"][str(bad_file)])
        self.assertIsNone(out["input_dir_summaries"][str(bad_dir)])

    def test_python_version_format(self):
        out = lineage_mod.compute_lineage(builder_path="x.py")
        # e.g. "3.11.4"
        self.assertRegex(out["python_version"], r"^\d+\.\d+\.\d+$")


class PromotionLineageTests(unittest.TestCase):
    def test_required_fields_present(self):
        out = lineage_mod.promotion_lineage()
        for key in (
            "schema_version", "promoted_at_utc",
            "git_sha", "git_branch", "git_dirty",
        ):
            self.assertIn(key, out)

    def test_promoted_at_utc_is_iso_format(self):
        out = lineage_mod.promotion_lineage()
        # Ends with Z (UTC), parseable as ISO
        self.assertTrue(out["promoted_at_utc"].endswith("Z"))


class PromotionLineageWiringTests(unittest.TestCase):
    """End-to-end: promote a synthetic stake-scaling artifact and
    verify the audit row carries both source_artifact_lineage (from
    the artifact's own `lineage` block) and promotion_lineage
    (fresh git stamp at promotion time)."""

    def test_source_artifact_lineage_flows_to_audit_row(self):
        ANALYSIS_DIR_LOCAL = PROJECT_DIR / "scripts" / "analysis"
        if str(ANALYSIS_DIR_LOCAL) not in sys.path:
            sys.path.insert(0, str(ANALYSIS_DIR_LOCAL))
        import promote  # noqa: E402

        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            # Synthetic stake-scaling verdict with a `lineage` block.
            report = td / "stake_scaling.json"
            report.write_text(json.dumps({
                "verdict": "promote",
                "verdict_reason": "ok",
                "n_sessions": 30,
                "thresholds": {"min_sessions": 30},
                # The source artifact carries its own build-time lineage
                "lineage": {
                    "schema_version": 1,
                    "git_sha": "sourcesha1",
                    "git_branch": "main",
                    "git_dirty": False,
                    "builder_path": "scripts/analysis/analyze_stake_scaling_promotion.py",
                    "built_at_utc": "2026-05-17T01:00:00Z",
                },
            }), encoding="utf-8")
            overrides = td / "live_overrides.json"
            from types import SimpleNamespace
            args = SimpleNamespace(
                stake_scaling_report_path=report,
                event_log_path=td / "events.jsonl",
                live_overrides_path=overrides,
                operator="alice",
                side="over",
                dry_run=True,
                force=False,
            )
            rc = promote.cmd_stake_scaling(args)
            self.assertEqual(rc, 0)

            rows = [
                json.loads(line) for line in
                (td / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            # Dry-run row -- no lineage added because lineage is only on the success path
            self.assertEqual(rows[-1]["action"], "dry_run")

            # Now do a real promotion (force=True since the lineage
            # block only fires on success/forced).
            args.dry_run = False
            args.force = True
            (td / "events.jsonl").write_text("", encoding="utf-8")
            rc = promote.cmd_stake_scaling(args)
            self.assertEqual(rc, 0)
            rows = [
                json.loads(line) for line in
                (td / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            success_row = next(
                r for r in rows if r["action"] in ("promoted", "forced")
            )
            self.assertIn("source_artifact_lineage", success_row)
            self.assertEqual(
                success_row["source_artifact_lineage"]["git_sha"],
                "sourcesha1",
            )
            self.assertEqual(
                success_row["source_artifact_lineage"]["builder_path"],
                "scripts/analysis/analyze_stake_scaling_promotion.py",
            )
            self.assertIn("promotion_lineage", success_row)
            # Promotion lineage was captured AT promote time; has its
            # own timestamp (different from the source's built_at_utc).
            self.assertIn("promoted_at_utc", success_row["promotion_lineage"])


class ExtractLineageFromArtifactTests(unittest.TestCase):
    def test_returns_lineage_when_present(self):
        payload = {"lineage": {"git_sha": "abc123"}, "other": 1}
        self.assertEqual(
            lineage_mod.extract_lineage_from_artifact(payload),
            {"git_sha": "abc123"},
        )

    def test_returns_none_when_missing(self):
        self.assertIsNone(
            lineage_mod.extract_lineage_from_artifact({"other": 1}),
        )

    def test_returns_none_when_wrong_type(self):
        """Defensive: an artifact with `lineage` as a string (bad
        legacy) should return None rather than crash later."""
        self.assertIsNone(
            lineage_mod.extract_lineage_from_artifact({"lineage": "not a dict"}),
        )


if __name__ == "__main__":
    unittest.main()
