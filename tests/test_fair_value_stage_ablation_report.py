import json
from pathlib import Path

from scripts.analysis import fair_value_stage_ablation_report as fvsa


def _row(**overrides):
    base = {
        "mode": "live",
        "session_date": "2026-05-08",
        "signal_model_family": "score_event_transition",
        "away_score_before": 1,
        "home_score_before": 2,
        "inning": 5,
        "inning_state": "Top",
        "outs": 1,
        "runners_on": 0,
        "line": 8.5,
        "decision_ask": 0.50,
        "target_over_win": 1,
        "current_state_value_base_poisson": 0.52,
        "current_state_value_fv_raw": 0.53,
        "base_fair_value": 0.62,
        "stage2_run_env_delta": 0.10,
        "team_offense_delta": -0.05,
        "fair_value_raw": 0.645,
        "fair_value": 0.64,
        "inferred_runs": 1,
        "stage2_weather_model_usable": True,
        "weather_model_usable": True,
        "stadium_weather_exposure": "open",
        "weather_source_status": "ok",
        "shadow_phantom_risk_band": "low",
    }
    base.update(overrides)
    return base


class _FakeCache:
    def __init__(self):
        self.cells = {
            "current": {"o85": 0.56},
            "after": {"o85": 0.66},
        }

    def lookup_with_meta(self, **kwargs):
        if kwargs["away_score"] == 2 and kwargs["home_score"] == 2:
            return 0.71, {"state_cell_key": "after"}
        return 0.57, {"state_cell_key": "current"}


def test_stage_predictions_reconstruct_stage2_and_stage3():
    row = _row()

    preds = fvsa.stage_predictions(row)

    assert preds["market_ask_baseline"] == 0.5
    assert preds["stage1_after_score_event_inference"] == 0.62
    assert preds["stage2_after_run_env"] > preds["stage1_after_score_event_inference"]
    assert preds["stage3_after_team_offense"] == 0.645
    assert preds["final_runtime_fv"] == 0.64


def test_build_report_contains_stage_weather_and_market_sections(tmp_path):
    rows = [
        _row(target_over_win=1, decision_ask=0.50, base_fair_value=0.62, fair_value=0.64),
        _row(target_over_win=0, decision_ask=0.50, base_fair_value=0.88, fair_value=0.90),
        _row(
            signal_model_family="no_score_drift",
            target_over_win=1,
            decision_ask=0.44,
            base_fair_value=0.55,
            fair_value_raw=0.57,
            fair_value=0.58,
            stage2_run_env_delta="",
            current_state_value_stage2_run_env_delta=0.02,
            current_state_value_fv_raw=0.57,
            inferred_runs="",
        ),
    ]

    report = fvsa.build_report(
        rows,
        input_path=tmp_path / "input.jsonl",
        mode="live",
        max_date="2026-05-08",
        min_rows=1,
    )

    assert report["row_counts"]["labeled_rows"] == 3
    assert "score_event_transition" in report["stage_summaries"]
    assert "stage2_after_run_env" in report["stage_summaries"]["score_event_transition"]
    assert "weather_model_usable" in report["weather_ablation"]
    assert "score_event_transition" in report["market_anchoring"]
    assert report["incremental_comparisons"]["score_event_transition"]
    assert "bucket_diagnostics" in report
    assert "by_ask_bucket" in report["bucket_diagnostics"]


def test_filter_rows_honors_min_date():
    rows = [
        _row(session_date="2026-05-07"),
        _row(session_date="2026-05-08"),
        _row(session_date="2026-05-09"),
    ]

    out = fvsa.filter_rows(rows, mode="live", min_date="2026-05-08", max_date="2026-05-09")

    assert [r["session_date"] for r in out] == ["2026-05-08", "2026-05-09"]


def test_stage1_cache_recomputes_logged_stage1_values():
    row = _row(
        away_score_before=1,
        home_score_before=2,
        inning_state="Top",
        inferred_runs=1,
        current_state_value_base_poisson=0.11,
        base_fair_value=0.12,
    )

    preds = fvsa.stage_predictions(row, stage1_cache=_FakeCache())

    assert preds["current_state_stage1_poisson"] == 0.57
    assert preds["current_state_stage1_empirical"] == 0.56
    assert preds["stage1_after_score_event_inference"] == 0.71
    assert preds["final_runtime_fv"] is not None


def test_cli_writes_json_and_markdown(tmp_path):
    input_path = tmp_path / "rows.jsonl"
    input_path.write_text(
        "\n".join(json.dumps(_row(target_over_win=v)) for v in (1, 0, 1)) + "\n",
        encoding="utf-8",
    )
    output_root = tmp_path / "out"

    rc = fvsa.main([
        "--input-path",
        str(input_path),
        "--output-root",
        str(output_root),
        "--max-date",
        "2026-05-08",
        "--min-date",
        "2026-05-08",
        "--min-rows",
        "1",
    ])

    assert rc == 0
    report_path = output_root / "fair_value_stage_ablation_report.json"
    md_path = output_root / "fair_value_stage_ablation_report.md"
    assert report_path.exists()
    assert md_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["as_of_date"] == "2026-05-08"
    assert payload["min_date"] == "2026-05-08"
    assert "Fair Value Stage Ablation Report" in md_path.read_text(encoding="utf-8")
