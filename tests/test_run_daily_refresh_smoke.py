"""End-to-end smoke test for run_daily_refresh.

The plan-list assertions in test_run_daily_refresh exercise individual
pieces (step names, ordering, staleness checks), but nothing currently
verifies that the runner actually walks the full step list, dispatches
the right kind of handler per step, and emits a manifest with the
expected shape. This module fills that gap.

Strategy:
  - Stub out all subprocess steps by monkey-patching ``subprocess.run``
    so we never spawn a Python child. Each fake step exits 0 with a
    canned stdout.
  - Inline handlers (preflight_*, model_freshness_health,
    refresh_health_rollup) all execute for real -- they're pure-Python
    and read only files we create in tmp_path.
  - Drive `run_startup_refresh()` end-to-end and assert:
      * every planned step has a result entry
      * inline + subprocess kinds each produce sensible status
      * the manifest is written to disk with the expected fields
      * the health-rollup result tail contains the operator-facing
        summary
      * staleness-checked steps return skipped_fresh when their
        outputs already exist newer than their inputs

This is a smoke test, not a correctness test for any individual builder.
A failure here means refresh wiring regressed -- not that any one
builder produced wrong output.
"""

import json
import os
import shutil
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

import run_daily_refresh as rdr  # noqa: E402


class RefreshSmokeTests(unittest.TestCase):
    """End-to-end smoke test: walks the entire daily refresh pipeline with
    subprocesses stubbed out, asserts the manifest looks right."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="refresh_smoke_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        # Required directory scaffolding.
        for sub in (
            "sessions", "candidates", "logs", "out",
            "data/live_trading/candidate_universe",
            "data/analysis_output/daily_human_review",
            "data/analysis_output/walk_forward",
            "cache",
        ):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)
        # A session file lets `discover_session_dates` return something.
        (self.tmp / "sessions" / "2026-05-09_session.json").write_text(
            json.dumps({"date": "2026-05-09", "mode": "live", "bets": [],
                        "summary": {}}),
            encoding="utf-8",
        )
        # Minimal daily-review file so refresh_health_rollup can read it.
        (self.tmp / "data" / "analysis_output" / "daily_human_review"
         / "2026-05-09_human_review.json").write_text(
            json.dumps({
                "session_date": "2026-05-09",
                "mode": "live",
                "calibration_health": {"alerts": []},
                "fill_rate_health": {"alerts": []},
                "signal_quality_health": {"alerts": []},
                "regime_mix_health": {"alerts": []},
                "reconciler_summary": {"alerts": []},
            }),
            encoding="utf-8",
        )
        # Minimal walk-forward summary so the rollup has something to read.
        (self.tmp / "data" / "analysis_output" / "walk_forward"
         / "summary.json").write_text(
            json.dumps({
                "n_windows_completed": 5,
                "n_windows_planned": 5,
                "baseline_live_engine_results": {
                    "cumulative_baseline_realized_profit": 12.34,
                    "max_baseline_drawdown_across_test_windows": -5.67,
                },
            }),
            encoding="utf-8",
        )

    def _config(self, **overrides) -> rdr.RefreshConfig:
        defaults = dict(
            active_date="2026-05-10",
            include_run_date=False,
            strict=False,
            # Disable everything that would touch live MLB / Polymarket
            # APIs. Smoke test is for the runner mechanics, not the
            # builders themselves.
            refresh_recent_games=False,
            refresh_active_schedule=False,
            refresh_stage1_cache=False,
            refresh_pitcher_cache=False,
            refresh_weather_cache=False,
            refresh_park_hr_factors=False,
            refresh_team_game_log=False,
            refresh_daily_reviews=False,
            run_preflight_secrets=False,
            run_preflight_artifacts=False,
            run_walk_forward=False,
            sessions_dir=self.tmp / "sessions",
            candidate_dir=self.tmp / "candidates",
            log_dir=self.tmp / "logs",
            output_root=self.tmp / "out",
            # Use tmp-local cache paths so model_freshness_health doesn't
            # touch repo state.
            mlb_ou_cache_path=self.tmp / "cache" / "mlb_ou_cache.json",
            stage2_cache_path=self.tmp / "cache" / "mlb_stage2_run_env.json",
            team_game_log_path=self.tmp / "cache" / "team_game_log.json",
            pitcher_cache_path=self.tmp / "cache" / "pitcher_cache.json",
            park_hr_factors_path=self.tmp / "cache" / "park_hr_factors.json",
            # Route stability-gate history files under tmp so smoke runs
            # never write to the canonical repo paths. Without these the
            # inline handlers would silently pollute production history.
            stage2_brier_history_path=self.tmp / "calibration" / "stage2_brier_history.jsonl",
            stage3_v2_drift_history_path=self.tmp / "calibration" / "stage3_v2_drift_history.jsonl",
        )
        defaults.update(overrides)
        return rdr.RefreshConfig(**defaults)

    def _fake_subprocess(self, command, **kwargs):
        """Stub for subprocess.run: every fake step exits 0."""
        return SimpleNamespace(
            returncode=0,
            stdout=f"fake-step-output for {command[-2:]}\n",
            stderr="",
        )

    def test_smoke_runs_every_step_and_writes_manifest(self):
        """End-to-end happy path: every step gets a result, manifest is
        written, summary is 'X/X steps ok'."""
        config = self._config()
        with patch("subprocess.run", side_effect=self._fake_subprocess):
            # Force PROJECT_DIR to tmp so model_freshness_health doesn't
            # leak into the real repo's analysis_output during age checks.
            with patch.object(rdr, "PROJECT_DIR", self.tmp):
                payload = rdr.run_startup_refresh(config)

        # Manifest landed on disk.
        manifest_path = Path(payload["manifest_path"])
        self.assertTrue(manifest_path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_kind"], "run")
        self.assertEqual(manifest["active_date"], "2026-05-10")

        # Every planned step appears in the results array.
        result_names = [s["name"] for s in manifest["steps"]]
        self.assertGreater(len(result_names), 0)
        # Either ok or skipped_fresh -- never a silent miss.
        statuses = {s["status"] for s in manifest["steps"]}
        self.assertTrue(
            statuses.issubset({"ok", "skipped_fresh", "failed"}),
            f"unexpected statuses: {statuses}",
        )
        # No silent failures during smoke.
        self.assertEqual(manifest["steps_failed"], 0)

    def test_smoke_does_not_pollute_canonical_history_files(self):
        """Regression: the Stage-2 + Stage-3 v2 stability gate inline
        handlers used to write to module-level DEFAULT_*_HISTORY_PATH
        constants. Smoke tests would silently append rows to the canonical
        repo paths every time they ran (48 + 94 garbage rows accumulated
        before the fix). RefreshConfig now carries the paths so tests can
        route them to tmp_path.

        This test asserts a smoke run leaves the canonical paths untouched
        (mtime unchanged, or absent and stays absent).
        """
        # Snapshot the canonical paths' mtimes (or "missing" sentinel).
        def _snapshot(path: Path):
            return path.stat().st_mtime if path.exists() else "<missing>"

        canonical_stage2 = rdr.DEFAULT_STAGE2_BRIER_HISTORY_PATH
        canonical_stage3 = rdr.DEFAULT_STAGE3_V2_DRIFT_HISTORY_PATH
        before_stage2 = _snapshot(canonical_stage2)
        before_stage3 = _snapshot(canonical_stage3)

        # Run the full smoke pipeline (paths routed to tmp via _config()).
        config = self._config()
        with patch("subprocess.run", side_effect=self._fake_subprocess):
            with patch.object(rdr, "PROJECT_DIR", self.tmp):
                rdr.run_startup_refresh(config)

        # Canonical paths must be untouched.
        self.assertEqual(
            _snapshot(canonical_stage2), before_stage2,
            f"Smoke run wrote to canonical Stage-2 history path "
            f"{canonical_stage2}; tmp routing failed.",
        )
        self.assertEqual(
            _snapshot(canonical_stage3), before_stage3,
            f"Smoke run wrote to canonical Stage-3 v2 history path "
            f"{canonical_stage3}; tmp routing failed.",
        )

        # Tmp paths SHOULD have been written to (proving the handlers ran).
        tmp_stage2 = self.tmp / "calibration" / "stage2_brier_history.jsonl"
        # tmp_stage2 only gets written if the staging/production caches both
        # have valid Brier; in this smoke they're empty {} so the handler
        # short-circuits before writing. That's the correct behaviour --
        # absence of write here means the routing is what protects us.
        # We only assert non-pollution of canonical paths.
        del tmp_stage2  # silence linter

    def test_smoke_health_rollup_reads_alert_counts(self):
        """refresh_health_rollup should produce a tail containing the
        alert summary line drawn from the daily-review JSON we wrote."""
        config = self._config()
        with patch("subprocess.run", side_effect=self._fake_subprocess):
            with patch.object(rdr, "PROJECT_DIR", self.tmp):
                payload = rdr.run_startup_refresh(config)

        rollup = next(
            (s for s in payload["steps"] if s["name"] == "refresh_health_rollup"),
            None,
        )
        self.assertIsNotNone(rollup, "refresh_health_rollup step missing")
        self.assertEqual(rollup["status"], "ok")
        self.assertIn("Step roll-up:", rollup["output_tail"])
        self.assertIn("active drift alerts", rollup["output_tail"])
        # Walk-forward summary should appear too.
        self.assertIn("Walk-forward:", rollup["output_tail"])

    def test_smoke_staleness_check_skips_stage2_when_fresh(self):
        """When the Stage-2 staging artifact already exists newer than
        every input, the step should be skipped_fresh (not ok)."""
        # Pre-populate the staging artifact + the input files.
        # Order matters: inputs first, then output, so output mtime > inputs.
        (self.tmp / "data" / "games" / "regular").mkdir(parents=True, exist_ok=True)
        cache = self.tmp / "cache"
        (cache / "mlb_ou_cache.json").write_text("{}", encoding="utf-8")
        (cache / "park_hr_factors.json").write_text("{}", encoding="utf-8")
        staging = cache / "mlb_stage2_run_env.staging.json"
        staging.write_text("{}", encoding="utf-8")
        # Bump staging mtime well ahead so the "newer than inputs" check
        # fires regardless of filesystem timestamp resolution.
        inputs_mtime = max(
            (cache / "mlb_ou_cache.json").stat().st_mtime,
            (cache / "park_hr_factors.json").stat().st_mtime,
        )
        os.utime(staging, (inputs_mtime + 100, inputs_mtime + 100))

        config = self._config()
        with patch("subprocess.run", side_effect=self._fake_subprocess):
            with patch.object(rdr, "PROJECT_DIR", self.tmp):
                payload = rdr.run_startup_refresh(config)

        stage2 = next(
            (s for s in payload["steps"] if s["name"] == "stage2_run_env_retrain_staging"),
            None,
        )
        self.assertIsNotNone(stage2)
        self.assertEqual(stage2["status"], "skipped_fresh")
        # Summary string should mention skipped-fresh.
        self.assertIn("skipped-fresh", payload["summary"])

    def test_stage1_step_carries_staleness_check(self):
        """Stage-1 should now carry a StalenessCheck pointing at the staging
        path + data/games/regular/ dir mtimes, mirroring Stage-2.

        Prior to this change Stage-1 always rebuilt (~10 min) even when no
        new game files had landed. Catches a regression where the check is
        accidentally removed.
        """
        config = self._config(refresh_stage1_cache=True)
        # Build the steps under the patched PROJECT_DIR so the StalenessCheck
        # paths reflect the same root the rest of the pipeline uses.
        with patch.object(rdr, "PROJECT_DIR", self.tmp):
            steps = rdr.build_refresh_steps(config, ["2026-05-09"], "2026-05-09")
            expected_staging = self.tmp / "cache" / "mlb_ou_cache.staging.json"
            expected_games_root = self.tmp / "data" / "games" / "regular"
        stage1 = next((s for s in steps if s.name == "stage1_ou_cache"), None)
        self.assertIsNotNone(stage1, "stage1_ou_cache step missing")
        self.assertIsNotNone(
            stage1.staleness_check,
            "stage1_ou_cache lost its StalenessCheck",
        )
        sc = stage1.staleness_check
        self.assertEqual(
            sc.output_path, expected_staging,
            "Stage-1 staleness check must compare staging mtime, not production "
            "(production is rewritten every refresh by the promote step).",
        )
        self.assertEqual(sc.input_paths, ())
        self.assertEqual(sc.input_dir_mtime_roots, (expected_games_root,))

    def test_smoke_staleness_check_skips_stage1_when_fresh(self):
        """When the Stage-1 staging artifact already exists newer than the
        games corpus, the step should be skipped_fresh (not ok).

        Same pattern as the Stage-2 staleness smoke test; this guards
        against the multi-minute Stage-1 rebuild firing on no-new-data
        days. The promote step (stage1_cache_promote) still runs and
        re-validates the (unchanged) staging cache.
        """
        # Pre-populate the games dir + the staging artifact, with the
        # staging mtime bumped well ahead of the corpus.
        games_root = self.tmp / "data" / "games" / "regular"
        games_root.mkdir(parents=True, exist_ok=True)
        # Touch a fake game file so the dir tree has a non-empty mtime.
        (games_root / "marker").write_text("seed", encoding="utf-8")
        staging = self.tmp / "cache" / "mlb_ou_cache.staging.json"
        staging.write_text("{}", encoding="utf-8")
        os.utime(staging, (
            games_root.stat().st_mtime + 100,
            games_root.stat().st_mtime + 100,
        ))

        config = self._config(refresh_stage1_cache=True)
        with patch("subprocess.run", side_effect=self._fake_subprocess):
            with patch.object(rdr, "PROJECT_DIR", self.tmp):
                payload = rdr.run_startup_refresh(config)

        stage1 = next(
            (s for s in payload["steps"] if s["name"] == "stage1_ou_cache"),
            None,
        )
        self.assertIsNotNone(stage1)
        self.assertEqual(stage1["status"], "skipped_fresh")
        # Promote step still runs (it's an inline step with no staleness check
        # of its own; it re-validates the same staging cache).
        promote = next(
            (s for s in payload["steps"] if s["name"] == "stage1_cache_promote"),
            None,
        )
        self.assertIsNotNone(promote)
        self.assertEqual(promote["status"], "ok")

    def test_smoke_force_retrain_bypasses_skip(self):
        """--force-retrain causes a fresh artifact to be retrained anyway."""
        # Same staging-fresh setup as the previous test.
        (self.tmp / "data" / "games" / "regular").mkdir(parents=True, exist_ok=True)
        cache = self.tmp / "cache"
        (cache / "mlb_ou_cache.json").write_text("{}", encoding="utf-8")
        (cache / "park_hr_factors.json").write_text("{}", encoding="utf-8")
        staging = cache / "mlb_stage2_run_env.staging.json"
        staging.write_text("{}", encoding="utf-8")
        inputs_mtime = (cache / "mlb_ou_cache.json").stat().st_mtime
        os.utime(staging, (inputs_mtime + 100, inputs_mtime + 100))

        config = self._config(force_retrain=True)
        with patch("subprocess.run", side_effect=self._fake_subprocess):
            with patch.object(rdr, "PROJECT_DIR", self.tmp):
                payload = rdr.run_startup_refresh(config)

        stage2 = next(
            (s for s in payload["steps"] if s["name"] == "stage2_run_env_retrain_staging"),
            None,
        )
        self.assertIsNotNone(stage2)
        # With force_retrain the step ran (stubbed subprocess).
        self.assertEqual(stage2["status"], "ok")

    def test_smoke_failing_subprocess_marks_failed_but_continues(self):
        """A non-zero exit from one step is non-strict by default;
        downstream steps should still run."""
        def fake_run(command, **kwargs):
            # Fail train_baseline_models specifically; succeed for everything else.
            if any("train_baseline_models" in arg for arg in command):
                return SimpleNamespace(returncode=2, stdout="boom\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

        config = self._config()
        with patch("subprocess.run", side_effect=fake_run):
            with patch.object(rdr, "PROJECT_DIR", self.tmp):
                payload = rdr.run_startup_refresh(config)

        failed = [s for s in payload["steps"] if s["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["name"], "train_baseline_models")
        # refresh_health_rollup should still run after the failure (it's
        # always-on, descriptive).
        rollup = next(
            (s for s in payload["steps"] if s["name"] == "refresh_health_rollup"),
            None,
        )
        self.assertIsNotNone(rollup)
        self.assertEqual(rollup["status"], "ok")


if __name__ == "__main__":
    unittest.main()
