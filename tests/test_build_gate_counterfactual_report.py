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
