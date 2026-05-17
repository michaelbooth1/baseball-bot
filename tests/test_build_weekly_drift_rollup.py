"""Tests for build_weekly_drift_rollup.

The script is a pure transform: read N daily-review JSONs, render one
HTML page. We verify:
  - per-day metric extraction (including the attempt-vs-CLOB-success
    fill-rate semantics that motivated the 2026-05-13 fix)
  - window selection
  - end-to-end main() writes the dated + canonical files
  - rendered HTML carries the expected sections + KPI text
  - empty input yields a non-crashing "no data" page
"""

import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_weekly_drift_rollup as wdr  # noqa: E402


def _review_payload(*, session_date: str, mode: str = "live",
                    placed_clob: int = 5, filled_clob: int = 5,
                    placed_attempts: int = None, filled_attempts: int = None,
                    profit: float = 5.0, roi: float = 0.05,
                    win_rate: float = 0.6,
                    cal_alerts=None, fill_alerts=None,
                    sig_alerts=None, regime_alerts=None, recon_alerts=None,
                    tvds=None, recon_share: float = 0.0) -> dict:
    """Build a minimal daily-review JSON payload for tests."""
    if placed_attempts is None:
        placed_attempts = placed_clob
    if filled_attempts is None:
        filled_attempts = filled_clob
    fill_rate = (filled_attempts / placed_attempts) if placed_attempts else None
    return {
        "schema_version": 1,
        "session_date": session_date,
        "mode": mode,
        "session_summary": {
            "orders_placed": placed_clob,
            "orders_filled": filled_clob,
            "total_profit": profit,
            "roi": roi,
            "win_rate": win_rate,
            "signal_win_rate": win_rate,
        },
        "calibration_health": {"alerts": list(cal_alerts or [])},
        "fill_rate_health": {
            "today": {"placed": placed_attempts, "filled": filled_attempts,
                      "fill_rate": fill_rate},
            "alerts": list(fill_alerts or []),
        },
        "signal_quality_health": {"alerts": list(sig_alerts or [])},
        "regime_mix_health": {
            "tvd_by_dimension": dict(tvds or {}),
            "alerts": list(regime_alerts or []),
        },
        "reconciler_summary": {
            "reconciled_share": recon_share,
            "alerts": list(recon_alerts or []),
        },
    }


def _write_review(input_dir: Path, payload: dict) -> Path:
    p = input_dir / f"{payload['session_date']}_human_review.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


class ExtractDailyMetricsTests(unittest.TestCase):
    def test_minimal_payload(self):
        m = wdr.extract_daily_metrics(_review_payload(session_date="2026-05-12"))
        self.assertEqual(m.session_date, "2026-05-12")
        self.assertEqual(m.orders_placed, 5)
        self.assertEqual(m.orders_filled, 5)
        self.assertEqual(m.placement_attempts, 5)
        self.assertEqual(m.filled_attempts, 5)
        self.assertAlmostEqual(m.fill_rate, 1.0)
        self.assertAlmostEqual(m.roi, 0.05)

    def test_attempt_vs_clob_split(self):
        """Wallet-error day: 70 attempts, only 8 made it to the CLOB and filled."""
        m = wdr.extract_daily_metrics(_review_payload(
            session_date="2026-05-12",
            placed_clob=8, filled_clob=8,
            placed_attempts=70, filled_attempts=8,
        ))
        self.assertEqual(m.orders_placed, 8)
        self.assertEqual(m.placement_attempts, 70)
        self.assertAlmostEqual(m.fill_rate, 8 / 70, places=4)

    def test_alert_counts_aggregate(self):
        m = wdr.extract_daily_metrics(_review_payload(
            session_date="2026-05-12",
            cal_alerts=["a", "b"],
            fill_alerts=["c"],
            sig_alerts=[],
            regime_alerts=["d", "e", "f"],
            recon_alerts=[],
        ))
        self.assertEqual(m.calibration_alerts, 2)
        self.assertEqual(m.fill_rate_alerts, 1)
        self.assertEqual(m.signal_quality_alerts, 0)
        self.assertEqual(m.regime_mix_alerts, 3)
        # raw_alerts collects them all with the dim tag
        dims = {dim for dim, _ in m.raw_alerts}
        self.assertEqual(dims, {"calibration", "fill_rate", "regime_mix"})

    def test_max_tvd_picks_largest_dimension(self):
        m = wdr.extract_daily_metrics(_review_payload(
            session_date="2026-05-12",
            tvds={"ask_bucket": 0.20, "phantom_risk_band": 0.55, "current_state_edge_bucket": 0.18},
        ))
        self.assertAlmostEqual(m.regime_mix_max_tvd, 0.55)


class WindowSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wdr_window_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        for d in ("2026-05-06", "2026-05-08", "2026-05-09", "2026-05-12"):
            _write_review(self.tmp, _review_payload(session_date=d))

    def test_discover_sorted_by_date(self):
        files = wdr.discover_review_files(self.tmp)
        dates = [d for d, _ in files]
        self.assertEqual(dates, [date(2026, 5, 6), date(2026, 5, 8),
                                 date(2026, 5, 9), date(2026, 5, 12)])

    def test_window_trailing_inclusive(self):
        files = wdr.discover_review_files(self.tmp)
        window = wdr.select_window(files, end_date=date(2026, 5, 12), days=7)
        # 7-day window ending 2026-05-12 includes 2026-05-06 (start)
        self.assertEqual([d.isoformat() for d, _ in window],
                         ["2026-05-06", "2026-05-08", "2026-05-09", "2026-05-12"])

    def test_window_short_excludes_old_files(self):
        files = wdr.discover_review_files(self.tmp)
        window = wdr.select_window(files, end_date=date(2026, 5, 12), days=3)
        # 3-day window: 2026-05-10, 11, 12 -- only 2026-05-12 has data
        self.assertEqual([d.isoformat() for d, _ in window], ["2026-05-12"])


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wdr_e2e_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.input_dir = self.tmp / "daily_human_review"
        self.input_dir.mkdir(parents=True)
        self.output_dir = self.tmp / "weekly_rollup"

        # 7 sessions: a healthy run that ends with a wallet-error day.
        _write_review(self.input_dir, _review_payload(
            session_date="2026-05-06", profit=3.16, roi=0.32,
            placed_clob=3, filled_clob=3, placed_attempts=3, filled_attempts=3))
        _write_review(self.input_dir, _review_payload(
            session_date="2026-05-07", profit=10.46, roi=0.52,
            placed_clob=2, filled_clob=2, placed_attempts=2, filled_attempts=2))
        _write_review(self.input_dir, _review_payload(
            session_date="2026-05-08", profit=6.33, roi=0.21,
            placed_clob=3, filled_clob=2, placed_attempts=3, filled_attempts=2,
            fill_alerts=["fill rate dropped 33pp"]))
        _write_review(self.input_dir, _review_payload(
            session_date="2026-05-09", profit=0.0, roi=0.0,
            placed_clob=0, filled_clob=0, placed_attempts=0, filled_attempts=0))
        _write_review(self.input_dir, _review_payload(
            session_date="2026-05-10", profit=-10.0, roi=-1.0,
            placed_clob=1, filled_clob=0, placed_attempts=1, filled_attempts=0))
        _write_review(self.input_dir, _review_payload(
            session_date="2026-05-11", profit=6.66, roi=0.66,
            placed_clob=1, filled_clob=1, placed_attempts=1, filled_attempts=1))
        # Wallet-error day: 70 attempts, only 8 to the CLOB
        _write_review(self.input_dir, _review_payload(
            session_date="2026-05-12", profit=3.22, roi=0.04,
            placed_clob=8, filled_clob=8,
            placed_attempts=70, filled_attempts=8,
            fill_alerts=["fill rate dropped 72pp"],
            cal_alerts=["calibration drift detected"],
            regime_alerts=["ask_bucket: TVD=0.36"],
            tvds={"ask_bucket": 0.36, "phantom_risk_band": 0.49}))

    def test_main_writes_dated_and_canonical(self):
        rc = wdr.main([
            "--input-dir", str(self.input_dir),
            "--output-dir", str(self.output_dir),
            "--days", "7",
            "--end-date", "2026-05-12",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue((self.output_dir / "2026-05-12_weekly_rollup.html").exists())
        self.assertTrue((self.output_dir / "weekly_rollup.html").exists())

    def test_html_contents(self):
        wdr.main([
            "--input-dir", str(self.input_dir),
            "--output-dir", str(self.output_dir),
            "--days", "7",
            "--end-date", "2026-05-12",
        ])
        html = (self.output_dir / "weekly_rollup.html").read_text(encoding="utf-8")

        # Section headings present.
        for tag in ("Weekly Drift Rollup", "Active alerts", "Trend panel",
                    "Per-day detail", "Daily ROI", "Cumulative P&amp;L",
                    "Fill rate", "Filled WR", "Calibration alerts",
                    "Regime-mix max TVD", "Reconciler recovered share"):
            self.assertIn(tag, html, msg=f"missing section/panel: {tag}")

        # 8 sparkline SVGs (one per panel).
        self.assertEqual(html.count("<svg"), 8)

        # Per-day table has 7 rows (one per session).
        self.assertEqual(html.count("<tr><td>2026-"), 7)

        # KPI rollup uses the ATTEMPT denominator: 16 fills / 80 attempts.
        # Window total: clob filled = 3+2+2+0+0+1+8 = 16; attempts = 3+2+3+0+1+1+70 = 80.
        self.assertIn("16 filled / 80 attempts", html)
        # Aggregate fill rate = 16 / 80 = 20.0%
        self.assertIn("20.0%", html)

        # Active alerts feed surfaces alerts from days 6, 8, 12.
        self.assertIn("calibration drift detected", html)
        self.assertIn("fill rate dropped 72pp", html)
        # Newest session first ordering: 2026-05-12 entries appear before 2026-05-08
        i_12 = html.find("fill rate dropped 72pp")
        i_08 = html.find("fill rate dropped 33pp")
        self.assertTrue(0 < i_12 < i_08, "newest alerts should be first")

        # The wallet-error attempt > placed cell gets highlighted with bad class.
        self.assertIn('class="bad">70<', html)

    def test_empty_window_renders_safe_page(self):
        empty_input = self.tmp / "empty_input"
        empty_input.mkdir()
        rc = wdr.main([
            "--input-dir", str(empty_input),
            "--output-dir", str(self.output_dir),
            "--days", "7",
            "--end-date", "2026-05-12",
        ])
        # Empty input is not a hard failure; nothing is written but exit 0.
        self.assertEqual(rc, 0)
        self.assertFalse((self.output_dir / "weekly_rollup.html").exists())


if __name__ == "__main__":
    unittest.main()
