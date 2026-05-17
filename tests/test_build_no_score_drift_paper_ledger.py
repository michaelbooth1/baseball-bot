import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import evaluate_no_score_drift_policy as nsd  # noqa: E402
import build_no_score_drift_paper_ledger as ledger  # noqa: E402


def _candidate(**overrides):
    base = {
        "session_date": "2026-05-01",
        "mode": "live",
        "candidate_id": "cand_1",
        "ts": "2026-05-01T17:00:00Z",
        "game_pk": 101,
        "away_abbrev": "AWY",
        "home_abbrev": "HOM",
        "line": "8.5",
        "inning": 4,
        "inning_state": "Top",
        "outs": 0,
        "runners_on": 0,
        "away_score_before": 2,
        "home_score_before": 1,
        "current_total": 3,
        "score_segment_key": "2-1",
        "score_segment_drawdown": 0.08,
        "decision": "shadow_no_score_drift",
        "decision_reason": "state_value_no_score_drift",
        "decision_ask": 0.50,
        "best_bid": 0.47,
        "spread": 0.03,
        "fair_value": 0.66,
        "edge": 0.16,
        "current_state_value_edge": 0.16,
        "current_state_value_empirical_edge": 0.09,
        "shadow_no_score_drift_trigger": "both",
        "stadium_weather_exposure": "open_air",
        "weather_air_density_index": 0.99,
        "weather_wind_out_component_mph": 4.5,
    }
    base.update(overrides)
    return base


class NoScoreDriftPaperLedgerTests(unittest.TestCase):
    def test_taker_ledger_enforces_budget_and_per_game_line_caps(self):
        outcomes = {
            ("2026-05-01", "live", 101, "8.5"): {
                "over_hit": True,
                "final_total": 10,
                "final_away": 6,
                "final_home": 4,
            },
            ("2026-05-01", "live", 102, "8.5"): {
                "over_hit": False,
                "final_total": 7,
                "final_away": 3,
                "final_home": 4,
            },
        }
        candidates = [
            _candidate(candidate_id="first", game_pk=101, score_segment_key="2-1", decision_ask=0.50),
            _candidate(
                candidate_id="same_line_later",
                ts="2026-05-01T17:05:00Z",
                game_pk=101,
                score_segment_key="3-1",
                away_score_before=3,
                home_score_before=1,
                current_total=4,
                decision_ask=0.55,
            ),
            _candidate(
                candidate_id="other_game",
                ts="2026-05-01T17:10:00Z",
                game_pk=102,
                away_abbrev="OTH",
                home_abbrev="GM2",
                score_segment_key="0-0",
                away_score_before=0,
                home_score_before=0,
                current_total=0,
                decision_ask=0.60,
            ),
        ]
        policy_rows, _ = nsd.build_policy_rows(candidates, outcomes)

        rows = ledger.build_ledger_rows(
            policy_rows,
            raw_candidates=candidates,
            stake_usdc=10.0,
            daily_budget_usdc=20.0,
            per_game_budget_fraction=1.0,
            max_orders_per_game=3,
            max_orders_per_game_line=1,
            price_policy="taker",
        )

        by_candidate = {row["candidate_id"]: row for row in rows}
        self.assertEqual(by_candidate["first"]["decision"], "submitted")
        self.assertEqual(
            by_candidate["first"]["shadow_no_score_poisson_empirical_ask_drawdown_bucket"],
            "poisson_>=0.15|empirical_0.05-0.10|ask_0.40-0.55|drawdown_0.06-0.10",
        )
        self.assertEqual(by_candidate["first"]["stadium_weather_exposure"], "open_air")
        self.assertAlmostEqual(by_candidate["first"]["weather_air_density_index"], 0.99)
        self.assertAlmostEqual(by_candidate["first"]["weather_wind_out_component_mph"], 4.5)
        self.assertTrue(by_candidate["first"]["filled"])
        self.assertAlmostEqual(by_candidate["first"]["filled_shares"], 20.0)
        self.assertAlmostEqual(by_candidate["first"]["fill_cost_usdc"], 10.0)
        self.assertAlmostEqual(by_candidate["first"]["payout_usdc"], 20.0)
        self.assertAlmostEqual(by_candidate["first"]["profit_usdc"], 10.0)
        self.assertEqual(by_candidate["same_line_later"]["decision"], "skipped")
        self.assertEqual(by_candidate["same_line_later"]["skip_reason"], "max_orders_per_game_line")
        self.assertEqual(by_candidate["other_game"]["decision"], "submitted")
        self.assertFalse(by_candidate["other_game"]["over_hit"])
        self.assertAlmostEqual(by_candidate["other_game"]["profit_usdc"], -10.0)

        args = SimpleNamespace(
            mode="live",
            min_date="2026-05-01",
            max_date="2026-05-01",
            stake=10.0,
            daily_budget=20.0,
            per_game_budget_fraction=1.0,
            max_orders_per_game=3,
            max_orders_per_game_line=1,
            support_regimes="",
            price_policy="taker",
            fill_assumption="immediate",
            price_offset_cents=1.0,
            touch_window_seconds=900.0,
            min_poisson_edge=0.10,
            min_empirical_edge=0.08,
            include_unsettled=False,
        )
        summary = ledger.build_summary(
            rows,
            policy_counts={"policy_rows": len(policy_rows)},
            args=args,
            support_regimes=ledger.DEFAULT_SUPPORT_REGIMES,
        )
        self.assertIn("by_poisson_empirical_ask_drawdown", summary)

    def test_daily_budget_blocks_after_spend(self):
        outcomes = {
            ("2026-05-01", "live", 101, "8.5"): {"over_hit": True, "final_total": 10},
            ("2026-05-01", "live", 102, "8.5"): {"over_hit": True, "final_total": 10},
        }
        candidates = [
            _candidate(candidate_id="first", game_pk=101, decision_ask=0.50),
            _candidate(
                candidate_id="second",
                ts="2026-05-01T17:05:00Z",
                game_pk=102,
                away_abbrev="OTH",
                home_abbrev="GM2",
                score_segment_key="0-0",
                away_score_before=0,
                home_score_before=0,
                decision_ask=0.50,
            ),
        ]
        policy_rows, _ = nsd.build_policy_rows(candidates, outcomes)

        rows = ledger.build_ledger_rows(
            policy_rows,
            raw_candidates=candidates,
            stake_usdc=10.0,
            daily_budget_usdc=10.0,
            per_game_budget_fraction=1.0,
            max_orders_per_game=1,
            max_orders_per_game_line=1,
            price_policy="taker",
        )

        self.assertEqual(rows[0]["decision"], "submitted")
        self.assertEqual(rows[1]["decision"], "skipped")
        self.assertEqual(rows[1]["skip_reason"], "daily_budget_exhausted")

    def test_touch_within_segment_can_fill_resting_order(self):
        outcomes = {
            ("2026-05-01", "live", 101, "8.5"): {"over_hit": True, "final_total": 10},
        }
        candidates = [
            _candidate(
                candidate_id="first",
                ts="2026-05-01T17:00:00Z",
                game_pk=101,
                decision_ask=0.60,
                current_state_value_edge=0.16,
                current_state_value_empirical_edge=0.09,
            ),
            _candidate(
                candidate_id="later_touch",
                ts="2026-05-01T17:02:00Z",
                game_pk=101,
                decision_ask=0.55,
                current_state_value_edge=0.16,
                current_state_value_empirical_edge=0.09,
            ),
        ]
        policy_rows, counts = nsd.build_policy_rows(candidates, outcomes)
        self.assertEqual(counts["duplicate_rows_collapsed"], 1)

        rows = ledger.build_ledger_rows(
            policy_rows,
            raw_candidates=candidates,
            stake_usdc=10.0,
            daily_budget_usdc=80.0,
            per_game_budget_fraction=1.0,
            max_orders_per_game=1,
            max_orders_per_game_line=1,
            price_policy="ask_minus_cents",
            fill_assumption="touch_within_segment",
            price_offset_cents=5.0,
            touch_window_seconds=180.0,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "submitted")
        self.assertTrue(rows[0]["filled"])
        self.assertEqual(rows[0]["fill_source"], "segment_touch")
        self.assertAlmostEqual(rows[0]["limit_price"], 0.55)
        self.assertAlmostEqual(rows[0]["fill_seconds"], 120.0)
        self.assertAlmostEqual(rows[0]["profit_usdc"], 8.18)

    def test_summary_separates_submitted_and_skipped_rows(self):
        rows = [
            {"decision": "submitted", "filled": True, "settled": True, "over_hit": True, "profit_usdc": 5.0, "fill_cost_usdc": 10.0},
            {"decision": "submitted_unfilled", "filled": False},
            {"decision": "skipped", "skip_reason": "daily_budget_exhausted"},
        ]
        summary = ledger.summarize_ledger(rows)
        self.assertEqual(summary["submitted_orders"], 2)
        self.assertEqual(summary["filled_orders"], 1)
        self.assertEqual(summary["submitted_unfilled"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertAlmostEqual(summary["roi"], 0.5)

    def test_policy_variant_summaries_replay_trigger_families_independently(self):
        outcomes = {
            ("2026-05-01", "live", 101, "8.5"): {"over_hit": True, "final_total": 10},
            ("2026-05-01", "live", 102, "8.5"): {"over_hit": True, "final_total": 10},
            ("2026-05-01", "live", 103, "8.5"): {"over_hit": False, "final_total": 7},
        }
        candidates = [
            _candidate(
                candidate_id="both",
                game_pk=101,
                decision_ask=0.50,
                current_state_value_edge=0.16,
                current_state_value_empirical_edge=0.09,
                shadow_no_score_drift_trigger="both",
            ),
            _candidate(
                candidate_id="empirical",
                game_pk=102,
                away_abbrev="EMP",
                home_abbrev="HOM",
                score_segment_key="1-0",
                decision_ask=0.50,
                current_state_value_edge=0.04,
                current_state_value_empirical_edge=0.10,
                shadow_no_score_drift_trigger="empirical_edge",
            ),
            _candidate(
                candidate_id="poisson",
                game_pk=103,
                away_abbrev="POI",
                home_abbrev="HOM",
                score_segment_key="0-1",
                decision_ask=0.50,
                current_state_value_edge=0.14,
                current_state_value_empirical_edge=0.03,
                shadow_no_score_drift_trigger="po_edge",
            ),
        ]
        policy_rows, _ = nsd.build_policy_rows(candidates, outcomes)
        args = SimpleNamespace(
            stake=10.0,
            daily_budget=80.0,
            per_game_budget_fraction=1.0,
            max_orders_per_game=3,
            max_orders_per_game_line=1,
            price_policy="taker",
            fill_assumption="immediate",
            price_offset_cents=1.0,
            touch_window_seconds=900.0,
            include_unsettled=False,
        )

        variants = ledger.build_policy_variant_summaries(
            policy_rows,
            raw_candidates=candidates,
            args=args,
        )

        self.assertEqual(variants["both_only"]["overall"]["submitted_orders"], 1)
        self.assertEqual(variants["both_only"]["overall"]["wins"], 1)
        self.assertEqual(variants["empirical_or_both"]["overall"]["submitted_orders"], 2)
        self.assertEqual(variants["empirical_or_both"]["overall"]["wins"], 2)
        self.assertEqual(variants["poisson_only"]["overall"]["submitted_orders"], 1)
        self.assertEqual(variants["poisson_only"]["overall"]["losses"], 1)


if __name__ == "__main__":
    unittest.main()
