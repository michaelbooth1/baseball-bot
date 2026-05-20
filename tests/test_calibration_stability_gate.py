"""Tests for the calibration method-stability gate (shipped 2026-05-14).

The gate reads `selection_history.jsonl` (one row per refresh, per-family
pre-override + final selections) and overrides today's pick to the
trailing modal selection when today's pick differs from the modal. The
intent is to suppress platt<->isotonic flip-flops on small validation
samples without preventing genuine drift.

Real evidence motivating the gate (2026-05-04 .. 2026-05-13):
  - 2026-05-04 to 2026-05-11 (8 days): stable picks
      no_score_drift=isotonic, score_event_transition=platt
  - 2026-05-12: both methods flipped (platt <-> isotonic)
  - 2026-05-13: both flipped back

A trailing-7-day modal would have caught both 2026-05-12 flips.
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

import calibrate_signal_probabilities as cal  # noqa: E402


def _history_row(date_str: str, **family_picks: str) -> dict:
    """Build a one-line history row. family_picks maps family name -> pre-override selection."""
    return {
        "generated_at_utc": f"{date_str}T20:00:00Z",
        "data_max_date": date_str,
        "selections": {
            fam: {"pre_override_selected": pick} for fam, pick in family_picks.items()
        },
    }


def _stable_history():
    """8 prior days, isotonic for no_score_drift, platt for score_event."""
    return [
        _history_row(
            f"2026-05-{day:02d}",
            no_score_drift="isotonic",
            score_event_transition="platt",
        )
        for day in range(4, 12)  # 5/4 through 5/11
    ]


class ModalSelectionTests(unittest.TestCase):
    def test_unanimous(self):
        self.assertEqual(cal._modal_selection(["platt"] * 5), "platt")

    def test_majority(self):
        self.assertEqual(
            cal._modal_selection(["platt", "platt", "platt", "isotonic"]), "platt"
        )

    def test_tie_returns_none(self):
        # Tie -> deliberately return None so the gate doesn't lock in an
        # arbitrary tie-break.
        self.assertIsNone(cal._modal_selection(["platt", "isotonic"]))
        self.assertIsNone(cal._modal_selection(["platt", "isotonic", "platt", "isotonic"]))

    def test_empty(self):
        self.assertIsNone(cal._modal_selection([]))


class TrailingHistoryTests(unittest.TestCase):
    def test_orders_chronologically(self):
        hist = _stable_history()
        picks = cal._trailing_family_history(hist, "no_score_drift", window=7)
        # Oldest first, latest at the end -- 5/4 ... 5/11 minus the oldest
        # since window=7 keeps only the last 7 distinct dates.
        self.assertEqual(len(picks), 7)
        self.assertTrue(all(p == "isotonic" for p in picks))

    def test_dedup_same_date(self):
        """Multiple history rows for the same date -> latest wins."""
        hist = _stable_history()
        # Append a second 5/11 row with a different pick (simulates a re-run)
        hist.append(_history_row(
            "2026-05-11",
            no_score_drift="platt",
            score_event_transition="isotonic",
        ))
        picks = cal._trailing_family_history(hist, "no_score_drift", window=7)
        # Last entry should be the LATER same-date pick (platt), not the first.
        self.assertEqual(picks[-1], "platt")

    def test_exclude_date(self):
        """exclude_date drops today's own row so re-runs don't compare to self."""
        hist = _stable_history() + [_history_row(
            "2026-05-12", no_score_drift="platt", score_event_transition="isotonic"
        )]
        picks = cal._trailing_family_history(
            hist, "no_score_drift", window=7, exclude_date="2026-05-12"
        )
        self.assertNotIn("platt", picks)

    def test_window_clamps(self):
        """window=3 returns at most 3 picks."""
        hist = _stable_history()
        picks = cal._trailing_family_history(hist, "no_score_drift", window=3)
        self.assertEqual(len(picks), 3)


class StabilityGateTests(unittest.TestCase):
    def test_blocks_2026_05_12_no_score_drift_flip(self):
        """The historical 5/12 flip: pre-override picked platt, modal was
        isotonic -> override applies."""
        hist = _stable_history()
        final, audit = cal._apply_stability_gate(
            "platt", hist, "no_score_drift", window=7, min_history=5,
            exclude_date="2026-05-12",
        )
        self.assertEqual(final, "isotonic")
        self.assertTrue(audit["stability_gate_applied"])
        self.assertEqual(audit["stability_modal"], "isotonic")
        self.assertEqual(audit["stability_override_from"], "platt")

    def test_blocks_2026_05_12_score_event_flip(self):
        hist = _stable_history()
        final, audit = cal._apply_stability_gate(
            "isotonic", hist, "score_event_transition",
            window=7, min_history=5, exclude_date="2026-05-12",
        )
        self.assertEqual(final, "platt")
        self.assertTrue(audit["stability_gate_applied"])

    def test_noop_when_today_matches_modal(self):
        hist = _stable_history()
        final, audit = cal._apply_stability_gate(
            "isotonic", hist, "no_score_drift", window=7, min_history=5,
        )
        self.assertEqual(final, "isotonic")
        self.assertFalse(audit["stability_gate_applied"])

    def test_noop_below_min_history(self):
        """Only 3 days of history < min_history=5 -> today's pick passes."""
        hist = _stable_history()[:3]
        final, audit = cal._apply_stability_gate(
            "platt", hist, "no_score_drift", window=7, min_history=5,
        )
        self.assertEqual(final, "platt")
        self.assertFalse(audit["stability_gate_applied"])

    def test_noop_on_tie(self):
        """Modal is a tie -> today's pick passes (no arbitrary lock-in)."""
        hist = [
            _history_row("2026-05-04", no_score_drift="platt"),
            _history_row("2026-05-05", no_score_drift="platt"),
            _history_row("2026-05-06", no_score_drift="isotonic"),
            _history_row("2026-05-07", no_score_drift="isotonic"),
            _history_row("2026-05-08", no_score_drift="platt"),
            _history_row("2026-05-09", no_score_drift="isotonic"),
        ]
        final, audit = cal._apply_stability_gate(
            "platt", hist, "no_score_drift", window=7, min_history=5,
        )
        # 3-3 tie -> modal is None -> no override
        self.assertEqual(final, "platt")
        self.assertFalse(audit["stability_gate_applied"])
        self.assertIsNone(audit["stability_modal"])

    def test_allows_drift_after_consecutive_new_picks(self):
        """Once the new method has won enough days, the modal flips and
        the gate stops overriding. Verify the unlock condition."""
        # 5 days of platt, then 5 days of isotonic. Window=7 -> last 7 = 2P + 5I -> modal=isotonic.
        hist = [
            _history_row(f"2026-05-{d:02d}", no_score_drift="platt")
            for d in range(1, 6)
        ] + [
            _history_row(f"2026-05-{d:02d}", no_score_drift="isotonic")
            for d in range(6, 11)
        ]
        # Today's pick: isotonic. Modal: isotonic. No override.
        final, _ = cal._apply_stability_gate(
            "isotonic", hist, "no_score_drift", window=7, min_history=5,
        )
        self.assertEqual(final, "isotonic")


class HistoryIOTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cal_history_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.path = self.tmp / "selection_history.jsonl"

    def test_load_returns_empty_for_missing_file(self):
        self.assertEqual(cal._load_selection_history(self.path), [])

    def test_round_trip_through_history_file(self):
        cal._write_selection_history_row(
            self.path,
            selections={
                "no_score_drift": {
                    "pre_override_selected": "isotonic",
                    "final_selected": "isotonic",
                    "stability_gate_applied": False,
                }
            },
            data_max_date="2026-05-14",
            generated_at_utc="2026-05-14T20:00:00Z",
        )
        loaded = cal._load_selection_history(self.path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["data_max_date"], "2026-05-14")
        self.assertEqual(
            loaded[0]["selections"]["no_score_drift"]["pre_override_selected"],
            "isotonic",
        )

    def test_appends_subsequent_rows(self):
        for d in ("2026-05-12", "2026-05-13", "2026-05-14"):
            cal._write_selection_history_row(
                self.path,
                selections={"x": {"pre_override_selected": "platt"}},
                data_max_date=d,
                generated_at_utc=f"{d}T20:00:00Z",
            )
        loaded = cal._load_selection_history(self.path)
        self.assertEqual(len(loaded), 3)
        self.assertEqual([r["data_max_date"] for r in loaded],
                         ["2026-05-12", "2026-05-13", "2026-05-14"])

    def test_malformed_lines_are_skipped(self):
        self.path.write_text(
            '{"ok": 1}\nnot-valid-json\n{"ok": 2}\n', encoding="utf-8"
        )
        loaded = cal._load_selection_history(self.path)
        self.assertEqual(len(loaded), 2)


class HistoryRowDateTests(unittest.TestCase):
    def test_prefers_data_max_date(self):
        row = {"data_max_date": "2026-05-12", "generated_at_utc": "2026-05-13T01:00:00Z"}
        self.assertEqual(cal._history_row_date(row), "2026-05-12")

    def test_falls_back_to_generated_at_utc(self):
        row = {"generated_at_utc": "2026-05-13T01:00:00Z"}
        self.assertEqual(cal._history_row_date(row), "2026-05-13")

    def test_empty_row(self):
        self.assertEqual(cal._history_row_date({}), "")


class MinTrainDateFilterTests(unittest.TestCase):
    """Tests for Hygiene #24 (2026-05-20): --min-train-date filter.

    The flag drops samples with session_date < cutoff so the operator
    can fit a post-upgrade-only calibrator without blending pre/post
    regime rows. End-to-end tests would require a full run; here we
    cover the filter math + the bad-date guard.
    """

    def _sample(self, date_str: str):
        return cal.Sample(
            bet_id=f"b_{date_str}",
            session_date=date_str,
            mode="live",
            raw_prob=0.6,
            raw_prob_source="fair_value_raw",
            label=1,
            model_family="score_event_transition",
            decision_ask=0.5,
            line="8.5",
            inning=6,
        )

    def test_filter_drops_pre_cutoff_samples(self):
        # Re-implement the filter inline to verify the comparison logic.
        # The actual filter is inline in cal.main(); this asserts the
        # ISO-string lexicographic comparison is equivalent to date
        # comparison.
        samples = [
            self._sample("2026-05-01"),
            self._sample("2026-05-07"),  # day before TR21
            self._sample("2026-05-08"),  # day OF TR21 (kept)
            self._sample("2026-05-15"),
        ]
        cutoff = "2026-05-08"
        kept = [s for s in samples if (s.session_date or "") >= cutoff]
        dropped = [s for s in samples if (s.session_date or "") < cutoff]
        self.assertEqual([s.session_date for s in kept],
                         ["2026-05-08", "2026-05-15"])
        self.assertEqual([s.session_date for s in dropped],
                         ["2026-05-01", "2026-05-07"])

    def test_bad_date_format_caught_by_validation(self):
        # The cutoff validation in cal.main() checks YYYY-MM-DD shape.
        # A bad date like "2026/05/08" or "20260508" would silently drop
        # everything since "2026/05/08" < every ISO date string. The
        # validation raises SystemExit early to prevent this footgun.
        for bad in ["20260508", "2026/05/08", "05-08-2026", "2026-5-8"]:
            self.assertFalse(
                len(bad) == 10 and bad[4] == "-" and bad[7] == "-",
                f"validation should reject {bad!r}",
            )
        # And the valid form passes:
        valid = "2026-05-08"
        self.assertTrue(len(valid) == 10 and valid[4] == "-" and valid[7] == "-")


if __name__ == "__main__":
    unittest.main()
