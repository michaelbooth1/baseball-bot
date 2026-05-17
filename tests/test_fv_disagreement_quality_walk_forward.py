import json
import sys
import tempfile
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.analysis import fv_disagreement_quality_walk_forward as wf  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _cal_row(date: str, idx: int, *, label: int, ask: float = 0.60, fv: float = 0.80) -> dict:
    return {
        "mode": "live",
        "session_date": date,
        "candidate_id": f"c{idx}",
        "game_pk": 1000 + idx,
        "line": "8.5",
        "signal_model_family": "score_event_transition",
        "decision": "placed_order",
        "decision_reason": "placed_order",
        "decision_ask": ask,
        "fair_value": fv,
        "target_over_win": label,
        "target_taker_profit_units": round((1.0 / ask) - 1.0, 6) if label else -1.0,
        "inferred_state_stage1_trust_weight": 0.70,
        "inferred_state_effective_n_proxy": 90,
        "inning": 6,
        "runs_needed": 2.5,
    }


def test_plan_windows_uses_start_date_as_first_test_not_history_cutoff():
    windows = wf.plan_windows(
        ["2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05"],
        start_date=wf._parse_date("2026-05-05"),
        end_date=wf._parse_date("2026-05-05"),
        train_days=2,
        val_days=1,
        test_days=1,
        min_train_dates=2,
    )

    assert len(windows) == 1
    assert windows[0]["test_start"] == "2026-05-05"
    assert windows[0]["train_dates"] == ["2026-05-02", "2026-05-03"]
    assert windows[0]["val_dates"] == ["2026-05-04"]


def test_walk_forward_selects_bucket_on_train_validation_then_marks_test():
    calibration = [
        _cal_row("2026-05-01", 1, label=1),
        _cal_row("2026-05-02", 2, label=1),
        _cal_row("2026-05-03", 3, label=1),
        _cal_row("2026-05-04", 4, label=1),
    ]
    clv = [
        {
            "candidate_id": "c4",
            "has_late_price": True,
            "late_mid": 0.66,
            "clv_mid_vs_entry": 0.06,
            "realized_roi": 0.20,
            "realized_profit_usdc": 2.0,
            "fill_cost_usdc": 10.0,
        }
    ]

    combos, _ = wf.run_walk_forward(
        calibration_rows=calibration,
        clv_rows=clv,
        mode="live",
        families=["score_event_transition"],
        market_anchors=["ask"],
        min_date="",
        max_date="",
        start_date="2026-05-04",
        end_date="2026-05-04",
        train_days=2,
        val_days=1,
        test_days=1,
        min_train_dates=2,
        min_train_rows=2,
        min_train_bucket_rows=2,
        min_val_bucket_rows=1,
        min_train_brier_gain=0.0,
        min_val_brier_gain=0.0,
        max_selected_buckets=10,
        min_abs_disagreement=0.03,
    )

    payload = combos["score_event_transition|ask"]
    assert payload["window_results"][0]["completed"] is True
    assert payload["window_results"][0]["trusted_test_rows"] == 1
    assert payload["prediction_rows"][0]["trusted_disagreement"] is True
    assert payload["prediction_rows"][0]["clv_mid_vs_entry"] == 0.06
    assert payload["selected_bucket_rows"]


def test_validation_rejects_train_positive_bucket_when_val_is_bad():
    calibration = [
        _cal_row("2026-05-01", 1, label=1),
        _cal_row("2026-05-02", 2, label=1),
        _cal_row("2026-05-03", 3, label=0),
        _cal_row("2026-05-04", 4, label=1),
    ]

    combos, _ = wf.run_walk_forward(
        calibration_rows=calibration,
        clv_rows=[],
        mode="live",
        families=["score_event_transition"],
        market_anchors=["ask"],
        min_date="",
        max_date="",
        start_date="2026-05-04",
        end_date="2026-05-04",
        train_days=2,
        val_days=1,
        test_days=1,
        min_train_dates=2,
        min_train_rows=2,
        min_train_bucket_rows=2,
        min_val_bucket_rows=1,
        min_train_brier_gain=0.0,
        min_val_brier_gain=0.0,
        max_selected_buckets=10,
        min_abs_disagreement=0.03,
    )

    payload = combos["score_event_transition|ask"]
    assert payload["window_results"][0]["completed"] is True
    assert payload["window_results"][0]["selected_bucket_count"] == 0
    assert payload["prediction_rows"][0]["trusted_disagreement"] is False


def test_main_writes_summary_predictions_and_selected_buckets():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        calibration = root / "calibration.jsonl"
        clv = root / "clv.jsonl"
        out = root / "out"
        _write_jsonl(
            calibration,
            [
                _cal_row("2026-05-01", 1, label=1),
                _cal_row("2026-05-02", 2, label=1),
                _cal_row("2026-05-03", 3, label=1),
                _cal_row("2026-05-04", 4, label=1),
            ],
        )
        _write_jsonl(clv, [])

        wf.main(
            [
                "--calibration-table",
                str(calibration),
                "--clv-rows",
                str(clv),
                "--output-root",
                str(out),
                "--family",
                "score_event_transition",
                "--market-anchors",
                "ask",
                "--start-date",
                "2026-05-04",
                "--end-date",
                "2026-05-04",
                "--train-days",
                "2",
                "--val-days",
                "1",
                "--min-train-rows",
                "2",
                "--min-train-dates",
                "2",
                "--min-train-bucket-rows",
                "2",
                "--min-val-bucket-rows",
                "1",
                "--strict",
            ]
        )

        assert (out / "summary.json").exists()
        assert (out / "summary.md").exists()
        assert (out / "predictions.jsonl").exists()
        assert (out / "selected_buckets.jsonl").exists()
