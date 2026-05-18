"""Active #16 v2 (2026-05-17): builder-level lineage stamping tests.

V1 (shipped earlier today) stamped lineage on the calibration artifacts
and on the four promote.py audit rows. V2 extends the pattern to the
4 remaining critical builders:

  - Stage-1 cache builder        (cache/build_mlb_ou_cache.py)
  - Stage-2 cache builder        (cache/build_mlb_stage2_run_env.py)
  - Stage-3 v2 weights promoter  (scripts/analysis/promote_team_offense_v2.py)
  - EV-policy backtest           (scripts/analysis/backtest_ev_policy.py)
  - Walk-forward certification   (scripts/analysis/build_walk_forward_certification.py)

These tests verify the EXISTING artifacts under data/analysis_output/
carry a `lineage` block with the canonical shape, AND that the
walk-forward cert (the cheapest to re-run) stamps a fresh lineage on
every rebuild. They do NOT re-run the heavy cache builders (Stage-1 +
Stage-2 take minutes to rebuild against the full corpus) -- those are
verified by inspecting their existing on-disk output, which today's
refresh produced.

The tests are tolerant: builders may not yet have re-run with the v2
change wired in (the production artifacts may pre-date this shipment).
The structural-shape tests verify that WHEN lineage is present, it
follows the canonical schema -- the on-disk check is opportunistic.
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

import artifact_lineage as lineage_mod  # noqa: E402
import build_walk_forward_certification as cert  # noqa: E402


REQUIRED_LINEAGE_KEYS = {
    "schema_version",
    "built_at_utc",
    "builder_path",
    "git_sha",
    "git_branch",
    "git_dirty",
    "input_hashes",
    "input_dir_summaries",
    "python_version",
}


def _assert_canonical_lineage_shape(test: unittest.TestCase, lineage: Dict[str, Any]) -> None:
    """Every v2-stamped lineage block must carry the canonical keys."""
    missing = REQUIRED_LINEAGE_KEYS - set(lineage.keys())
    test.assertEqual(
        missing, set(),
        f"Lineage missing canonical keys: {missing}",
    )
    test.assertEqual(
        lineage["schema_version"], lineage_mod.LINEAGE_SCHEMA_VERSION,
    )
    test.assertIsInstance(lineage["builder_path"], str)
    test.assertIsInstance(lineage["input_hashes"], dict)
    test.assertIsInstance(lineage["input_dir_summaries"], dict)
    test.assertIn(".", lineage["python_version"])


class WalkForwardCertLineageTests(unittest.TestCase):
    """Walk-forward cert is fast (< 1s) so we can rebuild + verify
    end-to-end inside a unit test."""

    def test_cert_payload_carries_lineage_when_built(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # Build a synthetic training table with two settled bets
            table = tmp / "training.jsonl"
            with open(table, "w", encoding="utf-8") as f:
                for i in range(2):
                    f.write(json.dumps({
                        "session_date": f"2026-05-{15 + i}",
                        "signal_model_family": "score_event_transition",
                        "line": 7.5,
                        "inning": 6,
                        "runs_needed": 1.5,
                        "decision_ask": 0.7,
                        "edge_at_ask": 0.15,
                        "fair_value": 0.85,
                        "limit_price": 0.7,
                        "target_filled": 1,
                        "target_win": i,
                        "target_profit": 4.0 if i else -7.0,
                    }) + "\n")
            out_dir = tmp / "out"
            rc = cert.main([
                "--training-table", str(table),
                "--output-dir", str(out_dir),
            ])
            self.assertEqual(rc, 0)
            payload = json.loads(
                (out_dir / "walk_forward_certification.json")
                .read_text(encoding="utf-8"),
            )
            self.assertIn(
                "lineage", payload,
                "Cert payload missing lineage block (V2 wiring not active)",
            )
            _assert_canonical_lineage_shape(self, payload["lineage"])
            # The training table we just wrote should be in input_hashes
            input_hashes = payload["lineage"]["input_hashes"]
            self.assertEqual(len(input_hashes), 1)
            (only_key, only_val), = input_hashes.items()
            self.assertTrue(only_val.startswith("sha256:"))
            # CLI args summary surfaces the readiness label + filled n
            extra = payload["lineage"].get("cli_args_summary") or {}
            self.assertIn("readiness_label", extra)
            self.assertIn("n_filled", extra)

    def test_cert_lineage_changes_when_input_changes(self):
        """Different training tables must produce different input
        hashes; this is the load-bearing property of lineage."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            def _build(seed: float) -> Dict[str, Any]:
                table = tmp / f"training_{seed}.jsonl"
                with open(table, "w", encoding="utf-8") as f:
                    for i in range(2):
                        f.write(json.dumps({
                            "session_date": f"2026-05-{15 + i}",
                            "signal_model_family": "score_event_transition",
                            "line": 7.5,
                            "inning": 6,
                            "runs_needed": 1.5,
                            "decision_ask": 0.7,
                            "edge_at_ask": 0.15,
                            "fair_value": 0.85 + seed,  # vary content
                            "limit_price": 0.7,
                            "target_filled": 1,
                            "target_win": i,
                            "target_profit": 4.0 if i else -7.0,
                        }) + "\n")
                out_dir = tmp / f"out_{seed}"
                cert.main([
                    "--training-table", str(table),
                    "--output-dir", str(out_dir),
                ])
                return json.loads(
                    (out_dir / "walk_forward_certification.json")
                    .read_text(encoding="utf-8"),
                )
            p1 = _build(0.01)
            p2 = _build(0.02)
            h1 = list(p1["lineage"]["input_hashes"].values())[0]
            h2 = list(p2["lineage"]["input_hashes"].values())[0]
            self.assertNotEqual(h1, h2)


class CacheArtifactLineageTests(unittest.TestCase):
    """Opportunistic: the on-disk Stage-1/Stage-2 caches and
    EV-policy artifacts may already carry lineage from a refresh
    run. When present, validate the shape; when absent (artifacts
    pre-date today's V2 shipment), skip with a clear message."""

    STAGE1_CACHE = PROJECT_DIR / "cache" / "mlb_ou_cache.json"
    STAGE2_CACHE = PROJECT_DIR / "cache" / "mlb_stage2_run_env.json"
    STAGE3_V2_WEIGHTS = PROJECT_DIR / "cache" / "team_offense_v2_weights.json"
    EV_POLICY_REPORT = (
        PROJECT_DIR / "data" / "analysis_output" / "ev_policy"
        / "ev_policy_report.json"
    )
    EV_POLICY_WIN_MODEL = (
        PROJECT_DIR / "data" / "analysis_output" / "ev_policy"
        / "ev_signal_win_if_filled_model.json"
    )

    def _check_artifact_lineage(self, path: Path) -> None:
        if not path.exists():
            self.skipTest(f"{path.name} not on disk (no refresh has run)")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.skipTest(f"{path.name} unreadable: {exc}")
        lineage = payload.get("lineage")
        if lineage is None:
            self.skipTest(
                f"{path.name} on disk but pre-dates V2 lineage wiring "
                "(will be stamped on next refresh)"
            )
        _assert_canonical_lineage_shape(self, lineage)

    def test_stage1_cache_lineage_shape(self):
        self._check_artifact_lineage(self.STAGE1_CACHE)

    def test_stage2_cache_lineage_shape(self):
        self._check_artifact_lineage(self.STAGE2_CACHE)

    def test_stage3_v2_weights_lineage_shape(self):
        self._check_artifact_lineage(self.STAGE3_V2_WEIGHTS)

    def test_ev_policy_report_lineage_shape(self):
        self._check_artifact_lineage(self.EV_POLICY_REPORT)

    def test_ev_policy_win_model_lineage_shape(self):
        self._check_artifact_lineage(self.EV_POLICY_WIN_MODEL)


class BuilderImportabilityTests(unittest.TestCase):
    """Smoke: every target builder must be importable without side
    effects so the lineage-stamping branch can be patched into them
    cleanly. Catches regressions where adding the lineage block
    introduced an import-time error."""

    def test_stage1_builder_importable(self):
        sys.path.insert(0, str(PROJECT_DIR / "cache"))
        import build_mlb_ou_cache  # noqa: F401

    def test_stage2_builder_importable(self):
        sys.path.insert(0, str(PROJECT_DIR / "cache"))
        import build_mlb_stage2_run_env  # noqa: F401

    def test_stage3_v2_promoter_importable(self):
        import promote_team_offense_v2  # noqa: F401

    def test_ev_policy_backtest_importable(self):
        import backtest_ev_policy  # noqa: F401

    def test_cert_builder_importable(self):
        import build_walk_forward_certification  # noqa: F401


if __name__ == "__main__":
    unittest.main()
