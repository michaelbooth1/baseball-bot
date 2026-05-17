"""Phase B B2 (2026-05-16): side field on promote_events.jsonl + side
flag on promote.py CLI.

Tests cover:
  - PromotionEvent dataclass has a `side` field defaulting to "both"
  - to_row() serializes the side
  - The side-asymmetric handlers (stake-scaling, gate-threshold) write
    `side="over"` by default and respect --side under when passed
  - latest_promotion_event_for_lever's optional `side` filter matches
    same-side OR "both" rows (legacy rows + side-symmetric levers)
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import promote  # noqa: E402


class PromotionEventSideFieldTests(unittest.TestCase):
    def test_default_side_is_both(self):
        ev = promote.PromotionEvent(
            lever="stage2", action="promoted", operator="alice",
        )
        self.assertEqual(ev.side, "both")
        row = ev.to_row()
        self.assertEqual(row["side"], "both")

    def test_explicit_over_serializes(self):
        ev = promote.PromotionEvent(
            lever="stake_scaling", action="promoted", operator="alice",
            side="over",
        )
        self.assertEqual(ev.to_row()["side"], "over")

    def test_explicit_under_serializes(self):
        ev = promote.PromotionEvent(
            lever="stake_scaling", action="promoted", operator="alice",
            side="under",
        )
        self.assertEqual(ev.to_row()["side"], "under")


class StakeScalingCliSideTests(unittest.TestCase):
    def _make_args(self, *, td, side="over", verdict="promote"):
        report_path = td / "stake_scaling.json"
        report_path.write_text(
            json.dumps({
                "verdict": verdict,
                "verdict_reason": "ok",
                "n_sessions": 30,
                "thresholds": {"min_sessions": 30},
            }), encoding="utf-8",
        )
        overrides_path = td / "live_overrides.json"
        return SimpleNamespace(
            stake_scaling_report_path=report_path,
            event_log_path=td / "events.jsonl",
            live_overrides_path=overrides_path,
            operator="alice",
            side=side,
            dry_run=True,  # avoid actual mutations
            force=False,
        )

    def test_default_side_over_when_user_omits_flag(self):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            args = self._make_args(td=td, side="over")  # default mimics parser
            rc = promote.cmd_stake_scaling(args)
            self.assertEqual(rc, 0)
            rows = [json.loads(line) for line in
                    (td / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertGreaterEqual(len(rows), 1)
            for row in rows:
                self.assertEqual(row.get("side"), "over")

    def test_side_under_persists_to_audit_log(self):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            args = self._make_args(td=td, side="under")
            rc = promote.cmd_stake_scaling(args)
            self.assertEqual(rc, 0)
            rows = [json.loads(line) for line in
                    (td / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            for row in rows:
                self.assertEqual(row.get("side"), "under")


class LatestPromotionEventSideFilterTests(unittest.TestCase):
    def _events(self):
        return [
            {
                "generated_at_utc": "2026-05-10T01:00:00Z",
                "lever": "stake_scaling", "direction": "promote",
                "action": "promoted", "side": "over",
            },
            {
                "generated_at_utc": "2026-05-12T01:00:00Z",
                "lever": "stake_scaling", "direction": "promote",
                "action": "promoted", "side": "under",
            },
            {
                "generated_at_utc": "2026-05-14T01:00:00Z",
                "lever": "stage2", "direction": "promote",
                "action": "promoted", "side": "both",
            },
            {
                # Legacy row (no side field) — should be treated as "both"
                "generated_at_utc": "2026-05-08T01:00:00Z",
                "lever": "stake_scaling", "direction": "promote",
                "action": "promoted",
            },
        ]

    def test_no_filter_returns_latest(self):
        out = promote.latest_promotion_event_for_lever(
            self._events(), "stake_scaling",
        )
        self.assertEqual(out["generated_at_utc"], "2026-05-12T01:00:00Z")

    def test_over_filter_matches_over_and_both(self):
        out = promote.latest_promotion_event_for_lever(
            self._events(), "stake_scaling", side="over",
        )
        # Most recent matching over: 2026-05-10 (own side), 2026-05-08
        # (legacy both). 2026-05-10 wins.
        self.assertEqual(out["generated_at_utc"], "2026-05-10T01:00:00Z")

    def test_under_filter_matches_under_and_both(self):
        out = promote.latest_promotion_event_for_lever(
            self._events(), "stake_scaling", side="under",
        )
        # Most recent under match: 2026-05-12.
        self.assertEqual(out["generated_at_utc"], "2026-05-12T01:00:00Z")

    def test_both_filter_matches_only_both(self):
        """side='both' filter is strict -- only matches rows whose
        recorded side is exactly 'both' (legacy rows pass since they
        default to both via the missing-field fallback)."""
        out = promote.latest_promotion_event_for_lever(
            self._events(), "stake_scaling", side="both",
        )
        # Only the legacy row matches: 2026-05-08
        self.assertEqual(out["generated_at_utc"], "2026-05-08T01:00:00Z")

    def test_legacy_row_treated_as_both_for_side_filter(self):
        """A row without `side` should match side='over' (because
        'both' implies all sides). Verified via the over-filter test
        when only the legacy row exists."""
        events = [{
            "generated_at_utc": "2026-05-01T01:00:00Z",
            "lever": "stage2", "direction": "promote",
            "action": "promoted",
        }]
        for side in ("over", "under", "both"):
            out = promote.latest_promotion_event_for_lever(
                events, "stage2", side=side,
            )
            self.assertIsNotNone(
                out, f"legacy row should match side='{side}'",
            )

    def test_demote_events_filtered_out(self):
        events = [
            {
                "generated_at_utc": "2026-05-12T01:00:00Z",
                "lever": "stake_scaling", "direction": "demote",
                "action": "demoted", "side": "over",
            },
        ]
        out = promote.latest_promotion_event_for_lever(
            events, "stake_scaling", side="over",
        )
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
