import json
import sys
import tempfile
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.analysis import build_fv_disagreement_quality_report as report  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def test_quality_row_measures_fv_calibration_gain_and_clv():
    calibration_rows = [
        {
            "mode": "live",
            "session_date": "2026-05-14",
            "candidate_id": "c1",
            "game_pk": 1,
            "line": "8.5",
            "signal_model_family": "score_event_transition",
            "decision_ask": 0.60,
            "decision_market_mid_no_vig": 0.55,
            "fair_value": 0.80,
            "target_over_win": 1,
            "target_taker_profit_units": 0.6667,
            "inferred_state_stage1_trust_weight": 0.70,
            "inferred_state_effective_n_proxy": 90,
        }
    ]
    clv_rows = [
        {
            "candidate_id": "c1",
            "has_late_price": True,
            "late_mid": 0.66,
            "clv_mid_vs_entry": 0.06,
            "realized_roi": 0.20,
            "realized_profit_usdc": 2.0,
            "fill_cost_usdc": 10.0,
        }
    ]

    rows, stats = report.build_quality_rows(
        calibration_rows=calibration_rows,
        clv_rows=clv_rows,
        mode="live",
        family="all",
        min_date="",
        max_date="",
        market_anchor="ask",
        min_abs_disagreement=0.03,
    )

    assert stats["quality_rows"] == 1
    row = rows[0]
    assert row["fv_minus_market"] == 0.2
    assert row["disagreement_direction"] == "model_above_market"
    assert row["brier_gain_vs_market"] == 0.12
    assert row["logloss_gain_vs_market"] > 0
    assert row["fv_direction_correct"] is True
    assert row["clv_match_source"] == "candidate_id"
    assert row["clv_mid_vs_entry"] == 0.06
    assert row["stage1_trust_bucket"] == "0.50-0.75"
    assert row["stage1_effective_n_bucket"] == "75-149"


def test_market_anchor_can_use_no_vig_midpoint():
    row = {
        "mode": "live",
        "session_date": "2026-05-14",
        "candidate_id": "c1",
        "signal_model_family": "no_score_drift",
        "decision_ask": 0.70,
        "decision_market_mid_no_vig": 0.58,
        "fair_value": 0.68,
        "target_over_win": 1,
        "current_state_value_stage1_trust_weight": 0.40,
    }

    rows, _ = report.build_quality_rows(
        calibration_rows=[row],
        clv_rows=[],
        mode="live",
        family="all",
        min_date="",
        max_date="",
        market_anchor="mid_no_vig",
        min_abs_disagreement=0.03,
    )

    assert rows[0]["anchor_price_source"] == "decision_market_mid_no_vig"
    assert rows[0]["market_probability"] == 0.58
    assert rows[0]["fv_minus_market"] == 0.10


def test_clv_match_prefers_late_state_row_over_candidate_coverage_placeholder():
    calibration_row = {
        "mode": "live",
        "session_date": "2026-05-14",
        "candidate_id": "c1",
        "game_pk": 7,
        "line": "8.5",
        "inning": 5,
        "inning_state": "Top",
        "outs": 0,
        "runners_on": 1,
        "current_total": 4,
        "signal_model_family": "score_event_transition",
        "decision_ask": 0.60,
        "fair_value": 0.75,
        "target_over_win": 1,
    }
    clv_rows = [
        {"candidate_id": "c1", "has_late_price": False},
        {
            "mode": "live",
            "session_date": "2026-05-14",
            "game_pk": 7,
            "line": "8.5",
            "inning": 5,
            "inning_state": "Top",
            "outs": 0,
            "runners_on": 1,
            "current_total": 4,
            "entry_price": 0.60,
            "has_late_price": True,
            "late_mid": 0.64,
            "clv_mid_vs_entry": 0.04,
        },
    ]

    rows, _ = report.build_quality_rows(
        calibration_rows=[calibration_row],
        clv_rows=clv_rows,
        mode="live",
        family="all",
        min_date="",
        max_date="",
        market_anchor="ask",
        min_abs_disagreement=0.03,
    )

    assert rows[0]["clv_match_source"] == "state_key"
    assert rows[0]["has_late_price"] is True
    assert rows[0]["clv_mid_vs_entry"] == 0.04


def test_bucket_rows_rank_helpful_and_harmful_disagreement_groups():
    quality_rows = [
        {
            "family": "score_event_transition",
            "is_disagreement": True,
            "fv_gap_bucket": "0.05..0.10",
            "disagreement_direction": "model_above_market",
            "ask_bucket": "0.55-0.70",
            "stage1_trust_bucket": "0.50-0.75",
            "stage1_effective_n_bucket": "75-149",
            "current_state_edge_bucket": "0.08-0.12",
            "shadow_phantom_risk_bucket": "low",
            "inning_bucket": "5-6",
            "runs_needed_bucket": "1.5-2.5",
            "home_skip_bottom9_risk_bucket": "none",
            "decision_reason": "placed_order",
            "label_over_win": 1,
            "market_probability": 0.60,
            "fair_value": 0.68,
            "fv_minus_market": 0.08,
            "abs_fv_minus_market": 0.08,
            "market_brier": 0.16,
            "fv_brier": 0.1024,
            "market_logloss": 0.510826,
            "fv_logloss": 0.385662,
            "brier_gain_vs_market": 0.0576,
            "logloss_gain_vs_market": 0.125164,
            "fv_direction_correct": True,
            "clv_mid_vs_entry": 0.03,
            "realized_roi": 0.1,
            "realized_profit_usdc": 1.0,
            "fill_cost_usdc": 10.0,
            "stage1_trust_weight": 0.7,
            "stage1_effective_n": 90,
            "taker_profit_units": 0.6667,
            "limit_profit_units": 0.72,
        },
        {
            "family": "score_event_transition",
            "is_disagreement": True,
            "fv_gap_bucket": "0.05..0.10",
            "disagreement_direction": "model_above_market",
            "ask_bucket": "0.70-0.85",
            "stage1_trust_bucket": "0.50-0.75",
            "stage1_effective_n_bucket": "75-149",
            "current_state_edge_bucket": "0.08-0.12",
            "shadow_phantom_risk_bucket": "low",
            "inning_bucket": "5-6",
            "runs_needed_bucket": "1.5-2.5",
            "home_skip_bottom9_risk_bucket": "none",
            "decision_reason": "placed_order",
            "label_over_win": 0,
            "market_probability": 0.60,
            "fair_value": 0.68,
            "fv_minus_market": 0.08,
            "abs_fv_minus_market": 0.08,
            "market_brier": 0.36,
            "fv_brier": 0.4624,
            "market_logloss": 0.916291,
            "fv_logloss": 1.139434,
            "brier_gain_vs_market": -0.1024,
            "logloss_gain_vs_market": -0.223143,
            "fv_direction_correct": False,
            "clv_mid_vs_entry": -0.04,
            "stage1_trust_weight": 0.7,
            "stage1_effective_n": 90,
            "taker_profit_units": -1.0,
            "limit_profit_units": -1.0,
        },
    ]

    buckets = report.build_bucket_rows(quality_rows, min_bucket_rows=1)
    helpful = report._top_buckets(buckets, reverse=True)
    harmful = report._top_buckets(buckets, reverse=False)

    assert helpful[0]["mean_brier_gain_vs_market"] >= harmful[0]["mean_brier_gain_vs_market"]
    ask_bucket_rows = [b for b in buckets if b["bucket_dimension"] == "ask_bucket_x_gap"]
    assert {b["bucket_value"] for b in ask_bucket_rows} == {
        "0.55-0.70|0.05..0.10",
        "0.70-0.85|0.05..0.10",
    }


def test_main_writes_summary_rows_and_buckets():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        calibration = root / "calibration.jsonl"
        clv = root / "clv.jsonl"
        out = root / "out"
        _write_jsonl(
            calibration,
            [
                {
                    "mode": "live",
                    "session_date": "2026-05-14",
                    "candidate_id": "c1",
                    "signal_model_family": "score_event_transition",
                    "decision_ask": 0.60,
                    "fair_value": 0.80,
                    "target_over_win": 1,
                }
            ],
        )
        _write_jsonl(clv, [])

        report.main(
            [
                "--calibration-table",
                str(calibration),
                "--clv-rows",
                str(clv),
                "--output-root",
                str(out),
                "--min-bucket-rows",
                "1",
            ]
        )

        assert (out / "fv_disagreement_quality_summary.json").exists()
        assert (out / "fv_disagreement_quality_summary.md").exists()
        assert (out / "fv_disagreement_quality_rows.jsonl").exists()
        assert (out / "fv_disagreement_quality_buckets.csv").exists()
