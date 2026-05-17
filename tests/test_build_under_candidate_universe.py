"""Phase B A5 (2026-05-16): UNDER candidate-universe synthesis.

Verifies:
  - synthesize_under_row flips side/FV/edge correctly
  - Emission criteria: requires under_pair_available + fair_value_raw +
    under_best_ask
  - UNDER calibrator routing applies platt/isotonic when artifact loaded
  - fallback_flip path uses 1 - over_fair_value_calibrated when no
    UNDER calibrator is present
  - End-to-end: read OVER candidate jsonl, write UNDER sibling
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

import build_under_candidate_universe as bucu  # noqa: E402


def _make_over_row(**kw):
    base = {
        "candidate_id": "2026-05-15_1_8.5_000001",
        "ts": "2026-05-15T20:00:00Z",
        "game_pk": 1,
        "away_abbrev": "NYY",
        "home_abbrev": "BOS",
        "line": "8.5",
        "side": "over",
        "signal_model_family": "score_event_transition",
        "inning": 5,
        "outs": 1,
        "runners_on": 1,
        "decision": "trade",
        "decision_reason": "placed_bet",
        "decision_ask": 0.74,
        "best_bid": 0.71,
        "over_best_ask": 0.74,
        "over_best_bid": 0.71,
        "under_pair_available": True,
        "under_best_bid": 0.26,
        "under_best_ask": 0.29,
        "under_spread": 0.03,
        "fair_value_raw": 0.85,
        "fair_value": 0.78,
        "fair_value_calibrated": 0.78,
        "fair_value_calibration_method": "platt",
        "fair_value_calibration_family": "score_event_transition",
        "edge": 0.11,
    }
    base.update(kw)
    return base


class SynthesizeUnderRowCriteriaTests(unittest.TestCase):
    def test_emits_when_all_criteria_met(self):
        row = _make_over_row()
        out = bucu.synthesize_under_row(row)
        self.assertIsNotNone(out)
        self.assertEqual(out["side"], "under")
        self.assertEqual(out["decision"], "shadow_under")
        self.assertEqual(out["decision_reason"], "shadow_under_emission")

    def test_skips_when_under_pair_unavailable(self):
        row = _make_over_row(under_pair_available=False)
        self.assertIsNone(bucu.synthesize_under_row(row))

    def test_skips_when_fair_value_raw_missing(self):
        row = _make_over_row(fair_value_raw=None)
        self.assertIsNone(bucu.synthesize_under_row(row))

    def test_skips_when_under_best_ask_missing(self):
        row = _make_over_row(under_best_ask=None)
        self.assertIsNone(bucu.synthesize_under_row(row))

    def test_skips_when_fair_value_raw_out_of_bounds(self):
        for bad in (0.0, 1.0, -0.5, 1.5):
            row = _make_over_row(fair_value_raw=bad)
            self.assertIsNone(
                bucu.synthesize_under_row(row),
                f"should skip fair_value_raw={bad}",
            )


class SynthesizeUnderRowFlipTests(unittest.TestCase):
    def test_decision_ask_uses_under_best_ask_not_over(self):
        row = _make_over_row(decision_ask=0.74, under_best_ask=0.29)
        out = bucu.synthesize_under_row(row)
        self.assertEqual(out["decision_ask"], 0.29)

    def test_best_bid_uses_under_best_bid(self):
        row = _make_over_row(best_bid=0.71, under_best_bid=0.26)
        out = bucu.synthesize_under_row(row)
        self.assertEqual(out["best_bid"], 0.26)

    def test_fair_value_raw_is_one_minus_over(self):
        row = _make_over_row(fair_value_raw=0.85)
        out = bucu.synthesize_under_row(row)
        self.assertAlmostEqual(out["fair_value_raw"], 0.15, places=6)

    def test_fallback_flip_uses_one_minus_over_calibrated(self):
        """No UNDER calibrator: fair_value_calibrated falls back to
        (1 - over_fair_value_calibrated) to preserve OVER's
        calibration adjustment."""
        row = _make_over_row(fair_value_raw=0.85, fair_value_calibrated=0.78)
        out = bucu.synthesize_under_row(row, under_calibrator=None)
        # Expect under_calibrated = 1 - 0.78 = 0.22
        self.assertAlmostEqual(out["fair_value_calibrated"], 0.22, places=6)
        self.assertEqual(out["fair_value_calibration_method"], "fallback_flip")
        self.assertEqual(out["fair_value_calibration_side"], "under")
        self.assertFalse(out["fair_value_calibration_applied"])

    def test_edge_is_under_calibrated_minus_under_ask(self):
        row = _make_over_row(
            fair_value_raw=0.85,
            fair_value_calibrated=0.78,
            under_best_ask=0.20,
        )
        out = bucu.synthesize_under_row(row, under_calibrator=None)
        # fallback flip: under_calibrated = 1 - 0.78 = 0.22; edge = 0.22 - 0.20 = 0.02
        self.assertAlmostEqual(out["edge"], 0.02, places=6)

    def test_candidate_id_uses_under_suffix(self):
        row = _make_over_row(candidate_id="abc")
        out = bucu.synthesize_under_row(row)
        self.assertEqual(out["candidate_id"], "abc__under")

    def test_over_source_fields_preserved(self):
        row = _make_over_row(
            candidate_id="abc",
            decision="trade",
            fair_value_calibrated=0.78,
            decision_ask=0.74,
            edge=0.11,
        )
        out = bucu.synthesize_under_row(row)
        self.assertEqual(out["over_source_candidate_id"], "abc")
        self.assertEqual(out["over_source_decision"], "trade")
        self.assertEqual(out["over_source_fair_value_calibrated"], 0.78)
        self.assertEqual(out["over_source_decision_ask"], 0.74)
        self.assertEqual(out["over_source_edge"], 0.11)

    def test_bet_id_is_cleared(self):
        """UNDER candidates are shadow only; they should never carry
        a bet_id (which would imply a placed order)."""
        row = _make_over_row(bet_id="some_bet_id")
        out = bucu.synthesize_under_row(row)
        self.assertIsNone(out["bet_id"])

    def test_game_state_context_preserved(self):
        """Game-state fields (inning, scores, regime, weather) are
        side-agnostic and must carry over unchanged."""
        row = _make_over_row(
            inning=5, outs=1, runners_on=2,
            away_abbrev="NYY", home_abbrev="BOS", line="8.5",
            game_pk=1,
        )
        out = bucu.synthesize_under_row(row)
        self.assertEqual(out["inning"], 5)
        self.assertEqual(out["outs"], 1)
        self.assertEqual(out["runners_on"], 2)
        self.assertEqual(out["away_abbrev"], "NYY")
        self.assertEqual(out["game_pk"], 1)
        self.assertEqual(out["line"], "8.5")


class UnderCalibratorRoutingTests(unittest.TestCase):
    def test_platt_calibrator_applied(self):
        # Platt with a=0, b=2 (steeper): 0.15 -> sigmoid(2 * logit(0.15))
        #   logit(0.15) ≈ -1.7346; sigmoid(-3.469) ≈ 0.0302
        cal = {
            "families": {
                "score_event_transition": {
                    "selected_method": "platt",
                    "methods": {"platt": {"params": {"a": 0.0, "b": 2.0}}},
                },
            },
        }
        out = bucu.calibrate_under(0.15, "score_event_transition", cal)
        self.assertEqual(out["calibration_method"], "platt")
        self.assertTrue(out["calibration_applied"])
        self.assertAlmostEqual(out["fair_value_calibrated"], 0.0302, places=3)
        self.assertEqual(out["calibration_side"], "under")

    def test_isotonic_calibrator_applied(self):
        cal = {
            "families": {
                "score_event_transition": {
                    "selected_method": "isotonic",
                    "methods": {
                        "isotonic": {
                            "params": {
                                "knots": [[0.0, 0.0], [0.5, 0.4], [1.0, 1.0]],
                            },
                        },
                    },
                },
            },
        }
        out = bucu.calibrate_under(0.25, "score_event_transition", cal)
        self.assertEqual(out["calibration_method"], "isotonic")
        self.assertTrue(out["calibration_applied"])
        # Interpolate between (0, 0) and (0.5, 0.4): 0.25 -> 0.2
        self.assertAlmostEqual(out["fair_value_calibrated"], 0.2, places=3)

    def test_missing_family_falls_back_to_identity(self):
        """Family not present in artifact (e.g. only one family had
        enough rows). Should pass through raw + mark not-applied."""
        cal = {
            "families": {
                "score_event_transition": {
                    "selected_method": "platt",
                    "methods": {"platt": {"params": {"a": 0.0, "b": 2.0}}},
                },
            },
        }
        out = bucu.calibrate_under(0.50, "no_score_drift", cal)
        self.assertEqual(out["calibration_method"], "identity")
        self.assertFalse(out["calibration_applied"])
        self.assertAlmostEqual(out["fair_value_calibrated"], 0.50)

    def test_no_calibrator_signals_fallback_flip(self):
        out = bucu.calibrate_under(0.15, "score_event_transition", None)
        self.assertEqual(out["calibration_method"], "fallback_flip")
        self.assertFalse(out["calibration_applied"])

    def test_platt_calibrator_used_in_synthesis(self):
        """End-to-end: a synthesized UNDER row uses the UNDER Platt
        calibrator instead of the fallback_flip path."""
        cal = {
            "families": {
                "score_event_transition": {
                    "selected_method": "platt",
                    "methods": {"platt": {"params": {"a": 0.0, "b": 1.0}}},
                },
            },
        }
        row = _make_over_row(
            fair_value_raw=0.85,
            fair_value_calibrated=0.78,
            under_best_ask=0.20,
        )
        out = bucu.synthesize_under_row(row, under_calibrator=cal)
        self.assertEqual(out["fair_value_calibration_method"], "platt")
        self.assertTrue(out["fair_value_calibration_applied"])
        # With a=0, b=1 platt is identity, so under_calibrated == 0.15
        self.assertAlmostEqual(out["fair_value_calibrated"], 0.15, places=3)


class EndToEndSynthesisTests(unittest.TestCase):
    def _write_candidates(self, path: Path, rows):
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_build_for_mode_writes_under_files(self):
        with tempfile.TemporaryDirectory() as td:
            over_root = Path(td) / "candidate_universe"
            over_root.mkdir(parents=True)
            # Two dates of OVER candidates, mix of paired + unpaired rows
            self._write_candidates(
                over_root / "2026-05-14_candidates.jsonl",
                [
                    _make_over_row(candidate_id="a", fair_value_raw=0.7),
                    _make_over_row(candidate_id="b", under_pair_available=False),
                    _make_over_row(candidate_id="c", fair_value_raw=0.6),
                ],
            )
            self._write_candidates(
                over_root / "2026-05-15_candidates.jsonl",
                [_make_over_row(candidate_id="d", fair_value_raw=0.55)],
            )

            manifest = bucu.build_for_mode(
                over_root, min_date="", max_date="",
                under_calibrator=None,
            )

            # Two files emitted, one per OVER date
            self.assertEqual(len(manifest["files"]), 2)
            self.assertEqual(manifest["total_over_rows"], 4)
            # Three OVER rows met criteria (a, c, d); one (b) did not
            self.assertEqual(manifest["total_under_rows"], 3)

            day1_under = over_root / "2026-05-14_under_candidates.jsonl"
            self.assertTrue(day1_under.exists())
            day1_rows = [json.loads(line) for line in
                         day1_under.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(day1_rows), 2)
            self.assertEqual(set(r["side"] for r in day1_rows), {"under"})
            self.assertEqual(
                set(r["over_source_candidate_id"] for r in day1_rows),
                {"a", "c"},
            )

    def test_date_filters_honored(self):
        with tempfile.TemporaryDirectory() as td:
            over_root = Path(td) / "candidate_universe"
            over_root.mkdir(parents=True)
            for d in ("2026-05-10", "2026-05-14", "2026-05-15"):
                self._write_candidates(
                    over_root / f"{d}_candidates.jsonl",
                    [_make_over_row(candidate_id=f"r-{d}", fair_value_raw=0.5)],
                )

            manifest = bucu.build_for_mode(
                over_root, min_date="2026-05-12", max_date="2026-05-14",
                under_calibrator=None,
            )
            self.assertEqual(len(manifest["files"]), 1)
            self.assertEqual(manifest["files"][0]["date"], "2026-05-14")

    def test_does_not_recursively_process_emitted_under_files(self):
        """Subsequent runs must skip the previously-emitted
        `*_under_candidates.jsonl` files, not glob them as input."""
        with tempfile.TemporaryDirectory() as td:
            over_root = Path(td) / "candidate_universe"
            over_root.mkdir(parents=True)
            self._write_candidates(
                over_root / "2026-05-14_candidates.jsonl",
                [_make_over_row(candidate_id="a", fair_value_raw=0.7)],
            )
            # First run produces the UNDER sibling
            bucu.build_for_mode(over_root, min_date="", max_date="",
                                under_calibrator=None)
            self.assertTrue(
                (over_root / "2026-05-14_under_candidates.jsonl").exists()
            )
            # Second run: should NOT pick up the emitted UNDER file as
            # input (it would treat it as an OVER source).
            manifest = bucu.build_for_mode(over_root, min_date="", max_date="",
                                           under_calibrator=None)
            self.assertEqual(len(manifest["files"]), 1)
            self.assertEqual(manifest["files"][0]["date"], "2026-05-14")


if __name__ == "__main__":
    unittest.main()
