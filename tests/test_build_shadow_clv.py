"""Tests for build_shadow_clv (T1: shadow-CLV / post-signal market path).

Covers: per-capture metric computation, shared_capture_pointer following,
the pseudo-replication dedup (presets share one underlying book path), the
outcome join, and the won/lost x drift decomposition.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR / "scripts" / "analysis") not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR / "scripts" / "analysis"))

import build_shadow_clv as m  # noqa: E402


def _signal(bet_id, game_pk, line, token, ts, entry_ask, fv, base_fv=0.86):
    return {
        "type": "signal", "bet_id": bet_id, "ts": ts, "game_pk": game_pk,
        "line": str(line), "token_id": token, "entry_ask": entry_ask,
        "fair_value": fv, "base_fair_value": base_fv, "inning": 7,
    }


def _snaps(points):
    return [
        {"type": "snapshot", "seq": i, "elapsed_s": float(e),
         "book": {"best_bid": b, "best_ask": a}}
        for i, (e, b, a) in enumerate(points)
    ]


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def _write_inline_capture(root, date, *, bet_id, game_pk, line, token, ts,
                          entry_ask, fv, points, base_fv=0.86):
    p = root / "book_captures" / date / f"{bet_id}.jsonl"
    _write_jsonl(p, [_signal(bet_id, game_pk, line, token, ts, entry_ask, fv,
                             base_fv)] + _snaps(points))
    return p


def _write_outcomes(root, date, outcomes):
    p = root / "candidate_universe" / f"{date}_outcomes.jsonl"
    _write_jsonl(p, outcomes)


def _write_tape(root, date, bet_id, features):
    p = root / "tape_captures" / date / f"{bet_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"bet_id": bet_id, "features": features}),
                 encoding="utf-8")


# A favorable path: mid rises 0.70 -> 0.76 over 120s.
FAVORABLE = [(0, 0.68, 0.72), (30, 0.70, 0.74), (60, 0.72, 0.76), (120, 0.74, 0.78)]
# An adverse path: mid falls 0.70 -> 0.64.
ADVERSE = [(0, 0.68, 0.72), (30, 0.66, 0.70), (60, 0.64, 0.68), (120, 0.60, 0.68)]


class ParseCaptureTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_favorable_path_metrics(self):
        live = self.data / "live_trading"
        p = _write_inline_capture(
            live, "2026-06-13", bet_id="b1", game_pk=1, line="9.5",
            token="T1", ts="2026-06-13T22:00:00.0Z", entry_ask=0.70, fv=0.93,
            points=FAVORABLE,
        )
        row = m.parse_capture(p, date="2026-06-13", config_label="live",
                              outcome_lookup={(1, "9.5"): True})
        self.assertAlmostEqual(row["entry_mid"], 0.70, places=4)
        self.assertAlmostEqual(row["mid_120s"], 0.76, places=4)
        self.assertAlmostEqual(row["mid_drift_120s"], 0.06, places=4)
        # shadow_clv = mid@120 - entry_ask = 0.76 - 0.70
        self.assertAlmostEqual(row["shadow_clv_120s"], 0.06, places=4)
        self.assertEqual(row["adverse_sign"], "favorable")
        self.assertEqual(row["raw_fv_band"], "0.90-0.95")
        self.assertTrue(row["won"])
        self.assertTrue(row["path_complete"])

    def test_adverse_path_and_loss(self):
        live = self.data / "live_trading"
        p = _write_inline_capture(
            live, "2026-06-13", bet_id="b2", game_pk=2, line="9.5",
            token="T2", ts="2026-06-13T22:00:00.0Z", entry_ask=0.70, fv=0.97,
            points=ADVERSE,
        )
        row = m.parse_capture(p, date="2026-06-13", config_label="live",
                              outcome_lookup={(2, "9.5"): False})
        self.assertLess(row["mid_drift_120s"], 0)
        self.assertEqual(row["adverse_sign"], "adverse")
        self.assertEqual(row["raw_fv_band"], ">=0.95")
        self.assertFalse(row["won"])

    def test_follows_shared_capture_pointer(self):
        # The shared file holds the snapshots; the per-candidate file holds
        # only its own signal + a pointer (the fleet dedup shape).
        shared = self.data / "shared" / "2026-06-13" / "abc.jsonl"
        _write_jsonl(shared, [_signal("shared", 9, "9.5", "T9",
                                      "2026-06-13T22:00:00.0Z", 0.70, 0.93)]
                     + _snaps(FAVORABLE))
        root = self.data / "paper_A_current"
        pcap = root / "book_captures" / "2026-06-13" / "b9.jsonl"
        _write_jsonl(pcap, [
            _signal("b9", 9, "9.5", "T9", "2026-06-13T22:00:00.0Z", 0.71, 0.93),
            {"type": "shared_capture_pointer", "bet_id": "b9",
             "shared_capture_path": str(shared)},
        ])
        row = m.parse_capture(pcap, date="2026-06-13", config_label="A_current",
                              outcome_lookup={(9, "9.5"): True})
        self.assertIsNotNone(row)
        # Uses THIS file's entry_ask (0.71), shared snapshots for the path.
        self.assertAlmostEqual(row["entry_ask"], 0.71, places=4)
        self.assertAlmostEqual(row["mid_120s"], 0.76, places=4)
        self.assertEqual(row["adverse_sign"], "favorable")

    def test_dedup_collapses_shared_paths_in_aggregate(self):
        # live + a fleet preset evaluate the SAME (game,line,token,ts):
        # two candidate rows, ONE unique market path.
        live = self.data / "live_trading"
        _write_inline_capture(
            live, "2026-06-13", bet_id="L", game_pk=5, line="9.5", token="T5",
            ts="2026-06-13T22:00:00.0Z", entry_ask=0.70, fv=0.93, points=FAVORABLE,
        )
        fleet = self.data / "paper_C_raw"
        _write_inline_capture(
            fleet, "2026-06-13", bet_id="C", game_pk=5, line="9.5", token="T5",
            ts="2026-06-13T22:00:00.4Z", entry_ask=0.69, fv=0.93, points=FAVORABLE,
        )
        _write_outcomes(live, "2026-06-13", [{"game_pk": 5, "line": "9.5", "over_hit": True}])
        rows, summary = m.build(data_dir=self.data, since=None, until=None)
        self.assertEqual(summary["n_candidate_rows"], 2)
        self.assertEqual(summary["n_unique_paths"], 1)
        # Both arms still appear in the per-arm breakdown.
        self.assertIn("live", summary["by_config_label"])
        self.assertIn("C_raw", summary["by_config_label"])

    def test_decomposition_and_verdict(self):
        live = self.data / "live_trading"
        outs = []
        # 21 losers that drift adverse ("market knew") + 3 flat losers
        # ("model wrong") + 10 favorable winners. Enough to clear the
        # verdict floor (>= 20 settled) and produce ADVERSE_SELECTION.
        FLAT = [(0, 0.68, 0.72), (60, 0.685, 0.715), (120, 0.685, 0.715)]
        gp = 0
        for _ in range(21):
            gp += 1
            _write_inline_capture(live, "2026-06-13", bet_id=f"a{gp}", game_pk=gp,
                                  line="9.5", token=f"T{gp}",
                                  ts=f"2026-06-13T22:00:{gp:02d}.0Z",
                                  entry_ask=0.70, fv=0.96, points=ADVERSE)
            outs.append({"game_pk": gp, "line": "9.5", "over_hit": False})
        for _ in range(3):
            gp += 1
            _write_inline_capture(live, "2026-06-13", bet_id=f"f{gp}", game_pk=gp,
                                  line="9.5", token=f"T{gp}",
                                  ts=f"2026-06-13T22:00:{gp:02d}.0Z",
                                  entry_ask=0.70, fv=0.96, points=FLAT)
            outs.append({"game_pk": gp, "line": "9.5", "over_hit": False})
        for _ in range(10):
            gp += 1
            _write_inline_capture(live, "2026-06-13", bet_id=f"w{gp}", game_pk=gp,
                                  line="9.5", token=f"T{gp}",
                                  ts=f"2026-06-13T22:00:{gp:02d}.0Z",
                                  entry_ask=0.70, fv=0.96, points=FAVORABLE)
            outs.append({"game_pk": gp, "line": "9.5", "over_hit": True})
        _write_outcomes(live, "2026-06-13", outs)
        _, s = m.build(data_dir=self.data, since=None, until=None)
        self.assertEqual(s["n_settled_with_path"], 34)
        self.assertEqual(s["n_losses_settled"], 24)
        d = s["decomposition_2x2"]
        self.assertEqual(d["lost_adverse"], 21)
        self.assertEqual(d["lost_flat"], 3)
        self.assertEqual(d["won_favorable"], 10)
        self.assertAlmostEqual(s["market_knew_share"], 21 / 24, places=4)
        self.assertEqual(s["verdict"], "ADVERSE_SELECTION")


class TapeLayerTests(unittest.TestCase):

    def test_tape_direction_classifier(self):
        self.assertEqual(m._tape_direction(None), "no_tape")
        self.assertEqual(m._tape_direction({"trades_last_30s_count": None}), "no_tape")
        self.assertEqual(m._tape_direction({"trades_last_30s_count": 0}), "flat_tape")
        self.assertEqual(
            m._tape_direction({"trades_last_30s_count": 3, "signed_volume_last_30s": -5.0}),
            "informed_against",
        )
        self.assertEqual(
            m._tape_direction({"trades_last_30s_count": 3, "signed_volume_last_30s": 5.0}),
            "informed_with",
        )

    def test_tape_subverdict(self):
        self.assertEqual(m._tape_subverdict(5, 0, 5), "INSUFFICIENT_TAPE")   # < 10
        self.assertEqual(m._tape_subverdict(20, 1, 19), "CHASING")
        self.assertEqual(m._tape_subverdict(20, 15, 2), "INFORMED")
        self.assertEqual(m._tape_subverdict(20, 8, 8), "MIXED")

    def test_build_classifies_flat_tape_as_chasing(self):
        with tempfile.TemporaryDirectory() as td:
            live = Path(td) / "live_trading"
            outs = []
            # 12 adverse-drift LOSERS, each with a FLAT tape at signal.
            for gp in range(1, 13):
                _write_inline_capture(
                    live, "2026-06-13", bet_id=f"a{gp}", game_pk=gp, line="9.5",
                    token=f"T{gp}", ts=f"2026-06-13T22:00:{gp:02d}.0Z",
                    entry_ask=0.70, fv=0.96, points=ADVERSE,
                )
                outs.append({"game_pk": gp, "line": "9.5", "over_hit": False})
                _write_tape(live, "2026-06-13", f"a{gp}",
                            {"trades_last_30s_count": 0, "signed_volume_last_30s": None})
            # winners to clear the verdict floor -> ADVERSE_SELECTION.
            for gp in range(13, 23):
                _write_inline_capture(
                    live, "2026-06-13", bet_id=f"w{gp}", game_pk=gp, line="9.5",
                    token=f"T{gp}", ts=f"2026-06-13T22:00:{gp:02d}.0Z",
                    entry_ask=0.70, fv=0.96, points=FAVORABLE,
                )
                outs.append({"game_pk": gp, "line": "9.5", "over_hit": True})
            _write_outcomes(live, "2026-06-13", outs)
            _, s = m.build(data_dir=Path(td), since=None, until=None)
            self.assertEqual(s["verdict"], "ADVERSE_SELECTION")
            self.assertEqual(s["tape_subverdict"], "CHASING")
            td_ = s["tape_decomposition"]
            self.assertEqual(td_["flat_tape"], 12)
            self.assertEqual(td_["informed_against"], 0)
            self.assertAlmostEqual(td_["flat_share"], 1.0)


class BookQualityVerdictTests(unittest.TestCase):

    def test_taker_profit(self):
        self.assertAlmostEqual(
            m._taker_profit({"settled": True, "won": True, "entry_ask": 0.5}), 1.0)
        self.assertEqual(
            m._taker_profit({"settled": True, "won": False, "entry_ask": 0.5}), -1.0)
        self.assertIsNone(m._taker_profit({"settled": False}))
        self.assertIsNone(m._taker_profit({"settled": True, "won": True, "entry_ask": 0}))

    def _depth_bq(self, low, mid, high):
        return {"top_depth": {
            "metric": "entry_top_depth", "tertile_cuts": [400.0, 5800.0],
            "higher_is_worse": False, "worse_end": "low",
            "buckets": {
                "low": {"n": 200, "roi": low, "win_rate": 0.69},
                "mid": {"n": 200, "roi": mid, "win_rate": 0.66},
                "high": {"n": 200, "roi": high, "win_rate": 0.78},
            },
        }}

    def test_actionable_when_good_end_positive_and_rest_negative(self):
        # Real shape: bottom 2/3 by depth -EV, deep books +EV.
        v = m._book_quality_verdict(self._depth_bq(-0.04, -0.06, 0.13), 600)
        self.assertEqual(v["verdict"], "ACTIONABLE_FILTER")
        d = v["actionable_dimensions"][0]
        self.assertEqual(d["dimension"], "top_depth")
        self.assertEqual(d["keep_end"], "high")
        self.assertEqual(d["threshold"], 5800.0)

    def test_benign_when_good_end_not_clearly_positive(self):
        # Deep books only marginally better -> no actionable split.
        v = m._book_quality_verdict(self._depth_bq(-0.04, -0.06, 0.02), 600)
        self.assertEqual(v["verdict"], "BENIGN_DRAG")

    def test_insufficient_data(self):
        v = m._book_quality_verdict(self._depth_bq(-0.04, -0.06, 0.13), 50)
        self.assertEqual(v["verdict"], "INSUFFICIENT_DATA")


if __name__ == "__main__":
    unittest.main()
