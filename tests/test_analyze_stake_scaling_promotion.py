"""Tests for analyze_stake_scaling_promotion.

The analyzer reads filled+settled bets that carry
`calibrated_stake_multiplier`, buckets them into low/mid/high terciles,
and emits a promotion verdict (need_more_data / hold / promote).
This module covers:
  - Bet extraction from session JSON (filter to filled+settled, with
    multiplier present)
  - Tercile cutpoints + bucket assignment under bimodal data (most bets
    clamped to 0.5 or 1.5)
  - Verdict states (need_more_data on session count, need_more_data on
    bucket size, hold when margin too small, promote on a clear win)
  - End-to-end main() writes JSON + Markdown
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import analyze_stake_scaling_promotion as asp  # noqa: E402


def _bet(*, multiplier, won, profit=0.0, stake=10.0, fill_cost=10.0,
         edge_used=None, order_status="filled", settled=True):
    return {
        "calibrated_stake_multiplier": multiplier,
        "calibrated_stake_edge_used": edge_used,
        "stake": stake,
        "fill_cost_usdc": fill_cost,
        "profit": profit,
        "won": won,
        "order_status": order_status,
        "settled": settled,
    }


def _session(date_str: str, bets: list) -> dict:
    return {"date": date_str, "bets": bets}


def _write_session(sessions_dir: Path, payload: dict) -> Path:
    p = sessions_dir / f"{payload['date']}_session.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


class ExtractBetRowsTests(unittest.TestCase):
    def test_drops_unfilled_and_unsettled(self):
        session = _session("2026-05-13", [
            _bet(multiplier=1.5, won=True, profit=5.0),   # keep
            _bet(multiplier=0.8, won=True, order_status="cancelled"),  # drop
            _bet(multiplier=0.8, won=True, settled=False),  # drop
            _bet(multiplier=None, won=True),               # drop (no multiplier)
        ])
        rows = asp.extract_bet_rows(session)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].session_date, "2026-05-13")
        self.assertAlmostEqual(rows[0].multiplier, 1.5)

    def test_reconciled_filled_kept(self):
        session = _session("2026-05-13", [
            _bet(multiplier=1.0, won=False, order_status="reconciled_filled"),
        ])
        rows = asp.extract_bet_rows(session)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].won)

    def test_won_none_dropped(self):
        bet = _bet(multiplier=1.0, won=True)
        bet["won"] = None
        rows = asp.extract_bet_rows(_session("2026-05-13", [bet]))
        self.assertEqual(rows, [])


class TercileBucketingTests(unittest.TestCase):
    def test_cuts_with_bimodal_data(self):
        # Heavy clamp at floor + ceiling: 6 at 0.5, 1 at 0.8, 3 at 1.5
        mults = [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.8, 1.5, 1.5, 1.5]
        lo, hi = asp.tercile_cuts(mults)
        self.assertEqual(lo, 0.5)
        self.assertEqual(hi, 0.8)
        # Bucket assignment: <=0.5 -> low, >=0.8 -> high, else mid
        self.assertEqual(asp.assign_bucket(0.5, lo, hi), "low")
        self.assertEqual(asp.assign_bucket(0.8, lo, hi), "high")
        self.assertEqual(asp.assign_bucket(1.5, lo, hi), "high")
        self.assertEqual(asp.assign_bucket(0.65, lo, hi), "mid")

    def test_aggregate_buckets_counts_and_metrics(self):
        # Pure bimodal: 2 at 0.5, 2 at 1.5. low/high split cleanly, mid empty.
        rows = [
            asp.BetRow("2026-05-13", multiplier=0.5, edge_used=-0.05,
                       stake=10, fill_cost_usdc=10, profit=-10, won=False),
            asp.BetRow("2026-05-13", multiplier=0.5, edge_used=-0.05,
                       stake=10, fill_cost_usdc=10, profit=5.0, won=True),
            asp.BetRow("2026-05-13", multiplier=1.5, edge_used=0.2,
                       stake=10, fill_cost_usdc=10, profit=8.0, won=True),
            asp.BetRow("2026-05-13", multiplier=1.5, edge_used=0.2,
                       stake=10, fill_cost_usdc=10, profit=8.0, won=True),
        ]
        buckets = asp.aggregate_buckets(rows)
        self.assertEqual(buckets["low"].n, 2)
        self.assertEqual(buckets["mid"].n, 0)
        self.assertEqual(buckets["high"].n, 2)
        self.assertAlmostEqual(buckets["low"].win_rate, 0.5)
        self.assertAlmostEqual(buckets["high"].win_rate, 1.0)
        self.assertAlmostEqual(buckets["low"].roi, -5.0 / 20.0)
        self.assertAlmostEqual(buckets["high"].roi, 16.0 / 20.0)


class VerdictTests(unittest.TestCase):
    def _rows(self, low_split, high_split, low_profits=None, high_profits=None,
              n_sessions=30):
        """Build a row set distributed across `n_sessions` distinct dates.

        low_split / high_split are (wins, losses) tuples for the low and high
        multiplier cohorts. Each cohort distributes its rows round-robin
        across the same date set, but every date gets at least one low row
        and one high row so n_sessions truly equals the number of unique
        session_date strings in the output.
        """
        all_dates = [f"2026-04-{i:02d}" for i in range(1, n_sessions + 1)]

        def _make(cohort_split, multiplier, profits):
            wins, losses = cohort_split
            n = wins + losses
            wons = [True] * wins + [False] * losses
            profits = (profits or [0.0] * n)[:n]
            return [
                asp.BetRow(
                    session_date=all_dates[i % n_sessions],
                    multiplier=multiplier, edge_used=None,
                    stake=10.0, fill_cost_usdc=10.0,
                    profit=profits[i] if i < len(profits) else 0.0,
                    won=wons[i],
                )
                for i in range(n)
            ]

        rows = _make(low_split, 0.5, low_profits)
        rows += _make(high_split, 1.5, high_profits)
        return rows

    def test_need_more_data_on_few_sessions(self):
        # 5 sessions, each with 1 low + 1 high row. Plenty per bucket, but
        # session count < threshold.
        rows = self._rows((3, 2), (4, 1), n_sessions=5)
        buckets = asp.aggregate_buckets(rows)
        v = asp.compute_verdict(rows, buckets,
                                min_sessions=30, min_bets_per_bucket=5,
                                promote_min_wr_delta=0.05,
                                promote_min_roi_delta=0.05)
        self.assertEqual(v.label, "need_more_data")
        self.assertIn("sessions", v.reason)

    def test_need_more_data_on_thin_bucket(self):
        # 30 sessions covered (low cohort covers all), but high cohort has
        # only 2 bets total -- bucket gate fires before promotion gate.
        rows = self._rows((15, 15), (1, 1), n_sessions=30)
        buckets = asp.aggregate_buckets(rows)
        v = asp.compute_verdict(rows, buckets,
                                min_sessions=30, min_bets_per_bucket=5,
                                promote_min_wr_delta=0.05,
                                promote_min_roi_delta=0.05)
        self.assertEqual(v.label, "need_more_data")
        self.assertIn("bucket", v.reason)

    def test_hold_when_margin_too_small(self):
        # Both cohorts identical (15W/15L, P&L flat) -> WR & ROI deltas = 0
        flat_profits = [5.0] * 15 + [-5.0] * 15
        rows = self._rows(
            (15, 15), (15, 15),
            low_profits=flat_profits,
            high_profits=flat_profits,
            n_sessions=30,
        )
        buckets = asp.aggregate_buckets(rows)
        v = asp.compute_verdict(rows, buckets,
                                min_sessions=30, min_bets_per_bucket=5,
                                promote_min_wr_delta=0.05,
                                promote_min_roi_delta=0.05)
        self.assertEqual(v.label, "hold")
        self.assertAlmostEqual(v.wr_delta, 0.0)
        self.assertAlmostEqual(v.roi_delta, 0.0)

    def test_promote_on_clear_win(self):
        # Low: 15W/15L (50%), net 0 -> ROI 0
        # High: 24W/6L (80%), net +$90 on $300 stake -> ROI +0.30
        rows = self._rows(
            (15, 15), (24, 6),
            low_profits=[5.0] * 15 + [-5.0] * 15,
            high_profits=[5.0] * 24 + [-5.0] * 6,
            n_sessions=30,
        )
        buckets = asp.aggregate_buckets(rows)
        v = asp.compute_verdict(rows, buckets,
                                min_sessions=30, min_bets_per_bucket=5,
                                promote_min_wr_delta=0.05,
                                promote_min_roi_delta=0.05)
        self.assertEqual(v.label, "promote")
        self.assertGreater(v.wr_delta, 0.25)
        self.assertGreater(v.roi_delta, 0.25)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ssa_e2e_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.sessions_dir = self.tmp / "sessions"
        self.sessions_dir.mkdir(parents=True)
        self.output_dir = self.tmp / "out"

        # Two days of shadow data -- enough to render the report, not enough
        # to promote.
        _write_session(self.sessions_dir, _session("2026-05-12", [
            _bet(multiplier=0.5, won=True, profit=5.0, edge_used=-0.05),
            _bet(multiplier=0.5, won=False, profit=-10.0, edge_used=-0.05),
            _bet(multiplier=1.5, won=True, profit=8.0, edge_used=0.20),
        ]))
        _write_session(self.sessions_dir, _session("2026-05-13", [
            _bet(multiplier=0.5, won=False, profit=-10.0, edge_used=-0.04),
            _bet(multiplier=1.5, won=True, profit=8.0, edge_used=0.18),
        ]))

    def test_main_writes_artifacts(self):
        rc = asp.main([
            "--sessions-dir", str(self.sessions_dir),
            "--output-dir", str(self.output_dir),
        ])
        self.assertEqual(rc, 0)
        json_path = self.output_dir / "stake_scaling_analysis.json"
        md_path = self.output_dir / "stake_scaling_analysis.md"
        self.assertTrue(json_path.exists())
        self.assertTrue(md_path.exists())

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        # 2 sessions, way below threshold -> need_more_data
        self.assertEqual(payload["verdict"], "need_more_data")
        self.assertEqual(payload["n_sessions"], 2)
        self.assertEqual(payload["n_filled_bets"], 5)
        self.assertIn("low", payload["buckets"])
        self.assertIn("mid", payload["buckets"])
        self.assertIn("high", payload["buckets"])

        md_text = md_path.read_text(encoding="utf-8")
        self.assertIn("Stake-scaling promotion analyzer", md_text)
        self.assertIn("need_more_data", md_text)
        self.assertIn("| low |", md_text)


if __name__ == "__main__":
    unittest.main()
