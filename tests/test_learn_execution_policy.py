"""Tests for learn_execution_policy prototype.

Each helper is exercised on a tiny synthetic replay so we lock in the
pivoting, aggregation, oracle, cohort, and LOOCV behavior. No live data
or repo artifacts are touched.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import learn_execution_policy as lep  # noqa: E402


def _replay_row(
    *,
    bet_id: str,
    policy: str,
    realized_roi,
    filled: bool,
    decision_ask: float = 0.70,
    entry_spread: float = 0.03,
    inning: int = 5,
    marketable_at_entry: bool = False,
    fair_value: float = 0.80,
    session_date: str = "2026-05-01",
    fill_model: str = "queue_adjusted",
    stake: float = 20.0,
) -> dict:
    return {
        "bet_id": bet_id,
        "session_date": session_date,
        "mode": "live",
        "decision_ask": decision_ask,
        "entry_spread": entry_spread,
        "fair_value": fair_value,
        "inning": inning,
        "line": "8.5",
        "marketable_at_entry": marketable_at_entry,
        "shadow_phantom_risk_band": None,
        "state_value_strategy": "score_event_transition",
        "policy": policy,
        "fill_model": fill_model,
        "filled": filled,
        "realized_roi": realized_roi,
        "realized_profit": (realized_roi * stake) if realized_roi is not None else 0.0,
        "stake": stake,
    }


def _make_bet(
    *,
    bet_id: str,
    roi_by_policy: dict,
    **bet_kwargs,
) -> list:
    """Emit 4 rows (one per policy) for the queue_adjusted fill model.

    ``roi_by_policy`` maps policy -> realized_roi (or None for no-fill).
    """
    rows = []
    for policy in lep.POLICIES:
        roi = roi_by_policy.get(policy)
        rows.append(_replay_row(
            bet_id=bet_id,
            policy=policy,
            realized_roi=roi,
            filled=roi is not None,
            **bet_kwargs,
        ))
    return rows


class PivotAndAggregateTests(unittest.TestCase):
    def test_pivot_drops_bets_missing_a_policy(self):
        # Bet A has all 4 policies; Bet B is missing taker_like -> dropped.
        rows = _make_bet(bet_id="A", roi_by_policy={
            "current_limit": 0.10, "limit_p1c": 0.05,
            "limit_p2c": 0.02, "taker_like": 0.01,
        })
        rows.extend([
            _replay_row(bet_id="B", policy="current_limit", realized_roi=0.0, filled=False),
            _replay_row(bet_id="B", policy="limit_p1c", realized_roi=0.0, filled=False),
            _replay_row(bet_id="B", policy="limit_p2c", realized_roi=0.0, filled=False),
        ])
        bets = lep._pivot_bets(rows, fill_model="queue_adjusted")
        self.assertEqual(len(bets), 1)
        self.assertEqual(bets[0]["bet_id"], "A")

    def test_pivot_filters_by_fill_model(self):
        rows = _make_bet(bet_id="A", roi_by_policy={
            "current_limit": 0.10, "limit_p1c": 0.05,
            "limit_p2c": 0.02, "taker_like": 0.01,
        }, fill_model="queue_adjusted")
        rows += _make_bet(bet_id="A", roi_by_policy={
            "current_limit": 0.20, "limit_p1c": 0.10,
            "limit_p2c": 0.04, "taker_like": 0.02,
        }, fill_model="touch")
        adj = lep._pivot_bets(rows, fill_model="queue_adjusted")
        touch = lep._pivot_bets(rows, fill_model="touch")
        self.assertEqual(adj[0]["policy_roi"]["current_limit"], 0.10)
        self.assertEqual(touch[0]["policy_roi"]["current_limit"], 0.20)

    def test_no_fill_coerces_to_zero_for_mean_roi_per_bet(self):
        # current_limit fills with +0.10 on bet1, no-fill on bet2. Mean
        # over 2 bets should be 0.05 (treating no-fill as 0).
        rows = _make_bet(bet_id="b1", roi_by_policy={
            "current_limit": 0.10, "limit_p1c": 0.0,
            "limit_p2c": 0.0, "taker_like": 0.0,
        })
        rows += _make_bet(bet_id="b2", roi_by_policy={
            "current_limit": None, "limit_p1c": 0.0,
            "limit_p2c": 0.0, "taker_like": 0.0,
        })
        bets = lep._pivot_bets(rows, fill_model="queue_adjusted")
        aggregates = lep._policy_aggregates(bets)
        self.assertAlmostEqual(
            aggregates["current_limit"]["mean_roi_per_bet"], 0.05, places=6
        )
        # mean_roi_when_filled should be +0.10 since only one filled.
        self.assertAlmostEqual(
            aggregates["current_limit"]["mean_roi_when_filled"], 0.10, places=6
        )
        self.assertEqual(aggregates["current_limit"]["filled"], 1)
        self.assertEqual(aggregates["current_limit"]["fill_rate"], 0.5)


class OracleAndCohortTests(unittest.TestCase):
    def test_oracle_picks_best_per_bet(self):
        rows = _make_bet(bet_id="b1", roi_by_policy={
            "current_limit": 0.50, "limit_p1c": 0.10,
            "limit_p2c": 0.05, "taker_like": 0.01,
        })
        # b2: only taker_like fills.
        rows += _make_bet(bet_id="b2", roi_by_policy={
            "current_limit": None, "limit_p1c": None,
            "limit_p2c": None, "taker_like": 0.20,
        })
        bets = lep._pivot_bets(rows, fill_model="queue_adjusted")
        self.assertAlmostEqual(lep._oracle_roi_per_bet(bets), 0.35, places=6)
        dist = lep._oracle_choice_distribution(bets)
        self.assertEqual(dist.get("current_limit"), 1)
        self.assertEqual(dist.get("taker_like"), 1)

    def test_cohort_breakdown_identifies_per_bucket_best_policy(self):
        # Cheap bets (ask < 0.55): current_limit wins.
        cheap = []
        for i in range(3):
            cheap += _make_bet(
                bet_id=f"cheap-{i}",
                roi_by_policy={
                    "current_limit": 0.30, "limit_p1c": 0.10,
                    "limit_p2c": 0.05, "taker_like": 0.01,
                },
                decision_ask=0.50,
            )
        # Expensive bets (ask >= 0.85): taker_like wins.
        exp = []
        for i in range(3):
            exp += _make_bet(
                bet_id=f"exp-{i}",
                roi_by_policy={
                    "current_limit": None, "limit_p1c": None,
                    "limit_p2c": 0.02, "taker_like": 0.15,
                },
                decision_ask=0.90,
            )
        bets = lep._pivot_bets(cheap + exp, fill_model="queue_adjusted")
        cohorts = lep._cohort_breakdown(bets, feature_name="ask_bucket")
        self.assertEqual(cohorts["<0.55"]["best_policy"], "current_limit")
        self.assertEqual(cohorts[">=0.85"]["best_policy"], "taker_like")
        self.assertGreater(cohorts[">=0.85"]["headroom_vs_baseline"], 0.10)


class CandidateRuleAndLoocvTests(unittest.TestCase):
    def _bets(self, *, n_per_cohort: int = 6):
        # Per-cohort sample size of 6 keeps both cohorts above
        # MIN_COHORT_SAMPLE (5), so the cohort-specific lookup branch
        # actually runs instead of falling back to global best.
        rows = []
        for i in range(n_per_cohort):
            # marketable: current_limit wins.
            rows += _make_bet(
                bet_id=f"m{i}",
                roi_by_policy={
                    "current_limit": 0.40, "limit_p1c": 0.0,
                    "limit_p2c": 0.0, "taker_like": -0.10,
                },
                marketable_at_entry=True,
            )
        for i in range(n_per_cohort):
            # passive: taker_like wins.
            rows += _make_bet(
                bet_id=f"p{i}",
                roi_by_policy={
                    "current_limit": None, "limit_p1c": None,
                    "limit_p2c": None, "taker_like": 0.30,
                },
                marketable_at_entry=False,
            )
        return lep._pivot_bets(rows, fill_model="queue_adjusted")

    def test_always_taker_rule_picks_taker_for_every_bet(self):
        bets = self._bets(n_per_cohort=3)
        result = lep._evaluate_rule_in_sample(bets, lep._rule_always("taker_like"))
        self.assertEqual(result["choice_distribution"], {"taker_like": 6})

    def test_learned_by_marketable_lookup_inverts_for_each_cohort(self):
        bets = self._bets(n_per_cohort=6)
        rule = lep._learned_lookup_rule("marketable")
        # In-sample evaluation: marketable bets -> current_limit (cohort best),
        # passive bets -> taker_like.
        in_sample = lep._evaluate_rule_in_sample(bets, rule)
        self.assertEqual(
            in_sample["choice_distribution"],
            {"current_limit": 6, "taker_like": 6},
        )

    def test_learned_lookup_falls_back_to_global_when_cohort_too_small(self):
        # Two-bet dataset: every cohort has <= 2 entries, well below
        # MIN_COHORT_SAMPLE=5 -- expect the global-best fallback to fire.
        bets = self._bets(n_per_cohort=1)  # 2 bets total
        rule = lep._learned_lookup_rule("marketable")
        held_out = bets[0]
        training = bets[1:]
        # Training has 1 bet with taker_like=+0.30; global mean ROIs:
        #   current_limit/limit_p1c/limit_p2c: 0.0; taker_like: +0.30
        # -> taker_like wins via fallback regardless of held-out cohort.
        choice = rule(held_out, training)
        self.assertEqual(choice, "taker_like")

    def test_loocv_holds_out_each_bet(self):
        bets = self._bets(n_per_cohort=3)
        # always_taker_like is constant -> LOOCV mean ROI = in-sample mean.
        in_sample = lep._evaluate_rule_in_sample(bets, lep._rule_always("taker_like"))
        loocv = lep._evaluate_rule_loocv(bets, lep._rule_always("taker_like"))
        self.assertAlmostEqual(in_sample["mean_roi"], loocv["mean_roi"], places=6)


class EndToEndTests(unittest.TestCase):
    def test_build_report_emits_full_artifact(self):
        rows = []
        # 12 bets, 6 in each ask bucket. taker_like always wins.
        for i in range(6):
            rows += _make_bet(
                bet_id=f"cheap-{i}",
                roi_by_policy={
                    "current_limit": -0.10, "limit_p1c": -0.05,
                    "limit_p2c": 0.02, "taker_like": 0.12,
                },
                decision_ask=0.60,
            )
        for i in range(6):
            rows += _make_bet(
                bet_id=f"exp-{i}",
                roi_by_policy={
                    "current_limit": None, "limit_p1c": None,
                    "limit_p2c": 0.0, "taker_like": 0.20,
                },
                decision_ask=0.80,
            )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_path = root / "rows.jsonl"
            output_root = root / "out"
            input_path.write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
            )
            rc = lep.main([
                "--input-path", str(input_path),
                "--output-root", str(output_root),
            ])
            self.assertEqual(rc, 0)
            json_path = output_root / "learned_execution_policy_report.json"
            md_path = output_root / "learned_execution_policy_report.md"
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["bets"], 12)
            self.assertEqual(payload["baseline_policy"], "current_limit")
            self.assertGreater(payload["oracle_headroom_vs_baseline"], 0.10)
            # taker_like dominates -> best rule should be always_taker_like
            # or learned_by_* both routing to taker_like.
            self.assertIsNotNone(payload["best_rule_by_loocv"])
            self.assertGreater(payload["best_rule_loocv_roi"], 0.10)
            md = md_path.read_text(encoding="utf-8")
            self.assertIn("## Headroom", md)
            self.assertIn("## Per-policy aggregates", md)
            self.assertIn("## Candidate rules", md)

    def test_empty_input_returns_warning(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            input_path = root / "empty.jsonl"
            input_path.write_text("", encoding="utf-8")
            report = lep.build_report(input_path=input_path)
            self.assertEqual(report["bets"], 0)
            self.assertIn("warning", report)


if __name__ == "__main__":
    unittest.main()
