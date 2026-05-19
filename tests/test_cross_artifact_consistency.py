"""Active #16 v4 (2026-05-17): cross-artifact consistency check tests.

Two pieces under test:
  1. `compare_input_hash` in artifact_lineage.py -- the helper that
     reads a lineage's `input_hashes[path]`, hashes the file
     on disk, and classifies the relationship (match / stale /
     not_tracked / current_missing).
  2. `_cross_artifact_consistency_health` in
     build_daily_human_review_report.py -- the daily-review block
     that runs `compare_input_hash` over a configured artifact list,
     surfaces per-artifact stale alerts, and detects cross-artifact
     divergence (two artifacts share an input but recorded different
     hashes).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import artifact_lineage as al  # noqa: E402
import build_daily_human_review_report as bdhr  # noqa: E402


def _write_text(path: Path, content: str) -> None:
    """Write `content` as bytes to keep the on-disk content
    byte-identical to the input string (Path.write_text in text
    mode performs platform line-ending conversion on Windows,
    which would invalidate our hash fixtures)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


def _write_artifact_with_lineage(
    path: Path, *, input_hashes: Dict[str, str],
    built_at_utc: str = "2026-05-17T00:00:00Z",
) -> None:
    payload = {
        "data": {},
        "lineage": {
            "schema_version": 1,
            "built_at_utc": built_at_utc,
            "builder_path": "scripts/analysis/test_builder.py",
            "git_sha": "abc123",
            "git_branch": "main",
            "git_dirty": False,
            "input_hashes": input_hashes,
            "input_dir_summaries": {},
            "python_version": "3.11",
        },
    }
    _write_text(path, json.dumps(payload))


def _hash_text(text: str) -> str:
    """Compute the truncated sha256 the way artifact_lineage.hash_file
    does, for fixture construction."""
    import hashlib
    h = hashlib.sha256(text.encode("utf-8"))
    return f"sha256:{h.hexdigest()[:16]}"


# ---------------------------------------------------------------------------
# compare_input_hash tests
# ---------------------------------------------------------------------------


class CompareInputHashTests(unittest.TestCase):
    def test_none_lineage_returns_not_tracked(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "file.txt"
            _write_text(p, "x")
            v = al.compare_input_hash(None, p)
            self.assertEqual(v["status"], al.CONSISTENCY_NOT_TRACKED)

    def test_path_not_in_lineage_returns_not_tracked(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "file.txt"
            _write_text(p, "x")
            lineage = {"input_hashes": {"some/other/path.json": "sha256:abc"}}
            v = al.compare_input_hash(lineage, p)
            self.assertEqual(v["status"], al.CONSISTENCY_NOT_TRACKED)

    def test_match_when_recorded_equals_current(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "file.txt"
            content = "hello world"
            _write_text(p, content)
            recorded = _hash_text(content)
            lineage = {"input_hashes": {str(p): recorded}}
            v = al.compare_input_hash(lineage, p)
            self.assertEqual(v["status"], al.CONSISTENCY_MATCH)
            self.assertEqual(v["recorded_hash"], recorded)
            self.assertEqual(v["current_hash"], recorded)

    def test_stale_when_file_content_changes(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "file.txt"
            _write_text(p, "v1")
            recorded_v1 = _hash_text("v1")
            lineage = {"input_hashes": {str(p): recorded_v1}}
            # Mutate the file -> stale
            _write_text(p, "v2")
            v = al.compare_input_hash(lineage, p)
            self.assertEqual(v["status"], al.CONSISTENCY_STALE)
            self.assertEqual(v["recorded_hash"], recorded_v1)
            self.assertEqual(v["current_hash"], _hash_text("v2"))

    def test_current_missing_when_file_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "file.txt"
            lineage = {"input_hashes": {str(p): "sha256:abc"}}
            v = al.compare_input_hash(lineage, p)
            self.assertEqual(v["status"], al.CONSISTENCY_CURRENT_MISSING)
            self.assertEqual(v["recorded_hash"], "sha256:abc")
            self.assertIsNone(v["current_hash"])

    def test_repo_relative_key_resolution(self):
        # Lineage keys are stored as repo-relative paths; helper should
        # resolve the absolute path back to the same key.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sub = root / "subdir"
            sub.mkdir()
            p = sub / "file.txt"
            _write_text(p, "abc")
            recorded = _hash_text("abc")
            lineage = {"input_hashes": {"subdir/file.txt": recorded}}
            v = al.compare_input_hash(lineage, p, project_root=root)
            self.assertEqual(v["status"], al.CONSISTENCY_MATCH)
            self.assertEqual(v["input_path"], "subdir/file.txt")

    def test_malformed_lineage_returns_not_tracked(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "file.txt"
            _write_text(p, "x")
            # Lineage with input_hashes that isn't a dict
            v = al.compare_input_hash({"input_hashes": "not a dict"}, p)
            self.assertEqual(v["status"], al.CONSISTENCY_NOT_TRACKED)

    def test_non_dict_lineage_returns_not_tracked(self):
        v = al.compare_input_hash("not a dict", Path("/tmp/x"))
        self.assertEqual(v["status"], al.CONSISTENCY_NOT_TRACKED)


# ---------------------------------------------------------------------------
# _cross_artifact_consistency_health tests
# ---------------------------------------------------------------------------


class CrossArtifactConsistencyHealthTests(unittest.TestCase):
    """Build a synthetic 3-artifact / 2-input workspace and verify
    the daily-review block surfaces per-artifact stale + cross-
    artifact divergence as expected."""

    def _setup_workspace(self) -> Path:
        """Return a fresh temp project root with /cache, /data dirs."""
        td = Path(tempfile.mkdtemp())
        (td / "cache").mkdir()
        (td / "data" / "analysis_output" / "training_tables").mkdir(
            parents=True,
        )
        return td

    def _specs(
        self, root: Path,
        artifact_rel_paths: Dict[str, str],
    ) -> tuple:
        """Translate a {label: rel_path} dict into the
        artifact_specs tuple shape the block expects."""
        return tuple((label, path) for label, path in artifact_rel_paths.items())

    def test_all_artifacts_missing_no_alerts(self):
        root = self._setup_workspace()
        try:
            specs = self._specs(root, {
                "stage1_cache": "cache/mlb_ou_cache.json",
                "calibrator_over": (
                    "data/analysis_output/calibration/cal_over.json"
                ),
            })
            out = bdhr._cross_artifact_consistency_health(
                project_root=root, artifact_specs=specs,
            )
            self.assertEqual(out["alerts"], [])
            self.assertEqual(
                out["artifacts"]["stage1_cache"]["status"], "missing",
            )
            self.assertEqual(
                out["artifacts"]["calibrator_over"]["status"], "missing",
            )
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_pre_v2_artifact_tagged_no_lineage(self):
        root = self._setup_workspace()
        try:
            stage1 = root / "cache" / "mlb_ou_cache.json"
            _write_text(stage1, json.dumps({"cells": {}}))  # no lineage
            specs = self._specs(root, {
                "stage1_cache": "cache/mlb_ou_cache.json",
            })
            out = bdhr._cross_artifact_consistency_health(
                project_root=root, artifact_specs=specs,
            )
            self.assertEqual(
                out["artifacts"]["stage1_cache"]["status"],
                "no_lineage_pre_v2",
            )
            self.assertEqual(out["alerts"], [])
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_all_inputs_match_no_alerts(self):
        root = self._setup_workspace()
        try:
            # Create an upstream input file
            tt = root / "data" / "analysis_output" / "training_tables" / "tt.jsonl"
            _write_text(tt, "row1\n")
            tt_hash = _hash_text("row1\n")
            # Downstream artifact records the same hash
            cal = (
                root / "data" / "analysis_output"
                / "calibration" / "cal.json"
            )
            cal.parent.mkdir(parents=True, exist_ok=True)
            _write_artifact_with_lineage(cal, input_hashes={
                "data/analysis_output/training_tables/tt.jsonl": tt_hash,
            })
            specs = self._specs(root, {
                "calibrator_over": (
                    "data/analysis_output/calibration/cal.json"
                ),
            })
            out = bdhr._cross_artifact_consistency_health(
                project_root=root, artifact_specs=specs,
            )
            self.assertEqual(out["alerts"], [])
            cal_info = out["artifacts"]["calibrator_over"]
            self.assertEqual(cal_info["status"], "ok")
            self.assertEqual(
                cal_info["inputs"][0]["status"], al.CONSISTENCY_MATCH,
            )
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_stale_input_fires_per_artifact_alert(self):
        root = self._setup_workspace()
        try:
            # Input file starts at v1, downstream artifact records v1 hash
            tt = root / "data" / "analysis_output" / "training_tables" / "tt.jsonl"
            _write_text(tt, "v1")
            cal = (
                root / "data" / "analysis_output"
                / "calibration" / "cal.json"
            )
            cal.parent.mkdir(parents=True, exist_ok=True)
            _write_artifact_with_lineage(cal, input_hashes={
                "data/analysis_output/training_tables/tt.jsonl": (
                    _hash_text("v1")
                ),
            })
            # Update input AFTER artifact build -> stale
            _write_text(tt, "v2")
            specs = self._specs(root, {
                "calibrator_over": (
                    "data/analysis_output/calibration/cal.json"
                ),
            })
            out = bdhr._cross_artifact_consistency_health(
                project_root=root, artifact_specs=specs,
            )
            self.assertEqual(len(out["alerts"]), 1)
            self.assertIn("calibrator_over", out["alerts"][0])
            self.assertIn("does not match", out["alerts"][0])
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_cross_artifact_divergence_alert(self):
        root = self._setup_workspace()
        try:
            # Both artifacts depend on tt.jsonl but recorded DIFFERENT
            # hashes -- e.g. artifact_a built before a refresh,
            # artifact_b built after.
            tt = root / "data" / "analysis_output" / "training_tables" / "tt.jsonl"
            _write_text(tt, "v2")  # current state matches v2
            cal_dir = root / "data" / "analysis_output" / "calibration"
            cal_dir.mkdir(parents=True, exist_ok=True)
            cal_over = cal_dir / "cal_over.json"
            cal_under = cal_dir / "cal_under.json"
            _write_artifact_with_lineage(cal_over, input_hashes={
                "data/analysis_output/training_tables/tt.jsonl": (
                    _hash_text("v1")
                ),
            }, built_at_utc="2026-05-15T00:00:00Z")
            _write_artifact_with_lineage(cal_under, input_hashes={
                "data/analysis_output/training_tables/tt.jsonl": (
                    _hash_text("v2")
                ),
            }, built_at_utc="2026-05-17T00:00:00Z")
            specs = self._specs(root, {
                "calibrator_over": (
                    "data/analysis_output/calibration/cal_over.json"
                ),
                "calibrator_under": (
                    "data/analysis_output/calibration/cal_under.json"
                ),
            })
            out = bdhr._cross_artifact_consistency_health(
                project_root=root, artifact_specs=specs,
            )
            # Two alerts: cal_over stale (since current=v2 != recorded=v1)
            # AND the cross-artifact divergence alert
            stale_alerts = [
                a for a in out["alerts"] if "calibrator_over" in a
                and "does not match" in a
            ]
            divergence_alerts = [
                a for a in out["alerts"]
                if "cross-artifact divergence" in a
            ]
            self.assertEqual(len(stale_alerts), 1)
            self.assertEqual(len(divergence_alerts), 1)
            self.assertEqual(
                len(out["cross_artifact_divergences"]), 1,
            )
            div = out["cross_artifact_divergences"][0]
            self.assertEqual(
                div["input_path"],
                "data/analysis_output/training_tables/tt.jsonl",
            )
            self.assertEqual(len(div["groups"]), 2)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_two_artifacts_agree_no_divergence(self):
        root = self._setup_workspace()
        try:
            tt = root / "data" / "analysis_output" / "training_tables" / "tt.jsonl"
            _write_text(tt, "v1")
            tt_hash = _hash_text("v1")
            cal_dir = root / "data" / "analysis_output" / "calibration"
            cal_dir.mkdir(parents=True, exist_ok=True)
            cal_over = cal_dir / "cal_over.json"
            cal_under = cal_dir / "cal_under.json"
            _write_artifact_with_lineage(cal_over, input_hashes={
                "data/analysis_output/training_tables/tt.jsonl": tt_hash,
            })
            _write_artifact_with_lineage(cal_under, input_hashes={
                "data/analysis_output/training_tables/tt.jsonl": tt_hash,
            })
            specs = self._specs(root, {
                "calibrator_over": (
                    "data/analysis_output/calibration/cal_over.json"
                ),
                "calibrator_under": (
                    "data/analysis_output/calibration/cal_under.json"
                ),
            })
            out = bdhr._cross_artifact_consistency_health(
                project_root=root, artifact_specs=specs,
            )
            self.assertEqual(out["alerts"], [])
            self.assertEqual(out["cross_artifact_divergences"], [])
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_disjoint_inputs_no_divergence(self):
        # Two artifacts depend on DIFFERENT inputs; no divergence
        # possible because the input sets don't overlap.
        root = self._setup_workspace()
        try:
            tt_a = root / "data" / "analysis_output" / "training_tables" / "a.jsonl"
            tt_b = root / "data" / "analysis_output" / "training_tables" / "b.jsonl"
            _write_text(tt_a, "a-content")
            _write_text(tt_b, "b-content")
            cal_dir = root / "data" / "analysis_output" / "calibration"
            cal_dir.mkdir(parents=True, exist_ok=True)
            cal_over = cal_dir / "cal_over.json"
            cal_under = cal_dir / "cal_under.json"
            _write_artifact_with_lineage(cal_over, input_hashes={
                "data/analysis_output/training_tables/a.jsonl": (
                    _hash_text("a-content")
                ),
            })
            _write_artifact_with_lineage(cal_under, input_hashes={
                "data/analysis_output/training_tables/b.jsonl": (
                    _hash_text("b-content")
                ),
            })
            specs = self._specs(root, {
                "calibrator_over": (
                    "data/analysis_output/calibration/cal_over.json"
                ),
                "calibrator_under": (
                    "data/analysis_output/calibration/cal_under.json"
                ),
            })
            out = bdhr._cross_artifact_consistency_health(
                project_root=root, artifact_specs=specs,
            )
            self.assertEqual(out["alerts"], [])
            self.assertEqual(out["cross_artifact_divergences"], [])
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_corrupted_artifact_json_treated_as_pre_v2(self):
        root = self._setup_workspace()
        try:
            cal = root / "data" / "analysis_output" / "calibration" / "cal.json"
            cal.parent.mkdir(parents=True, exist_ok=True)
            cal.write_text("not json", encoding="utf-8")
            specs = self._specs(root, {
                "calibrator_over": (
                    "data/analysis_output/calibration/cal.json"
                ),
            })
            out = bdhr._cross_artifact_consistency_health(
                project_root=root, artifact_specs=specs,
            )
            # _read_lineage_from_path returns None on parse failure;
            # block treats that as no_lineage_pre_v2 (best signal
            # without crashing).
            self.assertEqual(
                out["artifacts"]["calibrator_over"]["status"],
                "no_lineage_pre_v2",
            )
            self.assertEqual(out["alerts"], [])
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_returns_required_schema_keys(self):
        out = bdhr._cross_artifact_consistency_health(
            project_root=PROJECT_DIR,
            artifact_specs=(),
        )
        for k in ("alerts", "artifacts", "cross_artifact_divergences"):
            self.assertIn(k, out)


class NotesBlockMirrorTests(unittest.TestCase):
    def test_notes_block_carries_cross_artifact_prefix(self):
        cah = {"alerts": ["cross-artifact divergence on `X`: ..."]}
        notes = bdhr._build_notes(
            session_summary={}, bet_totals={}, candidate_rollup={},
            log_health={},
            cross_artifact_consistency_health=cah,
        )
        prefixed = [
            n for n in notes if n.startswith("Cross-artifact:")
        ]
        self.assertEqual(len(prefixed), 1)


if __name__ == "__main__":
    unittest.main()
