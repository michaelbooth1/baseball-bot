"""Phase C-paper follow-up (2026-05-27): B4 milestone dashboard tests.

Covers `_under_paper_b4_milestone_health` in
`scripts/analysis/human_review/under_health.py` -- the daily-review
block that tracks the 5 ROADMAP B4 verdict conditions against
ACTUAL `side="under"` paper bets across the trailing 60d window.

Test surfaces:

1. **Per-condition arithmetic**: each of the 5 conditions evaluates
   correctly from synthetic session JSONs (sessions, n_settled,
   ROI, calibration delta, drift alerts).

2. **Verdict ladder**: NOT_EMITTING -> INSUFFICIENT_SESSIONS ->
   INSUFFICIENT_OUTCOMES -> SUB_ZERO_ROI -> CALIBRATION_OFF ->
   DRIFT_ALERT_PERSISTENT -> READY transitions fire at the right
   thresholds.

3. **Cross-root union**: bets present in BOTH paper_root/sessions
   and live_root/sessions for the same date count once per date
   (not double-counted by bet_id).

4. **Drift-alert self-loop guard**: `Under-B4:` alerts written
   yesterday must NOT be counted as `Under-` drift alerts today.

5. **Alert emission**: READY + SUB_ZERO_ROI + CALIBRATION_OFF +
   DRIFT_ALERT_PERSISTENT all emit Notes alerts at >=30 settled;
   quiet states (NOT_EMITTING, INSUFFICIENT_*) stay silent.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List


PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


from scripts.analysis.human_review.under_health import (  # noqa: E402
    _aggregate_paper_under_bets,
    _collect_paper_under_bets_for_date,
    _count_persistent_under_drift_alerts,
    _under_paper_b4_milestone_health,
)


# ----------------------------------------------------------------------
# Fixture helpers
# ----------------------------------------------------------------------

ANCHOR = "2026-06-30"  # session_date the milestone walks back from


def _under_bet(
    *,
    bet_id: str,
    game_pk: int = 12345,
    line: str = "8.5",
    entry_ask: float = 0.50,
    fair_value: float = 0.62,
    stake: float = 10.0,
    settled: bool = True,
    won: bool = True,
) -> Dict[str, Any]:
    """One synthetic UNDER bet record."""
    profit = (stake * (1.0 / entry_ask - 1.0)) if won else -stake
    return {
        "bet_id": bet_id,
        "side": "under",
        "game_pk": game_pk,
        "line": line,
        "entry_ask": entry_ask,
        "fair_value": fair_value,
        "stake": stake,
        "settled": settled,
        "won": won if settled else None,
        "profit": round(profit, 2) if settled else None,
    }


def _over_bet(bet_id: str) -> Dict[str, Any]:
    """One synthetic OVER bet record (should be IGNORED by the
    milestone tracker)."""
    return {
        "bet_id": bet_id,
        "side": "over",
        "game_pk": 99999,
        "line": "8.5",
        "entry_ask": 0.62,
        "fair_value": 0.78,
        "stake": 10.0,
        "settled": True,
        "won": True,
        "profit": 6.13,
    }


def _write_session(
    sessions_dir: Path, date_str: str, bets: List[Dict[str, Any]],
) -> Path:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{date_str}_session.json"
    path.write_text(
        json.dumps({"session_date": date_str, "bets": bets}),
        encoding="utf-8",
    )
    return path


def _write_review_with_alert(
    output_root: Path, date_str: str, *notes: str,
) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / f"{date_str}_human_review.json"
    path.write_text(
        json.dumps({"session_date": date_str, "notes": list(notes)}),
        encoding="utf-8",
    )
    return path


def _seed_sessions(
    *,
    paper_sessions_dir: Path,
    n_sessions: int,
    bets_per_session: int,
    won_count: int,
    entry_ask: float = 0.50,
    fair_value: float = 0.62,
    anchor: str = ANCHOR,
) -> List[str]:
    """Seed N sequential dates (oldest first) each carrying
    `bets_per_session` UNDER bets, of which `won_count` won."""
    dates: List[str] = []
    anchor_dt = datetime.strptime(anchor, "%Y-%m-%d")
    for offset in range(n_sessions):
        # offset=0 -> anchor; offset=N-1 -> oldest
        dt = anchor_dt - timedelta(days=offset)
        d_str = dt.strftime("%Y-%m-%d")
        dates.append(d_str)
        bets = []
        for i in range(bets_per_session):
            won = i < won_count
            bets.append(_under_bet(
                bet_id=f"{d_str}_b{i}",
                entry_ask=entry_ask,
                fair_value=fair_value,
                won=won,
            ))
        _write_session(paper_sessions_dir, d_str, bets)
    return dates


# ----------------------------------------------------------------------
# Verdict ladder
# ----------------------------------------------------------------------


class B4VerdictLadderTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.paper_dir = self.tmp / "paper_sessions"
        self.live_dir = self.tmp / "live_sessions"
        self.output_root = self.tmp / "daily_human_review"
        self.paper_dir.mkdir()
        self.live_dir.mkdir()
        self.output_root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, **kwargs):
        # extra_paper_sessions_dirs defaults to () here (NOT the
        # production B4_EXTRA_PAPER_SESSION_ROOTS default) so tests
        # stay isolated from the real data/paper_M_under_paper root.
        kwargs.setdefault("extra_paper_sessions_dirs", ())
        # The verdict-ladder tests exercise the underlying ladder logic, which
        # is preserved under the T5 DORMANT short-circuit; default dormant off
        # here so they assert the ladder (a dedicated test covers DORMANT).
        kwargs.setdefault("dormant", False)
        return _under_paper_b4_milestone_health(
            session_date=ANCHOR,
            paper_sessions_dir=self.paper_dir,
            live_sessions_dir=self.live_dir,
            output_root=self.output_root,
            **kwargs,
        )

    def test_not_emitting_when_no_under_bets(self):
        # No session files at all.
        payload = self._run()
        self.assertEqual(payload["status"], "NOT_EMITTING")
        self.assertEqual(
            payload["aggregate"]["n_paper_under_bets_total"], 0,
        )
        self.assertEqual(payload["aggregate"]["n_sessions_with_under_bets"], 0)
        self.assertEqual(payload["alerts"], [])

    def test_not_emitting_ignores_over_only_sessions(self):
        # 5 sessions with only OVER bets -> still NOT_EMITTING.
        for i in range(5):
            dt = datetime.strptime(ANCHOR, "%Y-%m-%d") - timedelta(days=i)
            _write_session(
                self.paper_dir, dt.strftime("%Y-%m-%d"),
                [_over_bet(f"o{i}")],
            )
        payload = self._run()
        self.assertEqual(payload["status"], "NOT_EMITTING")

    def test_insufficient_sessions(self):
        # 5 sessions with paper UNDER bets but B4 requires 60.
        _seed_sessions(
            paper_sessions_dir=self.paper_dir,
            n_sessions=5, bets_per_session=4, won_count=3,
        )
        payload = self._run()
        self.assertEqual(payload["status"], "INSUFFICIENT_SESSIONS")
        self.assertFalse(payload["conditions"]["sessions"]["pass"])
        self.assertEqual(payload["conditions"]["sessions"]["value"], 5)
        self.assertEqual(payload["conditions"]["sessions"]["remaining"], 55)
        # No alert because we don't spam progress as alerts; quiet
        # state is intentional.
        self.assertEqual(payload["alerts"], [])

    def test_insufficient_outcomes_when_sessions_met(self):
        # 60 sessions, each with 1 bet, 1 settled -> sessions pass
        # but n_settled = 60 < 150.
        _seed_sessions(
            paper_sessions_dir=self.paper_dir,
            n_sessions=60, bets_per_session=1, won_count=1,
        )
        payload = self._run()
        self.assertEqual(payload["status"], "INSUFFICIENT_OUTCOMES")
        self.assertTrue(payload["conditions"]["sessions"]["pass"])
        self.assertFalse(payload["conditions"]["n_settled"]["pass"])
        self.assertEqual(payload["conditions"]["n_settled"]["value"], 60)
        self.assertEqual(payload["conditions"]["n_settled"]["remaining"], 90)

    def test_sub_zero_roi_when_n_thresholds_met(self):
        # 60 sessions, 3 bets each = 180 settled. Ask=0.50, all LOSE
        # -> ROI = -100% (well below 0).
        _seed_sessions(
            paper_sessions_dir=self.paper_dir,
            n_sessions=60, bets_per_session=3, won_count=0,
            entry_ask=0.50, fair_value=0.10,
        )
        payload = self._run()
        self.assertEqual(payload["status"], "SUB_ZERO_ROI")
        self.assertTrue(payload["conditions"]["sessions"]["pass"])
        self.assertTrue(payload["conditions"]["n_settled"]["pass"])
        self.assertFalse(payload["conditions"]["roi"]["pass"])
        self.assertLess(payload["conditions"]["roi"]["value"], 0)
        # Alert fires (n_settled = 180 >= min_n_for_failure_alert).
        self.assertEqual(len(payload["alerts"]), 1)
        self.assertIn("SUB_ZERO_ROI", payload["alerts"][0])

    def test_calibration_off_when_delta_exceeds_tolerance(self):
        # 60 sessions, 3 bets each = 180 settled.
        # Set predicted_wr (fv) = 0.50 but actual wr much higher
        # (90%) so |delta| ~40pp >> 5pp tolerance. ROI is positive
        # at ask=0.50 so we pass ROI gate first.
        _seed_sessions(
            paper_sessions_dir=self.paper_dir,
            n_sessions=60, bets_per_session=3, won_count=3,  # 100% wr
            entry_ask=0.50, fair_value=0.50,
        )
        payload = self._run()
        self.assertEqual(payload["status"], "CALIBRATION_OFF")
        self.assertTrue(payload["conditions"]["roi"]["pass"])
        self.assertFalse(payload["conditions"]["calibration_delta_pp"]["pass"])
        self.assertEqual(len(payload["alerts"]), 1)
        self.assertIn("CALIBRATION_OFF", payload["alerts"][0])

    def test_drift_alert_persistent_when_3_of_7_days_have_under_alerts(self):
        # 60 sessions, 3 bets each, all WIN at ask=0.50 with fv=0.50
        # -> ROI passes; calibration delta is large though
        # (100% realized vs 50% predicted). We need a setup where
        # ROI + calibration pass but drift fails. The cleanest way
        # is to align predicted with realized: ask=0.50, fv=1.0 ->
        # predicted_wr=1.0 matches realized=1.0 (delta=0). ROI=100%.
        _seed_sessions(
            paper_sessions_dir=self.paper_dir,
            n_sessions=60, bets_per_session=3, won_count=3,
            entry_ask=0.50, fair_value=1.0,
        )
        # Seed 3 days of under: drift alerts in the trailing 7d.
        anchor_dt = datetime.strptime(ANCHOR, "%Y-%m-%d")
        for offset in range(1, 4):  # yesterday, 2 days ago, 3 days ago
            dt = anchor_dt - timedelta(days=offset)
            _write_review_with_alert(
                self.output_root, dt.strftime("%Y-%m-%d"),
                "Under: calibration drift detected at >=10pp",
            )
        payload = self._run()
        self.assertEqual(payload["status"], "DRIFT_ALERT_PERSISTENT")
        self.assertTrue(payload["conditions"]["roi"]["pass"])
        self.assertTrue(payload["conditions"]["calibration_delta_pp"]["pass"])
        self.assertFalse(payload["conditions"]["under_drift_alerts"]["pass"])
        self.assertEqual(
            payload["conditions"]["under_drift_alerts"]["days_with_alert"], 3,
        )
        self.assertEqual(len(payload["alerts"]), 1)
        self.assertIn("DRIFT_ALERT_PERSISTENT", payload["alerts"][0])

    def test_ready_when_all_5_conditions_pass(self):
        # 60 sessions, 3 bets each, all WIN at ask=0.50 with fv=1.0
        # -> all 5 pass + no drift alerts.
        _seed_sessions(
            paper_sessions_dir=self.paper_dir,
            n_sessions=60, bets_per_session=3, won_count=3,
            entry_ask=0.50, fair_value=1.0,
        )
        payload = self._run()
        self.assertEqual(payload["status"], "READY")
        for cond_name, cond in payload["conditions"].items():
            self.assertTrue(
                cond["pass"],
                msg=f"Expected {cond_name} to pass: {cond}",
            )
        self.assertEqual(len(payload["alerts"]), 1)
        self.assertIn("READY", payload["alerts"][0])
        self.assertIn("Under-B4:", payload["alerts"][0])


# ----------------------------------------------------------------------
# Cross-root union (bets in BOTH paper_root and live_root)
# ----------------------------------------------------------------------


class CrossRootUnionTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.paper_dir = self.tmp / "paper_sessions"
        self.live_dir = self.tmp / "live_sessions"
        self.output_root = self.tmp / "daily_human_review"

    def tearDown(self):
        self._tmp.cleanup()

    def test_same_bet_id_in_both_roots_counted_once(self):
        # Same bet appears in both paper and live session files.
        # Must count once.
        bet = _under_bet(bet_id="dup1")
        _write_session(self.paper_dir, ANCHOR, [bet])
        _write_session(self.live_dir, ANCHOR, [bet])
        day = _collect_paper_under_bets_for_date(
            session_date=ANCHOR,
            paper_sessions_dir=self.paper_dir,
            live_sessions_dir=self.live_dir,
        )
        self.assertEqual(day["n_paper_under_bets"], 1)
        self.assertEqual(len(day["settled_under_bets"]), 1)
        # Sources records BOTH roots so the operator sees the cross-
        # root presence even though the bet count dedupes.
        self.assertEqual(set(day["sources"]), {"paper", "live"})

    def test_distinct_bets_in_both_roots_both_counted(self):
        _write_session(self.paper_dir, ANCHOR, [_under_bet(bet_id="paper_a")])
        _write_session(self.live_dir, ANCHOR, [_under_bet(bet_id="live_b")])
        day = _collect_paper_under_bets_for_date(
            session_date=ANCHOR,
            paper_sessions_dir=self.paper_dir,
            live_sessions_dir=self.live_dir,
        )
        self.assertEqual(day["n_paper_under_bets"], 2)
        self.assertEqual(len(day["settled_under_bets"]), 2)

    def test_missing_session_file_returns_zero_without_raising(self):
        day = _collect_paper_under_bets_for_date(
            session_date="2099-12-31",  # no file
            paper_sessions_dir=self.paper_dir,
            live_sessions_dir=self.live_dir,
        )
        self.assertEqual(day["n_paper_under_bets"], 0)
        self.assertEqual(day["sources"], [])

    def test_malformed_session_json_is_tolerated(self):
        path = self.paper_dir / f"{ANCHOR}_session.json"
        self.paper_dir.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        # Should silently treat as empty session.
        day = _collect_paper_under_bets_for_date(
            session_date=ANCHOR,
            paper_sessions_dir=self.paper_dir,
            live_sessions_dir=self.live_dir,
        )
        self.assertEqual(day["n_paper_under_bets"], 0)

    def test_extra_fleet_root_bets_are_counted(self):
        """2026-06-10 B4 scanner fix: UNDER paper bets written by a
        parallel-engine fleet preset (data/paper_M_under_paper/sessions)
        must advance B4. Before this fix the milestone read 0/60 while
        the M preset accumulated UNDER evidence invisibly."""
        fleet_dir = self.tmp / "paper_M_under_paper" / "sessions"
        fleet_dir.mkdir(parents=True)
        _write_session(fleet_dir, ANCHOR, [_under_bet(bet_id="fleet_m_1")])
        day = _collect_paper_under_bets_for_date(
            session_date=ANCHOR,
            paper_sessions_dir=self.paper_dir,
            live_sessions_dir=self.live_dir,
            extra_paper_sessions_dirs=(fleet_dir,),
        )
        self.assertEqual(day["n_paper_under_bets"], 1)
        # Source label identifies the fleet engine by its paper_<name>
        # directory so the by_date drill-down shows provenance.
        self.assertEqual(day["sources"], ["fleet:paper_M_under_paper"])

    def test_extra_fleet_root_dedupes_against_primary_roots(self):
        """Same bet_id in the default paper root AND a fleet root must
        count once (mirrors the existing paper-vs-live dedup)."""
        self.paper_dir.mkdir(parents=True, exist_ok=True)
        fleet_dir = self.tmp / "paper_M_under_paper" / "sessions"
        fleet_dir.mkdir(parents=True)
        bet = _under_bet(bet_id="dup_fleet")
        _write_session(self.paper_dir, ANCHOR, [bet])
        _write_session(fleet_dir, ANCHOR, [bet])
        day = _collect_paper_under_bets_for_date(
            session_date=ANCHOR,
            paper_sessions_dir=self.paper_dir,
            live_sessions_dir=self.live_dir,
            extra_paper_sessions_dirs=(fleet_dir,),
        )
        self.assertEqual(day["n_paper_under_bets"], 1)
        self.assertEqual(
            set(day["sources"]), {"paper", "fleet:paper_M_under_paper"},
        )

    def test_default_extra_roots_constant_points_at_m_preset(self):
        """The production default must include the M_under_paper fleet
        root -- that's the engine currently accumulating B4 evidence."""
        from scripts.analysis.human_review.constants import (
            B4_EXTRA_PAPER_SESSION_ROOTS,
        )
        self.assertTrue(
            any(
                "paper_M_under_paper" in str(p)
                for p in B4_EXTRA_PAPER_SESSION_ROOTS
            ),
            f"expected paper_M_under_paper in {B4_EXTRA_PAPER_SESSION_ROOTS}",
        )


# ----------------------------------------------------------------------
# Aggregate math
# ----------------------------------------------------------------------


class AggregateMathTests(unittest.TestCase):

    def test_empty_aggregate_returns_zeros(self):
        agg = _aggregate_paper_under_bets([])
        self.assertEqual(agg["n_settled"], 0)
        self.assertIsNone(agg["realized_wr"])
        self.assertIsNone(agg["taker_roi"])

    def test_roi_math_3w_1l(self):
        # 3 wins at ask=0.50 (profit +$10 each); 1 loss (profit -$10).
        # Net profit = $20 on $40 stake -> ROI = 50%.
        bets = [
            _under_bet(bet_id=f"w{i}", entry_ask=0.50, won=True)
            for i in range(3)
        ] + [_under_bet(bet_id="l", entry_ask=0.50, won=False)]
        agg = _aggregate_paper_under_bets(bets)
        self.assertEqual(agg["n_settled"], 4)
        self.assertEqual(agg["n_wins"], 3)
        self.assertEqual(agg["n_losses"], 1)
        self.assertAlmostEqual(agg["realized_wr"], 0.75)
        self.assertAlmostEqual(agg["total_profit_usdc"], 20.0)
        self.assertAlmostEqual(agg["total_stake_usdc"], 40.0)
        self.assertAlmostEqual(agg["taker_roi"], 0.5)

    def test_calibration_delta_arithmetic(self):
        # 100% realized, 80% predicted -> +20pp.
        bets = [
            _under_bet(bet_id=f"w{i}", fair_value=0.80, won=True)
            for i in range(10)
        ]
        agg = _aggregate_paper_under_bets(bets)
        self.assertAlmostEqual(agg["realized_wr"], 1.0)
        self.assertAlmostEqual(agg["predicted_wr"], 0.80)
        self.assertAlmostEqual(agg["calibration_delta_pp"], 20.0)


# ----------------------------------------------------------------------
# Drift alert counting + self-loop guard
# ----------------------------------------------------------------------


class DriftAlertCountingTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.output_root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_under_prefixed_alert_is_counted(self):
        anchor_dt = datetime.strptime(ANCHOR, "%Y-%m-%d")
        for offset in range(1, 4):
            dt = anchor_dt - timedelta(days=offset)
            _write_review_with_alert(
                self.output_root, dt.strftime("%Y-%m-%d"),
                "Under: calibration drift detected",
            )
        out = _count_persistent_under_drift_alerts(
            session_date=ANCHOR,
            output_root=self.output_root,
            lookback_days=7,
        )
        self.assertEqual(out["days_with_alert"], 3)

    def test_under_b4_alert_is_NOT_counted_as_drift(self):
        """Critical: the B4 verdict alert uses prefix 'Under-B4:'
        which would otherwise match the 'Under-' drift prefix. The
        helper must explicitly exclude this to avoid a self-loop
        where yesterday's B4 status alert pollutes today's drift
        count and forces the verdict from READY to
        DRIFT_ALERT_PERSISTENT in perpetuity.
        """
        anchor_dt = datetime.strptime(ANCHOR, "%Y-%m-%d")
        for offset in range(1, 4):
            dt = anchor_dt - timedelta(days=offset)
            _write_review_with_alert(
                self.output_root, dt.strftime("%Y-%m-%d"),
                "Under-B4: READY -- B4 cleared (60+ sessions, ...)",
            )
        out = _count_persistent_under_drift_alerts(
            session_date=ANCHOR,
            output_root=self.output_root,
            lookback_days=7,
        )
        self.assertEqual(out["days_with_alert"], 0)
        self.assertEqual(out["days_scanned"], 3)

    def test_multiple_under_alerts_same_day_count_once(self):
        anchor_dt = datetime.strptime(ANCHOR, "%Y-%m-%d")
        dt = anchor_dt - timedelta(days=1)
        _write_review_with_alert(
            self.output_root, dt.strftime("%Y-%m-%d"),
            "Under: calibration drift detected",
            "Under-outcomes: ROI dropped",
            "Under-coverage: rate fell",
        )
        out = _count_persistent_under_drift_alerts(
            session_date=ANCHOR,
            output_root=self.output_root,
            lookback_days=7,
        )
        # Same day, 3 alerts -> counts as 1 day.
        self.assertEqual(out["days_with_alert"], 1)

    def test_non_under_alerts_are_ignored(self):
        anchor_dt = datetime.strptime(ANCHOR, "%Y-%m-%d")
        for offset in range(1, 4):
            dt = anchor_dt - timedelta(days=offset)
            _write_review_with_alert(
                self.output_root, dt.strftime("%Y-%m-%d"),
                "Calibration drift: over-side issue",
                "Fill-rate drift: dropped to 40%",
            )
        out = _count_persistent_under_drift_alerts(
            session_date=ANCHOR,
            output_root=self.output_root,
            lookback_days=7,
        )
        self.assertEqual(out["days_with_alert"], 0)


# ----------------------------------------------------------------------
# Alert emission gating
# ----------------------------------------------------------------------


class AlertEmissionGateTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.paper_dir = self.tmp / "paper_sessions"
        self.live_dir = self.tmp / "live_sessions"
        self.output_root = self.tmp / "daily_human_review"
        self.paper_dir.mkdir()
        self.live_dir.mkdir()
        self.output_root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_failure_alert_suppressed_under_min_n(self):
        # 60 sessions but only 5 settled bets each session = 300
        # settled; trip the SUB_ZERO_ROI gate. But override the
        # alert min_n to be higher than 300 -> alert should be
        # suppressed.
        _seed_sessions(
            paper_sessions_dir=self.paper_dir,
            n_sessions=60, bets_per_session=5, won_count=0,
            entry_ask=0.50, fair_value=0.10,
        )
        payload = _under_paper_b4_milestone_health(
            session_date=ANCHOR,
            paper_sessions_dir=self.paper_dir,
            live_sessions_dir=self.live_dir,
            extra_paper_sessions_dirs=(),
            output_root=self.output_root,
            min_n_for_failure_alert=1000,
            dormant=False,
        )
        self.assertEqual(payload["status"], "SUB_ZERO_ROI")
        # Status set; alert suppressed by min_n.
        self.assertEqual(payload["alerts"], [])

    def test_ready_alert_always_fires_regardless_of_n(self):
        # Even with min_n_for_failure_alert very high, READY alert
        # always fires (it's good news; surface it).
        _seed_sessions(
            paper_sessions_dir=self.paper_dir,
            n_sessions=60, bets_per_session=3, won_count=3,
            entry_ask=0.50, fair_value=1.0,
        )
        payload = _under_paper_b4_milestone_health(
            session_date=ANCHOR,
            paper_sessions_dir=self.paper_dir,
            live_sessions_dir=self.live_dir,
            extra_paper_sessions_dirs=(),
            output_root=self.output_root,
            min_n_for_failure_alert=99999,
            dormant=False,
        )
        self.assertEqual(payload["status"], "READY")
        self.assertEqual(len(payload["alerts"]), 1)

    def test_dormant_short_circuits_ladder_and_suppresses_alerts(self):
        """T5 (2026-06-15): when dormant, even a READY-grade sample reports
        status=DORMANT, preserves the underlying ladder verdict, and emits
        NO verdict-ladder alerts (the limiter is UNDER signal quality, not
        session count, so the clock should stop nagging)."""
        _seed_sessions(
            paper_sessions_dir=self.paper_dir,
            n_sessions=60, bets_per_session=3, won_count=3,
            entry_ask=0.50, fair_value=1.0,
        )
        payload = _under_paper_b4_milestone_health(
            session_date=ANCHOR,
            paper_sessions_dir=self.paper_dir,
            live_sessions_dir=self.live_dir,
            extra_paper_sessions_dirs=(),
            output_root=self.output_root,
            dormant=True,
        )
        self.assertEqual(payload["status"], "DORMANT")
        self.assertTrue(payload["dormant"])
        self.assertEqual(payload["underlying_status"], "READY")
        self.assertEqual(payload["alerts"], [])


# ----------------------------------------------------------------------
# build_report integration (smoke test)
# ----------------------------------------------------------------------


class BuildReportIntegrationTests(unittest.TestCase):
    """Make sure the helper is actually wired into the build_report
    return dict and Notes feed."""

    def test_block_appears_in_return_dict_and_notes_signature(self):
        src = (
            PROJECT_DIR / "scripts" / "analysis"
            / "build_daily_human_review_report.py"
        ).read_text(encoding="utf-8")
        self.assertIn("_under_paper_b4_milestone_health", src)
        self.assertIn(
            '"under_paper_b4_milestone_health":', src,
            "B4 milestone block missing from build_report return dict.",
        )
        self.assertIn(
            "under_paper_b4_milestone_health: Optional[Dict[str, Any]] = None",
            src,
            "B4 milestone block missing from _build_notes signature.",
        )


if __name__ == "__main__":
    unittest.main()
