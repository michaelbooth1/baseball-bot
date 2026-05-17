import json
import sys
import tempfile
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.analysis import calibration_market_anchored_alpha_walk_forward as wf


def _source_row(idx: int, date: str, *, family: str = "score_event_transition", win: int = 1) -> dict:
    ask = 0.55 if win else 0.65
    fair_value = 0.78 if win else 0.42
    return {
        "mode": "live",
        "session_date": date,
        "ts": f"{date}T18:00:00Z",
        "candidate_id": f"c{idx}",
        "game_pk": 1000 + idx,
        "away_abbrev": "AWY",
        "home_abbrev": "HOM",
        "line": "8.5",
        "signal_model_family": family,
        "state_value_strategy": family,
        "target_over_win": win,
        "decision_ask": ask,
        "decision_mid": ask - 0.01,
        "decision_market_mid_no_vig": ask - 0.04,
        "under_best_ask": 1.02 - ask,
        "spread": 0.02,
        "fair_value": fair_value,
        "fair_value_raw": fair_value,
        "base_fair_value": fair_value - 0.02,
        "raw_model_probability": fair_value,
        "raw_model_edge_to_ask": fair_value - ask,
        "inferred_state_base_poisson": fair_value - 0.02,
        "inferred_state_base_empirical": fair_value - 0.04,
        "inferred_state_effective_n": 50 + idx,
        "inning": 5,
        "inning_state": "Top",
        "outs": idx % 3,
        "runners_on": idx % 2,
        "current_total": 4,
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def test_plan_windows_uses_prior_history_before_start_date():
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
    assert windows[0]["train_dates_with_data"] == ["2026-05-02", "2026-05-03"]
    assert windows[0]["val_dates_with_data"] == ["2026-05-04"]
    assert windows[0]["skip_reason"] is None


def test_load_alpha_rows_keeps_family_and_requires_no_vig_for_no_vig_anchor():
    rows = [
        _source_row(1, "2026-05-01", family="score_event_transition", win=1),
        _source_row(2, "2026-05-01", family="no_score_drift", win=0),
    ]
    rows[1]["decision_market_mid_no_vig"] = None

    ask_rows = wf.load_alpha_rows(
        rows,
        mode="live",
        family="score_event_transition",
        anchor_price="ask",
        min_date="",
        max_date="",
    )
    no_vig_rows = wf.load_alpha_rows(
        rows,
        mode="live",
        family="no_score_drift",
        anchor_price="mid_no_vig",
        min_date="",
        max_date="",
    )

    assert len(ask_rows) == 1
    assert ask_rows[0]["family"] == "score_event_transition"
    assert ask_rows[0]["decision_ask"] == rows[0]["decision_ask"]
    assert len(no_vig_rows) == 0


def test_policy_summary_uses_clustered_ci():
    rows = [
        {"target_win": 1, "decision_ask": 0.50, "cluster_id": "g1"},
        {"target_win": 0, "decision_ask": 0.60, "cluster_id": "g2"},
        {"target_win": 1, "decision_ask": 0.40, "cluster_id": "g2"},
    ]

    summary = wf._policy_summary(rows, bootstrap_reps=25, seed=7)

    assert summary["settled_rows"] == 3
    assert summary["clusters"] == 2
    assert summary["cluster_bootstrap_ci"]["method"] == "cluster_bootstrap_by_game_date_line"
    assert summary["cluster_bootstrap_ci"]["profit_units_p025"] is not None


def test_main_writes_family_walk_forward_outputs():
    dates = [f"2026-05-0{i}" for i in range(1, 8)]
    rows = []
    idx = 1
    for date in dates:
        rows.append(_source_row(idx, date, win=1))
        idx += 1
        rows.append(_source_row(idx, date, win=0))
        idx += 1

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        table_path = root / "calibration_opportunity_training_table.jsonl"
        output_root = root / "out"
        _write_jsonl(table_path, rows)

        wf.main(
            [
                "--table-path",
                str(table_path),
                "--output-root",
                str(output_root),
                "--family",
                "score_event_transition",
                "--anchor-prices",
                "ask",
                "--train-days",
                "2",
                "--val-days",
                "1",
                "--test-days",
                "1",
                "--min-train-dates",
                "2",
                "--min-train-rows",
                "4",
                "--max-iter",
                "25",
                "--bootstrap-reps",
                "10",
            ]
        )

        summary = json.loads((output_root / "summary.json").read_text(encoding="utf-8"))
        combo = summary["families"]["score_event_transition|ask"]
        assert combo["windows_completed"] >= 1
        assert combo["out_of_sample_rows"] >= 1
        assert "market_ask" in combo["probability_metrics_all_test"]
        assert "market_mid_no_vig" in combo["probability_metrics_all_test"]
        assert "policy_pnl_all_test" in combo
        assert (output_root / "per_window_results.jsonl").exists()
        assert (output_root / "predictions.jsonl").exists()
