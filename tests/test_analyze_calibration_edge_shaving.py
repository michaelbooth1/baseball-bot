"""Tests for scripts/analysis/analyze_calibration_edge_shaving.py.

Covers the pure transforms (band-gated FV, Wilson interval, gate admission,
band labelling), cohort aggregation, the calibration-suppression cohort
split, the enforce_min_raw x min_edge scenario sweep, the recommendation
objective, verdict synthesis, and an end-to-end main() that writes JSON +
Markdown over synthetic candidate rows and a synthetic calibration artifact.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
TRADING_DIR = PROJECT_DIR / "scripts" / "trading"
for d in (ANALYSIS_DIR, TRADING_DIR):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))

import analyze_calibration_edge_shaving as ces  # noqa: E402
from probability_calibration import ProbabilityCalibrator  # noqa: E402


def _flat_platt_calibrator(a: float = 0.05, b: float = 0.83) -> ProbabilityCalibrator:
    """A near-flat Platt curve (mirrors the production score-event curve that
    pins everything to ~0.72)."""
    payload = {
        "schema_version": 2,
        "family_mode": "separate",
        "default_family": "score_event_transition",
        "selected_method": "platt",
        "methods": {"platt": {"params": {"a": a, "b": b}}},
        "families": {
            "score_event_transition": {
                "selected_method": "platt",
                "methods": {"platt": {"params": {"a": a, "b": b}}},
            }
        },
    }
    return ProbabilityCalibrator.from_payload(payload)


def _cand(raw_fv, ask, won, *, split="test", taker=None, limit=None,
          decision="skip_with_features", reason="gate_min_edge", line=8.5,
          min_edge_eff=0.15):
    if taker is None:
        taker = (1.0 / ask - 1.0) if won else -1.0
    return {
        "fair_value_raw": raw_fv,
        "decision_ask": ask,
        "target_over_win": won,
        "target_taker_profit_units": taker,
        "target_limit_profit_units": limit,
        "split": split,
        "session_date": "2026-05-17",
        "line": line,
        "decision": decision,
        "decision_reason": reason,
        "min_edge_effective": min_edge_eff,
    }


class BandGatedFvTests(unittest.TestCase):
    def test_enforce_applies_only_above_threshold(self):
        # raw below threshold -> kept raw
        self.assertAlmostEqual(
            ces.band_gated_fv(0.85, 0.72, mode="enforce", enforce_min_raw=0.90), 0.85
        )
        # raw at/above threshold -> calibrated
        self.assertAlmostEqual(
            ces.band_gated_fv(0.95, 0.72, mode="enforce", enforce_min_raw=0.90), 0.72
        )
        self.assertAlmostEqual(
            ces.band_gated_fv(0.90, 0.72, mode="enforce", enforce_min_raw=0.90), 0.72
        )

    def test_shadow_and_off_keep_raw(self):
        self.assertAlmostEqual(ces.band_gated_fv(0.97, 0.72, mode="shadow"), 0.97)
        self.assertAlmostEqual(ces.band_gated_fv(0.97, 0.72, mode="off"), 0.97)

    def test_threshold_above_one_disables_enforce(self):
        # enforce_min_raw=1.01: no raw can reach it, so calibration is off.
        self.assertAlmostEqual(
            ces.band_gated_fv(0.999, 0.72, mode="enforce", enforce_min_raw=1.01), 0.999
        )


class WilsonIntervalTests(unittest.TestCase):
    def test_zero_n(self):
        self.assertEqual(ces.wilson_interval(0, 0), (0.0, 0.0))

    def test_bounds_within_unit_interval(self):
        lo, hi = ces.wilson_interval(8, 10)
        self.assertGreaterEqual(lo, 0.0)
        self.assertLessEqual(hi, 1.0)
        self.assertLess(lo, 0.8)
        self.assertGreater(hi, 0.8)

    def test_all_wins_upper_is_one(self):
        lo, hi = ces.wilson_interval(10, 10)
        self.assertAlmostEqual(hi, 1.0)
        self.assertGreater(lo, 0.6)

    def test_more_data_tightens_interval(self):
        lo_small, hi_small = ces.wilson_interval(7, 10)
        lo_big, hi_big = ces.wilson_interval(70, 100)
        self.assertLess(hi_big - lo_big, hi_small - lo_small)


class GateAdmissionTests(unittest.TestCase):
    def test_min_edge_floor(self):
        self.assertFalse(ces.passes_edge_gates(0.10, 0.15, 0.22))
        self.assertTrue(ces.passes_edge_gates(0.16, 0.15, 0.22))

    def test_extreme_edge_ceiling(self):
        self.assertTrue(ces.passes_edge_gates(0.20, 0.15, 0.22))
        self.assertFalse(ces.passes_edge_gates(0.30, 0.15, 0.22))


class BandLabelTests(unittest.TestCase):
    def test_label_assignment(self):
        bands = ces.DEFAULT_RAW_FV_BANDS
        self.assertEqual(ces.raw_fv_band_label(0.92, bands), "[0.90,0.95)")
        self.assertEqual(ces.raw_fv_band_label(0.99, bands), "[0.95,1.00)")
        self.assertEqual(ces.raw_fv_band_label(0.50, bands), "[0.00,0.55)")

    def test_band_low_parser(self):
        self.assertAlmostEqual(ces._band_low("[0.90,0.95)"), 0.90)
        self.assertIsNone(ces._band_low("garbage"))


class CohortStatsTests(unittest.TestCase):
    def test_empty(self):
        s = ces.cohort_stats([])
        self.assertEqual(s["n"], 0)
        self.assertIsNone(s["win_rate"])

    def test_basic_math(self):
        cands = ces.build_candidates([
            _cand(0.95, 0.60, 1),
            _cand(0.95, 0.60, 1),
            _cand(0.95, 0.60, 0),
            _cand(0.95, 0.60, 0),
        ])
        s = ces.cohort_stats(cands)
        self.assertEqual(s["n"], 4)
        self.assertAlmostEqual(s["win_rate"], 0.5)
        self.assertAlmostEqual(s["avg_ask"], 0.60)
        self.assertAlmostEqual(s["edge_over_breakeven"], 0.5 - 0.60)
        # taker units: win = 1/0.6-1 = 0.6667, loss = -1 -> mean = (2*0.6667 - 2)/4
        self.assertAlmostEqual(s["taker_roi"], (2 * (1 / 0.6 - 1) - 2) / 4, places=6)


class BuildCandidatesTests(unittest.TestCase):
    def test_drops_unlabeled_and_bad_ask(self):
        rows = [
            _cand(0.95, 0.60, 1),
            {"fair_value_raw": 0.95, "decision_ask": 0.60, "target_over_win": None},  # unlabeled
            {"fair_value_raw": None, "decision_ask": 0.60, "target_over_win": 1},  # no raw fv
            {"fair_value_raw": 0.95, "decision_ask": 1.0, "target_over_win": 1},  # ask out of range
        ]
        cands = ces.build_candidates(rows)
        self.assertEqual(len(cands), 1)

    def test_split_filter(self):
        rows = [_cand(0.95, 0.6, 1, split="train"), _cand(0.95, 0.6, 1, split="test")]
        self.assertEqual(len(ces.build_candidates(rows, splits=["test"])), 1)


class SuppressionCohortTests(unittest.TestCase):
    def _apply_curve(self, cands, calibrator):
        for c in cands:
            c.cal_fv = float(calibrator.calibrate(c.raw_fv, model_family="score_event_transition"))
        return cands

    def test_over_shrinking_when_killed_bets_are_winners(self):
        cal = _flat_platt_calibrator()  # pins ~0.72
        # raw 0.95, ask 0.70 -> raw edge 0.25 > 0.22 -> blocked by extreme edge on raw,
        # so use ask that keeps raw edge in (min_edge, extreme]; raw 0.90 ask 0.72 ->
        # raw edge 0.18 passes; cal ~0.72 -> cal edge ~0 fails. These are winners.
        rows = [_cand(0.90, 0.72, 1) for _ in range(25)]
        cands = self._apply_curve(ces.build_candidates(rows), cal)
        sc = ces.suppression_cohort(cands, min_edge=0.15, extreme_edge_max=0.22, enforce_min_raw=0.90)
        self.assertEqual(sc["n"], 25)
        self.assertEqual(sc["verdict"], "over_shrinking")

    def test_justified_when_killed_bets_are_losers(self):
        cal = _flat_platt_calibrator()
        # winners=8 of 25 -> WR 0.32 < ask 0.72 -> killed bets were -EV -> justified
        rows = [_cand(0.90, 0.72, 1 if i < 8 else 0) for i in range(25)]
        cands = self._apply_curve(ces.build_candidates(rows), cal)
        sc = ces.suppression_cohort(cands, min_edge=0.15, extreme_edge_max=0.22, enforce_min_raw=0.90)
        self.assertEqual(sc["n"], 25)
        self.assertEqual(sc["verdict"], "justified")

    def test_insufficient_below_min_n(self):
        cal = _flat_platt_calibrator()
        rows = [_cand(0.90, 0.72, 1) for _ in range(5)]
        cands = self._apply_curve(ces.build_candidates(rows), cal)
        sc = ces.suppression_cohort(cands, min_edge=0.15, extreme_edge_max=0.22, enforce_min_raw=0.90)
        self.assertEqual(sc["verdict"], "insufficient_evidence")

    def test_no_suppression_when_calibration_off_band(self):
        cal = _flat_platt_calibrator()
        # raw 0.85 is below enforce_min_raw 0.90 -> calibration not applied ->
        # cal_fv == raw -> no suppression.
        rows = [_cand(0.85, 0.68, 1) for _ in range(25)]
        cands = self._apply_curve(ces.build_candidates(rows), cal)
        # but band_gated in suppression uses cal_edge from c.cal_fv which we set to
        # calibrated(0.85). The suppression function compares raw vs calibrated FV
        # directly (it models enforce as "calibrated applies"). For a faithful
        # below-band test we set cal_fv = raw to represent kept-raw.
        for c in cands:
            c.cal_fv = c.raw_fv
        sc = ces.suppression_cohort(cands, min_edge=0.15, extreme_edge_max=0.22, enforce_min_raw=0.90)
        self.assertEqual(sc["n"], 0)


class ReliabilityTests(unittest.TestCase):
    def test_closer_to_realized_and_positive_ev(self):
        cal = _flat_platt_calibrator()
        # band [0.90,0.95): raw avg ~0.92, calibrated ~0.72, realized WR 0.80, ask 0.74
        rows = [_cand(0.92, 0.74, 1 if i < 8 else 0) for i in range(10)]
        cands = ces.build_candidates(rows)
        for c in cands:
            c.cal_fv = float(cal.calibrate(c.raw_fv, model_family="score_event_transition"))
        table = ces.reliability_by_raw_band(cands, ces.DEFAULT_RAW_FV_BANDS)
        row = next(r for r in table if r["band"] == "[0.90,0.95)")
        self.assertEqual(row["n"], 10)
        self.assertAlmostEqual(row["win_rate"], 0.8)
        self.assertTrue(row["is_positive_ev"])  # 0.80 > 0.74
        # calibrated ~0.72 is closer to 0.80 than raw ~0.92
        self.assertEqual(row["closer_to_realized"], "calibrated")


class EdgeShavingTests(unittest.TestCase):
    def test_shave_only_counts_enforce_band(self):
        cal = _flat_platt_calibrator()
        rows = [_cand(0.97, 0.6, 1), _cand(0.85, 0.6, 1)]  # one in band, one below
        cands = ces.build_candidates(rows)
        for c in cands:
            c.cal_fv = float(cal.calibrate(c.raw_fv, model_family="score_event_transition"))
        sh = ces.edge_shaving_summary(cands, enforce_min_raw=0.90)
        self.assertEqual(sh["n_in_enforce_band"], 1)
        self.assertEqual(sh["n_total"], 2)
        self.assertGreater(sh["fv_shaved_mean"], 0.2)


class ScenarioSweepTests(unittest.TestCase):
    def test_calibration_off_admits_more_than_enforce(self):
        cal = _flat_platt_calibrator()
        # raw 0.90 ask 0.72: raw edge 0.18 passes min_edge 0.15; cal ~0.72 edge ~0 fails.
        rows = [_cand(0.90, 0.72, 1) for _ in range(20)]
        cands = ces.build_candidates(rows)
        for c in cands:
            c.cal_fv = float(cal.calibrate(c.raw_fv, model_family="score_event_transition"))
        scenarios = ces.gate_scenarios(
            cands,
            enforce_min_raw_sweep=[0.90, 1.01],
            min_edge_sweep=[0.15],
            extreme_edge_max=0.22,
            calibrator=cal,
            model_family="score_event_transition",
        )
        enforce = next(s for s in scenarios if abs(s["enforce_min_raw"] - 0.90) < 1e-9)
        off = next(s for s in scenarios if s["enforce_min_raw"] > 1.0)
        self.assertEqual(enforce["n"], 0)  # calibration kills them all
        self.assertEqual(off["n"], 20)  # calibration off admits all
        self.assertTrue(off["calibration_effectively_off"])


class RecommendationTests(unittest.TestCase):
    def test_picks_threshold_maximizing_total_taker_units(self):
        cal = _flat_platt_calibrator()
        # [0.90,0.95): winners (good to admit); [0.95,1.0): losers (bad to admit).
        good = [_cand(0.92, 0.74, 1) for _ in range(20)]   # WR 1.0 > ask -> +units
        bad = [_cand(0.99, 0.95, 0) for _ in range(20)]    # losers; but raw edge 0.04 < min_edge so excluded anyway
        cands = ces.build_candidates(good + bad)
        for c in cands:
            c.cal_fv = float(cal.calibrate(c.raw_fv, model_family="score_event_transition"))
        rec = ces.recommend_enforce_min_raw(
            cands,
            min_edge=0.15,
            extreme_edge_max=0.22,
            candidate_thresholds=[0.90, 0.95, 1.01],
            baseline_enforce_min_raw=0.90,
        )
        # raising threshold lets the +EV [0.90,0.95) band through.
        self.assertGreaterEqual(rec["recommended_enforce_min_raw"], 0.95)


class VerdictSynthesisTests(unittest.TestCase):
    def _reliability(self, pos_band_ev=True):
        # one enforce-zone band (>=0.90) that is +EV, one that is -EV
        return [
            {"band": "[0.90,0.95)", "n": 147, "is_positive_ev": pos_band_ev},
            {"band": "[0.95,1.00)", "n": 437, "is_positive_ev": False},
        ]

    def _recommendation(self, recommended, grid):
        return {
            "baseline_enforce_min_raw": 0.90,
            "recommended_enforce_min_raw": recommended,
            "by_threshold": grid,
        }

    def test_over_shrinking_partial_when_raising_helps(self):
        rel = self._reliability(pos_band_ev=True)
        grid = [
            {"enforce_min_raw": 0.90, "n_admitted": 45, "total_taker_units": 11.85,
             "win_rate": 0.80, "avg_ask": 0.632, "wilson_lo": 0.662},
            {"enforce_min_raw": 0.95, "n_admitted": 104, "total_taker_units": 20.37,
             "win_rate": 0.837, "avg_ask": 0.705, "wilson_lo": 0.754},
        ]
        rec = self._recommendation(0.95, grid)
        sup = {"verdict": "justified"}  # aggregate masks the per-band story
        v = ces._synthesize_verdict(rel, sup, rec, enforce_min_raw=0.90)
        self.assertEqual(v["label"], "OVER_SHRINKING_PARTIAL")
        self.assertEqual(v["recommended_enforce_min_raw"], 0.95)
        self.assertEqual(v["current_admitted_n"], 45)
        self.assertEqual(v["recommended_admitted_n"], 104)

    def test_justified_when_no_positive_band_and_no_improvement(self):
        rel = self._reliability(pos_band_ev=False)  # both enforce-zone bands -EV
        grid = [
            {"enforce_min_raw": 0.90, "n_admitted": 45, "total_taker_units": 11.85,
             "win_rate": 0.80, "avg_ask": 0.632, "wilson_lo": 0.662},
            {"enforce_min_raw": 0.95, "n_admitted": 104, "total_taker_units": 5.0,
             "win_rate": 0.70, "avg_ask": 0.72, "wilson_lo": 0.60},
        ]
        rec = self._recommendation(0.90, grid)  # raising does not help
        v = ces._synthesize_verdict(rel, {"verdict": "justified"}, rec, enforce_min_raw=0.90)
        self.assertEqual(v["label"], "JUSTIFIED")

    def test_no_recommendation_when_wilson_lb_below_breakeven(self):
        rel = self._reliability(pos_band_ev=True)
        grid = [
            {"enforce_min_raw": 0.90, "n_admitted": 45, "total_taker_units": 11.85,
             "win_rate": 0.80, "avg_ask": 0.632, "wilson_lo": 0.662},
            # higher units but Wilson LB below breakeven -> not a confident raise
            {"enforce_min_raw": 0.95, "n_admitted": 104, "total_taker_units": 20.0,
             "win_rate": 0.74, "avg_ask": 0.73, "wilson_lo": 0.70},
        ]
        rec = self._recommendation(0.95, grid)
        v = ces._synthesize_verdict(rel, {"verdict": "justified"}, rec, enforce_min_raw=0.90)
        self.assertNotEqual(v["label"], "OVER_SHRINKING_PARTIAL")


class EndToEndMainTests(unittest.TestCase):
    def test_main_writes_json_and_md(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # synthetic family table: a +EV [0.90,0.95) band and a -EV [0.95,1.0) tail
            rows = []
            for i in range(40):
                rows.append(_cand(0.92, 0.74, 1 if i < 32 else 0))  # WR 0.80
            for i in range(40):
                rows.append(_cand(0.98, 0.82, 1 if i < 28 else 0))  # WR 0.70 < 0.82
            table = tmp / "family.jsonl"
            with open(table, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
            art = tmp / "cal.json"
            with open(art, "w", encoding="utf-8") as f:
                json.dump({
                    "schema_version": 2,
                    "family_mode": "separate",
                    "default_family": "score_event_transition",
                    "selected_method": "platt",
                    "methods": {"platt": {"params": {"a": 0.05, "b": 0.83}}},
                    "families": {
                        "score_event_transition": {
                            "selected_method": "platt",
                            "methods": {"platt": {"params": {"a": 0.05, "b": 0.83}}},
                        }
                    },
                }, f)
            out = tmp / "out"
            rc = ces.main([
                "--family-table", str(table),
                "--calibration-artifact", str(art),
                "--output-root", str(out),
                "--output-stem", "ces",
            ])
            self.assertEqual(rc, 0)
            jpath = out / "ces.json"
            mpath = out / "ces.md"
            self.assertTrue(jpath.exists())
            self.assertTrue(mpath.exists())
            report = json.loads(jpath.read_text(encoding="utf-8"))
            self.assertIn("verdict", report)
            self.assertIn("reliability_by_raw_band", report)
            self.assertIn("gate_scenarios", report)
            self.assertIn("recommendation", report)
            # md renders the verdict header
            md = mpath.read_text(encoding="utf-8")
            self.assertIn("Calibration Edge-Shaving Deep Dive", md)
            self.assertIn("Verdict", md)

    def test_main_missing_inputs_returns_2(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rc = ces.main([
                "--family-table", str(tmp / "nope.jsonl"),
                "--calibration-artifact", str(tmp / "nope.json"),
            ])
            self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
