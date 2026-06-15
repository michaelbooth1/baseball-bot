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


if __name__ == "__main__":
    unittest.main()
