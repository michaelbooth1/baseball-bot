"""Tests for human_review.fleet_health._fleet_paired_delta_health.

2026-06-11. The paired-delta block computes per-preset shared/unique
bet cohorts vs the A_current baseline so fleet conclusions surface in
the daily review. The 2026-06-10 fleet audit found three presets whose
conclusions (E/G: edge floor locally optimal; N: null experiment) had
been sitting unread for ~2 weeks because only marginal per-engine
tables were published -- this block exists so that never recurs.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
for _dir in (
    PROJECT_DIR / "scripts" / "analysis",
    PROJECT_DIR / "scripts" / "trading",
):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

from human_review.fleet_health import (  # noqa: E402
    _fleet_paired_delta_health,
    _welch_t,
)

ANCHOR = "2026-06-30"


def _bet(game_pk, line, inning, won, profit, side="over", stake=10.0):
    return {
        "game_pk": game_pk,
        "line": str(line),
        "side": side,
        "inning": inning,
        "settled": True,
        "won": won,
        "profit": profit,
        "stake": stake,
    }


def _write_session(root: Path, label: str, date: str, bets):
    sessions = root / f"paper_{label}" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    payload = {"date": date, "bets": bets}
    (sessions / f"{date}_session.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _dates_back(n):
    from datetime import datetime, timedelta
    anchor = datetime.strptime(ANCHOR, "%Y-%m-%d")
    return [
        (anchor - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)
    ]


class FleetPairedDeltaTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, **kwargs):
        kwargs.setdefault("data_root", self.root)
        kwargs.setdefault("session_date", ANCHOR)
        return _fleet_paired_delta_health(**kwargs)

    # ------------------------------------------------------------------
    # Structural statuses
    # ------------------------------------------------------------------

    def test_no_baseline_sessions(self):
        _write_session(self.root, "X_variant", ANCHOR, [])
        payload = self._run()
        self.assertEqual(payload["status"], "no_baseline_sessions")

    def test_no_fleet_roots(self):
        _write_session(self.root, "A_current", ANCHOR, [])
        payload = self._run()
        self.assertEqual(payload["status"], "no_fleet_roots")

    def test_unparseable_date_is_check_error(self):
        payload = self._run(session_date="garbage")
        self.assertEqual(payload["status"], "check_error")

    def test_excluded_legacy_root_is_skipped(self):
        _write_session(self.root, "A_current", ANCHOR, [])
        # data/paper_trading is the legacy root, not a fleet preset.
        _write_session(self.root, "trading", ANCHOR, [])
        payload = self._run(excluded_root_names=("paper_trading",))
        self.assertEqual(payload["status"], "no_fleet_roots")

    def test_execution_sensitive_flag_set_for_fill_dependent_presets(self):
        """T6: F_no_dedup / M_under_paper deltas depend on fill behaviour
        paper overstates -> flagged execution_sensitive; FV-level arms are
        not."""
        shared = _bet(100, 7.5, 6, True, 5.0)
        _write_session(self.root, "A_current", ANCHOR, [shared])
        _write_session(self.root, "F_no_dedup", ANCHOR,
                       [shared, _bet(200, 8.5, 7, True, 6.0)])
        _write_session(self.root, "B_cal_only", ANCHOR,
                       [shared, _bet(300, 9.5, 8, False, -10.0)])
        eng = self._run()["engines"]
        self.assertTrue(eng["F_no_dedup"]["execution_sensitive"])
        self.assertFalse(eng["B_cal_only"]["execution_sensitive"])

    def test_retired_presets_excluded_by_default(self):
        """Retired fleet presets (E/G/N, removed from the launcher
        PRESETS on 2026-06-10) keep their data/paper_<label> root until
        sessions age out of the 30d window. They must NOT be compared by
        default -- otherwise N_extreme_edge_022 keeps firing its DEAD
        alert for ~30 days after retirement (the exact 2026-06-13 noise
        this exclusion removes)."""
        shared = _bet(100, 7.5, 6, True, 5.0)
        _write_session(self.root, "A_current", ANCHOR, [shared])
        _write_session(self.root, "N_extreme_edge_022", ANCHOR, [shared])
        _write_session(
            self.root, "Z_live_variant", ANCHOR,
            [shared, _bet(400, 9.5, 8, False, -10.0)],
        )
        payload = self._run()  # default excluded_root_names
        self.assertIn("Z_live_variant", payload["engines"])
        self.assertNotIn("N_extreme_edge_022", payload["engines"])

    # ------------------------------------------------------------------
    # Shared / unique arithmetic
    # ------------------------------------------------------------------

    def test_shared_and_unique_bet_split(self):
        shared = _bet(100, 7.5, 6, True, 5.0)
        a_only = _bet(200, 8.5, 7, True, 6.0)
        x_only = _bet(300, 9.5, 5, False, -10.0)
        _write_session(self.root, "A_current", ANCHOR, [shared, a_only])
        _write_session(self.root, "X_variant", ANCHOR, [shared, x_only])
        payload = self._run()
        e = payload["engines"]["X_variant"]
        self.assertEqual(e["shared_days"], 1)
        self.assertEqual(e["n_shared_bets"], 1)
        self.assertEqual(e["only_engine"]["n"], 1)
        self.assertEqual(e["only_baseline"]["n"], 1)
        self.assertAlmostEqual(e["delta_net_pnl"], -10.0 - 6.0)

    def test_same_signal_different_bet_ids_counts_as_shared(self):
        """Engines assign independent bet_id counters; identity is
        (date, game_pk, line, side, inning)."""
        a = dict(_bet(100, 7.5, 6, True, 5.0), bet_id="2026_a_0001")
        x = dict(_bet(100, 7.5, 6, True, 5.0), bet_id="2026_x_0007")
        _write_session(self.root, "A_current", ANCHOR, [a])
        _write_session(self.root, "X_variant", ANCHOR, [x])
        e = self._run()["engines"]["X_variant"]
        self.assertEqual(e["n_shared_bets"], 1)
        self.assertEqual(e["n_delta_bets"], 0)

    def test_under_and_over_same_line_are_distinct(self):
        a = _bet(100, 7.5, 6, True, 5.0, side="over")
        x = _bet(100, 7.5, 6, False, -10.0, side="under")
        _write_session(self.root, "A_current", ANCHOR, [a])
        _write_session(self.root, "X_variant", ANCHOR, [x])
        e = self._run()["engines"]["X_variant"]
        self.assertEqual(e["n_shared_bets"], 0)
        self.assertEqual(e["only_engine"]["n"], 1)
        self.assertEqual(e["only_baseline"]["n"], 1)

    def test_day_without_engine_session_not_shared(self):
        """A date where only the baseline ran contributes nothing --
        comparing bets across different game days would be noise."""
        d1, d2 = _dates_back(2)
        _write_session(self.root, "A_current", d1, [_bet(1, 7.5, 6, True, 5.0)])
        _write_session(self.root, "A_current", d2, [_bet(2, 7.5, 6, True, 5.0)])
        _write_session(self.root, "X_variant", d1, [_bet(1, 7.5, 6, True, 5.0)])
        e = self._run()["engines"]["X_variant"]
        self.assertEqual(e["shared_days"], 1)
        self.assertEqual(e["only_baseline"]["n"], 0)  # d2's bet not counted

    def test_engine_with_no_overlap_is_omitted(self):
        from datetime import datetime, timedelta
        old = (
            datetime.strptime(ANCHOR, "%Y-%m-%d") - timedelta(days=400)
        ).strftime("%Y-%m-%d")
        _write_session(self.root, "A_current", ANCHOR, [])
        _write_session(self.root, "Retired_X", old, [_bet(1, 7.5, 6, True, 5.0)])
        payload = self._run()
        self.assertNotIn("Retired_X", payload["engines"])

    # ------------------------------------------------------------------
    # Verdict ladder
    # ------------------------------------------------------------------

    def test_dead_verdict_for_zero_delta_flow(self):
        """N_extreme_edge_022 signature: >=7 shared days, identical
        bet sets, zero delta decisions -> DEAD + alert."""
        for d in _dates_back(8):
            bets = [_bet(100, 7.5, 6, True, 5.0)]
            _write_session(self.root, "A_current", d, bets)
            _write_session(self.root, "N_clone", d, list(bets))
        payload = self._run()
        e = payload["engines"]["N_clone"]
        self.assertEqual(e["verdict"], "DEAD")
        self.assertTrue(
            any("N_clone DEAD" in a for a in payload["alerts"]),
            payload["alerts"],
        )

    def test_collecting_when_sample_tiny(self):
        _write_session(self.root, "A_current", ANCHOR, [_bet(1, 7.5, 6, True, 5.0)])
        _write_session(
            self.root, "X_variant", ANCHOR,
            [_bet(1, 7.5, 6, True, 5.0), _bet(2, 8.5, 6, True, 6.0)],
        )
        e = self._run()["engines"]["X_variant"]
        self.assertEqual(e["verdict"], "COLLECTING")

    def test_conclusive_positive_fires_alert(self):
        """Engine's unique bets consistently profit while baseline's
        unique bets consistently lose -> CONCLUSIVE_POSITIVE."""
        dates = _dates_back(10)
        game = 1
        for d in dates:
            x_uniques = [
                _bet(game + i, 7.5, 6, True, 5.0 + 0.1 * i) for i in range(3)
            ]
            a_uniques = [
                _bet(game + 100 + i, 8.5, 6, False, -10.0 + 0.1 * i)
                for i in range(3)
            ]
            _write_session(self.root, "A_current", d, a_uniques)
            _write_session(self.root, "X_variant", d, x_uniques)
            game += 200
        payload = self._run()
        e = payload["engines"]["X_variant"]
        self.assertEqual(e["verdict"], "CONCLUSIVE_POSITIVE")
        self.assertEqual(e["days_to_significance"], 0)
        self.assertTrue(
            any("X_variant CONCLUSIVE_POSITIVE" in a for a in payload["alerts"]),
            payload["alerts"],
        )

    def test_conclusive_negative_when_engine_uniques_lose(self):
        dates = _dates_back(10)
        game = 1
        for d in dates:
            x_uniques = [
                _bet(game + i, 7.5, 6, False, -10.0 + 0.1 * i) for i in range(3)
            ]
            a_uniques = [
                _bet(game + 100 + i, 8.5, 6, True, 5.0 + 0.1 * i)
                for i in range(3)
            ]
            _write_session(self.root, "A_current", d, a_uniques)
            _write_session(self.root, "X_variant", d, x_uniques)
            game += 200
        payload = self._run()
        self.assertEqual(
            payload["engines"]["X_variant"]["verdict"], "CONCLUSIVE_NEGATIVE",
        )

    def test_one_sided_additive_preset_gets_one_sample_t(self):
        """F_no_dedup shape: engine strictly ADDS bets, never skips
        any of baseline's. Welch needs both sides; the one-sample
        fallback lets these presets conclude."""
        dates = _dates_back(10)
        game = 1
        for d in dates:
            base = [_bet(game, 7.5, 6, True, 5.0)]
            extra = [
                _bet(game + 1 + i, 9.5, 7, True, 4.0 + 0.2 * i)
                for i in range(3)
            ]
            _write_session(self.root, "A_current", d, base)
            _write_session(self.root, "F_clone", d, base + extra)
            game += 50
        e = self._run()["engines"]["F_clone"]
        self.assertIsNotNone(e["welch_t"])
        self.assertGreater(e["welch_t"], 0)
        self.assertIn(
            e["verdict"], ("CONCLUSIVE_POSITIVE", "TRENDING_POSITIVE"),
        )

    def test_days_to_significance_estimable_while_collecting(self):
        dates = _dates_back(6)
        game = 1
        for d in dates:
            x_uniques = [_bet(game, 7.5, 6, True, 5.0 + game * 0.01)]
            a_uniques = [_bet(game + 100, 8.5, 6, False, -10.0 + game * 0.01)]
            _write_session(self.root, "A_current", d, a_uniques)
            _write_session(self.root, "X_variant", d, x_uniques)
            game += 200
        e = self._run()["engines"]["X_variant"]
        if e["verdict"] not in ("CONCLUSIVE_POSITIVE", "CONCLUSIVE_NEGATIVE"):
            self.assertIsNotNone(e["days_to_significance"])
        else:
            self.assertEqual(e["days_to_significance"], 0)

    # ------------------------------------------------------------------
    # Welch helper edge cases
    # ------------------------------------------------------------------

    def test_welch_t_zero_variance_returns_none(self):
        self.assertIsNone(_welch_t([5.0, 5.0, 5.0], [5.0, 5.0]))

    def test_welch_t_one_sided_sign_convention(self):
        # Engine-only bets profiting -> positive.
        t_pos = _welch_t([5.0, 6.0, 4.0], [])
        self.assertGreater(t_pos, 0)
        # Baseline-only bets profiting (engine skipped winners) -> negative.
        t_neg = _welch_t([], [5.0, 6.0, 4.0])
        self.assertLess(t_neg, 0)

    def test_welch_t_insufficient_data_returns_none(self):
        self.assertIsNone(_welch_t([5.0], []))
        self.assertIsNone(_welch_t([], []))


class BuildReportIntegrationTests(unittest.TestCase):
    """The block must be wired into build_daily_human_review_report."""

    def test_block_wired_into_build_report(self):
        src = (
            PROJECT_DIR / "scripts" / "analysis"
            / "build_daily_human_review_report.py"
        ).read_text(encoding="utf-8")
        self.assertIn("_fleet_paired_delta_health", src)
        self.assertIn(
            '"fleet_paired_delta_health": fleet_paired_delta_health', src,
            "fleet paired-delta block missing from build_report return dict",
        )


if __name__ == "__main__":
    unittest.main()
