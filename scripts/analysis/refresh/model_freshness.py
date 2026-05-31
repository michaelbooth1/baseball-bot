"""model_freshness_health inline handler.

Compares Stage-2 staging vs production caches and age-checks the
canonical model artifacts. Descriptive only; never fails the refresh.
"""
from __future__ import annotations

from typing import List, Tuple

from . import config as _config
from .config import (
    RefreshConfig,
    STAGE2_BRIER_DRIFT_THRESHOLD,
    STAGE2_PROMOTION_WINDOW,
    STALE_MODEL_AGE_DAYS,
)
from .helpers import _now_iso
from .preflight import _inline, _safe_load_json
from .promotion_stage2 import (
    _artifact_age_days,
    _load_stage2_brier_history,
    _stage2_promotion_verdict,
    _stage2_validation_brier,
    _write_stage2_brier_history_row,
)


@_inline("model_freshness_health")
def _handle_model_freshness_health(config: RefreshConfig) -> Tuple[bool, str]:
    """Compare staging vs production Stage-2 + age-check model artifacts.

    Returns (ok, notes). Never fails the refresh -- this is descriptive
    only. The point is to surface promotion candidates and stale
    artifacts so they're visible at the end of every refresh.
    """
    notes: List[str] = []

    # Stage-2 staging vs production comparison.
    prod_path = config.stage2_cache_path
    staging_path = _config.PROJECT_DIR / "cache" / "mlb_stage2_run_env.staging.json"
    if staging_path.exists():
        prod_payload, prod_err = _safe_load_json(prod_path)
        stg_payload, stg_err = _safe_load_json(staging_path)
        if prod_err or stg_err:
            notes.append(
                f"Stage-2 comparison skipped: prod_err={prod_err} staging_err={stg_err}"
            )
        else:
            prod_brier = _stage2_validation_brier(prod_payload)
            stg_brier = _stage2_validation_brier(stg_payload)
            if prod_brier is None or stg_brier is None:
                notes.append(
                    f"Stage-2 comparison: validation Brier not found on one side "
                    f"(prod={prod_brier} staging={stg_brier})."
                )
            else:
                delta = stg_brier - prod_brier
                if abs(delta) >= STAGE2_BRIER_DRIFT_THRESHOLD:
                    direction = "IMPROVES" if delta < 0 else "REGRESSES"
                    notes.append(
                        f"ALERT Stage-2 staging {direction} validation Brier by "
                        f"{abs(delta):.4f} ({stg_brier:.4f} vs {prod_brier:.4f}). "
                        f"Promote with: copy {staging_path.name} -> {prod_path.name}."
                    )
                else:
                    notes.append(
                        f"ok Stage-2 staging matches production within tolerance "
                        f"(staging Brier {stg_brier:.4f} vs prod {prod_brier:.4f})."
                    )
                # Append today's observation to history, then check the
                # multi-day promotion stability gate.
                history_path = config.stage2_brier_history_path
                today_date_key = (config.max_date or config.active_date or "")[:10] or None
                _write_stage2_brier_history_row(
                    history_path,
                    production_brier=prod_brier,
                    staging_brier=stg_brier,
                    delta=delta,
                    data_max_date=today_date_key,
                    generated_at_utc=_now_iso(),
                )
                history_rows = _load_stage2_brier_history(history_path)
                verdict = _stage2_promotion_verdict(
                    history_rows,
                    exclude_date=today_date_key,
                )
                v_label = verdict["verdict"]
                if v_label == "promote":
                    notes.append(
                        f"ALERT Stage-2 PROMOTION READY: staging beat production by "
                        f">= {verdict['min_delta']:.4f} on "
                        f"{verdict['n_improving']}/{verdict['n_history']} of the last "
                        f"{STAGE2_PROMOTION_WINDOW} distinct dates "
                        f"(threshold {verdict['n_consecutive_required']}). "
                        f"Promote with: copy {staging_path.name} -> {prod_path.name}."
                    )
                elif v_label == "hold":
                    notes.append(
                        f"ok Stage-2 promotion stability gate: hold "
                        f"({verdict['n_improving']}/{verdict['n_history']} improving days, "
                        f"need {verdict['n_consecutive_required']})."
                    )
                else:  # insufficient_history
                    notes.append(
                        f"ok Stage-2 promotion stability gate: building history "
                        f"({verdict['n_history']}/{verdict['n_history_required']} "
                        f"distinct prior dates)."
                    )
    else:
        notes.append("Stage-2 staging artifact not present (retrain step skipped?).")

    # Stale-artifact age checks.
    age_targets = [
        ("Stage-2 production cache", config.stage2_cache_path),
        ("Stage-1 OU cache", config.mlb_ou_cache_path),
        ("EV-policy report",
         _config.PROJECT_DIR / "data" / "analysis_output" / "ev_policy" / "ev_policy_report.json"),
        ("EV-policy win model",
         _config.PROJECT_DIR / "data" / "analysis_output" / "model_baselines" / "signal_win_model.json"),
        ("EV-policy fill model",
         _config.PROJECT_DIR / "data" / "analysis_output" / "model_baselines" / "execution_fill_model.json"),
        ("Calibration artifact",
         _config.PROJECT_DIR / "data" / "analysis_output" / "calibration" / "signal_win_calibration.json"),
        ("Learned execution policy report",
         _config.PROJECT_DIR / "data" / "analysis_output" / "execution_policy_prototype" / "learned_execution_policy_report.json"),
        ("Stage-3 v2 fit (phase4_models.json)",
         _config.PROJECT_DIR / "data" / "analysis_output" / "team_offense_calibration" / "phase4_models.json"),
    ]
    for label, path in age_targets:
        age = _artifact_age_days(path)
        if age is None:
            notes.append(f"WARNING {label} missing at {path.name}")
            continue
        if age > STALE_MODEL_AGE_DAYS:
            notes.append(
                f"ALERT {label} is {age:.1f}d old (> {STALE_MODEL_AGE_DAYS}d). "
                "Check the corresponding rebuild step ran successfully."
            )
        else:
            notes.append(f"ok {label}: {age:.1f}d old")

    return True, "\n".join(notes)
