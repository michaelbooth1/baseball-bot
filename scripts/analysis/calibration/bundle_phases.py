"""Phase helpers for `_fit_calibration_bundle` in
calibrate_signal_probabilities.py. Extracted on 2026-05-25 (Tier 2.5)
to split a 311-line monster into 4 focused phases:

  - _score_methods_on_splits   (predictions + eval metrics for raw/platt/isotonic)
  - _select_method_with_audits (best-method selection + stability gate + input-drift)
  - _build_calibration_payload (the JSON payload + report payload dicts)
  - _build_prediction_rows     (per-bet prediction row list)

The orchestrator (`_fit_calibration_bundle`) calls each in sequence
and returns their composed result. Pure decomposition: behavior is
identical to the pre-refactor version.

This module is internal to the calibration subpackage. Tests should
exercise it through `_fit_calibration_bundle`, not directly.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .methods import (
    _fit_platt,
    _fit_isotonic,
    _predict_platt,
    _predict_isotonic,
    _metrics_bundle,
    _select_best_method,
)
from .stability_gate import _apply_stability_gate

LOGGER = logging.getLogger("calibrate_signal_probabilities")

# Mirrors the constants in calibrate_signal_probabilities.py; kept here
# so this module is self-contained.
INPUT_DRIFT_TRIGGER_PSI_THRESHOLD = 0.25
INPUT_DRIFT_TRIGGER_MIN_MAJOR_FEATURES = 2


def _score_methods_on_splits(
    *,
    train_probs: Sequence[float],
    train_labels: Sequence[int],
    val_probs: Sequence[float],
    val_labels: Sequence[int],
    test_probs: Sequence[float],
    test_labels: Sequence[int],
    platt_params: Dict[str, float],
    isotonic_params: Dict[str, List[float]],
    export_platt_params: Dict[str, float],
    export_isotonic_params: Dict[str, List[float]],
    artifact_purpose: str,
) -> Tuple[
    Dict[str, Dict[str, List[float]]],
    Dict[str, Dict[str, List[float]]],
    Dict[str, Dict[str, Any]],
]:
    """Phase 2 + part of Phase 3. Score raw/platt/isotonic on each split,
    plus the export variants when artifact_purpose='runtime-refit'.
    Returns (method_predictions, export_method_predictions, method_eval).
    """
    method_predictions: Dict[str, Dict[str, List[float]]] = {
        "raw": {
            "train": list(train_probs),
            "validation": list(val_probs),
            "test": list(test_probs),
        },
        "platt": {
            "train": _predict_platt(train_probs, platt_params),
            "validation": _predict_platt(val_probs, platt_params),
            "test": _predict_platt(test_probs, platt_params),
        },
        "isotonic": {
            "train": _predict_isotonic(train_probs, isotonic_params),
            "validation": _predict_isotonic(val_probs, isotonic_params),
            "test": _predict_isotonic(test_probs, isotonic_params),
        },
    }
    export_method_predictions: Dict[str, Dict[str, List[float]]] = method_predictions
    if artifact_purpose == "runtime-refit":
        export_method_predictions = {
            "raw": method_predictions["raw"],
            "platt": {
                "train": _predict_platt(train_probs, export_platt_params),
                "validation": _predict_platt(val_probs, export_platt_params),
                "test": _predict_platt(test_probs, export_platt_params),
            },
            "isotonic": {
                "train": _predict_isotonic(train_probs, export_isotonic_params),
                "validation": _predict_isotonic(val_probs, export_isotonic_params),
                "test": _predict_isotonic(test_probs, export_isotonic_params),
            },
        }

    method_eval: Dict[str, Dict[str, Any]] = {}
    for name in ["raw", "platt", "isotonic"]:
        method_eval[name] = {
            "train": _metrics_bundle(train_labels, train_probs, method_predictions[name]["train"]),
            "validation": _metrics_bundle(val_labels, val_probs, method_predictions[name]["validation"]),
            "test": _metrics_bundle(test_labels, test_probs, method_predictions[name]["test"]),
        }
    return method_predictions, export_method_predictions, method_eval


def _select_method_with_audits(
    method_eval: Dict[str, Dict[str, Any]],
    *,
    model_family: Optional[str],
    max_date: str,
    identity_rejection_train_ece_delta: float,
    stability_history: Optional[List[Dict[str, Any]]],
    stability_window: int,
    stability_min_history: int,
    stability_gate_enabled: bool,
    input_drift_status: Optional[Dict[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    """Phase 3. Select best method, then run the stability gate and
    input-drift audit. Logs each event at WARNING.

    Returns (selected_method, selection_audit).
    """
    selected_method, selection_audit = _select_best_method(
        method_eval,
        identity_rejection_train_ece_delta=identity_rejection_train_ece_delta,
    )
    if selection_audit.get("identity_rejection_applied"):
        family_msg = f" for {model_family}" if model_family else ""
        LOGGER.warning(
            "Identity-rejection guard fired%s: validation picked 'raw' (logloss=%.4f) "
            "but train ECE shows '%s' is %.4f better calibrated (raw=%.4f vs "
            "challenger=%.4f, threshold=%.2f). Selected '%s'.",
            family_msg,
            float(selection_audit.get("primary_validation_logloss") or 0.0),
            selection_audit.get("challenger"),
            float(selection_audit.get("identity_rejection_train_ece_gap") or 0.0),
            float(selection_audit.get("raw_train_ece") or 0.0),
            float(selection_audit.get("challenger_train_ece") or 0.0),
            float(selection_audit.get("identity_rejection_threshold") or 0.0),
            selected_method,
        )

    # Stability gate
    pre_override_method = selected_method
    selection_audit["pre_override_selected"] = pre_override_method
    if stability_gate_enabled and model_family and stability_history is not None:
        exclude_date = (max_date or "") and str(max_date)[:10]
        final_method, stability_audit = _apply_stability_gate(
            pre_override_method,
            stability_history,
            model_family,
            window=stability_window,
            min_history=stability_min_history,
            exclude_date=exclude_date or None,
        )
        selection_audit.update(stability_audit)
        if stability_audit.get("stability_gate_applied"):
            family_msg = f" for {model_family}" if model_family else ""
            LOGGER.warning(
                "Calibration stability gate fired%s: today's pick was '%s' but "
                "trailing %d-day modal is '%s' (history=%s). Overriding to '%s' "
                "to avoid daily flip-flop on small validation samples.",
                family_msg, pre_override_method,
                stability_audit.get("stability_window"),
                stability_audit.get("stability_modal"),
                stability_audit.get("stability_history"),
                final_method,
            )
        selected_method = final_method
    else:
        selection_audit["stability_gate_enabled"] = False

    # Input-drift audit
    if input_drift_status is not None:
        selection_audit.update(input_drift_status)
        if input_drift_status.get("input_drift_triggered"):
            family_msg = f" for {model_family}" if model_family else ""
            top = input_drift_status.get("input_drift_major_features") or []
            top_summary = ", ".join(
                f"{r['feature']} PSI {r['psi']:.2f}" for r in top[:5]
            )
            LOGGER.warning(
                "Input-drift flag set%s: %d continuous features at PSI >= %.2f "
                "(threshold trigger %d). Top: %s. Today's calibration method "
                "(%s) was selected on materially-shifted inputs.",
                family_msg,
                len(top),
                INPUT_DRIFT_TRIGGER_PSI_THRESHOLD,
                INPUT_DRIFT_TRIGGER_MIN_MAJOR_FEATURES,
                top_summary,
                selected_method,
            )
    return selected_method, selection_audit


def _build_calibration_payload(
    *,
    now_iso: str,
    selected_method: str,
    selection_audit: Dict[str, Any],
    method_eval: Dict[str, Dict[str, Any]],
    platt_params: Dict[str, float],
    isotonic_params: Dict[str, List[float]],
    export_platt_params: Dict[str, float],
    export_isotonic_params: Dict[str, List[float]],
    side: str,
    input_path: str,
    input_kind: str,
    mode: str,
    family_mode: str,
    model_family: Optional[str],
    min_date: str,
    max_date: str,
    n_total: int,
    n_train: int,
    n_val: int,
    n_test: int,
    split_dates: Dict[str, List[str]],
    skipped_reasons: Dict[str, int],
    probability_source_counts: Dict[str, int],
    family_counts: Dict[str, int],
    artifact_purpose: str,
    runtime_fit_scope: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Phase 4. Build the calibration_payload + report_payload dicts.
    Pure dict construction; no side effects."""
    calibration_payload = {
        "schema_version": 1,
        "generated_at_utc": now_iso,
        "selected_method": selected_method,
        "selection_metric": "validation_logloss",
        "selection_audit": selection_audit,
        "methods": {
            "raw": {
                "params": {},
                "metrics": method_eval["raw"],
            },
            "platt": {
                "params": export_platt_params,
                "evaluation_params": platt_params,
                "metrics": method_eval["platt"],
            },
            "isotonic": {
                "params": export_isotonic_params,
                "evaluation_params": isotonic_params,
                "metrics": method_eval["isotonic"],
            },
        },
        "side": side,
        "data": {
            "input_path": input_path,
            "input_kind": input_kind,
            "mode": mode,
            "side": side,
            "family_mode": family_mode,
            "model_family": model_family,
            "min_date": min_date or None,
            "max_date": max_date or None,
            "rows_total": n_total,
            "rows_train": n_train,
            "rows_validation": n_val,
            "rows_test": n_test,
            "dates_by_split": split_dates,
            "skipped_reasons": skipped_reasons,
            "probability_source_counts": probability_source_counts,
            "model_family_counts": family_counts,
        },
        "artifact_purpose": artifact_purpose,
        "fit_scope": runtime_fit_scope,
        "selection_source": "train_validation_test_evaluation_split",
        "notes": {
            "label": (
                ("under_win = 1 - over_win; flipped from " if side == "under" else "")
                + "won_counterfactual for signals_master; "
                "target_counterfactual_win for candidate_universe; "
                "target_over_win (gated on label_final_available) for "
                "calibration_opportunity_training"
            ),
            "probability_field": (
                "1 - fair_value_raw (fallback: 1 - fair_value)"
                if side == "under"
                else "fair_value_raw (fallback: fair_value)"
            ),
            "selection_policy": "Best validation logloss among raw/platt/isotonic.",
            "runtime_modes": (
                "Phase A2: UNDER calibration is offline / shadow only "
                "until Phase B/C wire it into the live engine."
                if side == "under"
                else "Use with --prob-calibration-mode shadow|enforce."
            ),
        },
    }

    report_payload = {
        "generated_at_utc": now_iso,
        "selected_method": selected_method,
        "method_eval": method_eval,
        "artifact_purpose": artifact_purpose,
        "fit_scope": runtime_fit_scope,
        "evaluation_platt_params": platt_params,
        "evaluation_isotonic_params": isotonic_params,
        "export_platt_params": export_platt_params,
        "export_isotonic_params": export_isotonic_params,
        "dataset": calibration_payload["data"],
    }
    return calibration_payload, report_payload


def _build_prediction_rows(
    *,
    split_buckets: Dict[str, List[Any]],
    method_predictions: Dict[str, Dict[str, List[float]]],
    export_method_predictions: Dict[str, Dict[str, List[float]]],
    selected_method: str,
    artifact_purpose: str,
) -> List[Dict[str, Any]]:
    """Phase 5. Build the per-bet prediction row list for the report
    sidecar. Each row records all 3 method probabilities + the
    selected one (both evaluation-fit and runtime-refit variants).
    """
    pred_rows: List[Dict[str, Any]] = []
    for split_name, bucket in split_buckets.items():
        raw_ps = method_predictions["raw"][split_name]
        platt_ps = method_predictions["platt"][split_name]
        iso_ps = method_predictions["isotonic"][split_name]
        export_raw_ps = export_method_predictions["raw"][split_name]
        export_platt_ps = export_method_predictions["platt"][split_name]
        export_iso_ps = export_method_predictions["isotonic"][split_name]
        for i, s in enumerate(bucket):
            selected_prob_evaluation = (
                raw_ps[i]
                if selected_method == "identity"
                else platt_ps[i]
                if selected_method == "platt"
                else iso_ps[i]
            )
            selected_prob_runtime_refit = (
                export_raw_ps[i]
                if selected_method == "identity"
                else export_platt_ps[i]
                if selected_method == "platt"
                else export_iso_ps[i]
            )
            pred_rows.append(
                {
                    "bet_id": s.bet_id,
                    "session_date": s.session_date,
                    "mode": s.mode,
                    "model_family": s.model_family,
                    "split": split_name,
                    "label": s.label,
                    "raw_prob": raw_ps[i],
                    "raw_prob_source": s.raw_prob_source,
                    "platt_prob": platt_ps[i],
                    "isotonic_prob": iso_ps[i],
                    "selected_prob": selected_prob_evaluation,
                    "selected_prob_evaluation": selected_prob_evaluation,
                    "selected_prob_runtime_refit": selected_prob_runtime_refit,
                    "artifact_purpose": artifact_purpose,
                    "decision_ask": s.decision_ask,
                    "line": s.line,
                    "inning": s.inning,
                }
            )
    return pred_rows
