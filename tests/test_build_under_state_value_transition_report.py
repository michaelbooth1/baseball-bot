"""Phase A3 (2026-05-16): UNDER-side state-value transition report.

Sibling to `test_build_state_value_transition_report.py`. Verifies:
  - under_hit derivation = not over_hit (with None passthrough)
  - under-side ROI math uses under_best_ask (not decision_ask)
  - regime classifiers are inverted relative to OVER
  - under_ask_coverage tracks the ~50% pair-availability reality
  - ranked rows surface most-negative current-state edge (highest
    UNDER opportunity) at the top, opposite of OVER report
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_under_state_value_transition_report as usv  # noqa: E402


class UnderHitDerivationTests(unittest.TestCase):
    def test_under_hit_flips_over_hit(self):
        self.assertEqual(usv._under_hit(True), False)
        self.assertEqual(usv._under_hit(False), True)

    def test_under_hit_passes_none_through(self):
        """Unsettled games must remain unsettled in the Under frame."""
        self.assertIsNone(usv._under_hit(None))


class UnderAskExtractionTests(unittest.TestCase):
    def test_returns_none_when_pair_unavailable(self):
        """The ~50% production case: under tick didn't arrive in the
        same poll cycle, so under_pair_available=False; the under ask
        is intentionally None to keep that row out of ROI math."""
        row = {"under_pair_available": False, "under_best_ask": 0.40}
        self.assertIsNone(usv._under_ask_for(row))

    def test_returns_ask_when_pair_available(self):
        row = {"under_pair_available": True, "under_best_ask": 0.40}
        self.assertEqual(usv._under_ask_for(row), 0.40)

    def test_returns_none_when_pair_flag_missing(self):
        row = {"under_best_ask": 0.40}  # no under_pair_available key
        self.assertIsNone(usv._under_ask_for(row))


class UnderRegimeClassifierTests(unittest.TestCase):
    def test_low_phantom_negative_current_edge_is_under_candidate(self):
        """Under-side analog: Over over-valued by current-state edge
        AND no phantom overheating => Under is undervalued candidate."""
        row = {
            "shadow_phantom_risk_band": "low",
            "current_state_value_edge": -0.12,
        }
        self.assertEqual(
            usv._under_score_event_regime(row),
            "low_medium_phantom_negative_current_edge",
        )

    def test_high_phantom_positive_current_edge_under_contrarian(self):
        row = {
            "shadow_phantom_risk_band": "high",
            "current_state_value_edge": 0.10,
        }
        self.assertEqual(
            usv._under_score_event_regime(row),
            "high_phantom_positive_current_edge",
        )

    def test_missing_current_edge_returns_missing(self):
        row = {"shadow_phantom_risk_band": "high"}
        self.assertEqual(
            usv._under_score_event_regime(row), "missing_current_edge"
        )

    def test_no_score_under_support_classification(self):
        row = {
            "current_state_value_edge": -0.15,
            "current_state_value_empirical_edge": -0.10,
        }
        self.assertEqual(
            usv._under_no_score_regime(row),
            "poisson_and_empirical_under_support",
        )

    def test_no_score_empirical_only_support(self):
        row = {
            "current_state_value_edge": 0.0,
            "current_state_value_empirical_edge": -0.10,
        }
        self.assertEqual(
            usv._under_no_score_regime(row), "empirical_under_support"
        )

    def test_no_score_weak_support(self):
        row = {
            "current_state_value_edge": -0.05,
            "current_state_value_empirical_edge": -0.05,
        }
        # neither passes the threshold
        self.assertEqual(
            usv._under_no_score_regime(row),
            "weak_or_missing_under_support",
        )


class UnderReportRoiAndCoverageTests(unittest.TestCase):
    def _outcomes(self):
        return {
            # Over WINS (so Under LOSES) at line 8.5
            ("2026-05-01", "live", 1, "8.5"): {
                "over_hit": True, "final_total": 11,
            },
            # Over LOSES (so Under WINS) at line 9.5
            ("2026-05-01", "live", 2, "9.5"): {
                "over_hit": False, "final_total": 7,
            },
        }

    def _rows(self):
        return [
            # Over-loss row: under wins. under_ask 0.30 -> payout 1/0.30 - 1
            {
                "session_date": "2026-05-01", "mode": "live",
                "candidate_id": "under_winner",
                "game_pk": 2, "line": "9.5",
                "decision": "trade",
                "state_value_strategy": "score_event_transition",
                "decision_ask": 0.70, "edge": 0.10,
                "current_state_value_edge": -0.10,
                "shadow_phantom_risk_band": "low",
                "shadow_phantom_risk_score": 0.20,
                "under_pair_available": True,
                "under_best_bid": 0.28,
                "under_best_ask": 0.30,
            },
            # Over-win row: under loses. under_ask 0.40 -> -1 ROI
            {
                "session_date": "2026-05-01", "mode": "live",
                "candidate_id": "under_loser",
                "game_pk": 1, "line": "8.5",
                "decision": "trade",
                "state_value_strategy": "score_event_transition",
                "decision_ask": 0.60, "edge": 0.05,
                "current_state_value_edge": 0.10,
                "shadow_phantom_risk_band": "high",
                "shadow_phantom_risk_score": 0.80,
                "under_pair_available": True,
                "under_best_bid": 0.38,
                "under_best_ask": 0.40,
            },
            # Row with no under_pair: counts in rows but not in
            # under_ask_present / ROI
            {
                "session_date": "2026-05-01", "mode": "live",
                "candidate_id": "no_under_book",
                "game_pk": 1, "line": "8.5",
                "decision": "trade",
                "state_value_strategy": "score_event_transition",
                "decision_ask": 0.60, "edge": 0.05,
                "current_state_value_edge": -0.08,
                "shadow_phantom_risk_band": "low",
                "under_pair_available": False,
            },
        ]

    def test_under_roi_math_uses_under_ask_not_decision_ask(self):
        report = usv.build_report(self._rows(), self._outcomes(), top_n=10)
        overall = report["score_event_transition"]["overall"]
        # 3 rows total; 2 with under_ask available
        self.assertEqual(overall["rows"], 3)
        self.assertEqual(overall["rows_with_under_ask"], 2)
        self.assertAlmostEqual(overall["under_ask_coverage"], 2 / 3, places=3)
        # 2 settled rows (both have under_pair_available=True and outcomes).
        # The 3rd row's outcome is the same key as under_loser
        # (game_pk=1, line=8.5) so it labels as over_hit=True -> under_hit=False.
        # All three settled rows: 1 under win (game_pk=2), 2 under losses.
        self.assertEqual(overall["label_rows"], 3)
        self.assertAlmostEqual(overall["under_win_rate"], 1 / 3, places=3)
        # ROI: under_winner: 1/0.30 - 1 = 2.333...; under_loser: -1;
        # no_under_book has no under_ask so its profit is NOT added.
        # Mean of [2.333..., -1] = 0.6666...
        self.assertAlmostEqual(overall["taker_roi_per_cost"], 0.6667, places=3)

    def test_unpaired_rows_count_but_dont_pollute_under_roi(self):
        """Critical: a row without under_pair_available must NOT
        add a 0 or fallback to decision_ask -- it must be excluded
        from the ROI/profits aggregation so the metric remains
        purely under-side."""
        # Pure unpaired-only batch:
        rows = [{
            "session_date": "2026-05-01", "mode": "live",
            "candidate_id": "unpaired_only",
            "game_pk": 1, "line": "8.5",
            "decision": "trade",
            "state_value_strategy": "score_event_transition",
            "decision_ask": 0.60,
            "under_pair_available": False,
        }]
        report = usv.build_report(rows, self._outcomes(), top_n=10)
        overall = report["score_event_transition"]["overall"]
        self.assertEqual(overall["rows"], 1)
        self.assertEqual(overall["rows_with_under_ask"], 0)
        # No under-ask -> no taker ROI math
        self.assertIsNone(overall["taker_roi_per_cost"])
        self.assertIsNone(overall["taker_profit_units"])


class UnderReportRankingTests(unittest.TestCase):
    def test_top_rows_surface_most_negative_current_state_edge(self):
        """For UNDER, the most valuable signal is the most NEGATIVE
        current_state_value_edge (Over is the most over-valued).
        Opposite ordering vs the Over report."""
        outcomes = {
            ("2026-05-01", "live", k, "8.5"): {"over_hit": False, "final_total": 7}
            for k in (1, 2, 3)
        }
        rows = [
            {
                "session_date": "2026-05-01", "mode": "live",
                "candidate_id": "weakest_under",
                "game_pk": 1, "line": "8.5",
                "decision": "shadow_no_score_drift",
                "current_state_value_edge": 0.05,
                "current_state_value_empirical_edge": 0.02,
            },
            {
                "session_date": "2026-05-01", "mode": "live",
                "candidate_id": "strongest_under",
                "game_pk": 2, "line": "8.5",
                "decision": "shadow_no_score_drift",
                "current_state_value_edge": -0.20,
                "current_state_value_empirical_edge": -0.18,
            },
            {
                "session_date": "2026-05-01", "mode": "live",
                "candidate_id": "medium_under",
                "game_pk": 3, "line": "8.5",
                "decision": "shadow_no_score_drift",
                "current_state_value_edge": -0.08,
                "current_state_value_empirical_edge": -0.05,
            },
        ]
        report = usv.build_report(rows, outcomes, top_n=10)
        ranked = report["no_score_drift"]["ranked_under_support_rows"]
        # strongest_under (most negative empirical) should be first
        self.assertEqual(ranked[0]["candidate_id"], "strongest_under")
        self.assertEqual(ranked[1]["candidate_id"], "medium_under")
        self.assertEqual(ranked[2]["candidate_id"], "weakest_under")


class UnderReportShapeTests(unittest.TestCase):
    def test_report_stamps_side_under(self):
        report = usv.build_report([], {}, top_n=10)
        self.assertEqual(report["side"], "under")

    def test_compact_row_includes_under_hit_field(self):
        outcomes = {
            ("2026-05-01", "live", 1, "8.5"): {"over_hit": True, "final_total": 11},
        }
        row = {
            "session_date": "2026-05-01", "mode": "live",
            "candidate_id": "x", "game_pk": 1, "line": "8.5",
            "decision": "trade", "state_value_strategy": "score_event_transition",
            "current_state_value_edge": -0.10,
            "shadow_phantom_risk_band": "low",
            "under_pair_available": True, "under_best_ask": 0.30,
        }
        compact = usv._compact_row(row, outcomes)
        self.assertIn("under_hit", compact)
        self.assertEqual(compact["under_hit"], False)  # over hit -> under did not
        self.assertEqual(compact["over_hit"], True)
        self.assertEqual(compact["under_best_ask"], 0.30)


if __name__ == "__main__":
    unittest.main()
