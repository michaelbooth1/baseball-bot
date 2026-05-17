import json
import tempfile
import unittest
from pathlib import Path

from scripts.analysis.audit_stage1_inferred_empirical import (
    build_report,
    dedupe_state_rows,
    load_audit_rows,
)
from scripts.analysis.analyze_polymarket_overreactions import OUCache
from scripts.trading.stage1_cache_audit import resolve_cell_line_probability


class Stage1InferredEmpiricalAuditTests(unittest.TestCase):
    def test_empirical_line_resolver_matches_poisson_fallback_shape(self) -> None:
        cell = {
            "o65": 0.60,
            "o75": 0.48,
            "po65": 0.66,
            "po75": 0.55,
        }

        prob, meta = resolve_cell_line_probability(cell, requested_line="5.5", prefix="o")

        self.assertIsNotNone(prob)
        self.assertEqual(meta["line_fallback_mode"], "extrapolate_low")
        self.assertEqual(meta["line_source_key"], "o65")
        self.assertEqual(meta["line_source_key_high"], "o75")

    def test_audit_backfills_empirical_from_legacy_candidate_cell_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate_root = root / "candidate_universe"
            candidate_root.mkdir()
            cache_path = root / "mlb_ou_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "meta": {},
                        "cells": {
                            "1_0_5_T_1_0": {
                                "n": 88,
                                "n_samples": 88,
                                "po65": 0.82,
                                "o65": 0.71,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            candidate = {
                "candidate_id": "c1",
                "session_date": "2026-05-07",
                "mode": "live",
                "signal_model_family": "score_event_transition",
                "game_pk": 1,
                "line": "6.5",
                "inning": 5,
                "inning_state": "Top",
                "outs": 1,
                "runners_on": 0,
                "away_score_before": 0,
                "home_score_before": 0,
                "inferred_runs": 1,
                "decision_ask": 0.70,
                "base_fair_value": 0.82,
                "decision_reason": "gate_min_edge",
                "inference_state_cell_key": "1_0_5_T_1_0",
                "inference_state_fallback_level": 0,
                "inference_line_fallback_mode": "exact",
                "inference_line_source_key": "po65",
            }
            outcome = {
                "session_date": "2026-05-07",
                "mode": "live",
                "game_pk": 1,
                "line": "6.5",
                "over_hit": True,
            }
            (candidate_root / "2026-05-07_candidates.jsonl").write_text(
                json.dumps(candidate) + "\n",
                encoding="utf-8",
            )
            (candidate_root / "2026-05-07_outcomes.jsonl").write_text(
                json.dumps(outcome) + "\n",
                encoding="utf-8",
            )

            rows, counters = load_audit_rows(
                candidate_root,
                cache=OUCache(cache_path),
                min_date="2026-05-07",
                max_date="2026-05-07",
            )
            deduped = dedupe_state_rows(rows)
            report = build_report(
                deduped,
                raw_rows=rows,
                counters=counters,
                min_date="2026-05-07",
                max_date="2026-05-07",
                candidate_root=candidate_root,
                cache_path=cache_path,
            )

            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["empirical"], 0.71)
            self.assertAlmostEqual(rows[0]["poisson_minus_empirical"], 0.11)
            self.assertEqual(rows[0]["support_n"], 88.0)
            self.assertEqual(report["primary_summary"]["n_empirical_available"], 1)


if __name__ == "__main__":
    unittest.main()
