"""Tests for the Stage-3 v2 weights externalization (2026-05-12).

The runtime can load betas + shrinkage from
`cache/team_offense_v2_weights.json` instead of the compiled-in
``DEFAULT_BETA_*`` constants. These tests cover:
  - JSON override is consumed when present
  - Missing file falls back cleanly to compiled defaults
  - Malformed JSON / wrong schema_version logs warning and falls back
  - `promote_team_offense_v2` builds a valid payload from phase4_models.json
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import team_offense_model as tom  # noqa: E402
import promote_team_offense_v2 as promote  # noqa: E402


class LoadWeightsOverridesTests(unittest.TestCase):
    def test_missing_file_returns_empty_overrides(self):
        result = tom._load_weights_overrides(Path("/no/such/file.json"))
        self.assertEqual(result, {})

    def test_full_payload_returns_all_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "weights.json"
            p.write_text(json.dumps({
                "schema_version": 1,
                "betas": {
                    "prior_season": -0.20,
                    "season_to_date": 0.15,
                    "momentum_10": 0.18,
                },
                "shrinkage": {
                    "sigma_within_sq": 12.0,
                    "tau_sq_season": 0.30,
                    "tau_sq_prior": 0.20,
                    "tau_sq_momentum": 0.25,
                },
                "bounds": {"max_logit_delta": 0.50},
            }), encoding="utf-8")
            overrides = tom._load_weights_overrides(p)
        self.assertAlmostEqual(overrides["beta_prior_season"], -0.20)
        self.assertAlmostEqual(overrides["beta_season_to_date"], 0.15)
        self.assertAlmostEqual(overrides["beta_momentum_10"], 0.18)
        self.assertAlmostEqual(overrides["sigma_within_sq"], 12.0)
        self.assertAlmostEqual(overrides["max_logit_delta"], 0.50)

    def test_wrong_schema_version_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "weights.json"
            p.write_text(json.dumps({
                "schema_version": 99,  # unsupported
                "betas": {"prior_season": -0.99},
            }), encoding="utf-8")
            overrides = tom._load_weights_overrides(p)
        # Wrong schema_version -> empty dict so compiled defaults win.
        self.assertEqual(overrides, {})

    def test_malformed_json_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "weights.json"
            p.write_text("{not valid json", encoding="utf-8")
            overrides = tom._load_weights_overrides(p)
        self.assertEqual(overrides, {})

    def test_partial_overrides_only_override_what_is_present(self):
        # JSON with only a beta override -- shrinkage + bounds keep defaults.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "weights.json"
            p.write_text(json.dumps({
                "schema_version": 1,
                "betas": {"prior_season": -0.10},
            }), encoding="utf-8")
            overrides = tom._load_weights_overrides(p)
        self.assertEqual(overrides, {"beta_prior_season": -0.10})


class FromPayloadAcceptsOverridesTests(unittest.TestCase):
    """When overrides are passed to from_payload, they reach the
    instance attributes (driving the actual FV adjustment)."""

    def _minimal_payload(self) -> dict:
        return {
            "games": [
                {"date": "2024-04-01", "away": "AAA", "home": "BBB",
                 "away_runs": 5, "home_runs": 3},
            ],
            "mlb_avg_rpg": 4.5,
        }

    def test_defaults_when_no_overrides(self):
        m = tom.TeamOffenseModel.from_payload(self._minimal_payload())
        self.assertAlmostEqual(m.beta_prior_season, tom.DEFAULT_BETA_PRIOR_SEASON)
        self.assertAlmostEqual(m.beta_season_to_date, tom.DEFAULT_BETA_SEASON_TO_DATE)
        self.assertAlmostEqual(m.beta_momentum_10, tom.DEFAULT_BETA_MOMENTUM_10)

    def test_overrides_reach_instance(self):
        m = tom.TeamOffenseModel.from_payload(
            self._minimal_payload(),
            beta_prior_season=-0.30,
            beta_season_to_date=0.05,
            beta_momentum_10=0.40,
            max_logit_delta=0.45,
        )
        self.assertAlmostEqual(m.beta_prior_season, -0.30)
        self.assertAlmostEqual(m.beta_season_to_date, 0.05)
        self.assertAlmostEqual(m.beta_momentum_10, 0.40)
        self.assertAlmostEqual(m.max_logit_delta, 0.45)


class PromoteTeamOffenseV2Tests(unittest.TestCase):
    """Sanity-check the promotion script builds the right payload shape."""

    def _phase4_payload(self) -> dict:
        return {
            "models": {
                "model_3": {
                    "coefficients": {
                        "prior_season": -0.15,
                        "season_to_date": 0.14,
                        "momentum_10": 0.15,
                    },
                },
            },
            "shrinkage": {
                "sigma_within_sq": 10.0,
                "tau_sq_season": 0.24,
                "tau_sq_prior": 0.17,
                "tau_sq_momentum": 0.20,
            },
        }

    def test_build_weights_payload_extracts_betas(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "phase4_models.json"
            source.write_text(json.dumps(self._phase4_payload()), encoding="utf-8")
            payload = promote.build_weights_payload(source)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["model_name"], "phase4_model3")
        self.assertAlmostEqual(payload["betas"]["prior_season"], -0.15)
        self.assertAlmostEqual(payload["betas"]["season_to_date"], 0.14)
        self.assertAlmostEqual(payload["betas"]["momentum_10"], 0.15)
        self.assertIn("shrinkage", payload)
        self.assertAlmostEqual(payload["shrinkage"]["sigma_within_sq"], 10.0)
        self.assertEqual(payload["bounds"]["max_logit_delta"], 0.60)

    def test_missing_model_3_raises(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "phase4_models.json"
            source.write_text(json.dumps({"models": {"model_1": {}, "model_2": {}}}), encoding="utf-8")
            with self.assertRaises(RuntimeError) as ctx:
                promote.build_weights_payload(source)
            self.assertIn("could not locate Model 3", str(ctx.exception))

    def test_missing_source_artifact_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            promote.build_weights_payload(Path("/no/such/phase4.json"))
        self.assertIn("Source artifact not found", str(ctx.exception))

    def test_payload_consumable_by_runtime_loader(self):
        """End-to-end: promote a fit, then verify the runtime overrides
        match what we wrote. Closes the loop on the externalization
        feature."""
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "phase4_models.json"
            output = Path(td) / "weights.json"
            source.write_text(json.dumps(self._phase4_payload()), encoding="utf-8")

            rc = promote.main([
                "--source-artifact", str(source),
                "--output-path", str(output),
            ])
            self.assertEqual(rc, 0)
            self.assertTrue(output.exists())

            overrides = tom._load_weights_overrides(output)
            self.assertAlmostEqual(overrides["beta_prior_season"], -0.15)
            self.assertAlmostEqual(overrides["beta_season_to_date"], 0.14)
            self.assertAlmostEqual(overrides["beta_momentum_10"], 0.15)
            self.assertAlmostEqual(overrides["max_logit_delta"], 0.60)


if __name__ == "__main__":
    unittest.main()
