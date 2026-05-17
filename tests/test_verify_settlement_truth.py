"""Active #12 (2026-05-17): settlement-truth verifier tests.

Covers:
  - GameFinalState loader: present / missing / unreadable / not-final
  - expected_won_for_bet math (over/under, .5 lines)
  - verify_bet classification ladder for each result_code
  - build_report aggregation
  - End-to-end main() over synthetic sessions + game JSONs
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

import verify_settlement_truth as vst  # noqa: E402


def _write_game(root: Path, *, game_pk: int, session_date: str,
                home_runs=None, away_runs=None, is_final=True,
                detailed_state=None):
    """Write a synthetic MLB game JSON at the expected path."""
    path = vst.game_json_path(root, game_pk, session_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    abstract = "Final" if is_final else "Live"
    state = detailed_state or ("Final" if is_final else "In Progress")
    payload = {
        "gameData": {
            "status": {
                "abstractGameState": abstract,
                "detailedState": state,
            },
            "teams": {
                "home": {"abbreviation": "BOS"},
                "away": {"abbreviation": "NYY"},
            },
        },
        "liveData": {
            "linescore": {
                "teams": {
                    "home": {"runs": home_runs},
                    "away": {"runs": away_runs},
                },
            },
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _bet(**kw):
    base = {
        "bet_id": "2026-05-15_1_8.5_0001",
        "game_pk": 1,
        "line": "8.5",
        "side": "over",
        "order_status": "filled",
        "settled": True,
        "won": True,
        "profit": 5.0,
        "final_total": 11,
    }
    base.update(kw)
    return base


class GameFinalStateLoadingTests(unittest.TestCase):
    def test_loads_present_final_game(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_game(root, game_pk=1, session_date="2026-05-15",
                        home_runs=5, away_runs=6)
            state = vst.load_game_final_state(root, 1, "2026-05-15")
            self.assertTrue(state.is_final)
            self.assertEqual(state.total_runs, 11)
            self.assertEqual(state.home_runs, 5)
            self.assertEqual(state.away_runs, 6)
            self.assertEqual(state.status_label, "Final")

    def test_returns_missing_when_file_absent(self):
        with tempfile.TemporaryDirectory() as td:
            state = vst.load_game_final_state(Path(td), 1, "2026-05-15")
            self.assertEqual(state.status_label, "missing")
            self.assertFalse(state.is_final)
            self.assertIsNone(state.total_runs)

    def test_returns_unreadable_when_json_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = vst.game_json_path(root, 1, "2026-05-15")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json", encoding="utf-8")
            state = vst.load_game_final_state(root, 1, "2026-05-15")
            self.assertEqual(state.status_label, "unreadable")

    def test_not_final_status_returns_is_final_false(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_game(root, game_pk=1, session_date="2026-05-15",
                        home_runs=0, away_runs=0, is_final=False,
                        detailed_state="In Progress")
            state = vst.load_game_final_state(root, 1, "2026-05-15")
            self.assertFalse(state.is_final)
            self.assertEqual(state.total_runs, 0)


class ExpectedWonMathTests(unittest.TestCase):
    def test_over_wins_when_total_above_line(self):
        self.assertTrue(vst.expected_won_for_bet(
            side="over", line=8.5, total=11,
        ))

    def test_over_loses_when_total_below_line(self):
        self.assertFalse(vst.expected_won_for_bet(
            side="over", line=8.5, total=7,
        ))

    def test_under_wins_when_total_below_line(self):
        self.assertTrue(vst.expected_won_for_bet(
            side="under", line=8.5, total=7,
        ))

    def test_under_loses_when_total_above_line(self):
        self.assertFalse(vst.expected_won_for_bet(
            side="under", line=8.5, total=11,
        ))

    def test_returns_none_when_line_or_total_missing(self):
        self.assertIsNone(vst.expected_won_for_bet(
            side="over", line=None, total=11,
        ))
        self.assertIsNone(vst.expected_won_for_bet(
            side="over", line=8.5, total=None,
        ))


class VerifyBetClassificationTests(unittest.TestCase):
    def _verify(self, bet, *, home_runs=None, away_runs=None,
                is_final=True, status_label="Final",
                missing_file=False, unreadable=False):
        if missing_file:
            state = vst.GameFinalState(
                game_pk=bet.get("game_pk", 1), is_final=False,
                total_runs=None, home_runs=None, away_runs=None,
                status_label="missing", source_path=None,
            )
        elif unreadable:
            state = vst.GameFinalState(
                game_pk=bet.get("game_pk", 1), is_final=False,
                total_runs=None, home_runs=None, away_runs=None,
                status_label="unreadable", source_path="bad",
            )
        else:
            total = (
                home_runs + away_runs
                if home_runs is not None and away_runs is not None
                else None
            )
            state = vst.GameFinalState(
                game_pk=bet.get("game_pk", 1),
                is_final=is_final, total_runs=total,
                home_runs=home_runs, away_runs=away_runs,
                status_label=status_label, source_path="x",
            )
        return vst.verify_bet(bet, state, session_date="2026-05-15")

    def test_ok_when_resolution_matches_and_total_matches(self):
        r = self._verify(_bet(won=True, final_total=11),
                          home_runs=5, away_runs=6)
        self.assertEqual(r.result_code, "ok")

    def test_resolution_mismatch_when_engine_says_won_but_mlb_disagrees(self):
        r = self._verify(_bet(won=True, final_total=11),
                          home_runs=2, away_runs=3)  # actual total = 5
        self.assertEqual(r.result_code, "resolution_mismatch")
        self.assertIn("expected_won=False", r.notes)

    def test_total_mismatch_when_won_agrees_but_total_disagrees(self):
        """Engine total=11 (over wins), MLB total=12 (over still wins).
        Same side of the line -> total_mismatch, NOT
        resolution_mismatch."""
        r = self._verify(_bet(won=True, final_total=11),
                          home_runs=6, away_runs=6)  # actual total = 12
        self.assertEqual(r.result_code, "total_mismatch")
        self.assertIn("engine_total=11", r.notes)

    def test_stale_filled_when_won_none_and_game_final(self):
        r = self._verify(_bet(won=None, settled=False, final_total=None),
                          home_runs=5, away_runs=6)
        self.assertEqual(r.result_code, "stale_filled")

    def test_game_not_final_yet_when_settled_but_mlb_in_progress(self):
        r = self._verify(_bet(won=True, settled=True),
                          home_runs=0, away_runs=0,
                          is_final=False, status_label="Pre-Game")
        self.assertEqual(r.result_code, "game_not_final_yet")
        self.assertIn("Pre-Game", r.notes)

    def test_not_yet_settled_when_in_progress_and_no_won(self):
        """In-progress game + bet not yet settled = expected state,
        not an alert; classified separately for visibility."""
        r = self._verify(_bet(won=None, settled=False),
                          home_runs=2, away_runs=1,
                          is_final=False, status_label="In Progress")
        self.assertEqual(r.result_code, "not_yet_settled")

    def test_missing_mlb_data_when_file_absent(self):
        r = self._verify(_bet(), missing_file=True)
        self.assertEqual(r.result_code, "missing_mlb_data")

    def test_missing_mlb_data_when_file_unreadable(self):
        r = self._verify(_bet(), unreadable=True)
        self.assertEqual(r.result_code, "missing_mlb_data")

    def test_not_filled_for_cancelled_bets(self):
        r = self._verify(_bet(order_status="cancelled", won=None))
        self.assertEqual(r.result_code, "not_filled")

    def test_not_filled_for_error_bets(self):
        r = self._verify(_bet(order_status="error", won=None))
        self.assertEqual(r.result_code, "not_filled")


class UnderSideVerificationTests(unittest.TestCase):
    def test_under_bet_passes_when_under_won(self):
        bet = _bet(side="under", won=True, final_total=7)
        state = vst.GameFinalState(
            game_pk=1, is_final=True, total_runs=7,
            home_runs=3, away_runs=4, status_label="Final",
            source_path="x",
        )
        r = vst.verify_bet(bet, state, session_date="2026-05-15")
        self.assertEqual(r.result_code, "ok")

    def test_under_bet_resolution_mismatch_when_engine_disagrees(self):
        bet = _bet(side="under", won=True, final_total=7)
        state = vst.GameFinalState(
            game_pk=1, is_final=True, total_runs=11,
            home_runs=5, away_runs=6, status_label="Final",
            source_path="x",
        )
        r = vst.verify_bet(bet, state, session_date="2026-05-15")
        self.assertEqual(r.result_code, "resolution_mismatch")


class BuildReportTests(unittest.TestCase):
    def _verif(self, code, *, session_date="2026-05-10", bet_id="b"):
        return vst.BetVerification(
            bet_id=bet_id, session_date=session_date, game_pk=1,
            line=8.5, side="over", order_status="filled",
            settled=True, engine_won=True, engine_final_total=11,
            engine_profit=5.0, mlb_is_final=True, mlb_total_runs=11,
            mlb_home_runs=5, mlb_away_runs=6, expected_won=True,
            result_code=code,
        )

    def test_counts_aggregate_correctly(self):
        rows = [
            self._verif("ok", bet_id="a"),
            self._verif("ok", bet_id="b"),
            self._verif("resolution_mismatch", bet_id="c"),
            self._verif("stale_filled", bet_id="d"),
            self._verif("missing_mlb_data", bet_id="e"),
            self._verif("not_filled", bet_id="f"),
        ]
        report = vst.build_report(rows, today="2026-05-17")
        c = report["counts"]
        self.assertEqual(c["total_bets_seen"], 6)
        self.assertEqual(c["filled_or_settled_total"], 5)
        self.assertEqual(c["ok"], 2)
        self.assertEqual(c["resolution_mismatch"], 1)
        self.assertEqual(c["stale_filled"], 1)
        self.assertEqual(c["missing_mlb_data"], 1)
        self.assertEqual(c["not_filled"], 1)
        # missing share = 1 / 5 filled = 0.20
        self.assertAlmostEqual(report["missing_mlb_data_share"], 0.20)
        # ok share = 2 / 5
        self.assertAlmostEqual(report["ok_share"], 0.40)

    def test_oldest_stale_filled_age_picks_max(self):
        rows = [
            self._verif("stale_filled", bet_id="a", session_date="2026-05-15"),
            self._verif("stale_filled", bet_id="b", session_date="2026-05-10"),
            self._verif("stale_filled", bet_id="c", session_date="2026-05-13"),
        ]
        report = vst.build_report(rows, today="2026-05-17")
        # max age = 2026-05-17 - 2026-05-10 = 7
        self.assertEqual(report["oldest_stale_filled_age_days"], 7)

    def test_empty_input_yields_zero_counts(self):
        report = vst.build_report([], today="2026-05-17")
        self.assertEqual(report["counts"]["total_bets_seen"], 0)
        self.assertIsNone(report["ok_share"])
        self.assertIsNone(report["oldest_stale_filled_age_days"])


class EndToEndMainTests(unittest.TestCase):
    def test_main_writes_outputs_against_synthetic_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sessions = root / "sessions"
            sessions.mkdir()
            games = root / "games"
            out = root / "out"

            # Session 1: one OK bet
            session1 = sessions / "2026-05-15_session.json"
            session1.write_text(json.dumps({
                "session_date": "2026-05-15",
                "bets": [
                    _bet(bet_id="ok_bet", game_pk=1, line="8.5",
                         won=True, final_total=11),
                ],
            }), encoding="utf-8")
            _write_game(games, game_pk=1, session_date="2026-05-15",
                        home_runs=5, away_runs=6)

            # Session 2: one resolution mismatch
            session2 = sessions / "2026-05-16_session.json"
            session2.write_text(json.dumps({
                "session_date": "2026-05-16",
                "bets": [
                    _bet(bet_id="bad_bet", game_pk=2, line="8.5",
                         won=True, final_total=11),
                ],
            }), encoding="utf-8")
            _write_game(games, game_pk=2, session_date="2026-05-16",
                        home_runs=2, away_runs=3)  # total = 5 (under)

            vst.main([
                "--sessions-dir", str(sessions),
                "--games-root", str(games),
                "--output-root", str(out),
                "--mode", "live",
                "--today", "2026-05-17",
            ])

            json_path = out / "settlement_truth_report.json"
            md_path = out / "settlement_truth_report.md"
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            counts = payload["counts"]
            self.assertEqual(counts["ok"], 1)
            self.assertEqual(counts["resolution_mismatch"], 1)
            # Markdown surfaces the mismatch row (by date+game+line)
            md = md_path.read_text(encoding="utf-8")
            self.assertIn("resolution_mismatch", md)
            self.assertIn("game=2", md)
            self.assertIn("2026-05-16", md)
            # JSON exposes the bet_id for drill-in
            self.assertEqual(
                payload["by_result_code"]["resolution_mismatch"][0]["bet_id"],
                "bad_bet",
            )


if __name__ == "__main__":
    unittest.main()
