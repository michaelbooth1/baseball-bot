"""Tests for build_edge_atlas (long-form research, 2026-05-27).

Coverage:
  - line_to_emp_key / line_to_poisson_key formats
  - normalize_inning_state (Top/Bottom → T/B, Middle/End → None)
  - derive_cell_key (happy path + missing-field + invalid-state)
  - _percentile (single value, sorted interpolation)
  - _parse_cell_key (round-trip)
  - _inning_band + _score_diff_band coverage
  - accumulate_observations end-to-end through one synthetic candidate
    file, including the inferred-runs / boundary-ask / wide-spread /
    non-over-side filters that exist to suppress garbage observations
  - build_atlas_rows joins observations to a synthetic cache + computes
    bias / significance / realized-outcome overlay correctly
  - summarize_by aggregation respects the qualifying floor + computes
    stake-weighted bias
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_edge_atlas as ea  # noqa: E402


class LineAndStateKeyTests(unittest.TestCase):
    def test_line_emp_key(self):
        self.assertEqual(ea.line_to_emp_key("7.5"), "o75")
        self.assertEqual(ea.line_to_emp_key("10.5"), "o105")
        self.assertEqual(ea.line_to_emp_key("5.5"), "o55")

    def test_line_poisson_key(self):
        self.assertEqual(ea.line_to_poisson_key("7.5"), "po75")
        self.assertEqual(ea.line_to_poisson_key("10.5"), "po105")

    def test_inning_state_normalization(self):
        self.assertEqual(ea.normalize_inning_state("Top"), "T")
        self.assertEqual(ea.normalize_inning_state("Bottom"), "B")
        self.assertEqual(ea.normalize_inning_state("top"), "T")
        # Middle/End mean no batter at plate -- no matching cache cell.
        self.assertIsNone(ea.normalize_inning_state("Middle"))
        self.assertIsNone(ea.normalize_inning_state("End"))
        self.assertIsNone(ea.normalize_inning_state(None))


class DeriveCellKeyTests(unittest.TestCase):
    def test_happy_path(self):
        row = {
            "away_score_before": 2,
            "home_score_before": 1,
            "inning": 7,
            "inning_state": "Bottom",
            "outs": 2,
            "runners_on": 4,  # 3B only
        }
        self.assertEqual(ea.derive_cell_key(row), "2_1_7_B_2_4")

    def test_extras_bucketed_to_10(self):
        row = {
            "away_score_before": 5,
            "home_score_before": 5,
            "inning": 13,
            "inning_state": "Top",
            "outs": 0,
            "runners_on": 0,
        }
        self.assertEqual(ea.derive_cell_key(row), "5_5_10_T_0_0")

    def test_missing_fields_return_none(self):
        # Each required field missing -> None
        base = {
            "away_score_before": 0, "home_score_before": 0,
            "inning": 1, "inning_state": "Top",
            "outs": 0, "runners_on": 0,
        }
        for missing in (
            "away_score_before", "home_score_before", "inning",
            "inning_state", "outs", "runners_on",
        ):
            row = dict(base)
            row[missing] = None
            self.assertIsNone(
                ea.derive_cell_key(row),
                f"expected None when {missing} is None",
            )

    def test_middle_end_state_returns_none(self):
        # End-of-inning rows can't map to a per-PA cell.
        row = {
            "away_score_before": 0, "home_score_before": 0,
            "inning": 4, "inning_state": "End",
            "outs": 3, "runners_on": 0,
        }
        self.assertIsNone(ea.derive_cell_key(row))

    def test_invalid_outs_or_bases_returns_none(self):
        # outs in [0, 2]; bases mask in [0, 7].
        base = {
            "away_score_before": 0, "home_score_before": 0,
            "inning": 1, "inning_state": "Top",
            "outs": 0, "runners_on": 0,
        }
        for field, bad in [("outs", 4), ("runners_on", 99)]:
            row = dict(base); row[field] = bad
            self.assertIsNone(ea.derive_cell_key(row))


class PercentileTests(unittest.TestCase):
    def test_single_value(self):
        self.assertEqual(ea._percentile([0.5], 0.25), 0.5)
        self.assertEqual(ea._percentile([0.5], 0.75), 0.5)

    def test_empty_returns_none(self):
        self.assertIsNone(ea._percentile([], 0.5))

    def test_interpolation(self):
        # 5 elements: indices 0..4. q=0.25 -> pos=1.0 -> exactly s[1].
        self.assertAlmostEqual(ea._percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.25), 2.0)
        self.assertAlmostEqual(ea._percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5), 3.0)
        self.assertAlmostEqual(ea._percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.75), 4.0)


class ParseCellKeyTests(unittest.TestCase):
    def test_round_trip(self):
        parsed = ea._parse_cell_key("3_4_5_B_2_7")
        self.assertEqual(parsed, (3, 4, 5, "B", 2, 7))

    def test_invalid_format_returns_none(self):
        self.assertIsNone(ea._parse_cell_key("3_4_5_B_2"))  # too few
        self.assertIsNone(ea._parse_cell_key("3_4_5_X_2_7"))  # bad half
        self.assertIsNone(ea._parse_cell_key("abc_d_e_T_0_0"))  # not numeric


class BandsTests(unittest.TestCase):
    def test_inning_bands(self):
        self.assertEqual(ea._inning_band(1), "inn_1-3")
        self.assertEqual(ea._inning_band(3), "inn_1-3")
        self.assertEqual(ea._inning_band(4), "inn_4-5")
        self.assertEqual(ea._inning_band(5), "inn_4-5")
        self.assertEqual(ea._inning_band(7), "inn_6-7")
        self.assertEqual(ea._inning_band(9), "inn_8-9")
        self.assertEqual(ea._inning_band(10), "inn_10+")

    def test_score_diff_bands(self):
        self.assertEqual(ea._score_diff_band(-5), "trailing>=4")
        self.assertEqual(ea._score_diff_band(-2), "trailing_1-3")
        self.assertEqual(ea._score_diff_band(0), "tied")
        self.assertEqual(ea._score_diff_band(2), "leading_1-3")
        self.assertEqual(ea._score_diff_band(5), "leading>=4")


class AccumulateObservationsFiltersTests(unittest.TestCase):
    """The walker drops:
      - non-OVER side (UNDER candidates leak in but break the bias math)
      - inferred_runs >= 1 (stale schedule vs market regime)
      - boundary asks (<=0.01 or >=0.99)
      - wide-spread asks (ask - bid > 0.25)
      - missing cell key / missing decision_ask / missing line
    """

    def _candidate(self, **overrides):
        base = {
            "decision_ask": 0.55,
            "side": "over",
            "game_pk": 100,
            "line": "8.5",
            "inning": 5,
            "inning_state": "Top",
            "outs": 1,
            "runners_on": 0,
            "away_score_before": 2,
            "home_score_before": 3,
            "best_bid": 0.50,
            "inferred_runs": None,
        }
        base.update(overrides)
        return base

    def _write_and_walk(self, rows):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "candidate_universe").mkdir()
            fp = root / "candidate_universe" / "2026-05-01_candidates.jsonl"
            with open(fp, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
            obs, stats = ea.accumulate_observations([root])
            return obs, stats

    def test_clean_over_row_is_kept(self):
        obs, stats = self._write_and_walk([self._candidate()])
        self.assertEqual(stats.get("observations_kept"), 1)
        key = ("2_3_5_T_1_0", "8.5")
        self.assertIn(key, obs)
        self.assertEqual(obs[key].asks, [0.55])
        self.assertEqual(obs[key].game_pks, {100})

    def test_under_side_is_skipped(self):
        obs, stats = self._write_and_walk([self._candidate(side="under")])
        self.assertEqual(stats.get("non_over_side_skipped"), 1)
        self.assertEqual(len(obs), 0)

    def test_inferred_runs_active_is_skipped(self):
        obs, stats = self._write_and_walk([self._candidate(inferred_runs=1)])
        self.assertEqual(stats.get("inference_active_skipped"), 1)
        self.assertEqual(len(obs), 0)

    def test_boundary_asks_are_skipped(self):
        rows = [
            self._candidate(decision_ask=0.005),
            self._candidate(decision_ask=0.995),
            self._candidate(decision_ask=0.50),  # this one survives
        ]
        obs, stats = self._write_and_walk(rows)
        self.assertEqual(stats.get("boundary_ask"), 2)
        self.assertEqual(stats.get("observations_kept"), 1)

    def test_wide_spread_is_skipped(self):
        rows = [
            self._candidate(decision_ask=0.88, best_bid=0.08),  # 80c spread
            self._candidate(decision_ask=0.55, best_bid=0.50),  # 5c, fine
        ]
        obs, stats = self._write_and_walk(rows)
        self.assertEqual(stats.get("wide_spread_skipped"), 1)
        self.assertEqual(stats.get("observations_kept"), 1)

    def test_middle_end_inning_state_is_skipped(self):
        rows = [self._candidate(inning_state="End")]
        obs, stats = self._write_and_walk(rows)
        self.assertEqual(stats.get("no_cell_key"), 1)
        self.assertEqual(len(obs), 0)


class BuildAtlasRowsTests(unittest.TestCase):
    """Joining observations to a synthetic cache produces correct bias +
    significance + realized-outcome overlay numbers."""

    def test_bias_and_significance(self):
        obs = {
            ("3_2_5_T_1_0", "8.5"): ea.CellLineObs(
                cell_key="3_2_5_T_1_0",
                line="8.5",
                asks=[0.60, 0.62, 0.58, 0.61, 0.59],
                game_pks={100, 200},
            ),
        }
        cells = {
            "3_2_5_T_1_0": {
                "n": 75, "n_samples": 120, "o85": 0.30, "po85": 0.28,
                "label": "Top5  1 out  bases=---",
            },
        }
        rows = ea.build_atlas_rows(obs, cells, outcomes=None)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertAlmostEqual(r.p_empirical, 0.30)
        self.assertAlmostEqual(r.market_ask_median, 0.60, places=4)
        # bias = 0.60 - 0.30 = +0.30
        self.assertAlmostEqual(r.bias_market_minus_empirical, 0.30, places=4)
        self.assertAlmostEqual(r.abs_bias, 0.30, places=4)
        # significance = 0.30 * sqrt(5) ≈ 0.6708
        self.assertAlmostEqual(r.significance, round(0.30 * (5 ** 0.5), 4), places=4)
        # cell label propagates
        self.assertEqual(r.cell_label, "Top5  1 out  bases=---")

    def test_realized_outcomes_overlay(self):
        obs = {
            ("3_2_5_T_1_0", "8.5"): ea.CellLineObs(
                cell_key="3_2_5_T_1_0",
                line="8.5",
                asks=[0.55, 0.58, 0.60, 0.62, 0.65],
                game_pks={100, 200, 300},
            ),
        }
        cells = {
            "3_2_5_T_1_0": {
                "n": 50, "n_samples": 80, "o85": 0.40, "po85": 0.38,
                "label": "Top5  1 out  bases=---",
            },
        }
        # game 100 final=10 (Over 8.5), game 200 final=7 (Under),
        # game 300 final=9 (Over). 2/3 over hits.
        outcomes = {
            (100, "8.5"): 10,
            (200, "8.5"): 7,
            (300, "8.5"): 9,
        }
        rows = ea.build_atlas_rows(obs, cells, outcomes=outcomes)
        r = rows[0]
        self.assertEqual(r.n_settled_game_lines, 3)
        self.assertEqual(r.realized_over_hits, 2)
        self.assertAlmostEqual(r.realized_over_rate, 2 / 3, places=4)
        # realized - empirical = 0.6667 - 0.40 = +0.2667
        self.assertAlmostEqual(
            r.realized_minus_empirical, round(2 / 3 - 0.40, 4), places=4,
        )

    def test_missing_cache_entry_still_produces_row_with_none_p_emp(self):
        # Cache lookup misses → bias is None, no significance.
        obs = {
            ("9_9_9_T_2_0", "11.5"): ea.CellLineObs(
                cell_key="9_9_9_T_2_0",
                line="11.5",
                asks=[0.5],
                game_pks={1},
            ),
        }
        rows = ea.build_atlas_rows(obs, cells={}, outcomes=None)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertIsNone(r.p_empirical)
        self.assertIsNone(r.bias_market_minus_empirical)
        self.assertIsNone(r.significance)


class SummarizeByTests(unittest.TestCase):
    def _row(self, **overrides):
        base = dict(
            cell_key="0_0_5_T_1_0", line="8.5",
            inning=5, half="T", outs=1, bases_mask=0,
            away_score=0, home_score=0, score_diff=0,
            cell_label="L",
            p_empirical=0.30, p_poisson=0.28,
            mlb_n_games=60, mlb_n_samples=120,
            market_n_ticks=20, market_n_games=4,
            market_ask_median=0.40,
            market_ask_p25=0.38, market_ask_p75=0.42,
            market_ask_min=0.35, market_ask_max=0.45,
            bias_market_minus_empirical=0.10,
            abs_bias=0.10,
            significance=0.45,
        )
        base.update(overrides)
        return ea.AtlasRow(**base)

    def test_aggregates_only_qualifying_rows(self):
        # 5 rows in bucket A (all qualify), 1 row in bucket B (n_ticks
        # below floor). With min_qualifying_rows=2, B should be dropped.
        rows = [self._row(line="8.5", market_n_ticks=20) for _ in range(5)]
        rows.append(self._row(line="9.5", market_n_ticks=20))  # only 1
        out = ea.summarize_by(rows, lambda r: r.line, min_qualifying_rows=2)
        labels = [s["bucket"] for s in out]
        self.assertIn("8.5", labels)
        self.assertNotIn("9.5", labels)

    def test_stake_weighted_bias(self):
        # Two rows: one with 10 ticks and bias +0.10, one with 90 ticks
        # and bias +0.20. Weighted mean = (0.10*10 + 0.20*90) / 100 = 0.19.
        rows = [
            self._row(market_n_ticks=10, bias_market_minus_empirical=0.10),
            self._row(market_n_ticks=90, bias_market_minus_empirical=0.20),
        ]
        out = ea.summarize_by(rows, lambda r: "all", min_qualifying_rows=1)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0]["stake_weighted_bias"], 0.19, places=4)


class EndToEndPayloadTests(unittest.TestCase):
    def test_build_payload_with_synthetic_cache_and_one_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "candidate_universe").mkdir()
            # 12 OVER ticks in the same cell. p_emp=0.30, ask median ~0.50
            # -> bias +0.20.
            rows = []
            for i in range(12):
                rows.append({
                    "decision_ask": 0.50,
                    "side": "over",
                    "game_pk": 100 + (i % 3),
                    "line": "8.5",
                    "inning": 5,
                    "inning_state": "Top",
                    "outs": 1,
                    "runners_on": 0,
                    "away_score_before": 3,
                    "home_score_before": 2,
                    "best_bid": 0.48,
                    "inferred_runs": None,
                })
            fp = root / "candidate_universe" / "2026-05-01_candidates.jsonl"
            with open(fp, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")

            # Synthetic outcomes file.
            of = root / "candidate_universe" / "2026-05-01_outcomes.jsonl"
            with open(of, "w", encoding="utf-8") as f:
                for gpk in (100, 101, 102):
                    f.write(json.dumps({
                        "game_pk": gpk, "line": "8.5", "final_total": 7,
                    }) + "\n")

            # Synthetic cache file with the matching cell.
            cache_path = root / "mlb_ou_cache.json"
            cache_payload = {
                "meta": {"history_start_date": "2021-04-01",
                         "history_end_date": "2025-09-28",
                         "seasons": ["2021"], "total_games": 100},
                "cells": {
                    "3_2_5_T_1_0": {
                        "n": 75, "n_samples": 200,
                        "o85": 0.30, "po85": 0.28,
                        "label": "Top5  1 out  bases=---",
                    },
                },
            }
            cache_path.write_text(json.dumps(cache_payload), encoding="utf-8")

            payload = ea.build_atlas_payload(cache_path, [root])
            self.assertGreaterEqual(payload["headline"]["total_observations"], 12)
            # 1 qualifying pair (passes 40-game + 10-tick floors).
            self.assertEqual(payload["headline"]["n_qualifying_rows"], 1)
            top = payload["top_overall_significance"]
            self.assertEqual(len(top), 1)
            self.assertAlmostEqual(top[0]["bias_market_minus_empirical"], 0.20)
            # All 3 settled games went Under -> realized_over_rate = 0
            self.assertEqual(top[0]["realized_over_hits"], 0)
            self.assertEqual(top[0]["n_settled_game_lines"], 3)
            self.assertAlmostEqual(top[0]["realized_over_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
