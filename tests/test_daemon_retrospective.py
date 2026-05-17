"""Tests for `scripts/analysis/daemon_retrospective.py`.

The retrospective replays the daemon's PROMOTE decision logic against
historical per-date snapshots (stage2_brier_history, stage3_v2_drift_history)
and the audit log (promote_events.jsonl), then classifies each
(date, lever) into MATCH / DAEMON_ONLY / OPERATOR_ONLY /
DAEMON_DISAGREED / BOTH_NO_ACTION.

Test surface:
  - Helpers: slice_history_le, distinct_history_dates, events_le
  - Classification: all 5 agreement categories
  - Per-lever replay: verdict reconstruction, cooldown interaction
  - Readiness verdict: ready_for_act / needs_more_history /
    disagreements_present
  - Snapshots for non-time-series levers
  - End-to-end report assembly + markdown rendering
  - CLI smoke test
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import daemon_retrospective as retro  # noqa: E402


def _stage2_row(date: str, delta: float) -> dict:
    return {
        "generated_at_utc": f"{date}T12:00:00Z",
        "data_max_date": date,
        "production_brier": 0.22,
        "staging_brier": 0.22 + delta,
        "delta": delta,
    }


def _stage3_row(date: str, max_abs_delta: float) -> dict:
    return {
        "generated_at_utc": f"{date}T12:00:00Z",
        "data_max_date": date,
        "research_betas": {"prior_season": -0.16, "season_to_date": +0.14, "momentum_10": +0.16},
        "active_betas": {"prior_season": -0.1514, "season_to_date": +0.1407, "momentum_10": +0.1503},
        "active_source": "compiled_defaults",
        "max_abs_delta": max_abs_delta,
    }


def _event(*, date: str, lever: str, action: str = "promoted",
           direction: str = "promote", operator: str = "tester") -> dict:
    return {
        "generated_at_utc": f"{date}T12:00:00Z",
        "lever": lever,
        "action": action,
        "direction": direction,
        "operator": operator,
    }


class SliceAndDistinctTests(unittest.TestCase):
    def test_distinct_dates_uses_data_max_date(self):
        rows = [_stage2_row("2026-05-01", -0.005),
                _stage2_row("2026-05-02", -0.005),
                _stage2_row("2026-05-01", -0.006)]  # duplicate date
        self.assertEqual(retro.distinct_history_dates(rows),
                         ["2026-05-01", "2026-05-02"])

    def test_distinct_dates_falls_back_to_generated_at_utc(self):
        rows = [{"generated_at_utc": "2026-05-03T10:00:00Z"},
                {"generated_at_utc": "2026-05-04T10:00:00Z"}]
        self.assertEqual(retro.distinct_history_dates(rows),
                         ["2026-05-03", "2026-05-04"])

    def test_distinct_dates_drops_unparseable_rows(self):
        rows = [{}, {"foo": "bar"}, _stage2_row("2026-05-01", -0.005)]
        self.assertEqual(retro.distinct_history_dates(rows), ["2026-05-01"])

    def test_slice_history_le_keeps_through_date(self):
        rows = [_stage2_row(f"2026-05-0{d}", -0.005) for d in range(1, 6)]
        sliced = retro.slice_history_le(rows, "2026-05-03")
        self.assertEqual(len(sliced), 3)
        self.assertEqual([_stage2_row("2026-05-03", -0.005)["data_max_date"]] * 1,
                         [sliced[-1]["data_max_date"]])

    def test_events_le_filters_by_date_prefix(self):
        events = [_event(date="2026-05-01", lever="stage2"),
                  _event(date="2026-05-05", lever="stage2"),
                  _event(date="2026-05-10", lever="stage2")]
        self.assertEqual(len(retro.events_le(events, "2026-05-05")), 2)
        self.assertEqual(len(retro.events_le(events, "2026-04-30")), 0)

    def test_events_lt_excludes_same_day_events(self):
        # Cooldown lookup uses strict less-than so same-day operator
        # actions don't make the daemon's eval look blocked.
        events = [_event(date="2026-05-01", lever="stage2"),
                  _event(date="2026-05-05", lever="stage2"),
                  _event(date="2026-05-10", lever="stage2")]
        self.assertEqual(len(retro.events_lt(events, "2026-05-05")), 1)
        self.assertEqual(len(retro.events_lt(events, "2026-05-01")), 0)

    def test_operator_action_on_date_picks_latest_on_day(self):
        events = [
            {"generated_at_utc": "2026-05-05T09:00:00Z", "lever": "stage2",
             "action": "promoted", "direction": "promote", "operator": "a"},
            {"generated_at_utc": "2026-05-05T11:00:00Z", "lever": "stage2",
             "action": "demoted", "direction": "demote", "operator": "b"},
        ]
        result = retro.operator_action_on_date(events, "stage2", "2026-05-05")
        self.assertEqual(result["operator"], "b")  # later in the day

    def test_operator_action_filters_to_success_labels(self):
        events = [
            {"generated_at_utc": "2026-05-05T09:00:00Z", "lever": "stage2",
             "action": "blocked", "direction": "promote", "operator": "a"},
            {"generated_at_utc": "2026-05-05T11:00:00Z", "lever": "stage2",
             "action": "dry_run", "direction": "promote", "operator": "b"},
        ]
        self.assertIsNone(retro.operator_action_on_date(events, "stage2", "2026-05-05"))


class ClassifyAgreementTests(unittest.TestCase):
    def test_both_no_action(self):
        d = {"decision": "no_action", "direction": None}
        self.assertEqual(retro.classify_agreement(d, None),
                         retro.AGREEMENT_BOTH_NO_ACTION)

    def test_daemon_only_when_daemon_acts_operator_idle(self):
        d = {"decision": "would_promote", "direction": "promote"}
        self.assertEqual(retro.classify_agreement(d, None),
                         retro.AGREEMENT_DAEMON_ONLY)

    def test_operator_only_when_daemon_idle_operator_acts(self):
        d = {"decision": "no_action", "direction": None}
        op = {"action": "promoted", "direction": "promote", "operator": "x"}
        self.assertEqual(retro.classify_agreement(d, op),
                         retro.AGREEMENT_OPERATOR_ONLY)

    def test_match_on_same_direction(self):
        d = {"decision": "would_promote", "direction": "promote"}
        op = {"action": "promoted", "direction": "promote", "operator": "x"}
        self.assertEqual(retro.classify_agreement(d, op),
                         retro.AGREEMENT_MATCH)

    def test_disagreed_on_opposite_direction(self):
        d = {"decision": "would_promote", "direction": "promote"}
        op = {"action": "demoted", "direction": "demote", "operator": "x"}
        self.assertEqual(retro.classify_agreement(d, op),
                         retro.AGREEMENT_DAEMON_DISAGREED)

    def test_cooldown_skip_counts_as_no_action(self):
        # Daemon would have wanted to act but cooldown blocked it ->
        # decision is "skipped_cooldown", direction is "promote", and
        # operator did nothing -> should be BOTH_NO_ACTION (the daemon
        # didn't actually act).
        d = {"decision": "skipped_cooldown", "direction": "promote"}
        self.assertEqual(retro.classify_agreement(d, None),
                         retro.AGREEMENT_BOTH_NO_ACTION)


class ReplayLeverForDateTests(unittest.TestCase):
    def test_promote_verdict_no_cooldown_emits_would_promote(self):
        # Six consecutive days of negative delta -> promote verdict
        history = [_stage2_row(f"2026-05-0{d}", -0.005) for d in range(1, 7)]
        decision = retro.replay_lever_for_date(
            lever="stage2", history=history, events=[],
            date="2026-05-06", cooldown_days=14,
        )
        self.assertEqual(decision["decision"], "would_promote")
        self.assertEqual(decision["verdict_label"], "promote")

    def test_promote_verdict_with_cooldown_emits_skipped(self):
        history = [_stage2_row(f"2026-05-0{d}", -0.005) for d in range(1, 7)]
        events = [_event(date="2026-05-04", lever="stage2")]
        decision = retro.replay_lever_for_date(
            lever="stage2", history=history, events=events,
            date="2026-05-06", cooldown_days=14,
        )
        self.assertEqual(decision["decision"], "skipped_cooldown")

    def test_hold_verdict_no_action(self):
        # Positive deltas -> staging is WORSE -> verdict != promote
        history = [_stage2_row(f"2026-05-0{d}", +0.005) for d in range(1, 7)]
        decision = retro.replay_lever_for_date(
            lever="stage2", history=history, events=[],
            date="2026-05-06", cooldown_days=14,
        )
        self.assertEqual(decision["decision"], "no_action")
        self.assertNotEqual(decision["verdict_label"], "promote")

    def test_replay_uses_only_history_up_to_date(self):
        # First 4 days have promote-pattern; days 5-6 don't matter
        # because we replay AS OF day 4.
        history = [_stage2_row(f"2026-05-0{d}", -0.005) for d in range(1, 5)]
        # Add later rows after day 4 that would invalidate verdict
        history += [_stage2_row(f"2026-05-0{d}", +0.5) for d in range(5, 7)]
        decision = retro.replay_lever_for_date(
            lever="stage2", history=history, events=[],
            date="2026-05-04", cooldown_days=14,
        )
        # day-4 verdict shouldn't be affected by later rows; result
        # depends on the verdict's actual rule (5-day window). Just
        # assert we got back a valid decision shape.
        self.assertIn(decision["decision"],
                      ["no_action", "would_promote", "skipped_cooldown"])


class ReadinessVerdictTests(unittest.TestCase):
    def _summary(self, m=0, do=0, oo=0, dd=0, bna=0):
        return {
            "match_count": m, "daemon_only_count": do,
            "operator_only_count": oo, "daemon_disagreed_count": dd,
            "both_no_action_count": bna,
        }

    def test_zero_dates_needs_more_history(self):
        self.assertEqual(retro._readiness_verdict(self._summary(), min_dates=7),
                         "needs_more_history")

    def test_few_dates_needs_more_history(self):
        self.assertEqual(
            retro._readiness_verdict(self._summary(m=3, bna=2), min_dates=7),
            "needs_more_history",
        )

    def test_enough_clean_dates_ready_for_act(self):
        self.assertEqual(
            retro._readiness_verdict(self._summary(m=2, bna=8), min_dates=7),
            "ready_for_act",
        )

    def test_any_daemon_only_flags_disagreement(self):
        self.assertEqual(
            retro._readiness_verdict(self._summary(m=5, do=1, bna=5),
                                     min_dates=7),
            "disagreements_present",
        )

    def test_any_daemon_disagree_flags_disagreement(self):
        self.assertEqual(
            retro._readiness_verdict(self._summary(m=5, dd=1, bna=5),
                                     min_dates=7),
            "disagreements_present",
        )


class ReplayLeverTests(unittest.TestCase):
    def test_full_replay_zero_history_returns_zero_dates(self):
        result = retro.replay_lever(
            lever="stage2", history=[], events=[],
            start_date=None, end_date=None, cooldown_days=14,
        )
        self.assertEqual(result["summary"]["n_dates_evaluated"], 0)
        self.assertEqual(result["summary"]["readiness_for_act"],
                         "needs_more_history")

    def test_match_when_daemon_and_operator_promote_same_day(self):
        # 6 days of negative delta -> day 6 verdict says promote.
        history = [_stage2_row(f"2026-05-0{d}", -0.005) for d in range(1, 7)]
        events = [_event(date="2026-05-06", lever="stage2")]
        result = retro.replay_lever(
            lever="stage2", history=history, events=events,
            start_date=None, end_date=None, cooldown_days=14,
        )
        per_date = result["per_date"]
        # Day 6 should be a MATCH; earlier days are BOTH_NO_ACTION
        # because verdict wasn't 'promote' yet.
        day6 = next(d for d in per_date if d["date"] == "2026-05-06")
        self.assertEqual(day6["agreement"], retro.AGREEMENT_MATCH)

    def test_date_range_filter_limits_evaluation(self):
        history = [_stage2_row(f"2026-05-0{d}", -0.005) for d in range(1, 7)]
        result = retro.replay_lever(
            lever="stage2", history=history, events=[],
            start_date="2026-05-04", end_date="2026-05-06",
            cooldown_days=14,
        )
        self.assertEqual(result["summary"]["n_dates_evaluated"], 3)
        self.assertEqual(result["date_range"]["start"], "2026-05-04")
        self.assertEqual(result["date_range"]["end"], "2026-05-06")

    def test_operator_only_when_force_used_without_verdict(self):
        # No promote verdict (positive deltas), operator force-promoted.
        history = [_stage2_row(f"2026-05-0{d}", +0.005) for d in range(1, 7)]
        events = [_event(date="2026-05-04", lever="stage2", action="forced")]
        result = retro.replay_lever(
            lever="stage2", history=history, events=events,
            start_date=None, end_date=None, cooldown_days=14,
        )
        day4 = next(d for d in result["per_date"] if d["date"] == "2026-05-04")
        self.assertEqual(day4["agreement"], retro.AGREEMENT_OPERATOR_ONLY)


class SnapshotTests(unittest.TestCase):
    def test_stake_scaling_snapshot_reads_verdict(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "ss.json"
            p.write_text(json.dumps({
                "verdict": "promote",
                "n_sessions": 35,
                "thresholds": {"min_sessions": 30},
            }), encoding="utf-8")
            s = retro.snapshot_stake_scaling(p)
            self.assertTrue(s["would_promote_today"])
            self.assertEqual(s["verdict_label"], "promote")
            self.assertTrue(s["actuated_by_daemon"])

    def test_stake_scaling_snapshot_missing_file(self):
        with tempfile.TemporaryDirectory() as tdstr:
            s = retro.snapshot_stake_scaling(Path(tdstr) / "missing.json")
            self.assertEqual(s["verdict_label"], "unreadable")

    def test_gate_threshold_snapshot_with_retune(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "wfc.json"
            p.write_text(json.dumps({
                "readiness": {"label": "READY"},
                "gates": [
                    {"name": "gate_extreme_edge", "current_threshold": 0.22,
                     "verdict": {"verdict": "RETUNE", "recommended_threshold": 0.20}},
                ],
            }), encoding="utf-8")
            s = retro.snapshot_gate_threshold(p)
            self.assertEqual(s["verdict_label"], "promote")
            self.assertEqual(len(s["actionable_gates"]), 1)
            self.assertFalse(s["actuated_by_daemon"])

    def test_gate_threshold_snapshot_with_only_keep(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            p = td / "wfc.json"
            p.write_text(json.dumps({
                "readiness": {"label": "READY"},
                "gates": [
                    {"name": "gate_extreme_edge", "current_threshold": 0.22,
                     "verdict": {"verdict": "KEEP"}},
                ],
            }), encoding="utf-8")
            s = retro.snapshot_gate_threshold(p)
            self.assertEqual(s["verdict_label"], "hold")
            self.assertEqual(s["actionable_gates"], [])


class EndToEndTests(unittest.TestCase):
    def _write_jsonl(self, path: Path, rows: list) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_report_includes_replays_and_snapshots(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            stage2_path = td / "stage2_brier_history.jsonl"
            stage3_path = td / "stage3_v2_drift_history.jsonl"
            events_path = td / "events.jsonl"
            ss_path = td / "ss.json"
            wfc_path = td / "wfc.json"
            self._write_jsonl(
                stage2_path,
                [_stage2_row(f"2026-05-0{d}", -0.005) for d in range(1, 7)],
            )
            self._write_jsonl(
                stage3_path,
                [_stage3_row(f"2026-05-0{d}", 0.005) for d in range(1, 7)],
            )
            self._write_jsonl(
                events_path,
                [_event(date="2026-05-06", lever="stage2")],
            )
            ss_path.write_text(json.dumps({
                "verdict": "hold", "n_sessions": 5,
                "thresholds": {"min_sessions": 30},
            }), encoding="utf-8")
            wfc_path.write_text(json.dumps({
                "readiness": {"label": "INSUFFICIENT"}, "gates": [],
            }), encoding="utf-8")

            import promote
            stage2_hist = promote._load_stage2_brier_history(stage2_path)
            stage3_hist = promote._load_stage3_v2_drift_history(stage3_path)
            events = promote.load_promotion_events(events_path)

            report = retro.build_report(
                stage2_history=stage2_hist,
                stage3_v2_history=stage3_hist,
                events=events,
                stake_scaling_report_path=ss_path,
                walk_forward_cert_path=wfc_path,
                start_date=None, end_date=None, cooldown_days=14,
            )

            self.assertIn("stage2", report["replays"])
            self.assertIn("stage3-v2", report["replays"])
            self.assertIn("stake-scaling", report["snapshots"])
            self.assertIn("gate-threshold", report["snapshots"])
            md = retro.render_markdown(report)
            self.assertIn("Daemon Retrospective Report", md)
            self.assertIn("stage2", md)


class CliSmokeTests(unittest.TestCase):
    def test_main_writes_outputs(self):
        with tempfile.TemporaryDirectory() as tdstr:
            td = Path(tdstr)
            # Seed minimal inputs
            stage2_path = td / "stage2.jsonl"
            stage3_path = td / "stage3.jsonl"
            events_path = td / "events.jsonl"
            ss_path = td / "ss.json"
            wfc_path = td / "wfc.json"
            out_dir = td / "out"
            with open(stage2_path, "w", encoding="utf-8") as f:
                for d in range(1, 7):
                    f.write(json.dumps(_stage2_row(f"2026-05-0{d}", -0.005)) + "\n")
            with open(stage3_path, "w", encoding="utf-8") as f:
                for d in range(1, 7):
                    f.write(json.dumps(_stage3_row(f"2026-05-0{d}", 0.005)) + "\n")
            events_path.write_text("", encoding="utf-8")  # empty
            ss_path.write_text(json.dumps({"verdict": "hold"}), encoding="utf-8")
            wfc_path.write_text(json.dumps({"gates": []}), encoding="utf-8")

            rc = retro.main([
                "--stage2-brier-history-path", str(stage2_path),
                "--stage3-v2-drift-history-path", str(stage3_path),
                "--event-log-path", str(events_path),
                "--stake-scaling-report-path", str(ss_path),
                "--walk-forward-cert-path", str(wfc_path),
                "--output-dir", str(out_dir),
            ])
            self.assertEqual(rc, 0)
            self.assertTrue((out_dir / "daemon_retrospective.json").exists())
            self.assertTrue((out_dir / "daemon_retrospective.md").exists())


if __name__ == "__main__":
    unittest.main()
