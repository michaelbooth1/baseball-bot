"""Tests for build_stage1_nb_replay_report -- out-of-sample Stage-1
cache replay via logit delta substitution (Hygiene #3 follow-up,
2026-06-11)."""

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.analysis import build_stage1_nb_replay_report as replay  # noqa: E402


def _row(**overrides):
    base = {
        "session_date": "2026-06-01",
        "mode": "live",
        "side": "over",
        "inning": 6,
        "line": "7.5",
        "target_over_win": 1,
        "base_fair_value": 0.90,
        "stage2_run_env_delta": 0.10,
        "team_offense_delta": -0.05,
        "inferred_state_cell_key": "1_2_6_T_0_0",
        "inferred_state_line_key_poisson": "po75",
        "inferred_state_base_poisson": 0.90,
        "inferred_state_fallback_level": 0,
    }
    base.update(overrides)
    return base


PROD_CELLS = {"1_2_6_T_0_0": {"po75": 0.90, "n_samples": 500}}
NB_CELLS = {"1_2_6_T_0_0": {"po75": 0.80, "n_samples": 500}}


class ReplayRowTests(unittest.TestCase):

    def test_delta_substitution_math(self):
        rp = replay.replay_row(
            _row(), prod_cells=PROD_CELLS, variant_cells=NB_CELLS,
        )
        # delta = logit(0.80) - logit(0.90); p0_var = sigmoid(logit(0.90) + delta)
        # base == prod cache po here, so p0_var == the NB cache value.
        self.assertAlmostEqual(rp["p0_var"], 0.80, places=9)
        # p3 applies the stored S2+S3 chain on both.
        chain = 0.10 - 0.05
        self.assertAlmostEqual(
            rp["p3_prod"],
            1 / (1 + math.exp(-(math.log(0.9 / 0.1) + chain))),
            places=9,
        )
        self.assertLess(rp["p3_var"], rp["p3_prod"])

    def test_delta_applies_on_top_of_fallback_adjusted_base(self):
        """When production's runtime base differs from the raw cache po
        (fallback penalties), the variant gets the same delta applied
        ON TOP of the adjusted base -- not a raw cache substitution."""
        rp = replay.replay_row(
            _row(base_fair_value=0.85),  # runtime base != cache po 0.90
            prod_cells=PROD_CELLS, variant_cells=NB_CELLS,
        )
        delta = math.log(0.8 / 0.2) - math.log(0.9 / 0.1)
        expected_p0 = 1 / (1 + math.exp(-(math.log(0.85 / 0.15) + delta)))
        self.assertAlmostEqual(rp["p0_var"], expected_p0, places=9)

    def test_unreplayable_rows_return_none(self):
        # Missing cell in the variant cache.
        self.assertIsNone(replay.replay_row(
            _row(), prod_cells=PROD_CELLS, variant_cells={},
        ))
        # Missing line key.
        self.assertIsNone(replay.replay_row(
            _row(inferred_state_line_key_poisson="po115"),
            prod_cells=PROD_CELLS, variant_cells=NB_CELLS,
        ))
        # Missing chain inputs.
        self.assertIsNone(replay.replay_row(
            _row(stage2_run_env_delta=None),
            prod_cells=PROD_CELLS, variant_cells=NB_CELLS,
        ))
        # Boundary cache value.
        self.assertIsNone(replay.replay_row(
            _row(), prod_cells={"1_2_6_T_0_0": {"po75": 1.0}},
            variant_cells=NB_CELLS,
        ))


class ReplayTableTests(unittest.TestCase):

    def test_aggregates_bias_and_skips(self):
        rows = [
            _row(target_over_win=1),
            _row(target_over_win=0),
            _row(target_over_win=None),                # no label
            _row(session_date="2025-09-01"),           # pre-window
            _row(side="under"),                        # wrong side
            _row(inferred_state_cell_key="missing"),   # unreplayable
        ]
        rep = replay.replay_table(
            rows, prod_cells=PROD_CELLS, variant_cells=NB_CELLS,
            label_field="target_over_win", min_session_date="2026-01-01",
        )
        ov = rep["overall"]
        self.assertEqual(ov["n"], 2)
        self.assertEqual(ov["realized_wr"], 0.5)
        self.assertEqual(rep["skipped"]["no_label"], 1)
        self.assertEqual(rep["skipped"]["pre_window"], 1)
        self.assertEqual(rep["skipped"]["non_over_side"], 1)
        self.assertEqual(rep["skipped"]["unreplayable"], 1)
        # Variant bias must sit below production bias here (NB shrinks
        # the over-prediction while realized WR is 50%).
        self.assertLess(ov["bias_p3_variant_pp"], ov["bias_p3_prod_pp"])
        self.assertGreater(ov["bias_reduction_pp"], 0)

    def test_high_fv_zone_only_counts_danger_rows(self):
        low_cells_prod = {"1_2_6_T_0_0": {"po75": 0.55}}
        low_cells_nb = {"1_2_6_T_0_0": {"po75": 0.50}}
        rows = [_row(base_fair_value=0.55, stage2_run_env_delta=0.0,
                     team_offense_delta=0.0)]
        rep = replay.replay_table(
            rows, prod_cells=low_cells_prod, variant_cells=low_cells_nb,
            label_field="target_over_win", min_session_date="2026-01-01",
        )
        self.assertEqual(rep["overall"]["n"], 1)
        self.assertEqual(rep["high_fv_zone"]["n"], 0)

    def test_build_payload_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "prod.json").write_text(
                json.dumps({"cells": PROD_CELLS}), encoding="utf-8")
            (root / "nb.json").write_text(
                json.dumps({"cells": NB_CELLS}), encoding="utf-8")
            cand = root / "cands.jsonl"
            cand.write_text(
                "\n".join(json.dumps(_row(
                    target_over_win=i % 2,
                    label_final_available=True,
                )) for i in range(6)) + "\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                candidate_table=cand,
                bet_table=root / "missing_bets.jsonl",
                prod_cache=root / "prod.json",
                nb_cache=root / "nb.json",
                alt_a_cache=root / "no_alt.json",
                output_root=root / "out",
                min_session_date="2026-01-01",
            )
            payload = replay.build_payload(args)
            self.assertIn("nb_vs_prod", payload["variants"])
            self.assertNotIn("alt_a_vs_prod", payload["variants"])
            self.assertEqual(
                payload["variants"]["nb_vs_prod"]["candidates"]["overall"]["n"], 6,
            )
            self.assertIsNone(payload["variants"]["nb_vs_prod"]["bets"])
            md = replay.render_markdown(payload)
            self.assertIn("NB tail", md)
            self.assertIn("candidate universe", md)


if __name__ == "__main__":
    unittest.main()
