"""Tests for build_gate_counterfactual_report (Active #11).

The counterfactual builder replays every GateDef from the cert library
against three time windows (all / trailing_30d / trailing_7d) and
computes the realized P&L delta of each alt threshold vs. the current
production threshold. It also surfaces a `top_recommendations` ranking
of the highest-impact tightenings.

Coverage:
  - Window slicing (anchor = latest session date in the rows)
  - is_tightening direction inference for max- and min-direction gates
  - counterfactual_profit_delta_vs_current math (the load-bearing
    formula -- positive = improvement vs. current threshold)
  - kept_roi_delta_vs_current math
  - applicability predicates carried through window slicing
  - shadow-only gates excluded from top_recommendations
  - top_recommendations sort + min-delta + min-blocked-n filters
  - Confidence label boundaries
  - End-to-end main() writes JSON + Markdown
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_gate_counterfactual_report as cfr  # noqa: E402
import build_walk_forward_certification as cert  # noqa: E402


def _bet_row(**overrides) -> cert.BetRow:
    base: Dict[str, Any] = {
        "session_date": "2026-05-01",
        "family": "score_event_transition",
        "line": 7.5,
        "inning": 6,
        "runs_needed": 1.5,
        "decision_ask": 0.70,
        "edge_at_ask": 0.18,
        "fair_value": 0.88,
        "limit_price": 0.68,
        "current_state_edge": 0.05,
        "phantom_risk_band": "low",
        "target_filled": 1,
        "target_win": 1,
        "target_profit": 4.0,
        "current_total": 6,
        "lead_abs": 1,
        "base_fair_value": 0.86,
        "stage2_run_env_delta": -0.05,
    }
    base.update(overrides)
    return cert.BetRow(**base)


def _date_shift(days_before_anchor: int, anchor: str = "2026-05-17") -> str:
    """Return a session_date string `days_before_anchor` days before anchor."""
    dt = datetime.strptime(anchor, "%Y-%m-%d") - timedelta(days=days_before_anchor)
    return dt.strftime("%Y-%m-%d")


class WindowSlicingTests(unittest.TestCase):
    """Anchor is the LATEST session_date in the rows, not today's date."""

    def test_anchors_on_latest_row_not_today(self):
        # Latest row is 2026-04-15; 7-day window should hit 2026-04-09..15
        rows = [
            _bet_row(session_date="2026-04-15"),
            _bet_row(session_date="2026-04-10"),
            _bet_row(session_date="2026-04-08"),   # outside trailing_7d
            _bet_row(session_date="2026-04-01"),   # outside trailing_30d? no, 14d
        ]
        windows = cfr.slice_windows(rows)
        self.assertEqual(windows["all"].date_max, "2026-04-15")
        self.assertEqual(len(windows["trailing_7d"].rows), 2)
        # The 04-08 row is 7 days before 04-15 inclusive -> belongs to 7d
        # (window is [latest - 6, latest]). Wait: 04-15 - 6 = 04-09, so
        # 04-08 is OUTSIDE.
        d7_dates = sorted({r.session_date for r in windows["trailing_7d"].rows})
        self.assertEqual(d7_dates, ["2026-04-10", "2026-04-15"])

    def test_empty_rows_produces_empty_windows(self):
        windows = cfr.slice_windows([])
        for name in ("all", "trailing_30d", "trailing_7d"):
            self.assertEqual(len(windows[name].rows), 0)
            self.assertIsNone(windows[name].date_min)
            self.assertIsNone(windows[name].date_max)

    def test_invalid_date_excluded_from_anchor(self):
        rows = [
            _bet_row(session_date="not-a-date"),
            _bet_row(session_date="2026-05-01"),
        ]
        windows = cfr.slice_windows(rows)
        # Latest derived from valid row
        self.assertEqual(windows["all"].date_max, "2026-05-01")
        # 7d window still anchored on 05-01
        self.assertIn("2026-05-01", {r.session_date for r in windows["trailing_7d"].rows})

    def test_trailing_30d_includes_exactly_30_calendar_days(self):
        anchor = "2026-05-17"
        rows = [
            _bet_row(session_date=anchor),                 # day 0
            _bet_row(session_date=_date_shift(15, anchor)),  # day -15
            _bet_row(session_date=_date_shift(29, anchor)),  # day -29 (inside)
            _bet_row(session_date=_date_shift(30, anchor)),  # day -30 (outside)
        ]
        windows = cfr.slice_windows(rows)
        d30 = {r.session_date for r in windows["trailing_30d"].rows}
        self.assertIn(_date_shift(29, anchor), d30)
        self.assertNotIn(_date_shift(30, anchor), d30)


class TighteningDirectionTests(unittest.TestCase):
    """For max-direction gates LOWER tightens; for min-direction gates HIGHER tightens."""

    def test_max_direction_lower_is_tightening(self):
        g = cert.GATE_DEFS[0]  # gate_extreme_edge (max, current 0.22)
        self.assertEqual(g.direction, "max")
        self.assertEqual(g.current_threshold, 0.22)
        self.assertIs(cfr._is_tightening(g, 0.18), True)
        self.assertIs(cfr._is_tightening(g, 0.30), False)
        self.assertIsNone(cfr._is_tightening(g, 0.22))  # equals current

    def test_min_direction_higher_is_tightening(self):
        g = next(x for x in cert.GATE_DEFS if x.name == "gate_min_edge")
        self.assertEqual(g.direction, "min")
        self.assertIs(cfr._is_tightening(g, 0.12), True)
        self.assertIs(cfr._is_tightening(g, 0.08), False)

    def test_shadow_only_gate_returns_none(self):
        g = next(x for x in cert.GATE_DEFS if x.shadow_only)
        self.assertIsNone(cfr._is_tightening(g, 0.05))


class CounterfactualMathTests(unittest.TestCase):
    """The dollar-delta formula is the load-bearing piece of this report."""

    def _gate_extreme_edge(self):
        return next(x for x in cert.GATE_DEFS if x.name == "gate_extreme_edge")

    def test_tightening_blocks_a_losing_bet_saves_dollars(self):
        # gate_extreme_edge: current 0.22 (block edge > 0.22).
        # Build 4 bets: 3 within current keep, 1 losing bet at edge 0.25.
        rows = [
            _bet_row(edge_at_ask=0.15, target_win=1, target_profit=4.0),
            _bet_row(edge_at_ask=0.18, target_win=1, target_profit=4.0),
            _bet_row(edge_at_ask=0.20, target_win=1, target_profit=4.0),
            _bet_row(edge_at_ask=0.25, target_win=0, target_profit=-7.0),
        ]
        # Wait -- 0.25 > 0.22, so current threshold ALREADY blocks it.
        # Re-test: put the losing bet at edge=0.20 (kept by current 0.22,
        # would be blocked by 0.18) so tightening 0.22 -> 0.18 saves $7.
        rows = [
            _bet_row(edge_at_ask=0.15, target_win=1, target_profit=4.0),
            _bet_row(edge_at_ask=0.16, target_win=1, target_profit=4.0),
            _bet_row(edge_at_ask=0.20, target_win=0, target_profit=-7.0),  # tightening removes this
        ]
        g = self._gate_extreme_edge()
        sweep = cfr.evaluate_sweep_for_window(rows, g)
        # Find the 0.18 row
        s_018 = next(s for s in sweep if s.threshold == 0.18)
        # Tightening removes the 0.20 losing bet (-$7).
        # cf_delta = cur_blocked.profit - alt_blocked.profit
        #          = 0 - (-7) = +7   (we save $7)
        self.assertAlmostEqual(
            s_018.counterfactual_profit_delta_vs_current, 7.0, places=2,
        )
        self.assertIs(s_018.is_tightening, True)
        # The current 0.22 anchor row has cf_delta=None (it IS the current)
        s_022 = next(s for s in sweep if s.threshold == 0.22)
        self.assertTrue(s_022.is_current)
        self.assertIsNone(s_022.counterfactual_profit_delta_vs_current)

    def test_loosening_unblocking_a_winning_bet_shows_positive_delta(self):
        # The current threshold blocks a WINNING bet (rare but possible).
        # Loosening 0.22 -> 0.30 unblocks it -> +profit.
        rows = [
            _bet_row(edge_at_ask=0.15, target_win=1, target_profit=4.0),
            _bet_row(edge_at_ask=0.25, target_win=1, target_profit=4.0),  # currently blocked
        ]
        g = self._gate_extreme_edge()
        sweep = cfr.evaluate_sweep_for_window(rows, g)
        s_030 = next(s for s in sweep if s.threshold == 0.30)
        # At 0.30: blocked.profit = 0 (no bets above 0.30)
        # At 0.22: blocked.profit = +4 (the winning bet was blocked)
        # cf_delta = 4 - 0 = +4 (gaining the bet back)
        self.assertAlmostEqual(
            s_030.counterfactual_profit_delta_vs_current, 4.0, places=2,
        )
        self.assertIs(s_030.is_tightening, False)

    def test_current_threshold_row_has_no_delta(self):
        rows = [_bet_row(edge_at_ask=0.15)]
        g = self._gate_extreme_edge()
        sweep = cfr.evaluate_sweep_for_window(rows, g)
        anchor = next(s for s in sweep if s.is_current)
        self.assertIsNone(anchor.counterfactual_profit_delta_vs_current)
        self.assertIsNone(anchor.kept_roi_delta_vs_current)

    def test_kept_roi_delta_computed_when_both_rois_known(self):
        rows = [
            _bet_row(edge_at_ask=0.15, target_win=1, target_profit=4.0),
            _bet_row(edge_at_ask=0.16, target_win=1, target_profit=4.0),
            _bet_row(edge_at_ask=0.20, target_win=0, target_profit=-7.0),
        ]
        g = self._gate_extreme_edge()
        sweep = cfr.evaluate_sweep_for_window(rows, g)
        s_018 = next(s for s in sweep if s.threshold == 0.18)
        # Removing the losing bet improves kept ROI -- delta should be positive.
        self.assertIsNotNone(s_018.kept_roi_delta_vs_current)
        self.assertGreater(s_018.kept_roi_delta_vs_current, 0)


class ApplicabilityCarriedThroughTests(unittest.TestCase):
    """Composite gates must filter applicability inside the counterfactual."""

    def test_inn6_gate_only_evaluates_inning_6_rows(self):
        g = next(x for x in cert.GATE_DEFS if x.name == "gate_inn6_rn_max")
        # Mixed inning rows: 2 in inn 6 (in-domain), 3 in inn 7 (out-of-domain)
        rows = [
            _bet_row(inning=6, runs_needed=1.0, target_profit=4.0),
            _bet_row(inning=6, runs_needed=3.0, target_profit=-7.0),  # blocked at 2.5
            _bet_row(inning=7, runs_needed=3.0, target_profit=4.0),   # outside
            _bet_row(inning=7, runs_needed=4.0, target_profit=4.0),   # outside
            _bet_row(inning=7, runs_needed=5.0, target_profit=-7.0),  # outside
        ]
        sweep = cfr.evaluate_sweep_for_window(rows, g)
        for s in sweep:
            # n_applicable should equal the count of inning==6 rows (2).
            self.assertEqual(s.n_applicable, 2)


class TopRecommendationsTests(unittest.TestCase):
    def _gates_payload_with_one_clear_winner(self) -> List[Dict[str, Any]]:
        # 12 losing bets blocked by a tightening from 0.22 -> 0.18
        # => $-84 saved (-(-84) = +$84)
        # Plus a shadow gate and a tiny-blocked-n recommendation
        # that should NOT make the cut.
        return [
            {
                "name": "gate_extreme_edge",
                "current_threshold": 0.22,
                "direction": "max",
                "applicability_label": None,
                "shadow_only": False,
                "windows": {
                    "trailing_30d": {
                        "date_range": ["2026-05-01", "2026-05-15"],
                        "sweep": [
                            {
                                "threshold": 0.22,
                                "is_current": True,
                                "is_tightening": None,
                                "n_applicable": 100,
                                "kept": {"n_filled": 50, "roi": 0.05},
                                "blocked": {"n_filled": 0, "roi": None},
                                "counterfactual_profit_delta_vs_current": None,
                                "kept_roi_delta_vs_current": None,
                            },
                            {
                                "threshold": 0.18,
                                "is_current": False,
                                "is_tightening": True,
                                "n_applicable": 100,
                                "kept": {"n_filled": 38, "roi": 0.20},
                                "blocked": {"n_filled": 12, "roi": -0.50},
                                "counterfactual_profit_delta_vs_current": 84.0,
                                "kept_roi_delta_vs_current": 0.15,
                            },
                            {
                                "threshold": 0.30,
                                "is_current": False,
                                "is_tightening": False,
                                "n_applicable": 100,
                                "kept": {"n_filled": 50, "roi": 0.05},
                                "blocked": {"n_filled": 0, "roi": None},
                                "counterfactual_profit_delta_vs_current": 0.0,
                                "kept_roi_delta_vs_current": 0.0,
                            },
                        ],
                    }
                },
            },
            {
                # Shadow gates must NEVER appear in recommendations.
                "name": "shadow_gate_x",
                "current_threshold": None,
                "direction": "min",
                "applicability_label": None,
                "shadow_only": True,
                "windows": {
                    "trailing_30d": {
                        "sweep": [
                            {
                                "threshold": 0.05,
                                "is_current": False,
                                "is_tightening": None,
                                "n_applicable": 50,
                                "kept": {"n_filled": 20},
                                "blocked": {"n_filled": 30},
                                "counterfactual_profit_delta_vs_current": 500.0,
                                "kept_roi_delta_vs_current": None,
                            },
                        ],
                    }
                },
            },
            {
                # Tiny blocked-n should be filtered out by the min_blocked_n floor.
                "name": "gate_too_thin",
                "current_threshold": 0.5,
                "direction": "min",
                "applicability_label": None,
                "shadow_only": False,
                "windows": {
                    "trailing_30d": {
                        "sweep": [
                            {
                                "threshold": 0.6,
                                "is_current": False,
                                "is_tightening": True,
                                "n_applicable": 10,
                                "kept": {"n_filled": 9, "roi": 0.10},
                                "blocked": {"n_filled": 2, "roi": -1.0},
                                "counterfactual_profit_delta_vs_current": 14.0,
                                "kept_roi_delta_vs_current": 0.05,
                            },
                        ],
                    }
                },
            },
        ]

    def test_sorts_by_dollar_delta_desc(self):
        gates = self._gates_payload_with_one_clear_winner()
        recs = cfr.build_top_recommendations(gates, window_name="trailing_30d")
        self.assertGreaterEqual(len(recs), 1)
        self.assertEqual(recs[0]["gate"], "gate_extreme_edge")
        self.assertAlmostEqual(recs[0]["counterfactual_profit_delta_usd"], 84.0)
        self.assertEqual(recs[0]["from_threshold"], 0.22)
        self.assertEqual(recs[0]["to_threshold"], 0.18)

    def test_shadow_gates_excluded(self):
        gates = self._gates_payload_with_one_clear_winner()
        recs = cfr.build_top_recommendations(gates, window_name="trailing_30d")
        names = [r["gate"] for r in recs]
        self.assertNotIn("shadow_gate_x", names)

    def test_min_blocked_n_floor_excludes_thin_cohorts(self):
        gates = self._gates_payload_with_one_clear_winner()
        recs = cfr.build_top_recommendations(gates, window_name="trailing_30d")
        names = [r["gate"] for r in recs]
        self.assertNotIn("gate_too_thin", names)  # blocked_n=2 < 5

    def test_min_delta_floor_excludes_small_recommendations(self):
        gates = self._gates_payload_with_one_clear_winner()
        recs = cfr.build_top_recommendations(
            gates, window_name="trailing_30d", min_delta_usd=100.0,
        )
        # 84 < 100 -> no recommendations
        self.assertEqual(recs, [])

    def test_only_tightenings_included(self):
        # Loosening at 0.30 has cf_delta 0 anyway, but proves the filter logic
        gates = self._gates_payload_with_one_clear_winner()
        recs = cfr.build_top_recommendations(gates, window_name="trailing_30d")
        for r in recs:
            self.assertNotEqual(r["to_threshold"], 0.30)

    def test_confidence_label_boundaries(self):
        self.assertEqual(cfr._confidence_label(20), "high")
        self.assertEqual(cfr._confidence_label(19), "medium")
        self.assertEqual(cfr._confidence_label(10), "medium")
        self.assertEqual(cfr._confidence_label(9), "low")
        self.assertEqual(cfr._confidence_label(0), "low")

    def test_max_results_cap(self):
        gates = self._gates_payload_with_one_clear_winner() * 20  # repeat
        recs = cfr.build_top_recommendations(
            gates, window_name="trailing_30d", max_results=3,
        )
        self.assertLessEqual(len(recs), 3)


class CrossWindowReversalTests(unittest.TestCase):
    """Hygiene #5 (2026-05-26) -- recommendations get an extra
    lifetime + post-calibrator-enforce comparison; when the 30d
    direction inverts vs. a meaningfully-sized lifetime cohort, the
    recommendation is flagged `window_reversal=True` and its
    confidence is downgraded to `review_required`. This is the audit
    cure for the 2026-05-20 P2 `gate_min_current_total 4 -> 5`
    incident."""

    def _gate_extreme_edge(self):
        return next(x for x in cert.GATE_DEFS if x.name == "gate_extreme_edge")

    def test_lifetime_post_calibrator_window_present_in_slice(self):
        rows = [
            _bet_row(session_date="2026-05-25"),  # post-calibrator
            _bet_row(session_date="2026-04-15"),  # pre-calibrator
        ]
        windows = cfr.slice_windows(rows)
        self.assertIn("lifetime_post_calibrator_enforce", windows)
        post_dates = {
            r.session_date for r in windows["lifetime_post_calibrator_enforce"].rows
        }
        # Only the post-2026-05-19 row survives.
        self.assertEqual(post_dates, {"2026-05-25"})

    def test_no_reversal_when_30d_and_lifetime_agree(self):
        # 30d says "tightening 0.22 -> 0.18 saves money"; lifetime says
        # the same (just slightly less per bet). No reversal flag.
        # Place all losers at edge=0.20 (kept by 0.22, blocked by 0.18).
        rows = []
        # 15 lifetime-only (pre-calibrator) bets at edge 0.20, all losers.
        for i in range(15):
            rows.append(_bet_row(
                session_date="2026-03-01",
                edge_at_ask=0.20,
                target_win=0,
                target_profit=-10.0,
            ))
        # 30 recent (post-calibrator era) bets at edge 0.20, all losers.
        # session_date = latest in table.
        for i in range(30):
            rows.append(_bet_row(
                session_date="2026-05-25",
                edge_at_ask=0.20,
                target_win=0,
                target_profit=-10.0,
            ))
        payload = cfr.build_counterfactual_payload(rows)
        recs = payload["top_recommendations"]
        gate_recs = [r for r in recs if r.get("gate") == "gate_extreme_edge"]
        self.assertGreater(len(gate_recs), 0,
                           "expected at least one extreme-edge tightening rec")
        for rec in gate_recs:
            # Lifetime + post-calibrator deltas should agree in sign
            # with the 30d delta (all positive = saves $).
            self.assertGreater(
                rec["counterfactual_profit_delta_usd"], 0,
                "30d should show savings",
            )
            self.assertGreater(
                rec.get("lifetime_counterfactual_profit_delta_usd") or 0, 0,
                "lifetime should agree (also savings)",
            )
            self.assertFalse(rec["window_reversal"])
            self.assertNotEqual(rec["confidence"], "review_required")

    def test_reversal_flag_fires_when_lifetime_direction_inverts(self):
        # Construct the 2026-05-20 P2 audit scenario in miniature.
        # 30d window: tightening blocks losers -> looks great.
        # Lifetime: NET POSITIVE bets blocked -> tightening LOSES money.
        # For cf_delta lifetime to be negative, the bets being blocked
        # at edge=0.20 (kept by 0.22, blocked by 0.18) must have NET
        # POSITIVE profit across the lifetime cohort.
        rows = []
        # Lifetime-only winners (enough to make the lifetime cohort
        # net-positive even after the 30d losers drag it down).
        for i in range(30):
            rows.append(_bet_row(
                session_date="2026-03-01",
                edge_at_ask=0.20,
                target_win=1,
                target_profit=+6.0,    # 30 * +6 = +180
            ))
        # Recent losers (the load-bearing cohort in 30d).
        for i in range(15):
            rows.append(_bet_row(
                session_date="2026-05-25",
                edge_at_ask=0.20,
                target_win=0,
                target_profit=-10.0,   # 15 * -10 = -150
            ))
        # Lifetime alt_blocked.profit = +180 + (-150) = +30 (net winners
        # would be blocked by tightening). cf_delta_lifetime = 0 - +30 =
        # -30 (tightening LOSES money on lifetime). cf_delta_30d = +150
        # (tightening saves money on 30d). Direction inverts -> reversal.
        payload = cfr.build_counterfactual_payload(rows)
        recs = payload["top_recommendations"]
        # Should find the 0.22->0.18 tightening rec
        gate_recs = [r for r in recs if r.get("gate") == "gate_extreme_edge"]
        self.assertGreater(len(gate_recs), 0)
        rec = gate_recs[0]
        self.assertEqual(rec["to_threshold"], 0.18)
        # 30d delta is positive (we save $ in the recent window).
        self.assertGreater(rec["counterfactual_profit_delta_usd"], 0)
        # Lifetime delta should be NEGATIVE (tightening would have
        # missed the lifetime winners).
        self.assertLess(rec["lifetime_counterfactual_profit_delta_usd"], 0)
        # Reversal flag must fire.
        self.assertTrue(rec["window_reversal"])
        self.assertIn("lifetime_reversal", rec["window_reversal_flags"])
        # Confidence downgraded.
        self.assertEqual(rec["confidence"], "review_required")
        # Original confidence preserved for audit.
        self.assertIn("confidence_before_reversal_check", rec)
        # Rationale starts with the warning marker.
        self.assertTrue(rec["rationale"].startswith("⚠️ WINDOW REVERSAL"))

    def test_no_reversal_when_lifetime_cohort_too_small(self):
        # Recent 30d cohort says tighten = save $; lifetime has only 3
        # bets (below WINDOW_REVERSAL_MIN_LIFETIME_BLOCKED_N=10).
        # Should NOT downgrade -- not enough lifetime evidence.
        rows = []
        # 30d cohort: 15 losers blocked by tightening.
        for i in range(15):
            rows.append(_bet_row(
                session_date="2026-05-25",
                edge_at_ask=0.20,
                target_win=0,
                target_profit=-10.0,
            ))
        # Lifetime extras: only 3 winners.
        for i in range(3):
            rows.append(_bet_row(
                session_date="2026-03-01",
                edge_at_ask=0.20,
                target_win=1,
                target_profit=+6.0,
            ))
        payload = cfr.build_counterfactual_payload(rows)
        recs = payload["top_recommendations"]
        for rec in recs:
            if rec.get("gate") != "gate_extreme_edge":
                continue
            # Lifetime cohort too small -- no reversal flag.
            self.assertFalse(rec["window_reversal"])


class GateCounterfactualHealthBlockTests(unittest.TestCase):
    """Hygiene #5: the daily-review block must surface reversed
    recommendations as alerts AND suppress them from the actionable
    Notes feed."""

    def _build_report_with_reversed_rec(self, tmp_path: Path) -> Path:
        # Same shape as test_reversal_flag_fires_when_lifetime_direction_inverts
        # -- lifetime cohort dominated by winners so tightening LOSES
        # money on lifetime; recent cohort dominated by losers so
        # tightening SAVES money on 30d. Direction inverts -> reversal.
        rows = []
        for i in range(30):
            rows.append(_bet_row(
                session_date="2026-03-01",
                edge_at_ask=0.20,
                target_win=1, target_profit=+6.0,
            ))
        for i in range(15):
            rows.append(_bet_row(
                session_date="2026-05-25",
                edge_at_ask=0.20,
                target_win=0, target_profit=-10.0,
            ))
        payload = cfr.build_counterfactual_payload(rows)
        report_path = tmp_path / "gate_counterfactual_report.json"
        report_path.write_text(json.dumps(payload), encoding="utf-8")
        return report_path

    def test_health_block_surfaces_reversal_alert_and_suppresses_notes(self):
        # Import here so the test stays self-contained even when the
        # human_review package gets moved/refactored.
        sys.path.insert(
            0, str(PROJECT_DIR / "scripts" / "analysis"),
        )
        from human_review.core_health import _gate_counterfactual_health
        with tempfile.TemporaryDirectory() as td:
            report_path = self._build_report_with_reversed_rec(Path(td))
            block = _gate_counterfactual_health(
                report_path=report_path,
                session_date="2026-05-25",
            )
            # Reversed recs counter present + non-zero.
            self.assertGreater(block["reversed_recommendations_count"], 0)
            self.assertIn("reversed_recommendations", block)
            # Alerts list must carry the reversal warning text.
            reversal_alerts = [
                a for a in block["alerts"]
                if "window_reversal" in a or "Gate-counterfactual" in a
            ]
            self.assertGreater(len(reversal_alerts), 0)
            # The compact top_recommendations_30d entries still carry
            # the structured cross-window fields for dashboards.
            top = block.get("top_recommendations_30d", [])
            self.assertGreater(len(top), 0)
            self.assertIn("window_reversal", top[0])
            self.assertIn("lifetime_counterfactual_profit_delta_usd", top[0])


class PayloadBuildTests(unittest.TestCase):
    def test_payload_has_all_required_keys(self):
        rows = [_bet_row(edge_at_ask=0.18, target_profit=2.0)]
        payload = cfr.build_counterfactual_payload(rows)
        for key in (
            "schema_version", "generated_at_utc", "active_priority",
            "n_rows", "date_span", "windows", "config", "gates",
            "top_recommendations", "top_recommendations_trailing_7d",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["n_rows"], 1)
        # Every gate def must appear in the gates payload.
        names = [g["name"] for g in payload["gates"]]
        for g in cert.GATE_DEFS:
            self.assertIn(g.name, names)

    def test_empty_rows_produces_clean_payload(self):
        payload = cfr.build_counterfactual_payload([])
        self.assertEqual(payload["n_rows"], 0)
        self.assertIsNone(payload["date_span"])
        # All windows should be empty but present
        for name in ("all", "trailing_30d", "trailing_7d"):
            self.assertEqual(payload["windows"][name]["n_rows"], 0)
        # Each gate still gets a panel (just with empty data)
        self.assertEqual(len(payload["gates"]), len(cert.GATE_DEFS))
        # No recommendations possible
        self.assertEqual(payload["top_recommendations"], [])

    def test_current_threshold_always_appears_in_sweep_even_if_not_in_default_list(self):
        # Pass through a payload and check that for each non-shadow gate
        # the sweep contains an `is_current=True` row in every window.
        rows = [_bet_row()]
        payload = cfr.build_counterfactual_payload(rows)
        for g in payload["gates"]:
            if g.get("shadow_only"):
                continue
            for window_name, window in g["windows"].items():
                if window["n_rows"] == 0:
                    continue
                has_current = any(
                    s["is_current"] for s in (window.get("sweep") or [])
                )
                self.assertTrue(
                    has_current,
                    f"{g['name']} / {window_name} missing is_current row",
                )


class MarkdownRenderTests(unittest.TestCase):
    def test_markdown_includes_top_recommendations_header(self):
        payload = cfr.build_counterfactual_payload([_bet_row()])
        md = cfr.render_markdown(payload)
        self.assertIn("Top counterfactual recommendations", md)
        self.assertIn("Per-gate sweep panels", md)
        # Every gate name should appear somewhere
        for g in cert.GATE_DEFS:
            self.assertIn(g.name, md)

    def test_empty_recommendations_shows_friendly_message(self):
        payload = cfr.build_counterfactual_payload([])
        md = cfr.render_markdown(payload)
        self.assertIn("No counterfactual recommendations", md)


class EndToEndTests(unittest.TestCase):
    def test_main_writes_json_and_md_on_real_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            jsonl = tmp_dir / "training.jsonl"
            jsonl.write_text("", encoding="utf-8")  # empty input
            out_dir = tmp_dir / "out"
            rc = cfr.main([
                "--training-table", str(jsonl),
                "--output-dir", str(out_dir),
            ])
            self.assertEqual(rc, 0)
            self.assertTrue((out_dir / "gate_counterfactual_report.json").exists())
            self.assertTrue((out_dir / "gate_counterfactual_report.md").exists())
            # Loaded JSON has n_rows=0
            d = json.loads(
                (out_dir / "gate_counterfactual_report.json").read_text(
                    encoding="utf-8",
                ),
            )
            self.assertEqual(d["n_rows"], 0)


if __name__ == "__main__":
    unittest.main()
