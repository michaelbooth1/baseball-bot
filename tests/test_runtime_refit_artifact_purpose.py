from scripts.analysis import backtest_ev_policy as evp
from scripts.analysis import calibrate_signal_probabilities as calib
from scripts.analysis import train_calibration_market_anchored_alpha as cal_alpha
from scripts.analysis import train_market_anchored_alpha as maa


def test_calibration_runtime_refit_exports_all_label_params_but_keeps_eval_params():
    samples = [
        calib.Sample("a", "2026-05-01", "live", 0.90, "fair_value_raw", 1, "score_event_transition", None, None, None),
        calib.Sample("b", "2026-05-02", "live", 0.85, "fair_value_raw", 0, "score_event_transition", None, None, None),
        calib.Sample("c", "2026-05-03", "live", 0.80, "fair_value_raw", 1, "score_event_transition", None, None, None),
        calib.Sample("d", "2026-05-04", "live", 0.75, "fair_value_raw", 0, "score_event_transition", None, None, None),
        calib.Sample("e", "2026-05-05", "live", 0.70, "fair_value_raw", 0, "score_event_transition", None, None, None),
        calib.Sample("f", "2026-05-06", "live", 0.65, "fair_value_raw", 0, "score_event_transition", None, None, None),
    ]

    payload, report, preds = calib._fit_calibration_bundle(
        samples,
        input_path=calib.PROJECT_DIR / "dummy.jsonl",
        mode="live",
        min_date="",
        max_date="2026-05-06",
        val_frac=0.17,
        test_frac=0.17,
        input_kind="candidate_universe",
        family_mode="pooled",
        model_family="score_event_transition",
        strict=False,
        skipped_reasons={},
        probability_source_counts={"fair_value_raw": len(samples)},
        artifact_purpose="runtime-refit",
        identity_rejection_train_ece_delta=999.0,
        stability_gate_enabled=False,
    )

    assert payload["artifact_purpose"] == "runtime-refit"
    assert payload["fit_scope"] == "all_eligible_labeled_rows_after_method_selection"
    assert payload["methods"]["platt"]["params"] != payload["methods"]["platt"]["evaluation_params"]
    assert report["evaluation_platt_params"] == payload["methods"]["platt"]["evaluation_params"]
    assert report["export_platt_params"] == payload["methods"]["platt"]["params"]
    assert all(row["artifact_purpose"] == "runtime-refit" for row in preds)
    assert all("selected_prob_evaluation" in row for row in preds)
    assert all("selected_prob_runtime_refit" in row for row in preds)


def test_ev_runtime_refit_reuses_selected_hyperparams_and_all_eligible_rows():
    rows = [
        {"split": "train", "label_available": True, "target_filled": 1, "target_win": 1, "x": 0.1, "mode": "live"},
        {"split": "train", "label_available": True, "target_filled": 1, "target_win": 0, "x": 0.9, "mode": "live"},
        {"split": "validation", "label_available": True, "target_filled": 1, "target_win": 1, "x": 0.2, "mode": "live"},
        {"split": "test", "label_available": True, "target_filled": 1, "target_win": 0, "x": 0.8, "mode": "live"},
    ]

    eval_model = evp.train_binary_model(
        rows,
        label_col="target_win",
        feature_cols=["x", "mode"],
        row_filter_name="filled_only",
        strict=False,
    )
    refit_model = evp.refit_binary_model_for_runtime(
        rows,
        evaluation_model=eval_model,
        strict=False,
    )
    payload = evp._model_artifact_payload(
        trained=refit_model,
        artifact_role="ev_policy_signal_win_if_filled",
        runtime_safe=True,
        feature_policy="decision_time_runtime_reliable",
        model_family="score_event_transition",
        artifact_purpose="runtime-refit",
        evaluation_metrics=eval_model.metrics,
    )

    assert payload["artifact_purpose"] == "runtime-refit"
    assert payload["fit_scope"] == "all_eligible_labeled_rows_after_hyperparameter_selection"
    assert payload["search_selected"]["runtime_refit_rows"] == 4
    assert payload["evaluation_metrics"]["rows"]["train"] == 2
    assert payload["metrics"]["runtime_refit"]["rows"] == 4


def test_market_anchored_alpha_runtime_refit_keeps_evaluation_model_sidecar():
    rows = [
        {"split": "train", "target_win": 1, "market_price": 0.55, "side_row_id": "a", "session_date": "2026-05-01", "side": "over", "inning": 4},
        {"split": "train", "target_win": 0, "market_price": 0.58, "side_row_id": "b", "session_date": "2026-05-02", "side": "over", "inning": 5},
        {"split": "validation", "target_win": 1, "market_price": 0.57, "side_row_id": "c", "session_date": "2026-05-03", "side": "over", "inning": 6},
        {"split": "test", "target_win": 0, "market_price": 0.60, "side_row_id": "d", "session_date": "2026-05-04", "side": "over", "inning": 7},
    ]
    _report, eval_payload, _preds = maa.train_alpha_model(
        rows,
        feature_cols=["inning"],
        min_train_rows=2,
        max_iter=25,
        strict=False,
    )

    refit_payload = cal_alpha._refit_alpha_model_payload(
        rows,
        evaluation_model_payload=eval_payload,
        feature_cols=["inning"],
        max_iter=25,
        strict=False,
    )

    assert refit_payload["artifact_purpose"] == "runtime-refit"
    assert refit_payload["fit_scope"] == "all_eligible_labeled_rows_after_hyperparameter_selection"
    assert refit_payload["search_selected"]["runtime_refit_rows"] == 4
    assert refit_payload["evaluation_model"]["model"] == eval_payload["model"]
    assert refit_payload["metrics"]["runtime_refit"]["rows"] == 4
