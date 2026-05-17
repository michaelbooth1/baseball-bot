import json
import sys
import tempfile
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.analysis import build_clv_report as clv  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def test_build_signal_clv_row_positive_when_late_mid_beats_entry():
    signal = {
        "mode": "live",
        "session_date": "2026-05-14",
        "bet_id": "b1",
        "game_pk": 1,
        "line": "8.5",
        "side": "over",
        "signal_model_family": "score_event_transition",
        "source_has_ledger_events": True,
        "decision_ask": 0.60,
        "limit_price": 0.59,
        "fair_value": 0.75,
        "edge_at_ask": 0.15,
        "inning": 5,
        "runs_needed": 2.5,
        "t0_mid": 0.59,
        "t0_best_bid": 0.58,
        "t0_best_ask": 0.60,
    }
    snapshots = [
        {"mode": "live", "bet_id": "b1", "elapsed_s": 0.0, "best_bid": 0.58, "best_ask": 0.60, "mid": 0.59},
        {"mode": "live", "bet_id": "b1", "elapsed_s": 30.0, "best_bid": 0.63, "best_ask": 0.67, "mid": 0.65},
    ]
    trade = {
        "bet_id": "b1",
        "is_filled": True,
        "actual_fill_price": 0.59,
        "realized_profit_usdc": 1.0,
        "fill_cost_usdc": 10.0,
        "won": True,
        "is_live_money": True,
    }

    row = clv._build_signal_clv_row(signal, snapshots, trade, [30])

    assert row["gate_or_reason"] == "placed_order"
    assert row["late_mid"] == 0.65
    assert row["clv_mid_vs_entry"] == 0.05
    assert row["clv_mid_vs_execution"] == 0.06
    assert row["clv_positive_vs_entry"] is True
    assert row["realized_roi"] == 0.1
    assert row["mid_30s"] == 0.65


def test_build_clv_rows_adds_candidate_coverage_rows_without_late_price():
    signal_rows = [
        {"mode": "live", "session_date": "2026-05-14", "bet_id": "b1", "decision_ask": 0.60}
    ]
    snapshot_rows = [
        {"mode": "live", "bet_id": "b1", "elapsed_s": 0, "best_bid": 0.58, "best_ask": 0.60, "mid": 0.59},
        {"mode": "live", "bet_id": "b1", "elapsed_s": 60, "best_bid": 0.62, "best_ask": 0.64, "mid": 0.63},
    ]
    candidate_rows = [
        {
            "mode": "live",
            "session_date": "2026-05-14",
            "candidate_id": "c1",
            "decision_ask": 0.50,
            "decision": "shadow_no_score_drift",
            "decision_reason": "state_value_no_score_drift",
            "signal_model_family": "no_score_drift",
        }
    ]

    rows, stats = clv.build_clv_rows(
        signal_rows=signal_rows,
        snapshot_rows=snapshot_rows,
        trade_rows=[],
        candidate_rows=candidate_rows,
        mode="live",
        min_date="",
        max_date="",
        horizons=[30, 60],
        include_candidate_coverage_rows=True,
    )

    assert stats["snapshot_groups"] == 1
    assert len(rows) == 2
    assert {row["row_type"] for row in rows} == {"order_or_captured_signal", "candidate_no_late_price"}
    candidate = [row for row in rows if row["row_type"] == "candidate_no_late_price"][0]
    assert candidate["has_late_price"] is False
    assert candidate["gate_or_reason"] == "state_value_no_score_drift"


def test_summary_reports_clv_by_family_and_roi_bucket():
    rows = [
        {
            "signal_model_family": "score_event_transition",
            "gate_or_reason": "placed_order",
            "ask_bucket": "0.55-0.70",
            "edge_bucket": "0.10-0.15",
            "inning_bucket": "5-6",
            "runs_needed_bucket": "1.5-2.5",
            "phantom_risk_bucket": "low",
            "has_late_price": True,
            "clv_mid_vs_entry": 0.03,
            "clv_mid_vs_execution": 0.04,
            "realized_roi": 0.20,
            "realized_profit_usdc": 2.0,
            "fill_cost_usdc": 10.0,
        },
        {
            "signal_model_family": "score_event_transition",
            "gate_or_reason": "placed_order",
            "ask_bucket": "0.55-0.70",
            "edge_bucket": "0.10-0.15",
            "inning_bucket": "5-6",
            "runs_needed_bucket": "1.5-2.5",
            "phantom_risk_bucket": "low",
            "has_late_price": True,
            "clv_mid_vs_entry": -0.02,
            "clv_mid_vs_execution": -0.02,
            "realized_roi": -1.0,
            "realized_profit_usdc": -10.0,
            "fill_cost_usdc": 10.0,
        },
    ]

    summary = clv.build_summary(rows, config={}, load_stats={}, warnings=[])

    assert summary["row_counts"]["rows_with_late_price"] == 2
    assert summary["clv_by_family"]["score_event_transition"]["rows_with_late_price"] == 2
    assert summary["clv_by_family_gate"]["score_event_transition|placed_order"]["mean_clv_mid_vs_entry"] == 0.005
    assert summary["clv_vs_realized_roi"]["roi_by_clv_execution_bucket"]["2c..5c"]["roi_on_cost"] == 0.2


def test_main_writes_outputs():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        signals = root / "signals.jsonl"
        snapshots = root / "snapshots.jsonl"
        trades = root / "trades.jsonl"
        candidates = root / "candidates.jsonl"
        out = root / "out"
        _write_jsonl(
            signals,
            [{"mode": "live", "session_date": "2026-05-14", "bet_id": "b1", "decision_ask": 0.60}],
        )
        _write_jsonl(
            snapshots,
            [
                {"mode": "live", "bet_id": "b1", "elapsed_s": 0, "best_bid": 0.58, "best_ask": 0.60, "mid": 0.59},
                {"mode": "live", "bet_id": "b1", "elapsed_s": 30, "best_bid": 0.62, "best_ask": 0.64, "mid": 0.63},
            ],
        )
        _write_jsonl(trades, [])
        _write_jsonl(candidates, [])

        clv.main(
            [
                "--signals-master",
                str(signals),
                "--snapshots",
                str(snapshots),
                "--analysis-safe-trades",
                str(trades),
                "--calibration-table",
                str(candidates),
                "--output-root",
                str(out),
            ]
        )

        assert (out / "clv_rows.jsonl").exists()
        assert (out / "clv_rows.csv").exists()
        assert (out / "clv_summary.json").exists()
        assert (out / "clv_summary.md").exists()
