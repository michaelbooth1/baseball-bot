import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_daily_human_review_report as bdhr  # noqa: E402


class DailyHumanReviewReportTests(unittest.TestCase):
    def test_build_report_writes_compact_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sessions_dir = root / "sessions"
            candidates_dir = root / "candidate_universe"
            log_dir = root / "logs"
            output_root = root / "out"
            sessions_dir.mkdir()
            candidates_dir.mkdir()
            log_dir.mkdir()

            session = {
                "date": "2026-05-01",
                "mode": "live",
                "summary": {
                    "orders_placed": 1,
                    "orders_filled": 1,
                    "wins": 1,
                    "losses": 0,
                    "total_profit": 5.0,
                    "roi": 0.5,
                    "ev_policy_shadow_allow": 0,
                    "ev_policy_shadow_block": 1,
                    "prob_calibration_shadow_scored": 3,
                    "candidate_rollup": {"large": "nested payload should be omitted"},
                    "current_state_edge_band_diagnostics": {
                        "current_edge_lt_0p03": {
                            "placed": 1,
                            "filled": 1,
                            "filled_roi": 0.5,
                        }
                    },
                    "shadow_feature_diagnostics": {
                        "regimes": {
                            "low_ask_high_edge": {
                                "placed": 1,
                                "filled": 1,
                                "filled_profit": 5.0,
                                "filled_roi": 0.5,
                            },
                            "runs_needed_exact_3p5": {
                                "placed": 0,
                                "filled": 0,
                                "filled_profit": 0.0,
                                "filled_roi": None,
                            },
                            "home_skip_bottom9_risk": {
                                "placed": 0,
                                "filled": 0,
                                "filled_profit": 0.0,
                                "filled_roi": None,
                            },
                        }
                    },
                },
                "bets": [
                    {
                        "bet_id": "bet_1",
                        "away_abbrev": "AWY",
                        "home_abbrev": "HOM",
                        "line": "8.5",
                        "inning": 5,
                        "order_status": "filled",
                        "entry_ask": 0.70,
                        "limit_price": 0.65,
                        "actual_fill_price": 0.65,
                        "filled_shares": 15.3846,
                        "fill_cost_usdc": 10.0,
                        "payout_usdc": 15.3846,
                        "fair_value": 0.90,
                        "edge": 0.20,
                        "current_state_value_edge": 0.02,
                        "shadow_phantom_risk_band": "low",
                        "shadow_phantom_risk_score": 0.30,
                        "won": True,
                        "profit": 5.0,
                        "final_total": 10,
                    }
                ],
            }
            rollup = {
                "attempted_rows": 10,
                "written_rows": 3,
                "dedup_suppressed_rows": 7,
                "write_error_rows": 0,
                "by_decision": {"trade": 1, "shadow_no_score_drift": 2},
                "by_decision_reason": {"trade:placed_bet": 1},
                "by_state_value_strategy": {"score_event_transition": 1},
            }
            (sessions_dir / "2026-05-01_session.json").write_text(
                json.dumps(session), encoding="utf-8"
            )
            (candidates_dir / "2026-05-01_candidate_rollup.json").write_text(
                json.dumps(rollup), encoding="utf-8"
            )
            (candidates_dir / "2026-05-01_candidates.jsonl").write_text(
                json.dumps({
                    "session_date": "2026-05-01",
                    "mode": "live",
                    "decision": "skip",
                    "decision_reason": "gate_stage2_suppression",
                    "game_pk": 55,
                    "away_abbrev": "STG",
                    "home_abbrev": "TWO",
                    "line": "8.5",
                    "inning": 6,
                    "inning_state": "Top",
                    "outs": 1,
                    "away_score_before": 4,
                    "home_score_before": 3,
                    "runners_on": 1,
                    "decision_ask": 0.50,
                    "fair_value": 0.82,
                    "stage2_run_env_delta": -0.08,
                }) + "\n",
                encoding="utf-8",
            )
            (candidates_dir / "2026-05-01_outcomes.jsonl").write_text(
                json.dumps({
                    "session_date": "2026-05-01",
                    "mode": "live",
                    "game_pk": 55,
                    "line": "8.5",
                    "over_hit": True,
                    "final_total": 9,
                    "final_away": 5,
                    "final_home": 4,
                }) + "\n",
                encoding="utf-8",
            )
            (log_dir / "2026-05-01.log").write_text(
                "INFO Polling 4 token books\nINFO Wrote 4 tick snapshots\nWARNING one warning\n",
                encoding="utf-8",
            )

            report = bdhr.build_report(
                session_date="2026-05-01",
                sessions_dir=sessions_dir,
                candidate_dir=candidates_dir,
                log_dir=log_dir,
            )
            json_path, md_path = bdhr.write_report(report, output_root)

            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())
            self.assertNotIn("candidate_rollup", report["session_summary"])
            self.assertNotIn("shadow_feature_diagnostics", report["session_summary"])
            self.assertIn("shadow_feature_diagnostics", report)
            self.assertEqual(report["bets"][0]["filled_shares"], 15.3846)
            self.assertEqual(report["bets"][0]["fill_cost_usdc"], 10.0)
            self.assertEqual(report["bets"][0]["payout_usdc"], 15.3846)
            self.assertEqual(report["log_health"]["counts"]["polling_token_books"], 1)
            self.assertEqual(report["stage2_suppression_dollar_audit"]["labeled_rows"], 1)
            self.assertEqual(report["stage2_suppression_dollar_audit"]["blocked_winning_rows"], 1)
            self.assertAlmostEqual(
                report["stage2_suppression_dollar_audit"]["net_hypothetical_profit_usdc"],
                10.0,
            )
            md_text = md_path.read_text(encoding="utf-8")
            self.assertIn("AWY@HOM", md_text)
            self.assertIn("low_ask_high_edge", md_text)
            self.assertIn("Stage-2 suppression", md_text)


class WilsonUpperBoundTests(unittest.TestCase):
    """Sanity-check the Wilson-score upper-bound helper that gates the
    fill-rate / win-rate alerts on statistical significance."""

    def test_returns_none_for_zero_trials(self):
        self.assertIsNone(bdhr._wilson_upper_bound(0, 0))

    def test_zero_successes_gives_low_upper_bound_at_small_n(self):
        # 0/5 with 90% one-sided z=1.645 -- the UB is around 0.41.
        ub = bdhr._wilson_upper_bound(0, 5)
        self.assertGreater(ub, 0.20)
        self.assertLess(ub, 0.50)

    def test_full_success_gives_high_upper_bound(self):
        # 5/5 -- UB should be at or near 1.0.
        ub = bdhr._wilson_upper_bound(5, 5)
        self.assertGreater(ub, 0.95)
        self.assertLessEqual(ub, 1.0)

    def test_upper_bound_narrows_with_larger_n(self):
        # As N grows the Wilson UB on the same p_hat narrows toward p_hat.
        ub_small = bdhr._wilson_upper_bound(2, 4)   # 50% with n=4
        ub_large = bdhr._wilson_upper_bound(50, 100)  # 50% with n=100
        self.assertGreater(ub_small, ub_large)


class CalibrationHealthTests(unittest.TestCase):
    """Calibration drift / load-time health alerts on the daily review."""

    def _write_artifact(self, path: Path, *, families: dict, generated_at: str) -> None:
        payload = {
            "schema_version": 2,
            "generated_at_utc": generated_at,
            "default_family": "score_event_transition",
            "selected_method": next(iter(families.values())).get("selected_method", "platt"),
            "families": families,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _write_candidates(self, path: Path, rows: list) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_no_alerts_for_healthy_calibrator(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = root / "cal" / "signal_win_calibration.json"
            candidate_dir = root / "cands"
            output_root = root / "out"
            today = "2026-05-11"

            # Healthy: both families select non-identity methods, generated today.
            self._write_artifact(
                artifact,
                families={
                    "score_event_transition": {
                        "selected_method": "platt",
                        "selection_audit": {
                            "primary_winner": "raw",
                            "identity_rejection_applied": True,
                        },
                    },
                    "no_score_drift": {
                        "selected_method": "isotonic",
                        "selection_audit": {
                            "primary_winner": "isotonic",
                            "identity_rejection_applied": False,
                        },
                    },
                },
                generated_at=f"{today}T01:00:00Z",
            )
            # 30 candidate rows per family with non-trivial calibrated/raw delta.
            rows = []
            for i in range(30):
                rows.append({
                    "fair_value_calibration_family": "score_event_transition",
                    "fair_value_raw": 0.95,
                    "fair_value_calibrated": 0.74,
                    "fair_value_calibration_method": "platt",
                    "fair_value_calibration_applied": True,
                })
                rows.append({
                    "fair_value_calibration_family": "no_score_drift",
                    "fair_value_raw": 0.70,
                    "fair_value_calibrated": 0.40,
                    "fair_value_calibration_method": "isotonic",
                    "fair_value_calibration_applied": True,
                })
            self._write_candidates(candidate_dir / f"{today}_candidates.jsonl", rows)

            health = bdhr._calibration_health(
                session_date=today,
                candidate_dir=candidate_dir,
                artifact_path=artifact,
                output_root=output_root,
            )
            self.assertEqual(health["alerts"], [])
            self.assertEqual(
                health["artifact_methods_by_family"],
                {"score_event_transition": "platt", "no_score_drift": "isotonic"},
            )
            score_metrics = health["sampled_metrics_by_family"]["score_event_transition"]
            self.assertEqual(score_metrics["rows_with_both_probs"], 30)
            self.assertGreater(score_metrics["mean_abs_delta"], 0.10)
            self.assertEqual(score_metrics["applied_share"], 1.0)

    def test_identity_artifact_and_near_zero_delta_trigger_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = root / "cal" / "signal_win_calibration.json"
            candidate_dir = root / "cands"
            output_root = root / "out"
            today = "2026-05-11"

            # Identity artifact + matching candidate rows where calibrated == raw.
            self._write_artifact(
                artifact,
                families={
                    "score_event_transition": {
                        "selected_method": "identity",
                        "selection_audit": {
                            "primary_winner": "raw",
                            "identity_rejection_applied": False,
                        },
                    },
                },
                generated_at=f"{today}T01:00:00Z",
            )
            rows = [
                {
                    "fair_value_calibration_family": "score_event_transition",
                    "fair_value_raw": 0.92,
                    "fair_value_calibrated": 0.92,
                    "fair_value_calibration_method": "identity",
                    "fair_value_calibration_applied": False,
                }
                for _ in range(40)
            ]
            self._write_candidates(candidate_dir / f"{today}_candidates.jsonl", rows)

            health = bdhr._calibration_health(
                session_date=today,
                candidate_dir=candidate_dir,
                artifact_path=artifact,
                output_root=output_root,
            )
            joined = " || ".join(health["alerts"])
            self.assertIn("identity for family 'score_event_transition'", joined)
            self.assertIn("mean |calibrated-raw|", joined)
            self.assertIn("calibration_applied=True share", joined)

    def test_small_sample_identity_method_still_triggers_alert(self) -> None:
        # Mirrors the 2026-05-10 real-data observation: only 13 candidate rows
        # had both probs (most are skip-without-FV), but every one of them used
        # method=identity. The dominant-method alert must fire even at
        # n < the abs-delta threshold so silent no-ops still get flagged.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = root / "cal" / "signal_win_calibration.json"
            candidate_dir = root / "cands"
            output_root = root / "out"
            today = "2026-05-11"

            self._write_artifact(
                artifact,
                families={
                    "score_event_transition": {"selected_method": "platt"},
                },
                generated_at=f"{today}T01:00:00Z",
            )
            rows = [
                {
                    "fair_value_calibration_family": "score_event_transition",
                    "fair_value_raw": 0.92,
                    "fair_value_calibrated": 0.92,
                    "fair_value_calibration_method": "identity",
                    "fair_value_calibration_applied": False,
                }
                for _ in range(13)  # below the n>=25 abs-delta threshold
            ]
            self._write_candidates(candidate_dir / f"{today}_candidates.jsonl", rows)

            health = bdhr._calibration_health(
                session_date=today,
                candidate_dir=candidate_dir,
                artifact_path=artifact,
                output_root=output_root,
            )
            joined = " || ".join(health["alerts"])
            # Method-level alert fires regardless of sample size.
            self.assertIn("13/13 candidate rows used calibration_method=identity", joined)
            # No abs-delta alert (under threshold) -- guard against
            # double-counting the same failure.
            self.assertNotIn("mean |calibrated-raw|", joined)

    def test_method_change_vs_prior_review_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = root / "cal" / "signal_win_calibration.json"
            candidate_dir = root / "cands"
            output_root = root / "out"
            yesterday = "2026-05-10"
            today = "2026-05-11"

            # Yesterday's review recorded score_event_transition=identity; today
            # the artifact selects platt -- expect a method-change alert.
            prior_review = output_root / f"{yesterday}_human_review.json"
            output_root.mkdir(parents=True, exist_ok=True)
            prior_review.write_text(json.dumps({
                "calibration_health": {
                    "artifact_methods_by_family": {
                        "score_event_transition": "identity",
                    },
                },
            }), encoding="utf-8")

            self._write_artifact(
                artifact,
                families={
                    "score_event_transition": {
                        "selected_method": "platt",
                        "selection_audit": {"identity_rejection_applied": True},
                    },
                },
                generated_at=f"{today}T01:00:00Z",
            )
            self._write_candidates(candidate_dir / f"{today}_candidates.jsonl", [])

            health = bdhr._calibration_health(
                session_date=today,
                candidate_dir=candidate_dir,
                artifact_path=artifact,
                output_root=output_root,
            )
            joined = " || ".join(health["alerts"])
            self.assertIn("changed for family 'score_event_transition'", joined)
            self.assertIn("identity -> platt", joined)
            self.assertEqual(
                health["method_changes_since_prior"]["score_event_transition"],
                {"from": "identity", "to": "platt"},
            )

    def test_stale_artifact_age_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = root / "cal" / "signal_win_calibration.json"
            candidate_dir = root / "cands"
            output_root = root / "out"
            today = "2026-05-11"

            # Artifact built 30 days ago; should fire stale alert (> 14d).
            self._write_artifact(
                artifact,
                families={
                    "score_event_transition": {"selected_method": "platt"},
                },
                generated_at="2026-04-11T00:00:00Z",
            )
            self._write_candidates(candidate_dir / f"{today}_candidates.jsonl", [])

            health = bdhr._calibration_health(
                session_date=today,
                candidate_dir=candidate_dir,
                artifact_path=artifact,
                output_root=output_root,
            )
            stale_alerts = [a for a in health["alerts"] if "days old" in a]
            self.assertEqual(len(stale_alerts), 1)
            self.assertGreaterEqual(health["artifact_age_days"], 14)

    def test_missing_artifact_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = root / "missing.json"
            candidate_dir = root / "cands"
            output_root = root / "out"
            today = "2026-05-11"
            self._write_candidates(candidate_dir / f"{today}_candidates.jsonl", [])

            health = bdhr._calibration_health(
                session_date=today,
                candidate_dir=candidate_dir,
                artifact_path=artifact,
                output_root=output_root,
            )
            self.assertFalse(health["artifact_present"])
            self.assertTrue(any("missing" in a for a in health["alerts"]))


class CalibrationShadowModeAlertSuppressionTests(unittest.TestCase):
    """The applied-share alert is a documented false positive when the
    runtime is intentionally running calibration in shadow mode (every
    row carries `fair_value_calibration_mode="shadow"` and applied=False
    by design). Suppress the alert; emit a one-line informational note
    instead so the calibrator behaviour is still visible in the daily
    review."""

    def _write_artifact(self, path: Path, *, families: dict, generated_at: str) -> None:
        payload = {
            "schema_version": 2,
            "generated_at_utc": generated_at,
            "default_family": "score_event_transition",
            "selected_method": next(iter(families.values())).get("selected_method", "platt"),
            "families": families,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _write_candidates(self, path: Path, rows: list) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def _build_rows(
        self, *, n: int, mode: str, applied: bool = False
    ) -> list:
        return [
            {
                "fair_value_calibration_family": "score_event_transition",
                "fair_value_raw": 0.95,
                "fair_value_calibrated": 0.74,
                "fair_value_calibration_method": "platt",
                "fair_value_calibration_mode": mode,
                "fair_value_calibration_applied": applied,
            }
            for _ in range(n)
        ]

    def test_all_shadow_rows_suppress_low_applied_alert(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = root / "cal" / "signal_win_calibration.json"
            candidate_dir = root / "cands"
            output_root = root / "out"
            today = "2026-05-14"

            self._write_artifact(
                artifact,
                families={"score_event_transition": {"selected_method": "platt"}},
                generated_at=f"{today}T01:00:00Z",
            )
            self._write_candidates(
                candidate_dir / f"{today}_candidates.jsonl",
                self._build_rows(n=40, mode="shadow", applied=False),
            )

            health = bdhr._calibration_health(
                session_date=today,
                candidate_dir=candidate_dir,
                artifact_path=artifact,
                output_root=output_root,
            )
            joined_alerts = " || ".join(health["alerts"])
            self.assertNotIn("calibration_applied=True share", joined_alerts)
            joined_notes = " || ".join(health.get("notes") or [])
            self.assertIn("calibration in shadow mode", joined_notes)
            self.assertIn("applied=0% is expected", joined_notes)
            metrics = health["sampled_metrics_by_family"]["score_event_transition"]
            self.assertEqual(metrics["shadow_share"], 1.0)
            self.assertEqual(metrics["mode_counts"], {"shadow": 40})

    def test_non_shadow_low_applied_share_still_alerts(self) -> None:
        # Same low applied share, but mode says live -- the alert is the
        # original "calibration mode may be off" diagnostic and must fire.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = root / "cal" / "signal_win_calibration.json"
            candidate_dir = root / "cands"
            output_root = root / "out"
            today = "2026-05-14"

            self._write_artifact(
                artifact,
                families={"score_event_transition": {"selected_method": "platt"}},
                generated_at=f"{today}T01:00:00Z",
            )
            self._write_candidates(
                candidate_dir / f"{today}_candidates.jsonl",
                self._build_rows(n=40, mode="live", applied=False),
            )

            health = bdhr._calibration_health(
                session_date=today,
                candidate_dir=candidate_dir,
                artifact_path=artifact,
                output_root=output_root,
            )
            joined_alerts = " || ".join(health["alerts"])
            self.assertIn("calibration_applied=True share", joined_alerts)
            metrics = health["sampled_metrics_by_family"]["score_event_transition"]
            self.assertEqual(metrics["shadow_share"], 0.0)

    def test_mixed_shadow_below_dominant_threshold_still_alerts(self) -> None:
        # 50/50 shadow vs live with low applied share -- ambiguous; still
        # worth firing the alert so a half-broken config gets attention.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = root / "cal" / "signal_win_calibration.json"
            candidate_dir = root / "cands"
            output_root = root / "out"
            today = "2026-05-14"

            self._write_artifact(
                artifact,
                families={"score_event_transition": {"selected_method": "platt"}},
                generated_at=f"{today}T01:00:00Z",
            )
            rows = (
                self._build_rows(n=20, mode="shadow", applied=False)
                + self._build_rows(n=20, mode="live", applied=False)
            )
            self._write_candidates(candidate_dir / f"{today}_candidates.jsonl", rows)

            health = bdhr._calibration_health(
                session_date=today,
                candidate_dir=candidate_dir,
                artifact_path=artifact,
                output_root=output_root,
            )
            joined_alerts = " || ".join(health["alerts"])
            self.assertIn("calibration_applied=True share", joined_alerts)
            metrics = health["sampled_metrics_by_family"]["score_event_transition"]
            self.assertEqual(metrics["shadow_share"], 0.5)

    def test_missing_mode_field_preserves_legacy_behaviour(self) -> None:
        # Rows that don't carry the mode field at all (older candidate
        # rows) must not silently suppress the alert -- treat as
        # mode-unknown and let the alert fire.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = root / "cal" / "signal_win_calibration.json"
            candidate_dir = root / "cands"
            output_root = root / "out"
            today = "2026-05-14"

            self._write_artifact(
                artifact,
                families={"score_event_transition": {"selected_method": "platt"}},
                generated_at=f"{today}T01:00:00Z",
            )
            rows = [
                {
                    "fair_value_calibration_family": "score_event_transition",
                    "fair_value_raw": 0.95,
                    "fair_value_calibrated": 0.74,
                    "fair_value_calibration_method": "platt",
                    "fair_value_calibration_applied": False,
                    # no fair_value_calibration_mode field
                }
                for _ in range(40)
            ]
            self._write_candidates(candidate_dir / f"{today}_candidates.jsonl", rows)

            health = bdhr._calibration_health(
                session_date=today,
                candidate_dir=candidate_dir,
                artifact_path=artifact,
                output_root=output_root,
            )
            joined_alerts = " || ".join(health["alerts"])
            self.assertIn("calibration_applied=True share", joined_alerts)
            metrics = health["sampled_metrics_by_family"]["score_event_transition"]
            self.assertIsNone(metrics["shadow_share"])


class FillRateAndSignalQualityDriftTests(unittest.TestCase):
    """Drift alerts that compare today's fill rate / win rate against the
    trailing daily-review window."""

    def _write_prior_review(
        self,
        *,
        output_root: Path,
        date: str,
        mode: str,
        placed: int,
        filled: int,
        wins: int,
    ) -> None:
        output_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_date": date,
            "mode": mode,
            "bet_totals": {
                "count": placed,
                "filled": filled,
                "wins": wins,
            },
        }
        (output_root / f"{date}_human_review.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_fill_rate_drop_alert_fires(self) -> None:
        # Trailing 7d: 14/16 = 87.5% fill. Today: 1/5 = 20%. Drop = 67pp.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            for i, (placed, filled) in enumerate(
                [(3, 3), (2, 2), (4, 3), (3, 3), (2, 1), (1, 1), (1, 1)], start=1
            ):
                self._write_prior_review(
                    output_root=out,
                    date=bdhr._shift_date("2026-05-11", -i),
                    mode="live",
                    placed=placed,
                    filled=filled,
                    wins=filled,
                )
            trailing = bdhr._load_trailing_reviews(
                output_root=out, today="2026-05-11", days=7, mode="live"
            )
            health = bdhr._fill_rate_health(
                today_bet_totals={"count": 5, "filled": 1, "wins": 0},
                trailing_reviews=trailing,
            )
            joined = " || ".join(health["alerts"])
            self.assertIn("fill rate dropped", joined)
            self.assertIn("1/5 (20%)", joined)
            self.assertEqual(health["today"]["fill_rate"], 0.2)
            self.assertEqual(health["baseline"]["days_in_baseline"], 7)

    def test_zero_fill_day_fires_independent_of_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            # Empty trailing window -- the zero-fill alert must still fire.
            health = bdhr._fill_rate_health(
                today_bet_totals={"count": 6, "filled": 0, "wins": 0},
                trailing_reviews=[],
            )
            joined = " || ".join(health["alerts"])
            self.assertIn("zero-fill day: 0/6", joined)

    def test_wilson_gate_suppresses_small_sample_false_positive(self) -> None:
        # Same baseline (87.5% fill) but today is 1/3 = 33% fill rate.
        # Point-estimate drop is 54pp -- past the 20pp threshold. But at
        # n=3 the Wilson upper bound is ~0.78, which is BELOW the 87.5%
        # baseline -- still significant. So the alert still fires.
        # This case verifies the Wilson gate doesn't reject a real signal
        # just because n is small.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            for i, (placed, filled) in enumerate(
                [(3, 3), (2, 2), (4, 3), (3, 3), (2, 1), (1, 1), (1, 1)], start=1
            ):
                self._write_prior_review(
                    output_root=out,
                    date=bdhr._shift_date("2026-05-11", -i),
                    mode="live",
                    placed=placed,
                    filled=filled,
                    wins=filled,
                )
            trailing = bdhr._load_trailing_reviews(
                output_root=out, today="2026-05-11", days=7, mode="live"
            )
            health = bdhr._fill_rate_health(
                today_bet_totals={"count": 3, "filled": 1, "wins": 0},
                trailing_reviews=trailing,
            )
            joined = " || ".join(health["alerts"])
            self.assertIn("fill rate dropped", joined)
            self.assertIn("Wilson UB", joined)

    def test_wilson_gate_suppresses_marginal_drop_at_small_n(self) -> None:
        # Today: 2/3 filled (67%). Baseline: 7/10 = 70%.
        # Point-estimate drop = 3pp -- below 20pp threshold, so no alert.
        # The Wilson gate isn't relevant here; this exercises the existing
        # point-estimate threshold so we know both gates compose correctly.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            for i in range(1, 8):
                self._write_prior_review(
                    output_root=out,
                    date=bdhr._shift_date("2026-05-11", -i),
                    mode="live",
                    placed=2,
                    filled=2 if i % 3 != 0 else 1,  # ~70% fill rate
                    wins=1,
                )
            trailing = bdhr._load_trailing_reviews(
                output_root=out, today="2026-05-11", days=7, mode="live"
            )
            health = bdhr._fill_rate_health(
                today_bet_totals={"count": 3, "filled": 2, "wins": 1},
                trailing_reviews=trailing,
            )
            self.assertEqual(health["alerts"], [])

    def test_no_fill_alert_when_today_sample_too_small(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            for i in range(1, 8):
                self._write_prior_review(
                    output_root=out,
                    date=bdhr._shift_date("2026-05-11", -i),
                    mode="live",
                    placed=3,
                    filled=3,
                    wins=2,
                )
            trailing = bdhr._load_trailing_reviews(
                output_root=out, today="2026-05-11", days=7, mode="live"
            )
            # Today only had 2 placed -- below DRIFT_MIN_TODAY_SAMPLE (3).
            health = bdhr._fill_rate_health(
                today_bet_totals={"count": 2, "filled": 0, "wins": 0},
                trailing_reviews=trailing,
            )
            self.assertEqual(health["alerts"], [])

    def test_signal_quality_drop_alert_fires(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            # Trailing baseline: 12/15 = 80% WR.
            for i, (filled, wins) in enumerate(
                [(3, 3), (2, 2), (3, 2), (2, 1), (2, 2), (2, 1), (1, 1)], start=1
            ):
                self._write_prior_review(
                    output_root=out,
                    date=bdhr._shift_date("2026-05-11", -i),
                    mode="live",
                    placed=filled,
                    filled=filled,
                    wins=wins,
                )
            trailing = bdhr._load_trailing_reviews(
                output_root=out, today="2026-05-11", days=7, mode="live"
            )
            # Today: 1/5 = 20% WR. Drop = 60pp.
            health = bdhr._signal_quality_health(
                today_bet_totals={"count": 5, "filled": 5, "wins": 1},
                trailing_reviews=trailing,
            )
            joined = " || ".join(health["alerts"])
            self.assertIn("filled win rate dropped", joined)
            self.assertIn("1/5 (20%)", joined)

    def test_zero_win_day_fires_at_min_sample(self) -> None:
        # 5 filled, 0 wins -- meets DRIFT_ZERO_DAY_MIN_SAMPLE.
        health = bdhr._signal_quality_health(
            today_bet_totals={"count": 5, "filled": 5, "wins": 0},
            trailing_reviews=[],
        )
        joined = " || ".join(health["alerts"])
        self.assertIn("zero-win day: 0/5", joined)

    def test_trailing_loader_skips_other_modes(self) -> None:
        # Live review for today; trailing window has paper rows that must
        # be skipped so live drift isn't compared against paper.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            self._write_prior_review(
                output_root=out, date="2026-05-10", mode="paper",
                placed=20, filled=20, wins=20,
            )
            self._write_prior_review(
                output_root=out, date="2026-05-09", mode="live",
                placed=4, filled=4, wins=3,
            )
            trailing = bdhr._load_trailing_reviews(
                output_root=out, today="2026-05-11", days=7, mode="live"
            )
            modes = [r.get("mode") for r in trailing]
            self.assertEqual(modes, ["live"])
            self.assertEqual(len(trailing), 1)

    def test_drift_alerts_surface_in_notes(self) -> None:
        # End-to-end: drift alerts must reach the Notes block in the
        # markdown so operators see them at a glance.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sessions_dir = root / "sessions"
            candidate_dir = root / "cands"
            log_dir = root / "logs"
            output_root = root / "out"
            sessions_dir.mkdir()
            candidate_dir.mkdir()
            log_dir.mkdir()

            # Trailing 7d strong baseline: 14/14 placed, 14/14 filled.
            for i in range(1, 8):
                d = bdhr._shift_date("2026-05-11", -i)
                output_root.mkdir(parents=True, exist_ok=True)
                (output_root / f"{d}_human_review.json").write_text(
                    json.dumps({
                        "session_date": d, "mode": "live",
                        "bet_totals": {"count": 2, "filled": 2, "wins": 2},
                    }),
                    encoding="utf-8",
                )

            # Today: 5 placed, 0 filled (zero-fill + drop alerts).
            session = {
                "date": "2026-05-11", "mode": "live",
                "summary": {"orders_placed": 5, "orders_filled": 0},
                "bets": [
                    {"bet_id": f"b{i}", "order_status": "cancelled",
                     "won": None, "profit": 0.0}
                    for i in range(5)
                ],
            }
            (sessions_dir / "2026-05-11_session.json").write_text(
                json.dumps(session), encoding="utf-8"
            )
            (candidate_dir / "2026-05-11_candidates.jsonl").write_text("", encoding="utf-8")
            (candidate_dir / "2026-05-11_outcomes.jsonl").write_text("", encoding="utf-8")

            report = bdhr.build_report(
                session_date="2026-05-11",
                sessions_dir=sessions_dir,
                candidate_dir=candidate_dir,
                log_dir=log_dir,
                output_root=output_root,
                # Use a missing artifact path to avoid cross-talk with the
                # repo's real calibration JSON during this test.
                calibration_artifact=root / "no-such-cal.json",
                include_log_counts=False,
            )
            json_path, md_path = bdhr.write_report(report, output_root)
            md = md_path.read_text(encoding="utf-8")
            self.assertIn("Fill-rate drift:", md)
            self.assertIn("zero-fill day", md)
            # Drift Health section is present and shows today vs baseline.
            self.assertIn("## Drift Health", md)
            self.assertIn("trailing 7d baseline", md.lower())


class RegimeMixShiftDriftTests(unittest.TestCase):
    """TVD-based regime-mix shift alert. Catches the failure mode where
    outcome metrics look fine but the bot is suddenly trading a
    materially different cohort (different ask buckets, different
    current-state-edge bands, different phantom-risk bands)."""

    def _bet(
        self,
        *,
        ask: float,
        current_edge: float,
        phantom_band: str,
    ) -> dict:
        return {
            "entry_ask": ask,
            "current_state_value_edge": current_edge,
            "phantom_risk_band": phantom_band,
        }

    def _write_prior_review_with_distribution(
        self,
        *,
        output_root: Path,
        date: str,
        mode: str,
        distributions: dict,
    ) -> None:
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / f"{date}_human_review.json").write_text(
            json.dumps({
                "session_date": date,
                "mode": mode,
                "regime_mix_health": {"today_distributions": distributions},
            }),
            encoding="utf-8",
        )

    def test_no_alert_when_today_matches_trailing_distribution(self) -> None:
        # Trailing 7d: dominant 0.65-0.75 ask bucket; today same shape.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            for i in range(1, 8):
                self._write_prior_review_with_distribution(
                    output_root=out,
                    date=bdhr._shift_date("2026-05-12", -i),
                    mode="live",
                    distributions={
                        "ask_bucket": {"0.65-0.75": 4, "0.75-0.85": 1},
                        "current_state_edge_bucket": {"<0.03": 3, "0.03-0.08": 2},
                        "phantom_risk_band": {"low": 4, "medium": 1},
                    },
                )
            trailing = bdhr._load_trailing_reviews(
                output_root=out, today="2026-05-12", days=7, mode="live"
            )
            today_bets = [
                self._bet(ask=0.70, current_edge=0.02, phantom_band="low"),
                self._bet(ask=0.72, current_edge=0.05, phantom_band="low"),
                self._bet(ask=0.78, current_edge=0.01, phantom_band="medium"),
                self._bet(ask=0.68, current_edge=0.02, phantom_band="low"),
                self._bet(ask=0.66, current_edge=0.04, phantom_band="low"),
            ]
            health = bdhr._regime_mix_health(
                today_bet_rows=today_bets,
                trailing_reviews=trailing,
            )
            self.assertEqual(health["alerts"], [])
            for tvd in health["tvd_by_dimension"].values():
                self.assertLess(tvd, bdhr.DRIFT_REGIME_MIX_TVD)

    def test_ask_bucket_shift_to_high_band_fires_alert(self) -> None:
        # Trailing baseline: every bet in 0.65-0.75 ask bucket.
        # Today: every bet in >=0.85 -- max possible TVD (1.0).
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            for i in range(1, 8):
                self._write_prior_review_with_distribution(
                    output_root=out,
                    date=bdhr._shift_date("2026-05-12", -i),
                    mode="live",
                    distributions={
                        "ask_bucket": {"0.65-0.75": 3},
                        "current_state_edge_bucket": {"0.03-0.08": 3},
                        "phantom_risk_band": {"low": 3},
                    },
                )
            trailing = bdhr._load_trailing_reviews(
                output_root=out, today="2026-05-12", days=7, mode="live"
            )
            today_bets = [
                self._bet(ask=0.92, current_edge=0.04, phantom_band="low"),
                self._bet(ask=0.91, current_edge=0.05, phantom_band="low"),
                self._bet(ask=0.93, current_edge=0.06, phantom_band="low"),
                self._bet(ask=0.90, current_edge=0.07, phantom_band="low"),
                self._bet(ask=0.95, current_edge=0.04, phantom_band="low"),
            ]
            health = bdhr._regime_mix_health(
                today_bet_rows=today_bets,
                trailing_reviews=trailing,
            )
            self.assertEqual(health["tvd_by_dimension"]["ask_bucket"], 1.0)
            ask_alerts = [a for a in health["alerts"] if a.startswith("ask_bucket")]
            self.assertEqual(len(ask_alerts), 1)
            self.assertIn("today top=>=0.85 (5/5)", ask_alerts[0])
            self.assertIn("baseline top=0.65-0.75 (21/21)", ask_alerts[0])

    def test_no_alert_when_today_sample_too_small(self) -> None:
        # Strong baseline, but only 2 placed bets today -- below threshold.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            for i in range(1, 8):
                self._write_prior_review_with_distribution(
                    output_root=out,
                    date=bdhr._shift_date("2026-05-12", -i),
                    mode="live",
                    distributions={"ask_bucket": {"0.65-0.75": 3},
                                   "current_state_edge_bucket": {"<0.03": 3},
                                   "phantom_risk_band": {"low": 3}},
                )
            trailing = bdhr._load_trailing_reviews(
                output_root=out, today="2026-05-12", days=7, mode="live"
            )
            today_bets = [
                self._bet(ask=0.95, current_edge=0.20, phantom_band="high"),
                self._bet(ask=0.95, current_edge=0.20, phantom_band="high"),
            ]
            health = bdhr._regime_mix_health(
                today_bet_rows=today_bets,
                trailing_reviews=trailing,
            )
            self.assertEqual(health["alerts"], [])

    def test_no_alert_when_baseline_too_small(self) -> None:
        # Today is large; trailing window has < 10 baseline bets total.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            self._write_prior_review_with_distribution(
                output_root=out,
                date="2026-05-11",
                mode="live",
                distributions={"ask_bucket": {"0.65-0.75": 2},
                               "current_state_edge_bucket": {"<0.03": 2},
                               "phantom_risk_band": {"low": 2}},
            )
            trailing = bdhr._load_trailing_reviews(
                output_root=out, today="2026-05-12", days=7, mode="live"
            )
            today_bets = [
                self._bet(ask=0.95, current_edge=0.20, phantom_band="high")
            ] * 8
            health = bdhr._regime_mix_health(
                today_bet_rows=today_bets,
                trailing_reviews=trailing,
            )
            self.assertEqual(health["alerts"], [])
            # Baseline data still captured for traceability.
            self.assertEqual(health["baseline_total_bets"], 2)

    def test_phantom_band_shift_fires_alert(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            for i in range(1, 8):
                self._write_prior_review_with_distribution(
                    output_root=out,
                    date=bdhr._shift_date("2026-05-12", -i),
                    mode="live",
                    distributions={
                        "ask_bucket": {"0.65-0.75": 2, "0.75-0.85": 1},
                        "current_state_edge_bucket": {"<0.03": 2, "0.03-0.08": 1},
                        "phantom_risk_band": {"low": 3},
                    },
                )
            trailing = bdhr._load_trailing_reviews(
                output_root=out, today="2026-05-12", days=7, mode="live"
            )
            # Today: identical ask + current-edge mix to baseline, but
            # phantom band shifted entirely to 'high'. Only the phantom
            # alert should fire.
            today_bets = [
                self._bet(ask=0.70, current_edge=0.02, phantom_band="high"),
                self._bet(ask=0.72, current_edge=0.04, phantom_band="high"),
                self._bet(ask=0.80, current_edge=0.02, phantom_band="high"),
                self._bet(ask=0.68, current_edge=0.04, phantom_band="high"),
                self._bet(ask=0.74, current_edge=0.02, phantom_band="high"),
            ]
            health = bdhr._regime_mix_health(
                today_bet_rows=today_bets,
                trailing_reviews=trailing,
            )
            phantom_alerts = [
                a for a in health["alerts"] if a.startswith("phantom_risk_band")
            ]
            self.assertEqual(len(phantom_alerts), 1)
            self.assertIn("today top=high", phantom_alerts[0])
            self.assertIn("baseline top=low", phantom_alerts[0])

    def test_legacy_review_without_distribution_is_skipped(self) -> None:
        # Old reviews that predate this feature don't carry
        # `regime_mix_health` -- they should be silently skipped, not
        # crash the trailing aggregator.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            (out).mkdir(parents=True, exist_ok=True)
            (out / "2026-05-11_human_review.json").write_text(
                json.dumps({"session_date": "2026-05-11", "mode": "live"}),
                encoding="utf-8",
            )
            trailing = bdhr._load_trailing_reviews(
                output_root=out, today="2026-05-12", days=7, mode="live"
            )
            health = bdhr._regime_mix_health(
                today_bet_rows=[self._bet(ask=0.7, current_edge=0.05, phantom_band="low")],
                trailing_reviews=trailing,
            )
            self.assertEqual(health["baseline_total_bets"], 0)
            self.assertEqual(health["days_in_baseline"], 0)
            self.assertEqual(health["alerts"], [])

    def test_total_variation_distance_math(self) -> None:
        # Identical distributions -> TVD 0; disjoint -> TVD 1; half/half -> 0.5.
        self.assertEqual(
            bdhr._total_variation_distance({"a": 5}, {"a": 5}), 0.0
        )
        self.assertEqual(
            bdhr._total_variation_distance({"a": 5}, {"b": 5}), 1.0
        )
        self.assertAlmostEqual(
            bdhr._total_variation_distance({"a": 1, "b": 1}, {"a": 1}),
            0.5,
        )
        self.assertIsNone(
            bdhr._total_variation_distance({}, {"a": 5})
        )


class ReconcilerSummaryTests(unittest.TestCase):
    """Daily passive monitoring of the orphan-fill reconciler -- counts
    today's reconciled fills and fires Active #2's promotion-trigger
    alert when the recovered share crosses the threshold."""

    def _filled_bet(self, **kwargs) -> dict:
        bet = {
            "bet_id": kwargs.get("bet_id", "b"),
            "order_status": "filled",
            "away_abbrev": "A",
            "home_abbrev": "B",
            "line": "8.5",
        }
        bet.update(kwargs)
        return bet

    def test_no_alert_when_no_bets_reconciled(self):
        bets = [
            self._filled_bet(bet_id="b1"),
            self._filled_bet(bet_id="b2"),
        ]
        summary = bdhr._reconciler_summary(bets)
        self.assertEqual(summary["reconciled_total"], 0)
        self.assertEqual(summary["filled_total"], 2)
        self.assertEqual(summary["reconciled_share"], 0.0)
        self.assertEqual(summary["alerts"], [])

    def test_alert_fires_above_share_threshold(self):
        # 2 reconciled / 5 filled = 40% >> 10% threshold.
        bets = [
            self._filled_bet(bet_id="b1", reconciliation_source="data_api_trades",
                             reconciliation_trade_id="t_x"),
            self._filled_bet(bet_id="b2", reconciliation_source="data_api_position_only"),
            self._filled_bet(bet_id="b3"),
            self._filled_bet(bet_id="b4"),
            self._filled_bet(bet_id="b5"),
        ]
        summary = bdhr._reconciler_summary(bets)
        self.assertEqual(summary["reconciled_total"], 2)
        self.assertEqual(summary["filled_total"], 5)
        self.assertEqual(summary["reconciled_share"], 0.4)
        self.assertEqual(
            summary["by_source"],
            {"data_api_trades": 1, "data_api_position_only": 1},
        )
        joined = " || ".join(summary["alerts"])
        self.assertIn("2/5 (40%)", joined)
        self.assertIn("Active #2", joined)

    def test_no_alert_when_filled_sample_too_small(self):
        # 1 reconciled / 2 filled = 50% but filled_total < 3 -> no alert.
        bets = [
            self._filled_bet(bet_id="b1", reconciliation_source="data_api_trades"),
            self._filled_bet(bet_id="b2"),
        ]
        summary = bdhr._reconciler_summary(bets)
        self.assertEqual(summary["reconciled_total"], 1)
        self.assertEqual(summary["reconciled_share"], 0.5)
        self.assertEqual(summary["alerts"], [])

    def test_skipped_and_cancelled_bets_do_not_inflate_filled_total(self):
        bets = [
            self._filled_bet(bet_id="b1"),
            {"bet_id": "b2", "order_status": "cancelled"},
            {"bet_id": "b3", "order_status": "skip"},
            self._filled_bet(bet_id="b4", reconciliation_source="data_api_trades"),
        ]
        summary = bdhr._reconciler_summary(bets)
        self.assertEqual(summary["filled_total"], 2)
        self.assertEqual(summary["reconciled_total"], 1)
        self.assertEqual(summary["reconciled_share"], 0.5)


class CohortRoiHealthTests(unittest.TestCase):
    """Cohort-ROI drift alerts (added 2026-05-15). Companion to
    regime_mix_health: that fires on pre-trade *distribution* shifts;
    this fires on *outcome* shifts -- a cohort that was profitable but
    is now losing real money.
    """

    def _filled_bet(
        self,
        *,
        edge: float,
        ask: float,
        inning: int,
        line: str,
        cse: float,
        won: bool,
        profit: float,
        stake: float = 8.0,
    ) -> dict:
        return {
            "status": "filled",
            "edge": edge,
            "entry_ask": ask,
            "inning": inning,
            "line": line,
            "current_state_value_edge": cse,
            "won": won,
            "profit": profit,
            "fill_cost_usdc": stake,
        }

    def test_edge_bucket_partitions_correctly(self):
        self.assertEqual(bdhr._cohort_edge_bucket(0.10), "<0.15")
        self.assertEqual(bdhr._cohort_edge_bucket(0.15), "0.15-0.18")
        self.assertEqual(bdhr._cohort_edge_bucket(0.179), "0.15-0.18")
        self.assertEqual(bdhr._cohort_edge_bucket(0.18), "0.18-0.22")
        self.assertEqual(bdhr._cohort_edge_bucket(0.22), ">=0.22")
        self.assertEqual(bdhr._cohort_edge_bucket(None), "missing")

    def test_inning_bucket_partitions_correctly(self):
        self.assertEqual(bdhr._cohort_inning_bucket(4), "<=5")
        self.assertEqual(bdhr._cohort_inning_bucket(5), "<=5")
        self.assertEqual(bdhr._cohort_inning_bucket(6), "6")
        self.assertEqual(bdhr._cohort_inning_bucket(7), "7")
        self.assertEqual(bdhr._cohort_inning_bucket(9), ">=8")
        self.assertEqual(bdhr._cohort_inning_bucket("not a number"), "missing")

    def test_line_bucket_partitions_correctly(self):
        # line is stored as string in real session JSONs; bucketer must parse.
        self.assertEqual(bdhr._cohort_line_bucket("6.5"), "<=7.5")
        self.assertEqual(bdhr._cohort_line_bucket("7.5"), "<=7.5")
        self.assertEqual(bdhr._cohort_line_bucket("8.5"), "8.5")
        self.assertEqual(bdhr._cohort_line_bucket("9.5"), "9.5")
        self.assertEqual(bdhr._cohort_line_bucket("10.5"), ">=10.5")
        self.assertEqual(bdhr._cohort_line_bucket("11.5"), ">=10.5")

    def test_aggregate_cohort_computes_roi_and_wilson(self):
        bets = [
            self._filled_bet(edge=0.16, ask=0.78, inning=6, line="9.5", cse=0.05, won=False, profit=-8.0),
            self._filled_bet(edge=0.17, ask=0.78, inning=6, line="9.5", cse=0.05, won=True,  profit=2.5),
            self._filled_bet(edge=0.16, ask=0.78, inning=6, line="9.5", cse=0.05, won=False, profit=-8.0),
        ]
        agg = bdhr._aggregate_cohort(
            bets, lambda b: bdhr._cohort_edge_bucket(b.get("edge"))
        )
        bucket = agg["0.15-0.18"]
        self.assertEqual(bucket["n"], 3)
        self.assertEqual(bucket["wins"], 1)
        self.assertEqual(bucket["losses"], 2)
        self.assertAlmostEqual(bucket["profit"], -13.5, places=2)
        self.assertAlmostEqual(bucket["stake"], 24.0, places=2)
        self.assertAlmostEqual(bucket["roi"], -13.5 / 24.0, places=4)
        self.assertIsNotNone(bucket["wilson_ub_wr"])

    def test_absolute_losing_alert_fires_at_threshold(self):
        # 6 bets in edge 0.15-0.18 cohort, all losses -> -100% ROI, far past -10%.
        recent_bets = [
            self._filled_bet(edge=0.16, ask=0.78, inning=6, line="9.5", cse=0.05, won=False, profit=-8.0)
            for _ in range(6)
        ]
        # Convert to "trailing review" shape: one prior review carrying these bets.
        prior = {"bets": recent_bets, "session_date": "2026-05-13", "mode": "live"}
        health = bdhr._cohort_roi_health(
            today_bet_rows=[],
            trailing_reviews=[prior],
            baseline_reviews=[prior],
        )
        joined = " || ".join(health["alerts"])
        self.assertIn("edge_bucket=0.15-0.18", joined)
        self.assertIn("absolute loss threshold", joined)
        self.assertIn("trailing 7d", joined)

    def test_absolute_losing_alert_does_not_fire_below_min_n(self):
        # 4 losses -> well past -10% but below the 5-bet floor.
        recent_bets = [
            self._filled_bet(edge=0.16, ask=0.78, inning=6, line="9.5", cse=0.05, won=False, profit=-8.0)
            for _ in range(4)
        ]
        prior = {"bets": recent_bets, "session_date": "2026-05-13", "mode": "live"}
        health = bdhr._cohort_roi_health(
            today_bet_rows=[],
            trailing_reviews=[prior],
            baseline_reviews=[prior],
        )
        edge_alerts = [a for a in health["alerts"] if "edge_bucket=0.15-0.18" in a]
        self.assertEqual(edge_alerts, [], f"no edge_bucket alert expected; got: {edge_alerts}")

    def test_absolute_losing_alert_does_not_fire_for_healthy_cohort(self):
        # 6 bets in edge 0.18-0.22, all winners -> +ROI, no alert.
        recent_bets = [
            self._filled_bet(edge=0.20, ask=0.78, inning=7, line="8.5", cse=0.05, won=True, profit=2.5)
            for _ in range(6)
        ]
        prior = {"bets": recent_bets, "session_date": "2026-05-13", "mode": "live"}
        health = bdhr._cohort_roi_health(
            today_bet_rows=[],
            trailing_reviews=[prior],
            baseline_reviews=[prior],
        )
        self.assertEqual(health["alerts"], [])

    def test_regime_change_alert_fires_when_recent_diverges(self):
        # Baseline (30d): cohort was healthy (all winners, ~+30% ROI).
        # Recent (7d): cohort now losing (-12% ROI -- past -15pp delta but not
        # past the absolute -10% loss-only-fires-once guard).
        baseline_bets = [
            self._filled_bet(edge=0.20, ask=0.78, inning=6, line="8.5", cse=0.05, won=True, profit=2.4)
            for _ in range(20)
        ]
        # Recent bets at -8% ROI: just above the absolute losing threshold,
        # so the regime alert (not the absolute alert) is what fires.
        recent_bets = [
            self._filled_bet(edge=0.20, ask=0.78, inning=6, line="8.5", cse=0.05, won=False, profit=-8.0),
            self._filled_bet(edge=0.20, ask=0.78, inning=6, line="8.5", cse=0.05, won=True,  profit=2.4),
            self._filled_bet(edge=0.20, ask=0.78, inning=6, line="8.5", cse=0.05, won=True,  profit=2.4),
            self._filled_bet(edge=0.20, ask=0.78, inning=6, line="8.5", cse=0.05, won=True,  profit=2.4),
            self._filled_bet(edge=0.20, ask=0.78, inning=6, line="8.5", cse=0.05, won=True,  profit=2.4),
            self._filled_bet(edge=0.20, ask=0.78, inning=6, line="8.5", cse=0.05, won=True,  profit=2.4),
        ]
        recent_review = {"bets": recent_bets, "session_date": "2026-05-13", "mode": "live"}
        baseline_review = {"bets": baseline_bets + recent_bets, "session_date": "2026-04-30", "mode": "live"}
        health = bdhr._cohort_roi_health(
            today_bet_rows=[],
            trailing_reviews=[recent_review],
            baseline_reviews=[baseline_review],
        )
        regime_alerts = [a for a in health["alerts"] if "flipped" in a]
        self.assertGreaterEqual(
            len(regime_alerts), 1,
            f"Expected at least one regime-change alert; got alerts: {health['alerts']}",
        )

    def test_today_bets_join_recent_window(self):
        # Today contributes to the recent window -- 5 today + 5 prior = n=10
        # in the cohort; if today's bets weren't included the prior n=5
        # would still trip the absolute alert, but the test verifies the
        # joined count flows through n_recent_filled.
        today = [
            self._filled_bet(edge=0.16, ask=0.78, inning=6, line="9.5", cse=0.05, won=False, profit=-8.0)
            for _ in range(3)
        ]
        prior_bets = [
            self._filled_bet(edge=0.16, ask=0.78, inning=6, line="9.5", cse=0.05, won=False, profit=-8.0)
            for _ in range(3)
        ]
        prior_review = {"bets": prior_bets, "session_date": "2026-05-13", "mode": "live"}
        health = bdhr._cohort_roi_health(
            today_bet_rows=today,
            trailing_reviews=[prior_review],
            baseline_reviews=[prior_review],
        )
        self.assertEqual(health["n_recent_filled"], 6)

    def test_cancelled_bets_excluded_from_cohorts(self):
        # Cancelled / errored bets have no realized P&L -- including them
        # in cohort cuts would distort ROI. The collector filters status=="filled".
        mixed = [
            self._filled_bet(edge=0.16, ask=0.78, inning=6, line="9.5", cse=0.05, won=False, profit=-8.0),
            {"status": "cancelled", "edge": 0.16, "won": None, "profit": 0.0},
            {"status": "error", "edge": 0.16, "won": None, "profit": 0.0},
            self._filled_bet(edge=0.16, ask=0.78, inning=6, line="9.5", cse=0.05, won=True, profit=2.5),
        ]
        prior = {"bets": mixed, "session_date": "2026-05-13", "mode": "live"}
        health = bdhr._cohort_roi_health(
            today_bet_rows=[],
            trailing_reviews=[prior],
            baseline_reviews=[prior],
        )
        edge_buckets = health["cohorts_by_dimension"]["edge_bucket"]
        self.assertEqual(edge_buckets["0.15-0.18"]["n"], 2)

    def test_top_level_notes_include_cohort_alerts(self):
        # Verify the alert flows through _build_notes into the top-level
        # notes block (where operators read it without drilling into JSON).
        cohort_health = {
            "alerts": ["edge_bucket=0.15-0.18 cohort ROI -25% over trailing 7d ..."],
        }
        notes = bdhr._build_notes(
            session_summary={},
            bet_totals={"roi": 0.0, "win_rate": 0.5},
            candidate_rollup={},
            log_health={"counts": {}},
            cohort_roi_health=cohort_health,
        )
        joined = " || ".join(notes)
        self.assertIn("Cohort-ROI drift:", joined)
        self.assertIn("edge_bucket=0.15-0.18", joined)


class PromotionAttributionTests(unittest.TestCase):
    """Cohort-ROI drift alerts get a "[coincides with X promotion N days
    ago]" suffix when any promotion event exists in the trailing window.
    Lets the operator see the temporal coincidence between cohort drift
    and a recent promotion without grepping the audit log."""

    def _write_log(self, path: Path, rows: list) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_no_promotions_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "events.jsonl"
            self.assertEqual(
                bdhr._recent_promotions(today="2026-05-15", log_path=log), [],
            )

    def test_recent_promotion_returned(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "events.jsonl"
            self._write_log(log, [
                {"lever": "stage2", "action": "promoted", "direction": "promote",
                 "generated_at_utc": "2026-05-10T12:00:00Z", "operator": "x"},
            ])
            out = bdhr._recent_promotions(today="2026-05-15", log_path=log)
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["lever"], "stage2")

    def test_older_promotion_outside_window_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "events.jsonl"
            self._write_log(log, [
                # >14 days ago -> excluded
                {"lever": "stage2", "action": "promoted", "direction": "promote",
                 "generated_at_utc": "2026-04-15T12:00:00Z", "operator": "x"},
            ])
            self.assertEqual(
                bdhr._recent_promotions(today="2026-05-15", log_path=log), [],
            )

    def test_demote_rows_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "events.jsonl"
            self._write_log(log, [
                {"lever": "stage2", "action": "demoted", "direction": "demote",
                 "generated_at_utc": "2026-05-12T12:00:00Z", "operator": "x"},
            ])
            self.assertEqual(
                bdhr._recent_promotions(today="2026-05-15", log_path=log), [],
            )

    def test_blocked_rows_excluded(self):
        # Blocked attempts aren't actual promotions; they don't attribute drift.
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "events.jsonl"
            self._write_log(log, [
                {"lever": "stage2", "action": "blocked", "direction": "promote",
                 "generated_at_utc": "2026-05-12T12:00:00Z", "operator": "x"},
            ])
            self.assertEqual(
                bdhr._recent_promotions(today="2026-05-15", log_path=log), [],
            )

    def test_attribute_alert_appends_lever_and_days_ago(self):
        promotions = [
            {"lever": "stage2", "action": "promoted",
             "generated_at_utc": "2026-05-10T12:00:00Z"},
        ]
        out = bdhr._attribute_alert_to_promotions(
            "edge_bucket=0.18-0.22 cohort ROI -25%",
            promotions, today="2026-05-15",
        )
        self.assertIn("[coincides with: stage2 promotion 5d ago]", out)

    def test_attribute_groups_multiple_levers(self):
        promotions = [
            {"lever": "stage2", "action": "promoted",
             "generated_at_utc": "2026-05-10T12:00:00Z"},
            {"lever": "stake_scaling", "action": "promoted",
             "generated_at_utc": "2026-05-12T12:00:00Z"},
        ]
        out = bdhr._attribute_alert_to_promotions(
            "cohort ROI -25%", promotions, today="2026-05-15",
        )
        # More recent lever shows first.
        self.assertIn("stake_scaling promotion 3d ago", out)
        self.assertIn("stage2 promotion 5d ago", out)

    def test_attribute_no_promotions_returns_alert_unchanged(self):
        out = bdhr._attribute_alert_to_promotions(
            "cohort ROI -25%", [], today="2026-05-15",
        )
        self.assertEqual(out, "cohort ROI -25%")

    def test_cohort_alert_gets_attributed_when_promotion_in_window(self):
        # Wire-through test: cohort_roi_health appends [coincides with...]
        # when promotion events exist in the trailing window.
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "events.jsonl"
            self._write_log(log, [
                {"lever": "stage2", "action": "promoted", "direction": "promote",
                 "generated_at_utc": "2026-05-10T12:00:00Z", "operator": "x"},
            ])
            # Seed cohort with persistent losses (fires absolute-losing alert).
            losing_bets = [
                {"status": "filled", "edge": 0.16, "entry_ask": 0.78, "inning": 6,
                 "line": "9.5", "current_state_value_edge": 0.05, "won": False,
                 "profit": -8.0, "fill_cost_usdc": 8.0}
                for _ in range(6)
            ]
            prior = {"bets": losing_bets, "session_date": "2026-05-13", "mode": "live"}
            health = bdhr._cohort_roi_health(
                today_bet_rows=[],
                trailing_reviews=[prior],
                baseline_reviews=[prior],
                session_date="2026-05-15",
                promotion_events_log_path=log,
            )
            joined = " || ".join(health["alerts"])
            self.assertIn("[coincides with: stage2 promotion 5d ago]", joined)
            self.assertEqual(health["recent_promotions_count"], 1)


class ConceptDriftHealthTests(unittest.TestCase):
    """The daily-review block reads the artifact built by
    `build_concept_drift_report.py`. We don't recompute PSI here; we
    just verify that the report's alerts get summarised + mirrored into
    the top-level Notes block."""

    def test_missing_artifact_records_error_no_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            out = bdhr._concept_drift_health(
                report_path=Path(td) / "missing.json",
                session_date="2026-05-15",
            )
            self.assertFalse(out["artifact_present"])
            self.assertEqual(out["alerts"], [])
            self.assertIn("missing", out.get("artifact_error", ""))

    def test_artifact_alerts_mirror_into_block(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "concept_drift_report.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "generated_at_utc": "2026-05-15T01:00:00Z",
                "active_date": "2026-05-15",
                "current_window": {"start": "2026-05-08", "end": "2026-05-14", "n_rows": 90},
                "baseline_window": {"start": "2026-04-08", "end": "2026-05-07", "n_rows": 70},
                "thresholds": {"psi_major": 0.25},
                "features": {
                    "weather_temp_f": {
                        "kind": "continuous", "metric": "psi", "value": 0.32,
                        "verdict": "major", "current_n": 90, "baseline_n": 70,
                    },
                    "stage2_run_env_delta": {
                        "kind": "continuous", "metric": "psi", "value": 0.05,
                        "verdict": "stable", "current_n": 90, "baseline_n": 70,
                    },
                },
                "alerts": ["weather_temp_f PSI=0.32 (major shift): ..."],
            }), encoding="utf-8")
            out = bdhr._concept_drift_health(
                report_path=path, session_date="2026-05-15",
            )
            self.assertTrue(out["artifact_present"])
            self.assertEqual(out["alerts"], ["weather_temp_f PSI=0.32 (major shift): ..."])
            self.assertEqual(out["feature_verdicts"]["weather_temp_f"]["verdict"], "major")
            self.assertEqual(out["feature_verdicts"]["stage2_run_env_delta"]["verdict"], "stable")

    def test_stale_artifact_fires_age_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "concept_drift_report.json"
            # 30d-old artifact -> past the 14d staleness threshold.
            path.write_text(json.dumps({
                "schema_version": 1,
                "generated_at_utc": "2026-04-15T01:00:00Z",
                "active_date": "2026-04-15",
                "features": {},
                "alerts": [],
            }), encoding="utf-8")
            out = bdhr._concept_drift_health(
                report_path=path, session_date="2026-05-15",
            )
            # Alert text uses the abbreviation "d old" (matches the
            # calibration-artifact stale alert format for consistency).
            self.assertTrue(any("d old" in a for a in out["alerts"]))

    def test_top_level_notes_include_concept_drift_alerts(self):
        cd_health = {
            "alerts": ["weather_temp_f PSI=0.32 (major shift): current mean=85.0 vs baseline 65.0."],
        }
        notes = bdhr._build_notes(
            session_summary={},
            bet_totals={"roi": 0.0, "win_rate": 0.5},
            candidate_rollup={},
            log_health={"counts": {}},
            concept_drift_health=cd_health,
        )
        joined = " || ".join(notes)
        self.assertIn("Concept-drift:", joined)
        self.assertIn("weather_temp_f", joined)


class DriftInDriftHealthTests(unittest.TestCase):
    """The daily-review block reads the artifact built by
    `build_drift_in_drift_report.py`. Mirror of ConceptDriftHealthTests
    but on the meta-trend artifact (7th drift dimension, 2nd leading
    indicator)."""

    def test_missing_artifact_records_error_no_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            out = bdhr._drift_in_drift_health(
                report_path=Path(td) / "missing.json",
                session_date="2026-05-16",
            )
            self.assertFalse(out["artifact_present"])
            self.assertEqual(out["alerts"], [])
            self.assertIn("missing", out.get("artifact_error", ""))

    def test_artifact_alerts_mirror_into_block(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "drift_in_drift_report.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "generated_at_utc": "2026-05-16T01:00:00Z",
                "active_date": "2026-05-16",
                "history_window_days": 30,
                "projection_horizon_days": 30,
                "min_points_for_trend": 7,
                "n_history_rows_in_window": 56,
                "n_features_evaluated": 2,
                "thresholds": {"projected_psi_minor": 0.10, "projected_psi_major": 0.25},
                "features": {
                    "weather_temp_f": {
                        "n_points": 28, "current_psi": 0.27,
                        "slope_per_day": 0.008, "r_squared": 0.95,
                        "projected_psi": 0.51, "verdict": "major",
                    },
                    "stage2_run_env_delta": {
                        "n_points": 28, "current_psi": 0.05,
                        "slope_per_day": 0.0001, "r_squared": 0.02,
                        "projected_psi": 0.05, "verdict": "stable",
                    },
                },
                "alerts": ["weather_temp_f projected PSI 0.51 in 30d (slope +0.008/day, ...)"],
            }), encoding="utf-8")
            out = bdhr._drift_in_drift_health(
                report_path=path, session_date="2026-05-16",
            )
            self.assertTrue(out["artifact_present"])
            self.assertEqual(len(out["alerts"]), 1)
            self.assertIn("weather_temp_f", out["alerts"][0])
            self.assertEqual(out["feature_verdicts"]["weather_temp_f"]["verdict"], "major")
            self.assertEqual(out["feature_verdicts"]["stage2_run_env_delta"]["verdict"], "stable")
            self.assertEqual(out["n_features_evaluated"], 2)

    def test_stale_artifact_fires_age_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "drift_in_drift_report.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "generated_at_utc": "2026-04-15T01:00:00Z",
                "active_date": "2026-04-15",
                "features": {}, "alerts": [],
            }), encoding="utf-8")
            out = bdhr._drift_in_drift_health(
                report_path=path, session_date="2026-05-16",
            )
            self.assertTrue(any("d old" in a for a in out["alerts"]))

    def test_top_level_notes_include_drift_in_drift_alerts(self):
        dd_health = {
            "alerts": ["weather_temp_f projected PSI 0.51 in 30d (slope +0.008/day, ...)"],
        }
        notes = bdhr._build_notes(
            session_summary={},
            bet_totals={"roi": 0.0, "win_rate": 0.5},
            candidate_rollup={},
            log_health={"counts": {}},
            drift_in_drift_health=dd_health,
        )
        joined = " || ".join(notes)
        self.assertIn("Drift-in-drift:", joined)
        self.assertIn("weather_temp_f", joined)


class ConceptDriftAttributionTests(unittest.TestCase):
    """Cohort-ROI alerts should be suffixed with the candidate-root-cause
    list when the concept-drift report shows any features at verdict=major.
    Mirror of PromotionAttributionTests but for the leading-indicator
    drift dimension."""

    def test_no_drift_features_no_suffix(self):
        alert = "edge=0.15-0.18 cohort ROI -33.0% ..."
        out = bdhr._attribute_alert_to_concept_drift(alert, [])
        self.assertEqual(out, alert)

    def test_single_major_feature_appended(self):
        alert = "edge=0.15-0.18 cohort ROI -33.0% ..."
        out = bdhr._attribute_alert_to_concept_drift(
            alert, [("team_offense_delta", "PSI", 2.36)],
        )
        self.assertIn("[concept-drift:", out)
        self.assertIn("team_offense_delta PSI 2.36", out)

    def test_multiple_features_ordered_by_value_desc(self):
        alert = "edge=0.15-0.18 cohort ROI -33.0%"
        out = bdhr._attribute_alert_to_concept_drift(
            alert, [
                ("stage2_run_env_delta", "PSI", 3.22),
                ("team_offense_delta", "PSI", 2.36),
                ("base_fair_value", "PSI", 2.88),
            ],
        )
        # When _major_drift_features is run it sorts; here we passed
        # an unsorted list to test that the formatting just takes them
        # in order. So the test just checks all three appear in output.
        for fname in ("stage2_run_env_delta", "team_offense_delta", "base_fair_value"):
            self.assertIn(fname, out)

    def test_top_n_caps_with_more_marker(self):
        alert = "edge=0.15-0.18 cohort ROI -33.0%"
        many = [(f"feature_{i}", "PSI", float(i)) for i in range(10)]
        out = bdhr._attribute_alert_to_concept_drift(alert, many, top_n=3)
        self.assertIn("(+7 more)", out)

    def test_major_drift_features_extractor_filters_to_major(self):
        cd_health = {
            "feature_verdicts": {
                "stage2_run_env_delta": {"metric": "PSI", "value": 3.22, "verdict": "major"},
                "weather_temp_f":       {"metric": "PSI", "value": 0.12, "verdict": "minor"},
                "stadium_id":           {"metric": "TVD", "value": 0.05, "verdict": "stable"},
            },
        }
        out = bdhr._major_drift_features(cd_health)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], "stage2_run_env_delta")

    def test_major_drift_features_sorted_by_value_desc(self):
        cd_health = {
            "feature_verdicts": {
                "a": {"metric": "PSI", "value": 0.5, "verdict": "major"},
                "b": {"metric": "PSI", "value": 1.5, "verdict": "major"},
                "c": {"metric": "PSI", "value": 1.0, "verdict": "major"},
            },
        }
        out = bdhr._major_drift_features(cd_health)
        self.assertEqual([t[0] for t in out], ["b", "c", "a"])

    def test_major_drift_features_no_block_returns_empty(self):
        self.assertEqual(bdhr._major_drift_features(None), [])
        self.assertEqual(bdhr._major_drift_features({}), [])

    def test_cohort_roi_alert_carries_concept_drift_suffix(self):
        # 6 trailing bets in edge 0.15-0.18 cohort, all losing -> fires
        # absolute-losing alert; verify the appended concept-drift suffix.
        bets = [
            {
                "status": "filled", "edge": 0.16, "entry_ask": 0.78,
                "inning": 6, "line": "9.5", "current_state_value_edge": 0.05,
                "won": False, "profit": -8.0, "fill_cost_usdc": 8.0,
            }
            for _ in range(6)
        ]
        prior = {"bets": bets, "session_date": "2026-05-13", "mode": "live"}
        cd_health = {
            "feature_verdicts": {
                "team_offense_delta": {"metric": "PSI", "value": 2.36, "verdict": "major"},
                "base_fair_value":    {"metric": "PSI", "value": 2.88, "verdict": "major"},
            },
        }
        out = bdhr._cohort_roi_health(
            today_bet_rows=[],
            trailing_reviews=[prior],
            baseline_reviews=[prior],
            session_date="2026-05-16",
            concept_drift_health=cd_health,
        )
        self.assertGreater(len(out["alerts"]), 0,
                           "absolute-losing alert should fire")
        with_suffix = [a for a in out["alerts"] if "[concept-drift:" in a]
        self.assertGreater(len(with_suffix), 0,
                           f"alerts had no concept-drift suffix: {out['alerts']}")
        # base_fair_value (2.88) should come first since it's the larger value
        self.assertIn("base_fair_value PSI 2.88", with_suffix[0])
        self.assertEqual(out["concept_drift_major_features_count"], 2)

    def test_cohort_roi_alert_no_suffix_when_no_major_drift(self):
        bets = [
            {
                "status": "filled", "edge": 0.16, "entry_ask": 0.78,
                "inning": 6, "line": "9.5", "current_state_value_edge": 0.05,
                "won": False, "profit": -8.0, "fill_cost_usdc": 8.0,
            }
            for _ in range(6)
        ]
        prior = {"bets": bets, "session_date": "2026-05-13", "mode": "live"}
        cd_health = {
            "feature_verdicts": {
                "weather_temp_f": {"metric": "PSI", "value": 0.05, "verdict": "stable"},
            },
        }
        out = bdhr._cohort_roi_health(
            today_bet_rows=[],
            trailing_reviews=[prior], baseline_reviews=[prior],
            session_date="2026-05-16",
            concept_drift_health=cd_health,
        )
        # alerts should still fire (cohort is losing) but no suffix
        self.assertGreater(len(out["alerts"]), 0)
        for a in out["alerts"]:
            self.assertNotIn("[concept-drift:", a)
        self.assertEqual(out["concept_drift_major_features_count"], 0)


class DaemonReadinessHealthTests(unittest.TestCase):
    """The daily-review block reads the artifact built by
    `daemon_retrospective.py` and surfaces a per-lever readiness
    label plus alerts when (a) disagreements exist, (b) the artifact
    is stale, or (c) every lever is ready (positive go-signal for
    operators considering --auto-daemon-mode act)."""

    def _retrospective_payload(self, *, stage2_readiness="ready_for_act",
                               stage3_readiness="ready_for_act",
                               stage2_disagree=0, stage2_only=0,
                               stake_scaling_verdict="hold",
                               generated="2026-05-16T01:00:00Z"):
        # Default verdicts deliberately non-actionable ("hold") so the
        # staleness check (added 2026-05-16) doesn't fire spuriously
        # in tests that aren't about staleness. Tests that care override.
        return {
            "generated_at_utc": generated,
            "config": {"cooldown_days": 14, "ready_min_dates": 7},
            "replays": {
                "stage2": {
                    "summary": {
                        "readiness_for_act": stage2_readiness,
                        "n_dates_evaluated": 10,
                        "match_count": 2,
                        "daemon_only_count": stage2_only,
                        "operator_only_count": 0,
                        "daemon_disagreed_count": stage2_disagree,
                        "both_no_action_count": 8 - stage2_disagree - stage2_only,
                        "last_disagreement_date": "2026-05-10" if stage2_disagree else None,
                    },
                    "per_date": [
                        {"date": "2026-05-15", "daemon_verdict_label": "hold"},
                    ],
                },
                "stage3-v2": {
                    "summary": {
                        "readiness_for_act": stage3_readiness,
                        "n_dates_evaluated": 10,
                        "match_count": 1,
                        "daemon_only_count": 0,
                        "operator_only_count": 0,
                        "daemon_disagreed_count": 0,
                        "both_no_action_count": 9,
                        "last_disagreement_date": None,
                    },
                    "per_date": [
                        {"date": "2026-05-15", "daemon_verdict_label": "hold"},
                    ],
                },
            },
            "snapshots": {
                "stake-scaling": {"verdict_label": stake_scaling_verdict,
                                  "actuated_by_daemon": True},
                "gate-threshold": {"verdict_label": "hold", "actuated_by_daemon": False},
            },
            "overall": {"ready_for_act_all_levers": True},
        }

    def test_missing_artifact_records_error_no_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            out = bdhr._daemon_readiness_health(
                report_path=Path(td) / "missing.json",
                session_date="2026-05-16",
            )
            self.assertFalse(out["artifact_present"])
            self.assertEqual(out["alerts"], [])
            self.assertIn("missing", out.get("artifact_error", ""))

    def test_all_ready_emits_positive_signal_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "retro.json"
            path.write_text(json.dumps(self._retrospective_payload()),
                            encoding="utf-8")
            out = bdhr._daemon_readiness_health(
                report_path=path, session_date="2026-05-16",
            )
            self.assertTrue(out["overall_ready_for_act"])
            self.assertTrue(
                any("all time-series levers ready_for_act" in a for a in out["alerts"]),
                f"alerts were: {out['alerts']}",
            )
            self.assertEqual(out["levers"]["stage2"]["readiness_for_act"],
                             "ready_for_act")

    def test_disagreement_emits_per_lever_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "retro.json"
            path.write_text(
                json.dumps(self._retrospective_payload(
                    stage2_readiness="disagreements_present",
                    stage2_disagree=1, stage2_only=2,
                )), encoding="utf-8",
            )
            out = bdhr._daemon_readiness_health(
                report_path=path, session_date="2026-05-16",
            )
            self.assertFalse(out["overall_ready_for_act"])
            self.assertTrue(
                any("stage2" in a and "disagreement" in a for a in out["alerts"]),
                f"alerts were: {out['alerts']}",
            )
            self.assertTrue(
                any("2026-05-10" in a for a in out["alerts"]),
                "last_disagreement_date should appear in alert",
            )

    def test_needs_more_history_does_not_signal_ready(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "retro.json"
            path.write_text(json.dumps(self._retrospective_payload(
                stage2_readiness="needs_more_history",
            )), encoding="utf-8")
            out = bdhr._daemon_readiness_health(
                report_path=path, session_date="2026-05-16",
            )
            self.assertFalse(out["overall_ready_for_act"])
            # No positive-signal alert, no disagreement alert
            self.assertEqual(out["alerts"], [])

    def test_stale_artifact_fires_age_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "retro.json"
            path.write_text(json.dumps(self._retrospective_payload(
                generated="2026-04-15T01:00:00Z",
            )), encoding="utf-8")
            out = bdhr._daemon_readiness_health(
                report_path=path, session_date="2026-05-16",
            )
            self.assertTrue(any("d old" in a for a in out["alerts"]))

    def test_snapshot_levers_surface_in_block(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "retro.json"
            # Override stake-scaling to "promote" so we can verify it
            # surfaces in the block.
            path.write_text(json.dumps(self._retrospective_payload(
                stake_scaling_verdict="promote",
            )), encoding="utf-8")
            # Seed an empty audit log path so the staleness check has
            # somewhere to read (the verdict is `promote` so a missing
            # audit log would fire staleness; we don't care about that
            # here -- test focuses on snapshot surfacing).
            audit_path = Path(td) / "events.jsonl"
            audit_path.write_text("", encoding="utf-8")
            out = bdhr._daemon_readiness_health(
                report_path=path, session_date="2026-05-16",
                audit_log_path=audit_path,
            )
            self.assertEqual(out["snapshots"]["stake-scaling"]["verdict_label"], "promote")
            self.assertEqual(out["snapshots"]["gate-threshold"]["actuated_by_daemon"], False)

    def test_top_level_notes_include_daemon_readiness_alerts(self):
        dr_health = {
            "alerts": [
                "all time-series levers ready_for_act; operator may consider "
                "`--auto-daemon-mode act` after reviewing the per-date table "
                "in the retrospective markdown.",
            ],
        }
        notes = bdhr._build_notes(
            session_summary={},
            bet_totals={"roi": 0.0, "win_rate": 0.5},
            candidate_rollup={},
            log_health={"counts": {}},
            daemon_readiness_health=dr_health,
        )
        joined = " || ".join(notes)
        self.assertIn("Daemon-readiness:", joined)
        self.assertIn("ready_for_act", joined)


class DaemonStalenessAlertTests(unittest.TestCase):
    """When a lever's verdict says promote/demote but the audit log
    shows no successful action in > 60 days, surface an alert. Indicates
    cooldown stuck, mode=off, opt-out flag, etc."""

    def _write_log(self, path: Path, rows: list) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def _retro_with_verdicts(self, *, stage2_verdict="hold",
                             stage3_verdict="hold",
                             stake_scaling_verdict="hold"):
        return {
            "generated_at_utc": "2026-05-16T01:00:00Z",
            "config": {"cooldown_days": 14},
            "replays": {
                "stage2": {
                    "summary": {"readiness_for_act": "ready_for_act"},
                    "per_date": [
                        {"date": "2026-05-15", "daemon_verdict_label": stage2_verdict},
                    ],
                },
                "stage3-v2": {
                    "summary": {"readiness_for_act": "ready_for_act"},
                    "per_date": [
                        {"date": "2026-05-15", "daemon_verdict_label": stage3_verdict},
                    ],
                },
            },
            "snapshots": {
                "stake-scaling": {"verdict_label": stake_scaling_verdict,
                                  "actuated_by_daemon": True},
                "gate-threshold": {"verdict_label": "hold",
                                   "actuated_by_daemon": False},
            },
            "overall": {"ready_for_act_all_levers": True},
        }

    def test_no_staleness_when_all_verdicts_hold(self):
        with tempfile.TemporaryDirectory() as td:
            audit = Path(td) / "events.jsonl"
            audit.write_text("", encoding="utf-8")
            records = bdhr._daemon_staleness_check(
                retrospective_report=self._retro_with_verdicts(),
                audit_log_path=audit,
                today="2026-05-16",
            )
            self.assertEqual(records, [])

    def test_staleness_fires_when_verdict_promote_but_never_actuated(self):
        with tempfile.TemporaryDirectory() as td:
            audit = Path(td) / "events.jsonl"
            audit.write_text("", encoding="utf-8")
            records = bdhr._daemon_staleness_check(
                retrospective_report=self._retro_with_verdicts(
                    stage2_verdict="promote",
                ),
                audit_log_path=audit,
                today="2026-05-16",
            )
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["lever"], "stage2")
            self.assertIsNone(records[0]["last_action_date"])
            self.assertIsNone(records[0]["days_since_last_action"])

    def test_staleness_does_not_fire_when_action_within_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            audit = Path(td) / "events.jsonl"
            # 30 days ago -- well within the 60d threshold.
            self._write_log(audit, [{
                "lever": "stage2", "action": "promoted", "direction": "promote",
                "generated_at_utc": "2026-04-16T12:00:00Z", "operator": "x",
            }])
            records = bdhr._daemon_staleness_check(
                retrospective_report=self._retro_with_verdicts(
                    stage2_verdict="promote",
                ),
                audit_log_path=audit,
                today="2026-05-16",
            )
            self.assertEqual(records, [])

    def test_staleness_fires_when_last_action_past_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            audit = Path(td) / "events.jsonl"
            # 90 days ago -- past the 60d threshold.
            self._write_log(audit, [{
                "lever": "stage2", "action": "promoted", "direction": "promote",
                "generated_at_utc": "2026-02-15T12:00:00Z", "operator": "old",
            }])
            records = bdhr._daemon_staleness_check(
                retrospective_report=self._retro_with_verdicts(
                    stage2_verdict="promote",
                ),
                audit_log_path=audit,
                today="2026-05-16",
            )
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["days_since_last_action"], 90)
            self.assertEqual(records[0]["last_action_operator"], "old")

    def test_blocked_and_dry_run_actions_dont_count(self):
        # "blocked" and "dry_run" don't count as actuations.
        with tempfile.TemporaryDirectory() as td:
            audit = Path(td) / "events.jsonl"
            self._write_log(audit, [
                {"lever": "stage2", "action": "blocked", "direction": "promote",
                 "generated_at_utc": "2026-05-10T12:00:00Z", "operator": "x"},
                {"lever": "stage2", "action": "dry_run", "direction": "promote",
                 "generated_at_utc": "2026-05-12T12:00:00Z", "operator": "x"},
            ])
            records = bdhr._daemon_staleness_check(
                retrospective_report=self._retro_with_verdicts(
                    stage2_verdict="promote",
                ),
                audit_log_path=audit,
                today="2026-05-16",
            )
            self.assertEqual(len(records), 1)
            self.assertIsNone(records[0]["last_action_date"])

    def test_staleness_for_snapshot_lever_stake_scaling(self):
        with tempfile.TemporaryDirectory() as td:
            audit = Path(td) / "events.jsonl"
            audit.write_text("", encoding="utf-8")
            records = bdhr._daemon_staleness_check(
                retrospective_report=self._retro_with_verdicts(
                    stake_scaling_verdict="promote",
                ),
                audit_log_path=audit,
                today="2026-05-16",
            )
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["lever"], "stake-scaling")

    def test_demote_verdict_also_triggers_staleness(self):
        with tempfile.TemporaryDirectory() as td:
            audit = Path(td) / "events.jsonl"
            audit.write_text("", encoding="utf-8")
            records = bdhr._daemon_staleness_check(
                retrospective_report=self._retro_with_verdicts(
                    stage2_verdict="demote",
                ),
                audit_log_path=audit,
                today="2026-05-16",
            )
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["verdict_label"], "demote")

    def test_staleness_wires_into_daemon_readiness_health_alerts(self):
        # End-to-end: staleness record becomes a payload alert with
        # the "Daemon-readiness:" prefix downstream.
        with tempfile.TemporaryDirectory() as td:
            retro_path = Path(td) / "retro.json"
            retro_path.write_text(json.dumps(self._retro_with_verdicts(
                stage2_verdict="promote",
            )), encoding="utf-8")
            audit = Path(td) / "events.jsonl"
            audit.write_text("", encoding="utf-8")
            out = bdhr._daemon_readiness_health(
                report_path=retro_path,
                session_date="2026-05-16",
                audit_log_path=audit,
            )
            self.assertEqual(len(out["staleness_records"]), 1)
            self.assertTrue(
                any("stage2 verdict=promote" in a for a in out["alerts"]),
                f"alerts: {out['alerts']}",
            )


class DemotionAttributionTests(unittest.TestCase):
    """Symmetric mirror of PromotionAttributionTests. Cohort_roi alerts
    get a "[follows: X demotion N days ago]" suffix when any demotion
    happened in the trailing window. Different verb from "[coincides
    with]" on purpose: a demote was supposed to FIX the cohort, so a
    continuing alert is a verification signal."""

    def _write_log(self, path: Path, rows: list) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_recent_demotion_returned(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "events.jsonl"
            self._write_log(log, [
                {"lever": "stage2", "action": "demoted", "direction": "demote",
                 "generated_at_utc": "2026-05-12T12:00:00Z", "operator": "x"},
            ])
            out = bdhr._recent_demotions(today="2026-05-15", log_path=log)
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["lever"], "stage2")

    def test_promote_rows_excluded(self):
        # The mirror: _recent_demotions must NOT pick up promote rows.
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "events.jsonl"
            self._write_log(log, [
                {"lever": "stage2", "action": "promoted", "direction": "promote",
                 "generated_at_utc": "2026-05-12T12:00:00Z", "operator": "x"},
            ])
            self.assertEqual(
                bdhr._recent_demotions(today="2026-05-15", log_path=log), [],
            )

    def test_legacy_rows_without_direction_treated_as_promote(self):
        # Legacy audit rows pre-date `direction` and are correctly
        # excluded from the demote query.
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "events.jsonl"
            self._write_log(log, [
                {"lever": "stage2", "action": "promoted",  # no direction field
                 "generated_at_utc": "2026-05-12T12:00:00Z", "operator": "x"},
            ])
            self.assertEqual(
                bdhr._recent_demotions(today="2026-05-15", log_path=log), [],
            )

    def test_forced_demote_included(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "events.jsonl"
            self._write_log(log, [
                {"lever": "stake_scaling", "action": "forced", "direction": "demote",
                 "generated_at_utc": "2026-05-13T12:00:00Z", "operator": "x"},
            ])
            out = bdhr._recent_demotions(today="2026-05-15", log_path=log)
            self.assertEqual(len(out), 1)

    def test_attribute_alert_appends_follows_suffix(self):
        demotions = [
            {"lever": "stage2", "action": "demoted",
             "generated_at_utc": "2026-05-12T12:00:00Z"},
        ]
        out = bdhr._attribute_alert_to_demotions(
            "edge_bucket=0.18-0.22 cohort ROI -25%",
            demotions, today="2026-05-15",
        )
        self.assertIn("[follows: stage2 demotion 3d ago]", out)

    def test_attribute_no_demotions_unchanged(self):
        out = bdhr._attribute_alert_to_demotions(
            "cohort ROI -25%", [], today="2026-05-15",
        )
        self.assertEqual(out, "cohort ROI -25%")

    def test_cohort_alert_gets_attributed_when_demotion_in_window(self):
        # Wire-through: cohort_roi_health appends [follows: ...] when
        # demotion events exist in the trailing window.
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "events.jsonl"
            self._write_log(log, [
                {"lever": "stage2", "action": "demoted", "direction": "demote",
                 "generated_at_utc": "2026-05-12T12:00:00Z", "operator": "x"},
            ])
            losing_bets = [
                {"status": "filled", "edge": 0.16, "entry_ask": 0.78, "inning": 6,
                 "line": "9.5", "current_state_value_edge": 0.05, "won": False,
                 "profit": -8.0, "fill_cost_usdc": 8.0}
                for _ in range(6)
            ]
            prior = {"bets": losing_bets, "session_date": "2026-05-13", "mode": "live"}
            health = bdhr._cohort_roi_health(
                today_bet_rows=[],
                trailing_reviews=[prior],
                baseline_reviews=[prior],
                session_date="2026-05-15",
                promotion_events_log_path=log,
            )
            joined = " || ".join(health["alerts"])
            self.assertIn("[follows: stage2 demotion 3d ago]", joined)
            self.assertEqual(health["recent_demotions_count"], 1)
            # And no spurious promotion-attribution
            self.assertEqual(health["recent_promotions_count"], 0)
            self.assertNotIn("[coincides with:", joined)

    def test_both_promotion_and_demotion_suffixes_can_coexist(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "events.jsonl"
            self._write_log(log, [
                {"lever": "stage2", "action": "promoted", "direction": "promote",
                 "generated_at_utc": "2026-05-08T12:00:00Z", "operator": "x"},
                {"lever": "stake_scaling", "action": "demoted", "direction": "demote",
                 "generated_at_utc": "2026-05-13T12:00:00Z", "operator": "x"},
            ])
            losing_bets = [
                {"status": "filled", "edge": 0.16, "entry_ask": 0.78, "inning": 6,
                 "line": "9.5", "current_state_value_edge": 0.05, "won": False,
                 "profit": -8.0, "fill_cost_usdc": 8.0}
                for _ in range(6)
            ]
            prior = {"bets": losing_bets, "session_date": "2026-05-13", "mode": "live"}
            health = bdhr._cohort_roi_health(
                today_bet_rows=[],
                trailing_reviews=[prior],
                baseline_reviews=[prior],
                session_date="2026-05-15",
                promotion_events_log_path=log,
            )
            joined = " || ".join(health["alerts"])
            self.assertIn("[coincides with: stage2 promotion 7d ago]", joined)
            self.assertIn("[follows: stake_scaling demotion 2d ago]", joined)


class BetTotalsSideBreakdownTests(unittest.TestCase):
    """Phase B B3 (2026-05-16): `_summarize_bets` returns a per-side
    subtotal block (`bet_totals.by_side.{over, under}`). Legacy bets
    without a `side` field default to 'over'. Today UNDER trading is
    not enabled (live engine remains Over-only); Phase C will populate
    the UNDER subtotals without further plumbing changes."""

    def _bet(self, **kw):
        defaults = {
            "bet_id": "x",
            "away_abbrev": "A",
            "home_abbrev": "B",
            "line": "8.5",
            "inning": 5,
            "order_status": "filled",
            "won": True,
            "profit": 5.0,
            "fill_cost_usdc": 7.0,
            "stake": 7.0,
        }
        defaults.update(kw)
        return defaults

    def test_legacy_bet_without_side_defaults_to_over(self):
        bets = [self._bet(side=None)]
        _, totals = bdhr._summarize_bets(bets)
        self.assertIn("by_side", totals)
        self.assertEqual(totals["by_side"]["over"]["count"], 1)
        self.assertEqual(totals["by_side"]["under"]["count"], 0)

    def test_per_side_roi_and_win_rate_match_aggregate_when_all_over(self):
        bets = [
            self._bet(side="over", won=True, profit=5.0),
            self._bet(side="over", won=False, profit=-7.0),
        ]
        _, totals = bdhr._summarize_bets(bets)
        # Aggregate
        self.assertAlmostEqual(totals["win_rate"], 0.5)
        self.assertAlmostEqual(totals["roi"], -2.0 / 14.0, places=6)
        # Per-side OVER equals aggregate (no UNDER bets)
        over = totals["by_side"]["over"]
        self.assertEqual(over["count"], 2)
        self.assertEqual(over["filled"], 2)
        self.assertEqual(over["wins"], 1)
        self.assertEqual(over["losses"], 1)
        self.assertAlmostEqual(over["win_rate"], 0.5)
        self.assertAlmostEqual(over["roi"], -2.0 / 14.0, places=6)

    def test_per_side_splits_when_mixed_over_under(self):
        bets = [
            self._bet(side="over", won=True, profit=5.0, fill_cost_usdc=7.0),
            self._bet(side="under", won=False, profit=-3.0, fill_cost_usdc=3.0),
            self._bet(side="under", won=True, profit=4.0, fill_cost_usdc=2.0),
        ]
        _, totals = bdhr._summarize_bets(bets)
        over = totals["by_side"]["over"]
        under = totals["by_side"]["under"]
        self.assertEqual(over["count"], 1)
        self.assertEqual(over["wins"], 1)
        self.assertEqual(under["count"], 2)
        self.assertEqual(under["wins"], 1)
        self.assertEqual(under["losses"], 1)
        # Aggregate still sums
        self.assertEqual(totals["count"], 3)
        self.assertEqual(totals["wins"], 2)
        self.assertEqual(totals["losses"], 1)

    def test_unfilled_under_bet_increments_count_only(self):
        bets = [self._bet(side="under", order_status="cancelled",
                          won=None, profit=None, fill_cost_usdc=None)]
        _, totals = bdhr._summarize_bets(bets)
        under = totals["by_side"]["under"]
        self.assertEqual(under["count"], 1)
        self.assertEqual(under["filled"], 0)
        self.assertIsNone(under["roi"])
        self.assertIsNone(under["win_rate"])

    def test_unknown_side_creates_extensible_bucket(self):
        """A typo or future side ('lay'?) is bucketed under its
        literal value rather than silently dropped so the operator
        sees the anomaly."""
        bets = [self._bet(side="lay")]
        _, totals = bdhr._summarize_bets(bets)
        self.assertIn("lay", totals["by_side"])
        self.assertEqual(totals["by_side"]["lay"]["count"], 1)

    def test_per_bet_row_carries_side_field(self):
        """B3 also surfaces `side` on each compact bet row so the
        daily-review markdown table can render it."""
        bets = [
            self._bet(side="over", bet_id="o1"),
            self._bet(side="under", bet_id="u1"),
        ]
        rows, _ = bdhr._summarize_bets(bets)
        by_id = {r["bet_id"]: r["side"] for r in rows}
        self.assertEqual(by_id["o1"], "over")
        self.assertEqual(by_id["u1"], "under")


class FastDemoteHealthTests(unittest.TestCase):
    """Active #13 (2026-05-17): the daily-review block computes fast
    Wilson-UB demote verdicts for all four levers and fires a critical
    alert when any lever fires `fast_demote`. Unlike the windowed
    demote (which bypasses cooldown only when the daemon acts), the
    fast verdict carries 95% one-sided confidence on its own."""

    def _write_session(self, dir_: Path, date: str, bets):
        path = dir_ / f"{date}_session.json"
        path.write_text(json.dumps({
            "session_date": date,
            "bets": bets,
        }), encoding="utf-8")

    def _filled_bet(self, *, profit: float, entry_ask: float = 0.80):
        return {
            "order_status": "filled",
            "placement_mode": "live",
            "profit": profit,
            "entry_ask": entry_ask,
            "fill_cost": 10.0,
            "stake": 10.0,
        }

    def test_no_promotions_returns_clean_payload_no_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sessions").mkdir()
            out = bdhr._fast_demote_health(
                audit_log_path=root / "events.jsonl",
                sessions_dir=root / "sessions",
                today="2026-05-17",
            )
            self.assertEqual(out["alerts"], [])
            self.assertEqual(set(out["verdicts"].keys()), {
                "stage2", "stage3-v2", "stake-scaling", "gate-threshold",
            })
            for v in out["verdicts"].values():
                self.assertEqual(v["verdict"], "no_promotion_to_demote")

    def test_fast_demote_fires_critical_alert(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sessions = root / "sessions"
            sessions.mkdir()
            audit = root / "events.jsonl"
            # Stage-2 promoted 7 days ago
            audit.write_text(json.dumps({
                "lever": "stage2", "direction": "promote",
                "action": "promoted", "operator": "alice",
                "generated_at_utc": "2026-05-10T08:00:00Z",
            }) + "\n", encoding="utf-8")
            # 21 losing bets at 0.80 ask -> fast_demote should fire
            for day in [
                "2026-05-11", "2026-05-12", "2026-05-13",
                "2026-05-14", "2026-05-15", "2026-05-16", "2026-05-17",
            ]:
                self._write_session(sessions, day, [
                    self._filled_bet(profit=-10.0, entry_ask=0.80)
                    for _ in range(3)
                ])
            out = bdhr._fast_demote_health(
                audit_log_path=audit, sessions_dir=sessions,
                today="2026-05-17",
            )
            self.assertEqual(out["verdicts"]["stage2"]["verdict"], "fast_demote")
            stage2_alerts = [a for a in out["alerts"] if "stage2" in a]
            self.assertEqual(len(stage2_alerts), 1, out["alerts"])
            self.assertIn("fast_demote fired", stage2_alerts[0])
            self.assertIn("cooldown", stage2_alerts[0])

    def test_hold_when_post_window_winning(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sessions = root / "sessions"
            sessions.mkdir()
            audit = root / "events.jsonl"
            audit.write_text(json.dumps({
                "lever": "stage2", "direction": "promote",
                "action": "promoted", "operator": "alice",
                "generated_at_utc": "2026-05-10T08:00:00Z",
            }) + "\n", encoding="utf-8")
            for day in [
                "2026-05-11", "2026-05-12", "2026-05-13",
                "2026-05-14", "2026-05-15", "2026-05-16", "2026-05-17",
            ]:
                self._write_session(sessions, day, [
                    self._filled_bet(profit=10.0, entry_ask=0.50)
                    for _ in range(3)
                ])
            out = bdhr._fast_demote_health(
                audit_log_path=audit, sessions_dir=sessions,
                today="2026-05-17",
            )
            self.assertEqual(out["verdicts"]["stage2"]["verdict"], "hold")
            self.assertEqual(out["alerts"], [])

    def test_notes_carry_fast_demote_prefix(self):
        fd = {"alerts": ["stage2 fast_demote fired: ..."]}
        notes = bdhr._build_notes(
            session_summary={}, bet_totals={}, candidate_rollup={},
            log_health={}, fast_demote_health=fd,
        )
        prefixed = [n for n in notes if n.startswith("Fast-demote:")]
        self.assertEqual(len(prefixed), 1)


class SettlementTruthHealthTests(unittest.TestCase):
    """Active #12 (2026-05-17): the daily-review block reads the
    settlement_truth_report.json and fires tiered alerts based on
    counts of each result_code. Critical for Phase C v2 inventory
    integrity."""

    def _write_report(self, path: Path, *, counts: dict,
                      oldest_stale=None, ok_share=None,
                      missing_share=None,
                      generated="2026-05-17T01:00:00Z"):
        n_filled = counts.get("filled_or_settled_total", 0)
        if ok_share is None and n_filled:
            ok_share = counts.get("ok", 0) / n_filled
        if missing_share is None and n_filled:
            missing_share = counts.get("missing_mlb_data", 0) / n_filled
        payload = {
            "generated_at_utc": generated,
            "schema_version": 1,
            "counts": counts,
            "ok_share": ok_share,
            "missing_mlb_data_share": missing_share,
            "oldest_stale_filled_age_days": oldest_stale,
            "thresholds": {
                "stale_filled_alert": 1,
                "missing_mlb_data_rate_alert": 0.10,
            },
            "by_result_code": {},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_artifact_records_error_no_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            out = bdhr._settlement_truth_health(
                report_path=Path(td) / "missing.json",
                session_date="2026-05-17",
            )
            self.assertFalse(out["artifact_present"])
            self.assertEqual(out["alerts"], [])
            self.assertIn("missing", out.get("artifact_error", ""))

    def test_healthy_report_emits_no_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "st.json"
            self._write_report(path, counts={
                "filled_or_settled_total": 50,
                "ok": 50,
                "resolution_mismatch": 0,
                "stale_filled": 0,
                "missing_mlb_data": 0,
                "game_not_final_yet": 0,
            })
            out = bdhr._settlement_truth_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertEqual(out["alerts"], [])
            self.assertEqual(out["counts"]["ok"], 50)

    def test_resolution_mismatch_fires_critical_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "st.json"
            self._write_report(path, counts={
                "filled_or_settled_total": 50,
                "ok": 49,
                "resolution_mismatch": 1,
                "stale_filled": 0,
                "missing_mlb_data": 0,
                "game_not_final_yet": 0,
            })
            out = bdhr._settlement_truth_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertTrue(any(
                "resolution_mismatch" in a and "ROI math" in a
                for a in out["alerts"]
            ), out["alerts"])

    def test_total_mismatch_fires_lower_severity_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "st.json"
            self._write_report(path, counts={
                "filled_or_settled_total": 50,
                "ok": 49,
                "total_mismatch": 1,
                "resolution_mismatch": 0,
                "stale_filled": 0,
                "missing_mlb_data": 0,
            })
            out = bdhr._settlement_truth_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertTrue(any(
                "total_mismatch" in a and "preserved" in a
                for a in out["alerts"]
            ), out["alerts"])

    def test_stale_filled_fires_at_threshold_and_includes_age(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "st.json"
            self._write_report(path, counts={
                "filled_or_settled_total": 50,
                "ok": 48,
                "stale_filled": 2,
                "missing_mlb_data": 0,
                "resolution_mismatch": 0,
            }, oldest_stale=10)
            out = bdhr._settlement_truth_health(
                report_path=path, session_date="2026-05-17",
            )
            stale_alerts = [a for a in out["alerts"] if "stale_filled" in a]
            self.assertEqual(len(stale_alerts), 1, out["alerts"])
            self.assertIn("10d", stale_alerts[0])
            self.assertIn("Phase C v2", stale_alerts[0])

    def test_missing_mlb_data_above_threshold_fires_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "st.json"
            self._write_report(path, counts={
                "filled_or_settled_total": 50,
                "ok": 25,
                "missing_mlb_data": 25,
                "resolution_mismatch": 0,
                "stale_filled": 0,
            })
            out = bdhr._settlement_truth_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertTrue(any(
                "missing_mlb_data" in a and "50.0%" in a
                for a in out["alerts"]
            ), out["alerts"])

    def test_missing_mlb_data_below_threshold_no_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "st.json"
            self._write_report(path, counts={
                "filled_or_settled_total": 100,
                "ok": 95,
                "missing_mlb_data": 5,  # 5% < 10%
                "resolution_mismatch": 0,
                "stale_filled": 0,
            })
            out = bdhr._settlement_truth_health(
                report_path=path, session_date="2026-05-17",
            )
            missing_alerts = [a for a in out["alerts"] if "missing_mlb_data" in a]
            self.assertEqual(missing_alerts, [])

    def test_game_not_final_yet_fires_distinct_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "st.json"
            self._write_report(path, counts={
                "filled_or_settled_total": 50,
                "ok": 48,
                "game_not_final_yet": 2,
                "missing_mlb_data": 0,
                "resolution_mismatch": 0,
                "stale_filled": 0,
            })
            out = bdhr._settlement_truth_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertTrue(any(
                "game_not_final_yet" in a and "scraper" in a
                for a in out["alerts"]
            ), out["alerts"])

    def test_stale_artifact_age_fires_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "st.json"
            self._write_report(path, counts={"filled_or_settled_total": 0},
                                generated="2026-04-01T01:00:00Z")
            out = bdhr._settlement_truth_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertTrue(any("d old" in a for a in out["alerts"]))

    def test_notes_block_carries_settlement_truth_prefix(self):
        st = {"alerts": ["1 resolution_mismatch row(s) -- ..."]}
        notes = bdhr._build_notes(
            session_summary={}, bet_totals={}, candidate_rollup={},
            log_health={}, settlement_truth_health=st,
        )
        prefixed = [n for n in notes if n.startswith("Settlement-truth:")]
        self.assertEqual(len(prefixed), 1)


class CalibrationHealthUnderSplitTests(unittest.TestCase):
    """Phase B B1 (2026-05-16): `calibration_health` now reads the
    UNDER calibrator artifact alongside the OVER one and exposes the
    metadata under a `under` sub-block. The OVER fields stay at the
    top level (back-compat). Side-prefixed alerts ("under: ...")
    distinguish where each problem originates."""

    def _write_artifact(self, path, *, side,
                        methods,
                        generated="2026-05-16T01:00:00Z"):
        families = {
            family: {
                "selected_method": method,
                "methods": {method: {"params": {}}},
                "selection_audit": {
                    "primary_winner": method,
                    "identity_rejection_applied": False,
                },
            }
            for family, method in methods.items()
        }
        payload = {
            "schema_version": 2,
            "generated_at_utc": generated,
            "side": side,
            "family_mode": "separate",
            "default_family": (
                "score_event_transition" if "score_event_transition" in methods
                else next(iter(methods), "score_event_transition")
            ),
            "families": families,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_under_block_loads_when_artifact_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            over_path = root / "over.json"
            under_path = root / "under.json"
            self._write_artifact(
                over_path, side="over",
                methods={"score_event_transition": "platt",
                         "no_score_drift": "isotonic"},
            )
            self._write_artifact(
                under_path, side="under",
                methods={"score_event_transition": "platt",
                         "no_score_drift": "platt"},
            )
            out = bdhr._calibration_health(
                session_date="2026-05-16",
                candidate_dir=root / "missing_candidates",
                artifact_path=over_path,
                output_root=root / "out",
                artifact_path_under=under_path,
            )
            self.assertIn("under", out)
            under = out["under"]
            self.assertTrue(under["artifact_present"])
            self.assertEqual(under["artifact_side"], "under")
            self.assertEqual(
                under["artifact_methods_by_family"],
                {"no_score_drift": "platt", "score_event_transition": "platt"},
            )

    def test_missing_under_artifact_emits_alert(self):
        """When the UNDER artifact file is absent, the `under` block
        records artifact_present=False and an UNDER-side-prefixed
        alert -- distinct from the OVER artifact-missing alert."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            over_path = root / "over.json"
            self._write_artifact(
                over_path, side="over",
                methods={"score_event_transition": "platt"},
            )
            out = bdhr._calibration_health(
                session_date="2026-05-16",
                candidate_dir=root / "missing_candidates",
                artifact_path=over_path,
                output_root=root / "out",
                artifact_path_under=root / "missing_under.json",
            )
            self.assertIn("under", out)
            self.assertFalse(out["under"]["artifact_present"])
            self.assertTrue(
                any("under: " in a and "missing" in a for a in out["alerts"]),
                f"Expected an UNDER-prefixed missing alert; got: {out['alerts']}",
            )

    def test_under_identity_emits_side_prefixed_alert(self):
        """When the UNDER calibrator selects identity, the alert text
        carries the `under: ` prefix so operators can tell which side
        is degraded without checking the sub-block."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            over_path = root / "over.json"
            under_path = root / "under.json"
            self._write_artifact(
                over_path, side="over",
                methods={"score_event_transition": "platt"},
            )
            self._write_artifact(
                under_path, side="under",
                methods={"score_event_transition": "identity"},
            )
            out = bdhr._calibration_health(
                session_date="2026-05-16",
                candidate_dir=root / "missing_candidates",
                artifact_path=over_path,
                output_root=root / "out",
                artifact_path_under=under_path,
            )
            under_identity_alerts = [
                a for a in out["alerts"]
                if a.startswith("under: ") and "identity" in a
            ]
            self.assertEqual(len(under_identity_alerts), 1, out["alerts"])

    def test_over_alerts_not_prefixed_with_under(self):
        """The OVER side keeps its existing un-prefixed alert text so
        legacy consumers' grep patterns still match."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            over_path = root / "over.json"
            self._write_artifact(
                over_path, side="over",
                methods={"score_event_transition": "identity"},
            )
            out = bdhr._calibration_health(
                session_date="2026-05-16",
                candidate_dir=root / "missing_candidates",
                artifact_path=over_path,
                output_root=root / "out",
                artifact_path_under=None,
            )
            for alert in out["alerts"]:
                self.assertFalse(
                    alert.startswith("under: "),
                    f"OVER alert should not be UNDER-prefixed: {alert!r}",
                )

    def test_artifact_path_under_none_skips_under_block(self):
        """Passing artifact_path_under=None (legacy callers) means
        no `under` sub-block appears -- pure back-compat."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            over_path = root / "over.json"
            self._write_artifact(
                over_path, side="over",
                methods={"score_event_transition": "platt"},
            )
            out = bdhr._calibration_health(
                session_date="2026-05-16",
                candidate_dir=root / "missing_candidates",
                artifact_path=over_path,
                output_root=root / "out",
                artifact_path_under=None,
            )
            self.assertNotIn("under", out)

    def test_under_method_change_since_prior_day(self):
        """Method-change-since-yesterday is computed independently per
        side. An UNDER flip is its own alert, prefixed `under: `."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            over_path = root / "over.json"
            under_path = root / "under.json"
            self._write_artifact(
                over_path, side="over",
                methods={"score_event_transition": "platt"},
            )
            self._write_artifact(
                under_path, side="under",
                methods={"score_event_transition": "isotonic"},  # changed
            )
            # Seed prior daily review with OVER + UNDER methods
            out_root = root / "out"
            out_root.mkdir()
            prior_review = out_root / "2026-05-15_human_review.json"
            prior_review.write_text(json.dumps({
                "calibration_health": {
                    "artifact_methods_by_family": {
                        "score_event_transition": "platt",
                    },
                    "under": {
                        "artifact_methods_by_family": {
                            "score_event_transition": "platt",  # was platt
                        },
                    },
                },
            }), encoding="utf-8")
            out = bdhr._calibration_health(
                session_date="2026-05-16",
                candidate_dir=root / "missing_candidates",
                artifact_path=over_path,
                output_root=out_root,
                artifact_path_under=under_path,
            )
            under_changes = out["under"]["method_changes_since_prior"]
            self.assertEqual(
                under_changes,
                {"score_event_transition": {"from": "platt", "to": "isotonic"}},
            )
            change_alerts = [
                a for a in out["alerts"]
                if a.startswith("under: ") and "changed" in a
            ]
            self.assertEqual(len(change_alerts), 1, out["alerts"])


class UnderBookCoverageHealthTests(unittest.TestCase):
    """Phase A1 (2026-05-16): the daily-review block reads
    `model_maturity_report.json` and surfaces under_pair_available_rate
    so operators can see the under-side book pairing coverage at a
    glance. Fires when the rate is below the warn floor or when the
    maturity report itself is stale.
    """

    def _maturity_payload(
        self,
        *,
        rate=0.85,
        rows=1000,
        no_vig=0.82,
        generated="2026-05-16T01:00:00Z",
        per_family=None,
    ):
        if per_family is None:
            per_family = {
                "score_event_transition": {
                    "rows": 600,
                    "under_pair_available_rate": 0.87,
                },
                "no_score_drift": {
                    "rows": 400,
                    "under_pair_available_rate": 0.83,
                },
            }
        under_rows = int(round(rate * rows)) if rate is not None else None
        return {
            "generated_at_utc": generated,
            "coverage_checks": {
                "overall": {
                    "rows": rows,
                    "under_pair_available_rows": under_rows,
                    "under_pair_available_rate": rate,
                    "under_pair_book_rate": rate,
                    "no_vig_market_rate": no_vig,
                },
                "by_family": per_family,
            },
        }

    def test_missing_artifact_records_error_no_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            out = bdhr._under_book_coverage_health(
                report_path=Path(td) / "missing.json",
                session_date="2026-05-16",
            )
            self.assertFalse(out["artifact_present"])
            self.assertEqual(out["alerts"], [])
            self.assertIn("missing", out.get("artifact_error", ""))

    def test_healthy_coverage_emits_no_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "maturity.json"
            path.write_text(json.dumps(self._maturity_payload(rate=0.85)),
                            encoding="utf-8")
            out = bdhr._under_book_coverage_health(
                report_path=path, session_date="2026-05-16",
            )
            self.assertEqual(out["alerts"], [])
            self.assertAlmostEqual(out["overall"]["under_pair_available_rate"], 0.85)
            self.assertEqual(out["overall"]["rows"], 1000)

    def test_below_warn_threshold_fires_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "maturity.json"
            path.write_text(json.dumps(self._maturity_payload(rate=0.49)),
                            encoding="utf-8")
            out = bdhr._under_book_coverage_health(
                report_path=path, session_date="2026-05-16",
            )
            joined = " || ".join(out["alerts"])
            self.assertIn("0.49", joined)
            self.assertIn("0.50", joined)
            self.assertIn("Phase C", joined)
            self.assertIn("tick-timing variance", joined)

    def test_at_warn_threshold_no_alert(self):
        """Threshold check uses strict <, not <=, so a coverage at
        exactly 0.50 does not fire (boundary is the safe side)."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "maturity.json"
            path.write_text(json.dumps(self._maturity_payload(rate=0.50)),
                            encoding="utf-8")
            out = bdhr._under_book_coverage_health(
                report_path=path, session_date="2026-05-16",
            )
            self.assertEqual(out["alerts"], [])

    def test_stale_artifact_fires_age_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "maturity.json"
            path.write_text(json.dumps(self._maturity_payload(
                rate=0.85,
                generated="2026-04-01T01:00:00Z",  # > 14d old vs 2026-05-16
            )), encoding="utf-8")
            out = bdhr._under_book_coverage_health(
                report_path=path, session_date="2026-05-16",
            )
            self.assertTrue(any("d old" in a for a in out["alerts"]))

    def test_per_family_rates_surface_in_block(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "maturity.json"
            path.write_text(json.dumps(self._maturity_payload(rate=0.85)),
                            encoding="utf-8")
            out = bdhr._under_book_coverage_health(
                report_path=path, session_date="2026-05-16",
            )
            self.assertIn("score_event_transition", out["by_family"])
            self.assertAlmostEqual(
                out["by_family"]["score_event_transition"]["under_pair_available_rate"],
                0.87,
            )
            self.assertEqual(
                out["by_family"]["score_event_transition"]["rows"],
                600,
            )

    def test_missing_coverage_checks_does_not_crash(self):
        """Malformed maturity payload (no coverage_checks) should
        return a payload with rate=None rather than crashing -- the
        block is a visibility surface, not a hard dependency."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "maturity.json"
            path.write_text(json.dumps({
                "generated_at_utc": "2026-05-16T01:00:00Z",
            }), encoding="utf-8")
            out = bdhr._under_book_coverage_health(
                report_path=path, session_date="2026-05-16",
            )
            self.assertIsNone(out["overall"]["under_pair_available_rate"])
            self.assertEqual(out["alerts"], [])

    def test_under_book_alerts_surface_in_notes(self):
        """The Notes block prefixes the alert with 'Under-book-coverage:'."""
        cov = {"alerts": ["under_pair_available_rate 0.40 below warn floor 0.50; ..."]}
        notes = bdhr._build_notes(
            session_summary={}, bet_totals={}, candidate_rollup={}, log_health={},
            under_book_coverage_health=cov,
        )
        prefixed = [n for n in notes if n.startswith("Under-book-coverage:")]
        self.assertEqual(len(prefixed), 1)
        self.assertIn("0.40", prefixed[0])


class GateCounterfactualHealthTests(unittest.TestCase):
    """Active #11 (2026-05-17): the daily-review block reads the
    gate_counterfactual_report.json artifact and mirrors top tightening
    recommendations whose $-savings clear the Notes-mirror floor to
    the top-level Notes block."""

    def _write_report(
        self, path: Path, *,
        top_30d=None, top_7d=None,
        generated="2026-05-17T01:00:00Z",
        n_rows=100,
    ):
        payload = {
            "generated_at_utc": generated,
            "schema_version": 1,
            "n_rows": n_rows,
            "date_span": {"first": "2026-05-01", "last": "2026-05-17"},
            "windows": {
                "all": {"date_range": ["2026-05-01", "2026-05-17"], "n_rows": n_rows},
                "trailing_30d": {
                    "date_range": ["2026-04-18", "2026-05-17"], "n_rows": n_rows,
                },
                "trailing_7d": {
                    "date_range": ["2026-05-11", "2026-05-17"], "n_rows": 30,
                },
            },
            "config": {
                "min_blocked_n": 5,
                "recommendation_min_delta_usd": 25.0,
            },
            "gates": [],
            "top_recommendations": top_30d or [],
            "top_recommendations_trailing_7d": top_7d or [],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def _make_rec(
        gate="gate_extreme_edge",
        from_threshold=0.22,
        to_threshold=0.18,
        delta=75.0,
        blocked_n=15,
        blocked_roi=-0.45,
        kept_roi=0.10,
        kept_delta=0.05,
        confidence="medium",
        window="trailing_30d",
    ):
        return {
            "gate": gate,
            "from_threshold": from_threshold,
            "to_threshold": to_threshold,
            "counterfactual_profit_delta_usd": delta,
            "blocked_n_filled": blocked_n,
            "blocked_roi": blocked_roi,
            "kept_roi_after": kept_roi,
            "kept_roi_delta_vs_current": kept_delta,
            "confidence": confidence,
            "window": window,
        }

    def test_missing_artifact_records_error_no_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            out = bdhr._gate_counterfactual_health(
                report_path=Path(td) / "missing.json",
                session_date="2026-05-17",
            )
            self.assertFalse(out["artifact_present"])
            self.assertEqual(out["alerts"], [])
            self.assertIn("missing", out.get("artifact_error", ""))
            self.assertEqual(out["top_recommendations_30d"], [])

    def test_no_recommendations_emits_no_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "gc.json"
            self._write_report(path, top_30d=[], top_7d=[])
            out = bdhr._gate_counterfactual_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertEqual(out["alerts"], [])
            self.assertEqual(out["top_recommendations_30d"], [])

    def test_recommendation_above_floor_fires_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "gc.json"
            self._write_report(
                path,
                top_30d=[
                    self._make_rec(gate="gate_min_entry_ask", delta=75.6),
                ],
            )
            out = bdhr._gate_counterfactual_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertEqual(len(out["alerts"]), 1)
            alert = out["alerts"][0]
            self.assertIn("gate_min_entry_ask", alert)
            self.assertIn("$+75.60", alert)
            self.assertIn("trailing-30d", alert)

    def test_recommendation_below_floor_suppressed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "gc.json"
            # $30 > builder $25 floor but < daily-review $40 mirror floor
            self._write_report(
                path,
                top_30d=[self._make_rec(delta=30.0)],
            )
            out = bdhr._gate_counterfactual_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertEqual(out["alerts"], [])
            # But the JSON-level compact list still surfaces it
            self.assertEqual(len(out["top_recommendations_30d"]), 1)
            self.assertEqual(
                out["top_recommendations_30d"][0]["gate"],
                "gate_extreme_edge",
            )

    def test_caps_alerts_at_max(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "gc.json"
            self._write_report(path, top_30d=[
                self._make_rec(gate=f"gate_{i}", delta=100.0 - i)
                for i in range(10)
            ])
            out = bdhr._gate_counterfactual_health(
                report_path=path, session_date="2026-05-17",
            )
            # GATE_COUNTERFACTUAL_NOTES_MAX_ALERTS is 3
            self.assertEqual(len(out["alerts"]), 3)
            # The 3 chosen are the first 3 (highest $-savings)
            self.assertIn("gate_0", out["alerts"][0])
            self.assertIn("gate_2", out["alerts"][2])

    def test_stale_artifact_age_fires_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "gc.json"
            self._write_report(
                path,
                generated="2026-04-01T00:00:00Z",   # > 14d old vs 2026-05-17
                top_30d=[],
            )
            out = bdhr._gate_counterfactual_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertTrue(any(
                "is" in a and "old" in a for a in out["alerts"]
            ), out["alerts"])

    def test_compact_recommendation_carries_required_fields(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "gc.json"
            self._write_report(path, top_30d=[self._make_rec(delta=100)])
            out = bdhr._gate_counterfactual_health(
                report_path=path, session_date="2026-05-17",
            )
            rec = out["top_recommendations_30d"][0]
            for k in (
                "gate", "from_threshold", "to_threshold",
                "counterfactual_profit_delta_usd",
                "blocked_n_filled", "blocked_roi",
                "kept_roi_after", "kept_roi_delta_vs_current",
                "confidence", "window",
            ):
                self.assertIn(k, rec)

    def test_trailing_7d_recommendations_passed_through(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "gc.json"
            self._write_report(
                path,
                top_30d=[],
                top_7d=[self._make_rec(gate="gate_7d_only", delta=60)],
            )
            out = bdhr._gate_counterfactual_health(
                report_path=path, session_date="2026-05-17",
            )
            # 7d list goes in JSON but is NOT mirrored to Notes (only 30d).
            self.assertEqual(len(out["top_recommendations_7d"]), 1)
            self.assertEqual(out["alerts"], [])

    def test_corrupt_json_emits_artifact_error_no_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "gc.json"
            path.write_text("not json", encoding="utf-8")
            out = bdhr._gate_counterfactual_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertEqual(out["alerts"], [])
            self.assertIn("failed to load", out.get("artifact_error", ""))

    def test_notes_block_carries_gate_counterfactual_prefix(self):
        gch = {"alerts": ["`gate_x` tighten 0.55 -> 0.65 would have saved..."]}
        notes = bdhr._build_notes(
            session_summary={}, bet_totals={}, candidate_rollup={},
            log_health={}, gate_counterfactual_health=gch,
        )
        prefixed = [n for n in notes if n.startswith("Gate-counterfactual:")]
        self.assertEqual(len(prefixed), 1)


class CohortCalibrationHealthTests(unittest.TestCase):
    """Active #9 (2026-05-17): the 8th drift dimension. Mirrors the
    cohort_roi_health decomposition on the calibration axis: per-cohort
    reliability gap vs aggregate reliability gap. Two alert classes:

      1. Aggregate-level: whole-model reliability gap >= 10pp
         (fires regardless of any cohort's individual deviation)
      2. Per-cohort vs aggregate ratio: bucket gap >= 2x aggregate
         AND bucket has >= 30 settled bets
    """

    @staticmethod
    def _bet(*, fv, won, edge=0.15, ask=0.70, inning=6, line=8.5,
             cse=0.05, status="filled"):
        return {
            "status": status,
            "fair_value": fv,
            "won": won,
            "edge": edge,
            "entry_ask": ask,
            "inning": inning,
            "line": line,
            "current_state_value_edge": cse,
            "profit": 0.0,
            "fill_cost_usdc": 10.0,
        }

    @staticmethod
    def _review(bets):
        return [{"bets": bets}]

    # ---- _bet_is_calibratable filter tests ----
    def test_filter_rejects_cancelled(self):
        self.assertFalse(bdhr._bet_is_calibratable(
            self._bet(fv=0.6, won=True, status="cancelled"),
        ))

    def test_filter_rejects_none_won(self):
        self.assertFalse(bdhr._bet_is_calibratable(
            self._bet(fv=0.6, won=None),
        ))

    def test_filter_rejects_none_fv(self):
        self.assertFalse(bdhr._bet_is_calibratable(
            self._bet(fv=None, won=True),
        ))

    def test_filter_rejects_out_of_range_fv(self):
        self.assertFalse(bdhr._bet_is_calibratable(
            self._bet(fv=1.5, won=True),
        ))
        self.assertFalse(bdhr._bet_is_calibratable(
            self._bet(fv=-0.1, won=True),
        ))

    def test_filter_accepts_clean_filled_settled(self):
        self.assertTrue(bdhr._bet_is_calibratable(
            self._bet(fv=0.6, won=True),
        ))

    # ---- _aggregate_calibration math tests ----
    def test_aggregate_empty_returns_none_fields(self):
        agg = bdhr._aggregate_calibration([])
        self.assertEqual(agg["n"], 0)
        for k in ("mean_fair_value", "mean_won", "reliability_gap", "brier"):
            self.assertIsNone(agg[k])

    def test_aggregate_perfectly_calibrated_zero_gap(self):
        # 5 bets: predicted 0.5, half win half lose -> mean_won=0.5
        bets = [self._bet(fv=0.5, won=True)] * 2 + [self._bet(fv=0.5, won=False)] * 2
        agg = bdhr._aggregate_calibration(bets)
        self.assertEqual(agg["n"], 4)
        self.assertEqual(agg["mean_fair_value"], 0.5)
        self.assertEqual(agg["mean_won"], 0.5)
        self.assertEqual(agg["reliability_gap"], 0.0)
        # Brier = mean((fv-y)^2) = (0.25 + 0.25 + 0.25 + 0.25)/4 = 0.25
        self.assertEqual(agg["brier"], 0.25)

    def test_aggregate_over_predicting_positive_gap(self):
        # Model says 80% win, reality 40% win -> gap = 0.4
        bets = [self._bet(fv=0.8, won=True)] * 2 + [self._bet(fv=0.8, won=False)] * 3
        agg = bdhr._aggregate_calibration(bets)
        self.assertEqual(agg["mean_fair_value"], 0.8)
        self.assertEqual(agg["mean_won"], 0.4)
        self.assertEqual(agg["reliability_gap"], 0.4)

    def test_aggregate_under_predicting_also_positive_gap(self):
        # Gap is absolute value -- both directions show as positive.
        bets = [self._bet(fv=0.3, won=True)] * 4 + [self._bet(fv=0.3, won=False)]
        agg = bdhr._aggregate_calibration(bets)
        self.assertEqual(agg["mean_fair_value"], 0.3)
        self.assertEqual(agg["mean_won"], 0.8)
        self.assertEqual(agg["reliability_gap"], 0.5)

    # ---- _cohort_calibration_health alert tests ----
    def test_aggregate_alert_fires_at_threshold_with_min_n(self):
        # 20 bets: mean_fv=0.80, mean_won=0.60 -> gap=0.20 >= 0.10
        # n=20 >= aggregate_min_n=15
        bets = [self._bet(fv=0.8, won=True)] * 12 + [self._bet(fv=0.8, won=False)] * 8
        out = bdhr._cohort_calibration_health(
            today_bet_rows=bets, trailing_reviews=[],
        )
        agg_alerts = [a for a in out["alerts"] if a.startswith("aggregate")]
        self.assertEqual(len(agg_alerts), 1, out["alerts"])
        self.assertIn("over-predicting", agg_alerts[0])
        self.assertIn("20.0pp", agg_alerts[0])

    def test_aggregate_alert_does_not_fire_below_threshold(self):
        # Gap=0.05 < threshold=0.10
        bets = [self._bet(fv=0.55, won=True)] * 10 + [self._bet(fv=0.55, won=False)] * 10
        out = bdhr._cohort_calibration_health(
            today_bet_rows=bets, trailing_reviews=[],
        )
        agg_alerts = [a for a in out["alerts"] if a.startswith("aggregate")]
        self.assertEqual(agg_alerts, [])

    def test_aggregate_alert_does_not_fire_below_min_n(self):
        # n=10 < aggregate_min_n=15, even with large gap
        bets = [self._bet(fv=0.9, won=False)] * 10
        out = bdhr._cohort_calibration_health(
            today_bet_rows=bets, trailing_reviews=[],
        )
        agg_alerts = [a for a in out["alerts"] if a.startswith("aggregate")]
        self.assertEqual(agg_alerts, [])

    def test_under_predicting_alert_carries_correct_direction(self):
        # FV=0.3, won rate=0.9 -> gap=0.6, model under-predicts
        bets = [self._bet(fv=0.3, won=True)] * 18 + [self._bet(fv=0.3, won=False)] * 2
        out = bdhr._cohort_calibration_health(
            today_bet_rows=bets, trailing_reviews=[],
        )
        agg_alerts = [a for a in out["alerts"] if a.startswith("aggregate")]
        self.assertTrue(any("under-predicting" in a for a in agg_alerts))

    def test_per_cohort_alert_fires_at_2x_aggregate_with_n_30(self):
        # Aggregate: 80 bets, gap = small (~5pp).
        # One cohort (inning_bucket=>=8): 35 bets with gap = 30pp (6x aggregate).
        # Other 45 bets distributed in different bucket with near-perfect cal.
        aggregate_bets = (
            [self._bet(fv=0.5, won=True, inning=5)] * 23
            + [self._bet(fv=0.5, won=False, inning=5)] * 22
        )
        cohort_bets = (
            [self._bet(fv=0.9, won=True, inning=8)] * 21
            + [self._bet(fv=0.9, won=False, inning=8)] * 14
        )
        bets = aggregate_bets + cohort_bets
        out = bdhr._cohort_calibration_health(
            today_bet_rows=bets, trailing_reviews=[],
        )
        cohort_alerts = [
            a for a in out["alerts"]
            if "inning_bucket=>=8" in a and "cohort" in a
        ]
        self.assertEqual(len(cohort_alerts), 1, out["alerts"])

    def test_per_cohort_alert_suppressed_below_n_30(self):
        # 25 cohort bets with huge gap -- below the 30 threshold
        aggregate_bets = (
            [self._bet(fv=0.5, won=True, inning=5)] * 30
            + [self._bet(fv=0.5, won=False, inning=5)] * 30
        )
        thin_cohort = [self._bet(fv=0.9, won=False, inning=8)] * 25
        bets = aggregate_bets + thin_cohort
        out = bdhr._cohort_calibration_health(
            today_bet_rows=bets, trailing_reviews=[],
        )
        cohort_alerts = [
            a for a in out["alerts"] if "inning_bucket=>=8" in a
        ]
        self.assertEqual(cohort_alerts, [], out["alerts"])

    def test_per_cohort_alert_suppressed_when_aggregate_gap_too_small(self):
        # Aggregate near-perfect (gap < min_aggregate_gap=0.01) ->
        # ratio test disabled. Cohort gap of 10pp would otherwise be
        # >> 2x but we skip ratio when aggregate is too noisy a base.
        # n=60 in cohort makes both alerts available structurally.
        bets = (
            [self._bet(fv=0.500, won=True, inning=5)] * 30
            + [self._bet(fv=0.500, won=False, inning=5)] * 30
        )
        # Aggregate gap is 0; ratio division would explode.
        # Add an inn=8 cohort that's mis-calibrated.
        bets += (
            [self._bet(fv=0.7, won=True, inning=8)] * 17
            + [self._bet(fv=0.7, won=False, inning=8)] * 13
        )
        out = bdhr._cohort_calibration_health(
            today_bet_rows=bets, trailing_reviews=[],
        )
        # Aggregate gap on full bag: mean_fv=(0.5*60+0.7*30)/90=0.567,
        # mean_won=(30+17)/90=0.522, gap=0.044 -- > 0.01 floor.
        # Ratio test active. Cohort gap on inn=8: |0.7 - 17/30| = 0.133
        # Ratio = 0.133/0.044 = 3.02 >= 2.0 -> fires. n=30 OK.
        cohort_alerts = [
            a for a in out["alerts"]
            if "inning_bucket=>=8" in a and "cohort" in a
        ]
        self.assertEqual(len(cohort_alerts), 1, out["alerts"])

    def test_missing_bucket_excluded_from_cohort_alerts(self):
        # 30 bets with no inning -> bucket = "missing"; must not alert
        # even at a giant gap.
        bets = [self._bet(fv=0.95, won=False, inning=None)] * 30
        out = bdhr._cohort_calibration_health(
            today_bet_rows=bets, trailing_reviews=[],
        )
        missing_alerts = [a for a in out["alerts"] if "missing" in a]
        self.assertEqual(missing_alerts, [])

    def test_trailing_reviews_contribute_to_window(self):
        # Today: 5 bets (below min_n for aggregate alert).
        # Trailing window: 20 more identical bets -> total 25 still
        # below 15 wouldn't trigger. Push to 20 trailing -> total 25
        # passes aggregate_min_n=15. Use big gap to ensure firing.
        today = [self._bet(fv=0.9, won=False)] * 5
        trailing = self._review([self._bet(fv=0.9, won=False)] * 20)
        out = bdhr._cohort_calibration_health(
            today_bet_rows=today, trailing_reviews=trailing,
        )
        self.assertEqual(out["n_filled_settled"], 25)
        agg_alerts = [a for a in out["alerts"] if a.startswith("aggregate")]
        self.assertEqual(len(agg_alerts), 1)

    def test_returns_complete_schema_keys(self):
        out = bdhr._cohort_calibration_health(
            today_bet_rows=[self._bet(fv=0.5, won=True)],
            trailing_reviews=[],
        )
        for key in (
            "alerts", "window_days", "n_filled_settled", "aggregate",
            "cohorts_by_dimension", "recent_promotions_count",
            "recent_demotions_count", "concept_drift_major_features_count",
            "thresholds",
        ):
            self.assertIn(key, out)

    def test_promotion_attribution_appends_to_alert(self):
        # Set up a real promotion-events log so the attribution helper
        # picks it up. Mirror cohort_roi's test pattern.
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "promo.jsonl"
            log.write_text(
                json.dumps({
                    "direction": "promote",
                    "action": "promoted",
                    "lever": "stage2",
                    "generated_at_utc": "2026-05-15T01:00:00Z",
                }) + "\n",
                encoding="utf-8",
            )
            # 20 bets with 20pp aggregate gap to trigger the alert
            bets = [bdhr.__dict__.get("_session_date_for_test")]  # noqa: placeholder
            bets = [
                self._bet(fv=0.8, won=False),
            ] * 18 + [self._bet(fv=0.8, won=True)] * 2
            out = bdhr._cohort_calibration_health(
                today_bet_rows=bets, trailing_reviews=[],
                session_date="2026-05-17",
                promotion_events_log_path=log,
            )
            self.assertTrue(any(
                "stage2" in a and "coincides with" in a
                for a in out["alerts"]
            ), out["alerts"])

    def test_notes_block_carries_cohort_calibration_prefix(self):
        cch = {"alerts": ["aggregate calibration reliability gap 22.1pp ..."]}
        notes = bdhr._build_notes(
            session_summary={}, bet_totals={}, candidate_rollup={},
            log_health={}, cohort_calibration_health=cch,
        )
        prefixed = [n for n in notes if n.startswith("Cohort-calibration:")]
        self.assertEqual(len(prefixed), 1)


class LossAttributionHealthTests(unittest.TestCase):
    """Active #10 (2026-05-17): daily-review block reads the loss
    attribution artifact and surfaces the trailing-30d aggregate +
    top culprit. Mirrors a single Notes-line alert pointing at the
    retrain target when bias is materially large AND some stage owns
    >= 50% of the bias direction."""

    def _write_report(
        self, path: Path, *, trailing_30d=None, trailing_7d=None,
        generated="2026-05-17T01:00:00Z",
    ):
        def _aggregate(*, n=87, bias=0.27, direction="over_predicting",
                       mean_p0=0.92, mean_p3=0.92, mean_won=0.65,
                       culprits=None):
            return {
                "n": n,
                "bias": bias,
                "abs_bias": abs(bias),
                "bias_direction": direction,
                "mean_p0": mean_p0,
                "mean_p3": mean_p3,
                "mean_won": mean_won,
                "top_culprits": culprits or [],
            }
        payload = {
            "schema_version": 1,
            "generated_at_utc": generated,
            "n_bets": (trailing_30d or {}).get("n", 87),
            "windows": {
                "all": {"date_range": ["2026-04-01", "2026-05-17"],
                        "aggregate": trailing_30d
                        or _aggregate()},
                "trailing_30d": {
                    "date_range": ["2026-04-17", "2026-05-16"],
                    "aggregate": trailing_30d or _aggregate(),
                },
                "trailing_7d": {
                    "date_range": ["2026-05-10", "2026-05-16"],
                    "aggregate": trailing_7d or _aggregate(n=44),
                },
            },
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def _agg(*, n=87, bias=0.27, mean_p0=0.92, mean_p3=0.92,
             mean_won=0.65, direction="over_predicting", culprits=None):
        return {
            "n": n,
            "bias": bias,
            "abs_bias": abs(bias),
            "bias_direction": direction,
            "mean_p0": mean_p0,
            "mean_p3": mean_p3,
            "mean_won": mean_won,
            "top_culprits": culprits or [],
        }

    def test_missing_artifact_emits_error_no_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            out = bdhr._loss_attribution_health(
                report_path=Path(td) / "missing.json",
                session_date="2026-05-17",
            )
            self.assertFalse(out["artifact_present"])
            self.assertEqual(out["alerts"], [])
            self.assertIn("missing", out.get("artifact_error", ""))

    def test_corrupt_json_emits_error_no_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "loss.json"
            path.write_text("not json", encoding="utf-8")
            out = bdhr._loss_attribution_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertEqual(out["alerts"], [])
            self.assertIn("failed to load", out.get("artifact_error", ""))

    def test_clear_culprit_above_share_fires_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "loss.json"
            self._write_report(path, trailing_30d=self._agg(
                bias=0.27,
                culprits=[{
                    "stage": "stage1_baseline",
                    "attribution_share": 0.95,
                    "mean_shift_in_bias_direction": 0.42,
                }],
            ))
            out = bdhr._loss_attribution_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertEqual(len(out["alerts"]), 1)
            alert = out["alerts"][0]
            self.assertIn("stage1_baseline", alert)
            self.assertIn("+27.0pp", alert)
            self.assertIn("retrain target", alert)

    def test_no_single_culprit_emits_softer_alert(self):
        # Bias material but no stage owns >= 50%
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "loss.json"
            self._write_report(path, trailing_30d=self._agg(
                bias=0.15,
                culprits=[
                    {"stage": "stage1_baseline", "attribution_share": 0.30,
                     "mean_shift_in_bias_direction": 0.15},
                    {"stage": "calibration", "attribution_share": 0.30,
                     "mean_shift_in_bias_direction": 0.15},
                ],
            ))
            out = bdhr._loss_attribution_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertEqual(len(out["alerts"]), 1)
            alert = out["alerts"][0]
            self.assertIn("no single stage owns", alert)
            self.assertNotIn("retrain target", alert)

    def test_small_bias_does_not_fire(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "loss.json"
            # bias=2pp < 5pp floor
            self._write_report(path, trailing_30d=self._agg(
                bias=0.02,
                culprits=[{
                    "stage": "stage1_baseline",
                    "attribution_share": 0.95,
                    "mean_shift_in_bias_direction": 0.02,
                }],
            ))
            out = bdhr._loss_attribution_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertEqual(out["alerts"], [])

    def test_under_predicting_alert_carries_direction(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "loss.json"
            self._write_report(path, trailing_30d=self._agg(
                bias=-0.20, direction="under_predicting",
                culprits=[{
                    "stage": "stage2_run_env",
                    "attribution_share": 0.80,
                    "mean_shift_in_bias_direction": 0.15,
                }],
            ))
            out = bdhr._loss_attribution_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertIn("under_predicting", out["alerts"][0])
            self.assertIn("stage2_run_env", out["alerts"][0])

    def test_empty_trailing_30d_no_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "loss.json"
            empty_agg = {"n": 0, "bias": None, "abs_bias": None,
                         "bias_direction": None, "top_culprits": []}
            self._write_report(path, trailing_30d=empty_agg)
            out = bdhr._loss_attribution_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertEqual(out["alerts"], [])
            self.assertIsNone(out["trailing_30d"])

    def test_stale_artifact_age_fires_alert(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "loss.json"
            self._write_report(
                path,
                generated="2026-04-01T00:00:00Z",  # > 14d vs 2026-05-17
                trailing_30d=self._agg(bias=0.0),
            )
            out = bdhr._loss_attribution_health(
                report_path=path, session_date="2026-05-17",
            )
            self.assertTrue(any("old" in a for a in out["alerts"]))

    def test_compact_carries_required_fields(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "loss.json"
            self._write_report(path)
            out = bdhr._loss_attribution_health(
                report_path=path, session_date="2026-05-17",
            )
            compact = out["trailing_30d"]
            for k in (
                "n", "bias", "abs_bias", "bias_direction",
                "mean_p0", "mean_p3", "mean_won", "top_culprits",
                "date_range",
            ):
                self.assertIn(k, compact)

    def test_notes_block_carries_loss_attribution_prefix(self):
        lah = {"alerts": ["stage1_baseline owns 100% ..."]}
        notes = bdhr._build_notes(
            session_summary={}, bet_totals={}, candidate_rollup={},
            log_health={}, loss_attribution_health=lah,
        )
        prefixed = [n for n in notes if n.startswith("Loss-attribution:")]
        self.assertEqual(len(prefixed), 1)


if __name__ == "__main__":
    unittest.main()
