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

import build_side_neutral_opportunity_table as side_table  # noqa: E402
import build_under_paper_ledger as under_ledger  # noqa: E402
import train_market_anchored_alpha as alpha  # noqa: E402


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _tick(ts: str, side: str, bid: float, ask: float, **overrides):
    row = {
        "ts": ts,
        "game_pk": 123,
        "away_abbrev": "AWY",
        "home_abbrev": "HOM",
        "side": side,
        "line": "8.5",
        "market_id": "m1",
        "token_id": f"{side}_token",
        "game_status": "Live",
        "game_detailed_status": "In Progress",
        "inning": 4,
        "inning_state": "Top",
        "outs": 1,
        "balls": 0,
        "strikes": 1,
        "runners_on": 0,
        "away_score": 2,
        "home_score": 1,
        "book": {
            "ok": True,
            "best_bid": bid,
            "best_ask": ask,
            "ltp": (bid + ask) / 2.0,
        },
    }
    row.update(overrides)
    return row


def _opportunity(row_id: str, date: str, under_win: int, under_edge: float, under_ask: float = 0.50):
    fair_under = under_ask + under_edge
    fair_over = 1.0 - fair_under
    return {
        "row_id": row_id,
        "session_date": date,
        "ts": f"{date}T18:00:00Z",
        "game_pk": 100 + int(row_id[-1]),
        "away_abbrev": "AWY",
        "home_abbrev": "HOM",
        "line": "8.5",
        "inning": 5,
        "inning_state": "Top",
        "outs": 0,
        "runners_on": 0,
        "away_score": 2,
        "home_score": int(row_id[-1]) % 3,
        "current_total": 2 + (int(row_id[-1]) % 3),
        "expected_remaining_half_innings": 9.0,
        "expected_remaining_pa_bucket": "19+",
        "home_skip_bottom9_risk": 0.0,
        "over_bid": 0.49,
        "over_ask": 0.51,
        "under_bid": under_ask - 0.02,
        "under_ask": under_ask,
        "under_mid": under_ask - 0.01,
        "fair_under": fair_under,
        "fair_over": fair_over,
        "under_edge_to_ask": under_edge,
        "over_edge_to_ask": fair_over - 0.51,
        "under_market_logit_residual": 0.2,
        "over_under_ask_sum": 1.01,
        "target_under_win": under_win,
        "target_over_win": 1 - under_win,
        "label_final_available": True,
        "final_total": 7 if under_win else 10,
        "final_away": 4,
        "final_home": 3 if under_win else 6,
    }


class SideNeutralUnderAlphaTests(unittest.TestCase):
    def test_side_neutral_table_pairs_raw_ticks_and_labels_under(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_path = root / "cache.json"
            _write_json(
                cache_path,
                {
                    "meta": {"extras_bucket": 10, "max_combined": 20},
                    "cells": {
                        "2_1_4_T_1_0": {
                            "n": 10,
                            "po85": 0.38,
                            "o85": 0.36,
                        }
                    },
                },
            )
            game_dir = root / "poly" / "2026-05-09" / "AWY_at_HOM_123"
            _write_json(
                game_dir / "meta.json",
                {
                    "game": {
                        "game_pk": 123,
                        "game_date": "2026-05-09T18:00:00Z",
                        "away_abbrev": "AWY",
                        "home_abbrev": "HOM",
                    },
                    "ou_markets": [
                        {
                            "line": "8.5",
                            "market_id": "m1",
                            "over_token_id": "over_token",
                            "under_token_id": "under_token",
                        }
                    ],
                },
            )
            _append_jsonl(
                game_dir / "ou_8_5_over_yes.jsonl",
                _tick("2026-05-09T18:01:00Z", "over_yes", 0.44, 0.45),
            )
            _append_jsonl(
                game_dir / "ou_8_5_under_no.jsonl",
                _tick("2026-05-09T18:01:00.300000Z", "under_no", 0.55, 0.56),
            )
            _write_json(
                root / "games" / "2026" / "05" / "09" / "123.json",
                {
                    "gamePk": 123,
                    "liveData": {
                        "linescore": {
                            "teams": {
                                "away": {"runs": 4},
                                "home": {"runs": 3},
                            }
                        }
                    },
                },
            )

            rows, manifest = side_table.build_rows(
                polymarket_root=root / "poly",
                games_root=root / "games",
                cache_path=cache_path,
                min_date="2026-05-09",
                max_date="2026-05-09",
                sample_seconds=1.0,
                max_pair_lag_seconds=1.0,
                disable_stage2=True,
                disable_stage3=True,
            )

            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertAlmostEqual(row["fair_over"], 0.38)
            self.assertAlmostEqual(row["fair_under"], 0.62)
            self.assertAlmostEqual(row["under_edge_to_ask"], 0.06)
            self.assertEqual(row["target_over_win"], 0)
            self.assertEqual(row["target_under_win"], 1)
            self.assertEqual(row["best_side_by_edge"], "under")
            self.assertEqual(manifest["final_label_rows"], 1)

    def test_under_paper_ledger_uses_under_shares_and_dedupes_score_segment(self):
        rows = [
            _opportunity("r1", "2026-05-09", under_win=1, under_edge=0.08, under_ask=0.50),
            _opportunity("r2", "2026-05-09", under_win=1, under_edge=0.09, under_ask=0.49),
        ]
        rows[1]["game_pk"] = rows[0]["game_pk"]
        rows[1]["away_score"] = rows[0]["away_score"]
        rows[1]["home_score"] = rows[0]["home_score"]

        ledger_rows = under_ledger.build_ledger_rows(
            rows,
            stake_usdc=10.0,
            daily_budget_usdc=80.0,
            per_game_budget_fraction=1.0,
            min_under_edge=0.05,
            price_policy="taker",
        )

        self.assertEqual(len(ledger_rows), 1)
        row = ledger_rows[0]
        self.assertEqual(row["decision"], "submitted")
        self.assertTrue(row["filled"])
        self.assertEqual(row["under_hit"], 1)
        self.assertEqual(row["duplicate_rows_collapsed"], 2)
        self.assertAlmostEqual(row["filled_shares"], 20.0)
        self.assertAlmostEqual(row["fill_cost_usdc"], 10.0)
        self.assertAlmostEqual(row["payout_usdc"], 20.0)
        self.assertAlmostEqual(row["profit_usdc"], 10.0)

        args = SimpleNamespace(
            input_path=Path("side_neutral_opportunities.jsonl"),
            min_date="2026-05-09",
            max_date="2026-05-09",
            stake=10.0,
            daily_budget=80.0,
            per_game_budget_fraction=1.0,
            max_orders_per_game=2,
            max_orders_per_game_line=1,
            min_under_edge=0.05,
            price_policy="taker",
            fill_assumption="immediate",
            price_offset_cents=1.0,
            include_unsettled=False,
        )
        summary = under_ledger.build_summary(
            ledger_rows,
            len(rows),
            args,
            policy_variants=under_ledger.build_threshold_variant_summaries(rows, args),
        )
        self.assertIn("min_under_edge_0.10", summary["policy_variants"])
        self.assertIn("min_under_edge_0.15", summary["policy_variants"])

    def test_market_anchored_alpha_trains_with_market_logit_offset(self):
        opportunities = [
            _opportunity("r1", "2026-05-01", under_win=1, under_edge=0.12, under_ask=0.45),
            _opportunity("r2", "2026-05-01", under_win=0, under_edge=-0.08, under_ask=0.65),
            _opportunity("r3", "2026-05-02", under_win=1, under_edge=0.10, under_ask=0.46),
            _opportunity("r4", "2026-05-02", under_win=0, under_edge=-0.06, under_ask=0.66),
            _opportunity("r5", "2026-05-03", under_win=1, under_edge=0.11, under_ask=0.47),
            _opportunity("r6", "2026-05-03", under_win=0, under_edge=-0.07, under_ask=0.67),
        ]
        side_rows, split_dates = alpha.build_side_training_rows(
            opportunities,
            side="under",
            val_frac=0.20,
            test_frac=0.20,
        )
        report, model_payload, pred_rows = alpha.train_alpha_model(
            side_rows,
            min_train_rows=2,
            strict=True,
        )

        self.assertEqual(split_dates["train"], ["2026-05-01"])
        self.assertEqual(model_payload["fixed_offset"], "logit(market_price)")
        self.assertEqual(report["rows"]["total"], 6)
        self.assertEqual(report["by_side"], {"under": 6})
        self.assertEqual(len(pred_rows), 6)
        self.assertIn("market_anchored_alpha", report["metrics"]["test"])
        self.assertIn("alpha_edge_to_market", pred_rows[0])


if __name__ == "__main__":
    unittest.main()
