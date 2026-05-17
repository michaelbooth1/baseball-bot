import json
from pathlib import Path

from scripts.analysis import build_model_maturity_report as bmm


def _profit_unit(win: bool, price: float) -> float:
    return (1.0 / price - 1.0) if win else -1.0


def _row(
    *,
    family: str,
    date: str,
    win: bool,
    ask: float,
    fv: float,
    current_edge: float,
    score_confirmed: bool = False,
    no_score_60s: bool = False,
    p_score_event_proxy: float = 0.0,
) -> dict:
    return {
        "mode": "live",
        "session_date": date,
        "signal_model_family": family,
        "label_final_available": True,
        "target_over_win": win,
        "decision_ask": ask,
        "ask_bucket": "",
        "fair_value_raw": fv,
        "fair_value": fv,
        "fair_value_calibrated": fv,
        "current_state_value_edge": current_edge,
        "shadow_current_state_edge_bucket": "",
        "shadow_phantom_risk_bucket": "phantom_low" if current_edge >= 0.03 else "phantom_high",
        "label_score_confirmation_available": family == bmm.SCORE_EVENT_TRANSITION,
        "target_score_confirmed_60s": score_confirmed,
        "target_no_score_change_60s": no_score_60s,
        "shadow_p_score_event_proxy": p_score_event_proxy,
        "shadow_no_score_drift_trigger": "segment_drawdown",
        "under_pair_available": True,
        "under_best_bid": 0.20,
        "under_best_ask": 0.28,
        "over_mid_no_vig": ask,
        "under_mid_no_vig": 1.0 - ask,
        "decision_market_mid_no_vig": ask,
        "inference_panel_runs_considered": "1,2,3" if family == bmm.SCORE_EVENT_TRANSITION else "",
        "inference_panel_selected_runs": 1 if family == bmm.SCORE_EVENT_TRANSITION else "",
        "inference_run1_base_poisson": fv if family == bmm.SCORE_EVENT_TRANSITION else "",
        "inference_run2_base_poisson": fv if family == bmm.SCORE_EVENT_TRANSITION else "",
        "inference_run3_base_poisson": fv if family == bmm.SCORE_EVENT_TRANSITION else "",
        "inferred_state_base_poisson": fv,
        "inferred_state_base_empirical": max(0.01, min(0.99, fv - 0.02)),
        "target_taker_profit_units": _profit_unit(win, ask),
        "target_limit_profit_units": _profit_unit(win, max(0.01, ask - 0.02)),
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_model_maturity_counts_metrics_and_not_enough_data(tmp_path):
    rows = [
        _row(
            family=bmm.SCORE_EVENT_TRANSITION,
            date="2026-05-01",
            win=True,
            ask=0.62,
            fv=0.78,
            current_edge=0.09,
            score_confirmed=True,
        ),
        _row(
            family=bmm.SCORE_EVENT_TRANSITION,
            date="2026-05-01",
            win=False,
            ask=0.68,
            fv=0.24,
            current_edge=0.01,
            no_score_60s=True,
        ),
        _row(
            family=bmm.SCORE_EVENT_TRANSITION,
            date="2026-05-02",
            win=True,
            ask=0.74,
            fv=0.71,
            current_edge=0.04,
            score_confirmed=True,
        ),
        _row(
            family=bmm.NO_SCORE_DRIFT,
            date="2026-05-02",
            win=False,
            ask=0.58,
            fv=0.35,
            current_edge=0.02,
        ),
        _row(
            family=bmm.NO_SCORE_DRIFT,
            date="2026-05-03",
            win=True,
            ask=0.66,
            fv=0.72,
            current_edge=0.07,
        ),
    ]
    table = tmp_path / "calibration_opportunity_training_table.jsonl"
    _write_jsonl(table, rows)

    report = bmm.build_report(table_path=table, mode="live")

    assert report["overall_status"] == "not_enough_data"
    assert report["counts"]["rows_by_family"][bmm.SCORE_EVENT_TRANSITION] == 3
    assert report["counts"]["rows_by_family"][bmm.NO_SCORE_DRIFT] == 2
    assert report["counts"]["settled_labels_by_family"][bmm.SCORE_EVENT_TRANSITION] == 3
    assert report["coverage_checks"]["by_family"][bmm.SCORE_EVENT_TRANSITION]["under_pair_available_rate"] == 1.0
    assert report["coverage_checks"]["by_family"][bmm.SCORE_EVENT_TRANSITION]["run_count_panel_rate"] == 1.0
    assert report["coverage_checks"]["by_family"][bmm.NO_SCORE_DRIFT]["run_count_panel_rate"] == 0.0

    score = report["families"][bmm.SCORE_EVENT_TRANSITION]
    assert score["metrics"]["market_probability"]["n"] == 3
    assert score["metrics"]["raw_fv_probability"]["brier"] is not None
    assert score["metrics"]["raw_fv_probability"]["calibration_slope"] is not None
    # The score-confirmation probe must read shadow_p_score_event_proxy, not
    # fair_value_raw -- pairing the over-game FV against the 60s-score label
    # was a category error that produced AUC < 0.5 by construction.
    assert "score_confirmed_60s_proxy" in score["metrics"]
    assert "score_confirmed_60s_raw_fv" not in score["metrics"]
    assert score["metrics"]["score_confirmed_60s_proxy"]["probability_key"] == "shadow_p_score_event_proxy"
    assert (
        score["artifact_promotability"]["probability_calibration"]["status"]
        == "not_enough_data"
    )

    ask_buckets = score["roi"]["by_ask_bucket"]["taker"]
    current_buckets = score["roi"]["by_current_state_edge_bucket"]["taker"]
    phantom_buckets = score["roi"]["by_phantom_confirmation_bucket"]["taker"]
    assert ask_buckets
    assert current_buckets
    assert phantom_buckets


def test_model_maturity_cli_writes_json_and_markdown(tmp_path):
    rows = [
        _row(
            family=bmm.SCORE_EVENT_TRANSITION,
            date="2026-05-01",
            win=True,
            ask=0.62,
            fv=0.78,
            current_edge=0.09,
            score_confirmed=True,
        ),
        _row(
            family=bmm.SCORE_EVENT_TRANSITION,
            date="2026-05-02",
            win=False,
            ask=0.68,
            fv=0.24,
            current_edge=0.01,
            no_score_60s=True,
        ),
    ]
    table = tmp_path / "rows.jsonl"
    output = tmp_path / "out"
    _write_jsonl(table, rows)

    rc = bmm.main([
        "--table-path",
        str(table),
        "--output-root",
        str(output),
    ])

    assert rc == 0
    json_path = output / "model_maturity_report.json"
    md_path = output / "model_maturity_report.md"
    dated_json_path = output / "2026-05-02_model_maturity_report.json"
    assert json_path.exists()
    assert md_path.exists()
    assert dated_json_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["artifact_promotability"]["any_promotable"] is False
    markdown = md_path.read_text(encoding="utf-8").lower()
    assert "not enough" in markdown
    assert "coverage checks" in markdown
