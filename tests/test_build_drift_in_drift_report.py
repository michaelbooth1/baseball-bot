"""Tests for build_drift_in_drift_report (slow-creep drift detection).

The companion to concept_drift_report. concept_drift fires on day-vs-
baseline PSI (>= 0.25); this script fits an OLS slope on trailing 30d
of psi_history.jsonl and projects 30d forward to catch features that
drift slowly enough to never cross the daily threshold but accumulate
past it over weeks.

Tests focus on:
  - OLS math (perfect linear -> exact slope; flat -> zero; constant x -> handled)
  - Projection formula (intercept + slope * (last_x + horizon), clamped >= 0)
  - Verdict thresholds (projection >= 0.25 -> major; >= 0.10 -> minor; else stable)
  - Insufficient history -> insufficient_history verdict, no exception
  - History loading (skip malformed, exclude null values, same-date dedup)
  - End-to-end on synthetic history that fires the alert exactly when expected
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_drift_in_drift_report as didr  # noqa: E402


class OlsMathTests(unittest.TestCase):
    def test_perfect_linear_recovers_exact_slope(self):
        xs = list(range(10))
        ys = [2 * x + 5 for x in xs]  # slope=2, intercept=5
        slope, intercept, r2 = didr.ols_slope_intercept(xs, [float(y) for y in ys])
        self.assertAlmostEqual(slope, 2.0, places=6)
        self.assertAlmostEqual(intercept, 5.0, places=6)
        self.assertAlmostEqual(r2, 1.0, places=6)

    def test_flat_input_gives_zero_slope(self):
        xs = list(range(10))
        ys = [0.05] * 10
        slope, intercept, r2 = didr.ols_slope_intercept(xs, ys)
        self.assertAlmostEqual(slope, 0.0)
        self.assertAlmostEqual(intercept, 0.05)
        self.assertEqual(r2, 0.0)  # constant y -> R^2 undefined, returned 0

    def test_constant_x_returns_safe_default(self):
        # Degenerate case: all xs equal. Slope is undefined; helper should
        # return 0/mean rather than dividing by zero.
        xs = [5, 5, 5, 5]
        ys = [0.1, 0.2, 0.3, 0.4]
        slope, intercept, _ = didr.ols_slope_intercept(xs, ys)
        self.assertEqual(slope, 0.0)
        self.assertAlmostEqual(intercept, 0.25)

    def test_single_point(self):
        slope, intercept, r2 = didr.ols_slope_intercept([3.0], [0.07])
        self.assertEqual(slope, 0.0)
        self.assertEqual(intercept, 0.07)
        self.assertEqual(r2, 0.0)

    def test_empty(self):
        slope, intercept, r2 = didr.ols_slope_intercept([], [])
        self.assertEqual(slope, 0.0)
        self.assertEqual(intercept, 0.0)
        self.assertEqual(r2, 0.0)

    def test_noisy_input_recovers_roughly_correct_slope(self):
        # Linear with noise: slope ~0.01, noise ~0.001 -> r^2 should be high.
        xs = [float(i) for i in range(30)]
        ys = [0.05 + 0.01 * i + (0.001 if i % 2 == 0 else -0.001) for i in range(30)]
        slope, _, r2 = didr.ols_slope_intercept(xs, ys)
        self.assertAlmostEqual(slope, 0.01, places=3)
        self.assertGreater(r2, 0.99)


class ProjectionTests(unittest.TestCase):
    def test_positive_slope_projects_forward(self):
        # intercept + slope * (last_x + horizon) = 0.05 + 0.01 * (29 + 30) = 0.64
        proj = didr.project_psi(slope=0.01, intercept=0.05, last_x=29.0, horizon_days=30)
        self.assertAlmostEqual(proj, 0.64, places=6)

    def test_negative_slope_clamped_to_zero(self):
        # PSI is non-negative; a negative projection should be clamped.
        proj = didr.project_psi(slope=-0.01, intercept=0.05, last_x=29.0, horizon_days=30)
        self.assertEqual(proj, 0.0)

    def test_zero_slope_returns_intercept(self):
        proj = didr.project_psi(slope=0.0, intercept=0.07, last_x=0.0, horizon_days=30)
        self.assertAlmostEqual(proj, 0.07)


class EvaluateFeatureTests(unittest.TestCase):
    def test_insufficient_history_below_minimum(self):
        # 5 points, min_points=7
        points = [(-i, 0.05 + 0.001 * i) for i in reversed(range(5))]
        info = didr.evaluate_feature(
            "test_feature", points, min_points=7, horizon_days=30,
        )
        self.assertEqual(info["verdict"], "insufficient_history")
        self.assertEqual(info["n_points"], 5)

    def test_stable_feature_below_minor_threshold(self):
        # PSI hovering ~0.03 with zero slope -> stable
        points = [(float(-i), 0.03) for i in reversed(range(30))]
        info = didr.evaluate_feature(
            "stable", points, min_points=7, horizon_days=30,
            psi_minor=0.10, psi_major=0.25,
        )
        self.assertEqual(info["verdict"], "stable")
        self.assertLess(info["projected_psi"], 0.10)

    def test_slow_creep_major_fires(self):
        # 28 days of PSI drifting 0.008/day starting from 0.05.
        # day 0 (last) PSI = 0.05 + 27*0.008 = 0.266
        # projection at day 30 forward from day 0: intercept + slope*(0 + 30)
        # OLS intercept ~ 0.266 (last) - slope*27 = roughly the initial 0.05
        # So projected_psi = ~0.05 + 0.008 * (0 + 30) = ~0.29 -> MAJOR
        points = [(float(i - 27), 0.05 + 0.008 * i) for i in range(28)]
        info = didr.evaluate_feature(
            "slow_creep", points, min_points=7, horizon_days=30,
            psi_minor=0.10, psi_major=0.25,
        )
        self.assertEqual(info["verdict"], "major")
        self.assertGreater(info["projected_psi"], 0.25)
        self.assertAlmostEqual(info["slope_per_day"], 0.008, places=4)
        self.assertGreater(info["r_squared"], 0.99)

    def test_minor_creep_does_not_fire_major(self):
        # PSI drifting 0.003/day from 0.05. After 30d projection: should
        # land between 0.10 and 0.25 -> minor.
        points = [(float(i - 27), 0.05 + 0.003 * i) for i in range(28)]
        info = didr.evaluate_feature(
            "minor_creep", points, min_points=7, horizon_days=30,
            psi_minor=0.10, psi_major=0.25,
        )
        self.assertEqual(info["verdict"], "minor")

    def test_improving_feature_returns_stable(self):
        # PSI trending DOWN (drifting back toward baseline) -> projection
        # clamps to 0 -> stable.
        points = [(float(i - 27), 0.20 - 0.005 * i) for i in range(28)]
        info = didr.evaluate_feature(
            "improving", points, min_points=7, horizon_days=30,
        )
        self.assertEqual(info["verdict"], "stable")
        self.assertEqual(info["projected_psi"], 0.0)


class HistoryLoadingTests(unittest.TestCase):
    def test_empty_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "psi_history.jsonl"
            self.assertEqual(didr.load_psi_history(p), [])

    def test_skip_malformed_lines(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "psi_history.jsonl"
            p.write_text(
                '{"feature": "a", "value": 0.1, "active_date": "2026-05-15"}\n'
                'not a json line\n'
                '{"feature": "b", "value": 0.2, "active_date": "2026-05-15"}\n',
                encoding="utf-8",
            )
            rows = didr.load_psi_history(p)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["feature"], "a")
            self.assertEqual(rows[1]["feature"], "b")

    def test_trailing_window_filters_inclusive_endpoints(self):
        rows = [
            {"active_date": "2026-04-15", "feature": "x", "value": 0.05},  # outside (29d ago)
            {"active_date": "2026-04-16", "feature": "x", "value": 0.05},  # inside
            {"active_date": "2026-05-15", "feature": "x", "value": 0.05},  # active_date
        ]
        out = didr.trailing_window(
            rows, active_date="2026-05-15", window_days=30,
        )
        dates = sorted(r["active_date"] for r in out)
        self.assertEqual(dates, ["2026-04-16", "2026-05-15"])

    def test_per_feature_series_excludes_null_value_rows(self):
        # insufficient_data verdicts have value=None and must NOT contribute
        # to the slope fit (would skew the trend).
        rows = [
            {"active_date": "2026-05-10", "feature": "x", "value": 0.05,
             "generated_at_utc": "2026-05-10T01:00:00Z"},
            {"active_date": "2026-05-11", "feature": "x", "value": None,
             "generated_at_utc": "2026-05-11T01:00:00Z"},
            {"active_date": "2026-05-12", "feature": "x", "value": 0.07,
             "generated_at_utc": "2026-05-12T01:00:00Z"},
        ]
        series = didr.per_feature_series(rows, active_date="2026-05-15")
        self.assertIn("x", series)
        self.assertEqual(len(series["x"]), 2)

    def test_per_feature_series_dedupes_same_date_latest_wins(self):
        # Two rows for same (feature, date) -> keep the one with the most
        # recent generated_at_utc (a re-run later in the day).
        rows = [
            {"active_date": "2026-05-10", "feature": "x", "value": 0.05,
             "generated_at_utc": "2026-05-10T01:00:00Z"},
            {"active_date": "2026-05-10", "feature": "x", "value": 0.20,  # later run
             "generated_at_utc": "2026-05-10T22:00:00Z"},
        ]
        series = didr.per_feature_series(rows, active_date="2026-05-15")
        # Only one (day_index, psi) for that date; later value wins.
        psis = [psi for _, psi in series["x"]]
        self.assertEqual(psis, [0.20])

    def test_per_feature_series_day_index_relative_to_active_date(self):
        rows = [
            {"active_date": "2026-05-13", "feature": "x", "value": 0.05},
            {"active_date": "2026-05-15", "feature": "x", "value": 0.07},
        ]
        series = didr.per_feature_series(rows, active_date="2026-05-15")
        # day_index = days since active_date; today = 0, 2 days ago = -2
        day_indices = sorted(x for x, _ in series["x"])
        self.assertEqual(day_indices, [-2.0, 0.0])


class BuildReportEndToEndTests(unittest.TestCase):
    def _write_history(self, path: Path, rows: list) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8",
        )

    def test_synthetic_data_fires_alert_on_slow_creep(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            base = datetime.strptime("2026-05-16", "%Y-%m-%d")
            rows = []
            for i in range(28):
                date = (base - timedelta(days=27 - i)).strftime("%Y-%m-%d")
                # slow_creep: 0.008/day from 0.05 -> projects past major
                rows.append({
                    "generated_at_utc": f"{date}T12:00:00Z",
                    "active_date": date,
                    "feature": "slow_creep_major",
                    "value": 0.05 + 0.008 * i,
                })
                # stable: never moves
                rows.append({
                    "generated_at_utc": f"{date}T12:00:00Z",
                    "active_date": date,
                    "feature": "stable_feature",
                    "value": 0.03,
                })
            self._write_history(td / "psi_history.jsonl", rows)
            payload = didr.build_report(
                history_path=td / "psi_history.jsonl",
                active_date="2026-05-16",
            )
            self.assertEqual(payload["n_features_evaluated"], 2)
            self.assertEqual(payload["features"]["slow_creep_major"]["verdict"], "major")
            self.assertEqual(payload["features"]["stable_feature"]["verdict"], "stable")
            self.assertEqual(len(payload["alerts"]), 1)
            self.assertIn("slow_creep_major", payload["alerts"][0])

    def test_empty_history_returns_no_features_no_alerts(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "psi_history.jsonl").write_text("", encoding="utf-8")
            payload = didr.build_report(
                history_path=td / "psi_history.jsonl",
                active_date="2026-05-16",
            )
            self.assertEqual(payload["n_features_evaluated"], 0)
            self.assertEqual(payload["alerts"], [])

    def test_missing_history_file_handled_gracefully(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            payload = didr.build_report(
                history_path=td / "absent.jsonl",
                active_date="2026-05-16",
            )
            self.assertEqual(payload["n_features_evaluated"], 0)
            self.assertEqual(payload["alerts"], [])

    def test_main_writes_files(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            (td / "psi_history.jsonl").write_text("", encoding="utf-8")
            out_root = td / "out"
            rc = didr.main([
                "--history-path", str(td / "psi_history.jsonl"),
                "--output-root", str(out_root),
                "--active-date", "2026-05-16",
            ])
            self.assertEqual(rc, 0)
            self.assertTrue((out_root / "drift_in_drift_report.json").exists())
            self.assertTrue((out_root / "drift_in_drift_report.md").exists())

    def test_strict_returns_nonzero_on_major_alert(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            base = datetime.strptime("2026-05-16", "%Y-%m-%d")
            rows = [
                {
                    "generated_at_utc": f"{(base - timedelta(days=27 - i)).strftime('%Y-%m-%d')}T12:00:00Z",
                    "active_date": (base - timedelta(days=27 - i)).strftime("%Y-%m-%d"),
                    "feature": "slow_creep_major",
                    "value": 0.05 + 0.008 * i,
                }
                for i in range(28)
            ]
            self._write_history(td / "psi_history.jsonl", rows)
            rc = didr.main([
                "--history-path", str(td / "psi_history.jsonl"),
                "--output-root", str(td / "out"),
                "--active-date", "2026-05-16",
                "--strict",
            ])
            self.assertEqual(rc, 1)


class CliEntryPointTests(unittest.TestCase):
    def test_parse_args_defaults(self):
        args = didr.parse_args([])
        self.assertEqual(args.history_window_days, 30)
        self.assertEqual(args.projection_horizon_days, 30)
        self.assertEqual(args.min_points_for_trend, 7)
        self.assertEqual(args.psi_minor, 0.10)
        self.assertEqual(args.psi_major, 0.25)
        self.assertFalse(args.strict)


if __name__ == "__main__":
    unittest.main()
