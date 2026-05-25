import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Sequence

from .constants import (
    CALIBRATION_STALE_AGE_DAYS,
    COHORT_ROI_MIN_BETS_FOR_ALERT,
    COHORT_ROI_LOSING_THRESHOLD,
    COHORT_ROI_TRAILING_WINDOW_DAYS,
    COHORT_ROI_BASELINE_WINDOW_DAYS,
    COHORT_ROI_REGIME_DELTA,
    PROMOTION_ATTRIBUTION_WINDOW_DAYS,
    DEFAULT_PROMOTION_EVENTS_LOG,
    COHORT_CALIBRATION_MIN_N_FOR_ALERT,
    COHORT_CALIBRATION_GAP_RATIO_ALERT,
    COHORT_CALIBRATION_MIN_AGGREGATE_GAP,
    COHORT_CALIBRATION_AGGREGATE_GAP_ALERT,
    COHORT_CALIBRATION_AGGREGATE_MIN_N,
    COHORT_CALIBRATION_WINDOW_DAYS,
    CALIBRATION_NEAR_IDENTITY_DELTA,
    CALIBRATION_LOW_APPLIED_SHARE,
    CALIBRATION_SHADOW_MODE_DOMINANT_SHARE,
    CALIBRATOR_ENFORCE_BAND_GATE_THRESHOLD,
    CALIBRATOR_ENFORCE_MIN_EDGE_LOW_LINE,
    CALIBRATOR_ENFORCE_MIN_EDGE_HIGH_LINE,
    CALIBRATOR_ENFORCE_HIGH_LINE_CUTOFF,
    CALIBRATOR_ENFORCE_HIGH_BLOCK_RATE_ALERT,
    CALIBRATOR_ENFORCE_MIN_BAND_GATED_CANDIDATES_FOR_ZERO_ALERT,
    CALIBRATOR_ENFORCE_VOLUME_DROP_ALERT_PP,
    CALIBRATOR_ENFORCE_BASELINE_MIN_DAYS,
    CALIBRATOR_ENFORCE_BASELINE_WINDOW_DAYS,
    CALIBRATOR_ENFORCE_BLOCKED_OUTCOMES_DEFAULT_STAKE,
    CALIBRATOR_ENFORCE_BLOCKED_WR_MUTING_WINNERS,
    CALIBRATOR_ENFORCE_BLOCKED_OUTCOMES_MIN_FOR_ALERT,
    CALIBRATOR_ENFORCE_BLOCKED_NEGATIVE_SAVE_ALERT,
)
from .helpers import (
    _load_json,
    _load_jsonl,
    _safe_float,
    _safe_int,
    _wilson_upper_bound,
    _shift_date,
    _artifact_age_days,
    _drift_ask_bucket,
    _drift_current_state_edge_bucket,
)

def _per_family_calibration_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    by_family: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        family = str(
            row.get("fair_value_calibration_family")
            or row.get("signal_model_family")
            or "unknown"
        )
        by_family.setdefault(family, []).append(row)

    out: Dict[str, Dict[str, Any]] = {}
    for family, family_rows in sorted(by_family.items()):
        deltas: List[float] = []
        raws: List[float] = []
        cals: List[float] = []
        applied_flags: List[bool] = []
        methods: Counter = Counter()
        modes: Counter = Counter()
        for row in family_rows:
            raw = row.get("fair_value_raw")
            cal = row.get("fair_value_calibrated")
            method = str(row.get("fair_value_calibration_method") or "")
            if method:
                methods[method] += 1
            mode = str(row.get("fair_value_calibration_mode") or "")
            if mode:
                modes[mode] += 1
            applied = row.get("fair_value_calibration_applied")
            if applied is not None:
                applied_flags.append(bool(applied))
            try:
                if raw is None or cal is None:
                    continue
                rf = float(raw)
                cf = float(cal)
            except (TypeError, ValueError):
                continue
            raws.append(rf)
            cals.append(cf)
            deltas.append(abs(cf - rf))

        mode_total = sum(modes.values())
        shadow_share = (
            modes.get("shadow", 0) / mode_total if mode_total > 0 else None
        )

        if not deltas:
            out[family] = {
                "rows_total": len(family_rows),
                "rows_with_both_probs": 0,
                "mean_abs_delta": None,
                "max_abs_delta": None,
                "mean_raw": None,
                "mean_calibrated": None,
                "applied_share": None,
                "method_counts": dict(methods),
                "mode_counts": dict(modes),
                "shadow_share": round(shadow_share, 4) if shadow_share is not None else None,
            }
            continue

        applied_share = (
            sum(1 for f in applied_flags if f) / len(applied_flags)
            if applied_flags
            else None
        )
        out[family] = {
            "rows_total": len(family_rows),
            "rows_with_both_probs": len(deltas),
            "mean_abs_delta": round(sum(deltas) / len(deltas), 6),
            "max_abs_delta": round(max(deltas), 6),
            "mean_raw": round(sum(raws) / len(raws), 6),
            "mean_calibrated": round(sum(cals) / len(cals), 6),
            "applied_share": round(applied_share, 4) if applied_share is not None else None,
            "method_counts": dict(methods),
            "mode_counts": dict(modes),
            "shadow_share": round(shadow_share, 4) if shadow_share is not None else None,
        }
    return out


# Bucket helpers + COHORT_DIMENSIONS moved to calibration_buckets.py
# on 2026-05-25. Re-exported here for back-compat with any caller
# still doing `from .calibration_health import _cohort_edge_bucket` etc.
from .calibration_buckets import (  # noqa: F401  (re-export)
    _cohort_edge_bucket,
    _cohort_inning_bucket,
    _cohort_line_bucket,
    COHORT_DIMENSIONS,
)


def _collect_window_filled_bets(reviews: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for review in reviews:
        bets = review.get("bets") or []
        for bet in bets:
            if str(bet.get("status") or "") == "filled":
                out.append(bet)
    return out


def _aggregate_cohort(
    bets: List[Dict[str, Any]], bucket_fn: Callable[[Dict[str, Any]], str]
) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for bet in bets:
        bucket = bucket_fn(bet)
        agg = grouped.setdefault(
            bucket,
            {"n": 0, "wins": 0, "losses": 0, "profit": 0.0, "stake": 0.0},
        )
        agg["n"] += 1
        profit = _safe_float(bet.get("profit"))
        cost = _safe_float(bet.get("fill_cost_usdc"), _safe_float(bet.get("stake_or_cost"), 0.0))
        agg["profit"] += profit
        agg["stake"] += cost
        won = bet.get("won")
        if won is True:
            agg["wins"] += 1
        elif won is False:
            agg["losses"] += 1
    for bucket, agg in grouped.items():
        n = agg["n"]
        agg["wr"] = (agg["wins"] / n) if n else None
        agg["roi"] = (agg["profit"] / agg["stake"]) if agg["stake"] else None
        agg["wilson_ub_wr"] = _wilson_upper_bound(agg["wins"], agg["wins"] + agg["losses"])
        agg["profit"] = round(agg["profit"], 2)
        agg["stake"] = round(agg["stake"], 2)
        if agg["wr"] is not None:
            agg["wr"] = round(agg["wr"], 4)
        if agg["roi"] is not None:
            agg["roi"] = round(agg["roi"], 4)
        if agg["wilson_ub_wr"] is not None:
            agg["wilson_ub_wr"] = round(agg["wilson_ub_wr"], 4)
    return grouped


# Alert-attribution helpers + recent-events readers moved to
# calibration_attribution.py on 2026-05-25. Re-exported here for
# back-compat with any caller still doing
# `from .calibration_health import _attribute_alert_to_promotions` etc.
from .calibration_attribution import (  # noqa: F401  (re-export)
    _recent_events_by_direction,
    _recent_promotions,
    _recent_demotions,
    _attribute_alert_to_promotions,
    _attribute_alert_to_demotions,
    _major_drift_features,
    _attribute_alert_to_concept_drift,
    CONCEPT_DRIFT_ATTRIBUTION_TOP_N,
)


def _cohort_roi_health(
    *,
    today_bet_rows: List[Dict[str, Any]],
    trailing_reviews: List[Dict[str, Any]],
    baseline_reviews: List[Dict[str, Any]],
    session_date: Optional[str] = None,
    promotion_events_log_path: Path = DEFAULT_PROMOTION_EVENTS_LOG,
    concept_drift_health: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    today_filled = [b for b in today_bet_rows if str(b.get("status") or "") == "filled"]
    recent_bets = today_filled + _collect_window_filled_bets(trailing_reviews)
    baseline_bets = today_filled + _collect_window_filled_bets(baseline_reviews)

    cohorts_by_dim: Dict[str, Dict[str, Dict[str, Any]]] = {}
    baseline_cohorts_by_dim: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for dim_name, bucket_fn in COHORT_DIMENSIONS:
        cohorts_by_dim[dim_name] = _aggregate_cohort(recent_bets, bucket_fn)
        baseline_cohorts_by_dim[dim_name] = _aggregate_cohort(baseline_bets, bucket_fn)

    alerts: List[str] = []

    for dim_name, buckets in cohorts_by_dim.items():
        for bucket_label, agg in buckets.items():
            if bucket_label == "missing":
                continue
            if agg["n"] < COHORT_ROI_MIN_BETS_FOR_ALERT:
                continue
            roi = agg["roi"]
            if roi is None or roi > COHORT_ROI_LOSING_THRESHOLD:
                continue
            alerts.append(
                f"{dim_name}={bucket_label} cohort ROI {roi * 100:+.1f}% over "
                f"trailing {COHORT_ROI_TRAILING_WINDOW_DAYS}d "
                f"(n={agg['n']}, {agg['wins']}W/{agg['losses']}L, "
                f"profit=${agg['profit']:+.2f} on ${agg['stake']:.2f} stake); "
                f"absolute loss threshold {COHORT_ROI_LOSING_THRESHOLD * 100:+.0f}%."
            )

    for dim_name, buckets in cohorts_by_dim.items():
        baseline_buckets = baseline_cohorts_by_dim.get(dim_name, {})
        for bucket_label, recent_agg in buckets.items():
            if bucket_label == "missing":
                continue
            base_agg = baseline_buckets.get(bucket_label) or {}
            recent_roi = recent_agg.get("roi")
            base_roi = base_agg.get("roi")
            if recent_roi is None or base_roi is None:
                continue
            if recent_agg["n"] < COHORT_ROI_MIN_BETS_FOR_ALERT:
                continue
            if base_agg.get("n", 0) < COHORT_ROI_MIN_BETS_FOR_ALERT:
                continue
            if recent_roi <= COHORT_ROI_LOSING_THRESHOLD:
                continue
            delta = recent_roi - base_roi
            if delta > -COHORT_ROI_REGIME_DELTA:
                continue
            alerts.append(
                f"{dim_name}={bucket_label} cohort ROI flipped: "
                f"trailing {COHORT_ROI_TRAILING_WINDOW_DAYS}d {recent_roi * 100:+.1f}% "
                f"vs baseline {COHORT_ROI_BASELINE_WINDOW_DAYS}d {base_roi * 100:+.1f}% "
                f"(delta {delta * 100:+.1f}pp <= -{COHORT_ROI_REGIME_DELTA * 100:.0f}pp); "
                f"n_recent={recent_agg['n']}, n_baseline={base_agg['n']}."
            )

    recent_proms: List[Dict[str, Any]] = []
    recent_demos: List[Dict[str, Any]] = []
    if session_date is not None:
        recent_proms = _recent_promotions(
            today=session_date, log_path=promotion_events_log_path,
        )
        recent_demos = _recent_demotions(
            today=session_date, log_path=promotion_events_log_path,
        )
    if recent_proms and alerts:
        alerts = [
            _attribute_alert_to_promotions(a, recent_proms, today=session_date or "")
            for a in alerts
        ]
    if recent_demos and alerts:
        alerts = [
            _attribute_alert_to_demotions(a, recent_demos, today=session_date or "")
            for a in alerts
        ]
    drift_features = _major_drift_features(concept_drift_health)
    if drift_features and alerts:
        alerts = [
            _attribute_alert_to_concept_drift(a, drift_features)
            for a in alerts
        ]

    return {
        "alerts": alerts,
        "window_days": COHORT_ROI_TRAILING_WINDOW_DAYS,
        "baseline_window_days": COHORT_ROI_BASELINE_WINDOW_DAYS,
        "n_recent_filled": len(recent_bets),
        "n_baseline_filled": len(baseline_bets),
        "cohorts_by_dimension": cohorts_by_dim,
        "baseline_cohorts_by_dimension": baseline_cohorts_by_dim,
        "recent_promotions_count": len(recent_proms),
        "recent_demotions_count": len(recent_demos),
        "concept_drift_major_features_count": len(drift_features),
        "thresholds": {
            "min_bets_for_alert": COHORT_ROI_MIN_BETS_FOR_ALERT,
            "losing_threshold": COHORT_ROI_LOSING_THRESHOLD,
            "regime_delta": COHORT_ROI_REGIME_DELTA,
        },
    }


def _bet_is_calibratable(bet: Dict[str, Any]) -> bool:
    if str(bet.get("status") or "") != "filled":
        return False
    won = bet.get("won")
    if won not in (True, False):
        return False
    fv = bet.get("fair_value")
    try:
        fv = float(fv)
    except (TypeError, ValueError):
        return False
    if not (0.0 <= fv <= 1.0):
        return False
    return True


def _aggregate_calibration(
    bets: List[Dict[str, Any]],
) -> Dict[str, Any]:
    n = 0
    sum_fv = 0.0
    sum_won = 0.0
    sum_sq = 0.0
    for b in bets:
        try:
            fv = float(b.get("fair_value"))
        except (TypeError, ValueError):
            continue
        won = b.get("won")
        if won not in (True, False):
            continue
        n += 1
        sum_fv += fv
        sum_won += 1.0 if won else 0.0
        sum_sq += (fv - (1.0 if won else 0.0)) ** 2
    if n == 0:
        return {
            "n": 0,
            "mean_fair_value": None,
            "mean_won": None,
            "reliability_gap": None,
            "brier": None,
        }
    mean_fv = sum_fv / n
    mean_won = sum_won / n
    return {
        "n": n,
        "mean_fair_value": round(mean_fv, 4),
        "mean_won": round(mean_won, 4),
        "reliability_gap": round(abs(mean_fv - mean_won), 4),
        "brier": round(sum_sq / n, 4),
    }


def _cohort_calibration_health(
    *,
    today_bet_rows: List[Dict[str, Any]],
    trailing_reviews: List[Dict[str, Any]],
    session_date: Optional[str] = None,
    promotion_events_log_path: Path = DEFAULT_PROMOTION_EVENTS_LOG,
    concept_drift_health: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    today_calibratable = [
        b for b in today_bet_rows if _bet_is_calibratable(b)
    ]
    window_bets = (
        today_calibratable
        + [
            b for b in _collect_window_filled_bets(trailing_reviews)
            if _bet_is_calibratable(b)
        ]
    )

    aggregate = _aggregate_calibration(window_bets)
    cohorts_by_dim: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for dim_name, bucket_fn in COHORT_DIMENSIONS:
        per_bucket: Dict[str, List[Dict[str, Any]]] = {}
        for b in window_bets:
            bucket = bucket_fn(b)
            per_bucket.setdefault(bucket, []).append(b)
        cohorts_by_dim[dim_name] = {
            label: _aggregate_calibration(rows)
            for label, rows in per_bucket.items()
        }

    alerts: List[str] = []
    aggregate_gap = aggregate.get("reliability_gap")
    aggregate_n = aggregate.get("n", 0)

    if (
        aggregate_gap is not None
        and aggregate_gap >= COHORT_CALIBRATION_AGGREGATE_GAP_ALERT
        and aggregate_n >= COHORT_CALIBRATION_AGGREGATE_MIN_N
    ):
        mean_fv_agg = aggregate.get("mean_fair_value") or 0.0
        mean_won_agg = aggregate.get("mean_won") or 0.0
        direction = (
            "over-predicting" if mean_fv_agg > mean_won_agg
            else "under-predicting"
        )
        alerts.append(
            f"aggregate calibration reliability gap "
            f"{aggregate_gap * 100:.1f}pp over trailing "
            f"{COHORT_CALIBRATION_WINDOW_DAYS}d (n={aggregate_n}, "
            f"mean_fv {mean_fv_agg * 100:.1f}% vs mean_won "
            f"{mean_won_agg * 100:.1f}%) >= "
            f"{COHORT_CALIBRATION_AGGREGATE_GAP_ALERT * 100:.0f}pp "
            f"threshold. Model is {direction} systematically -- "
            "calibrator retrain or Stage-2/Stage-3 refresh likely "
            "warranted; cross-check concept_drift_health for input "
            "shift attribution."
        )

    aggregate_gap_ok_for_ratio = (
        aggregate_gap is not None
        and aggregate_gap >= COHORT_CALIBRATION_MIN_AGGREGATE_GAP
    )

    for dim_name, buckets in cohorts_by_dim.items():
        for bucket_label, agg in buckets.items():
            if bucket_label == "missing":
                continue
            if agg.get("n", 0) < COHORT_CALIBRATION_MIN_N_FOR_ALERT:
                continue
            cohort_gap = agg.get("reliability_gap")
            if cohort_gap is None:
                continue
            if not aggregate_gap_ok_for_ratio:
                continue
            ratio = cohort_gap / aggregate_gap if aggregate_gap else 0.0
            agg["reliability_gap_ratio_vs_aggregate"] = round(ratio, 3)
            if ratio < COHORT_CALIBRATION_GAP_RATIO_ALERT:
                continue
            mean_fv = agg.get("mean_fair_value") or 0.0
            mean_won = agg.get("mean_won") or 0.0
            direction = (
                "over-predicting" if mean_fv > mean_won
                else "under-predicting"
            )
            alerts.append(
                f"{dim_name}={bucket_label} cohort reliability gap "
                f"{cohort_gap * 100:.1f}pp (n={agg['n']}, "
                f"mean_fv {mean_fv * 100:.1f}% vs mean_won "
                f"{mean_won * 100:.1f}%) >= "
                f"{COHORT_CALIBRATION_GAP_RATIO_ALERT:.1f}x aggregate "
                f"gap {aggregate_gap * 100:.1f}pp over trailing "
                f"{COHORT_CALIBRATION_WINDOW_DAYS}d. Model is "
                f"{direction} in this cohort -- candidate for a "
                "Stage-2 / Stage-3 retrain or cohort-specific "
                "calibration adjustment."
            )

    recent_proms: List[Dict[str, Any]] = []
    recent_demos: List[Dict[str, Any]] = []
    if session_date is not None:
        recent_proms = _recent_promotions(
            today=session_date, log_path=promotion_events_log_path,
        )
        recent_demos = _recent_demotions(
            today=session_date, log_path=promotion_events_log_path,
        )
    if recent_proms and alerts:
        alerts = [
            _attribute_alert_to_promotions(
                a, recent_proms, today=session_date or "",
            )
            for a in alerts
        ]
    if recent_demos and alerts:
        alerts = [
            _attribute_alert_to_demotions(
                a, recent_demos, today=session_date or "",
            )
            for a in alerts
        ]
    drift_features = _major_drift_features(concept_drift_health)
    if drift_features and alerts:
        alerts = [
            _attribute_alert_to_concept_drift(a, drift_features)
            for a in alerts
        ]

    return {
        "alerts": alerts,
        "window_days": COHORT_CALIBRATION_WINDOW_DAYS,
        "n_filled_settled": len(window_bets),
        "aggregate": aggregate,
        "cohorts_by_dimension": cohorts_by_dim,
        "recent_promotions_count": len(recent_proms),
        "recent_demotions_count": len(recent_demos),
        "concept_drift_major_features_count": len(drift_features),
        "thresholds": {
            "min_n_for_alert": COHORT_CALIBRATION_MIN_N_FOR_ALERT,
            "gap_ratio_alert": COHORT_CALIBRATION_GAP_RATIO_ALERT,
            "min_aggregate_gap": COHORT_CALIBRATION_MIN_AGGREGATE_GAP,
        },
    }


def _calibration_artifact_metadata(
    *,
    artifact_path: Path,
    session_date: str,
    alert_side_prefix: str = "",
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "artifact_path": str(artifact_path),
        "artifact_present": artifact_path.exists(),
        "artifact_methods_by_family": {},
        "artifact_audit_by_family": {},
        "alerts": [],
    }
    alerts: List[str] = payload["alerts"]
    if not artifact_path.exists():
        alerts.append(
            f"{alert_side_prefix}calibration artifact missing at "
            f"{artifact_path}; runtime cannot calibrate."
        )
        return payload
    try:
        artifact = _load_json(artifact_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = artifact.get("generated_at_utc")
    payload["artifact_schema_version"] = artifact.get("schema_version")
    payload["artifact_default_family"] = artifact.get("default_family")
    payload["artifact_top_selected_method"] = artifact.get("selected_method")
    age = _artifact_age_days(artifact.get("generated_at_utc", ""), session_date)
    payload["artifact_age_days"] = age
    if age is not None and age > CALIBRATION_STALE_AGE_DAYS:
        alerts.append(
            f"{alert_side_prefix}calibration artifact is {age:.1f} days old "
            f"(> {CALIBRATION_STALE_AGE_DAYS}d threshold); "
            "rerun calibrate_signal_probabilities or daily refresh."
        )
    families = artifact.get("families") or {}
    methods: Dict[str, str] = {}
    audit: Dict[str, Any] = {}
    for family, family_payload in sorted(families.items()):
        if not isinstance(family_payload, dict):
            continue
        method = str(family_payload.get("selected_method") or "")
        methods[family] = method
        fam_audit = family_payload.get("selection_audit") or {}
        audit[family] = {
            "selected_method": method,
            "primary_winner": fam_audit.get("primary_winner"),
            "identity_rejection_applied": bool(
                fam_audit.get("identity_rejection_applied")
            ),
        }
        if method == "identity":
            alerts.append(
                f"{alert_side_prefix}calibration artifact selects identity "
                f"for family '{family}'; calibrated FV will equal raw FV "
                "in production."
            )
    if not families:
        top = str(artifact.get("selected_method") or "")
        if top == "identity":
            alerts.append(
                f"{alert_side_prefix}calibration artifact has no families "
                "block AND top selected_method=identity; runtime "
                "calibrator is a no-op."
            )
        payload.setdefault(
            "artifact_warning",
            "legacy single-family artifact (no families block); "
            "no_score_drift will fall back to identity in runtime.",
        )
    payload["artifact_methods_by_family"] = methods
    payload["artifact_audit_by_family"] = audit
    payload["artifact_side"] = str(artifact.get("side") or "")
    return payload


def _calibration_health(
    *,
    session_date: str,
    candidate_dir: Path,
    artifact_path: Path,
    output_root: Path,
    artifact_path_under: Optional[Path] = None,
    concept_drift_health: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "artifact_path": str(artifact_path),
        "artifact_present": artifact_path.exists(),
        "alerts": [],
        "notes": [],
    }
    alerts: List[str] = payload["alerts"]
    notes: List[str] = payload["notes"]

    artifact_methods: Dict[str, str] = {}
    artifact_audit: Dict[str, Any] = {}
    if artifact_path.exists():
        try:
            artifact = _load_json(artifact_path)
        except (OSError, json.JSONDecodeError) as exc:
            payload["artifact_error"] = f"failed to load: {exc}"
            artifact = {}
        else:
            payload["artifact_generated_at_utc"] = artifact.get("generated_at_utc")
            payload["artifact_schema_version"] = artifact.get("schema_version")
            payload["artifact_default_family"] = artifact.get("default_family")
            payload["artifact_top_selected_method"] = artifact.get("selected_method")
            age = _artifact_age_days(artifact.get("generated_at_utc", ""), session_date)
            payload["artifact_age_days"] = age
            if age is not None and age > CALIBRATION_STALE_AGE_DAYS:
                alerts.append(
                    f"calibration artifact is {age:.1f} days old "
                    f"(> {CALIBRATION_STALE_AGE_DAYS}d threshold); "
                    "rerun calibrate_signal_probabilities or daily refresh."
                )
            families = artifact.get("families") or {}
            # Input-drift alerts dedup: when both families flag the same
            # set of major-PSI features (the common case, since drift is
            # an upstream-input concept not a per-family fit decision),
            # emit a single alert rather than one per family.
            drift_alert_seen: set = set()
            for family, family_payload in sorted(families.items()):
                if not isinstance(family_payload, dict):
                    continue
                method = str(family_payload.get("selected_method") or "")
                artifact_methods[family] = method
                audit = family_payload.get("selection_audit") or {}
                input_drift_triggered = bool(audit.get("input_drift_triggered"))
                input_drift_major = audit.get("input_drift_major_features") or []
                artifact_audit[family] = {
                    "selected_method": method,
                    "primary_winner": audit.get("primary_winner"),
                    "identity_rejection_applied": bool(audit.get("identity_rejection_applied")),
                    "input_drift_triggered": input_drift_triggered,
                    "input_drift_major_features": input_drift_major,
                }
                if method == "identity":
                    alerts.append(
                        f"calibration artifact selects identity for family '{family}'; "
                        "calibrated FV will equal raw FV in production."
                    )
                if input_drift_triggered and input_drift_major:
                    feat_key = tuple(
                        sorted(str(r.get("feature") or "") for r in input_drift_major)
                    )
                    if feat_key not in drift_alert_seen:
                        drift_alert_seen.add(feat_key)
                        top_summary = ", ".join(
                            f"{r.get('feature')} PSI={r.get('psi'):.2f}"
                            for r in input_drift_major[:3]
                            if r.get("psi") is not None
                        )
                        # Hygiene #23: reword alert when ALL major-PSI
                        # features are attributable to known model
                        # upgrades (planned shift, calibrator was
                        # refit with mostly post-upgrade data).
                        # Read attribution from the upstream
                        # _concept_drift_health output if available.
                        attr_summary = (
                            concept_drift_health or {}
                        ).get("upgrade_attribution_summary") or {}
                        fully_attributed_to_upgrade = bool(
                            attr_summary.get("fully_attributed")
                        )
                        if fully_attributed_to_upgrade:
                            # Pull the upgrade name(s) for surfacing.
                            cd_feature_verdicts = (
                                concept_drift_health or {}
                            ).get("feature_verdicts") or {}
                            upgrade_names = set()
                            for fname in (
                                attr_summary.get("attributed_features") or []
                            ):
                                for upg in (
                                    cd_feature_verdicts.get(fname, {})
                                    .get("upgrade_attributions") or []
                                ):
                                    n = upg.get("name")
                                    if n:
                                        upgrade_names.add(str(n))
                            upgrade_label = (
                                "/".join(sorted(upgrade_names))
                                if upgrade_names else "known upgrade"
                            )
                            alerts.append(
                                f"calibrator input-drift TRIGGERED but BENIGN "
                                f"({len(input_drift_major)} features at PSI>="
                                f"{audit.get('input_drift_threshold', 0.25):.2f}): "
                                f"{top_summary}. All major-PSI features are "
                                f"attributable to {upgrade_label} (see "
                                "concept_drift_health.upgrade_attribution_summary). "
                                "Cause is a planned model upgrade, not regime change. "
                                "Calibrator was refit predominantly on post-upgrade "
                                "data; alert will auto-clear when the baseline window "
                                "slides past the upgrade date."
                            )
                        else:
                            alerts.append(
                                f"calibrator input-drift TRIGGERED ({len(input_drift_major)} continuous "
                                f"features at PSI>={audit.get('input_drift_threshold', 0.25):.2f}): "
                                f"{top_summary}. The runtime calibrator was refit on these inputs but "
                                "its training distribution materially differs from current production. "
                                "Decisions based on calibrated FV (esp. band-gated enforce at raw>=0.90) "
                                "stand on shifting ground -- cross-check concept_drift_health + consider "
                                "whether the artifact's stability gate is masking a needed method change."
                            )
            if not families:
                top = str(artifact.get("selected_method") or "")
                if top == "identity":
                    alerts.append(
                        "calibration artifact has no families block AND top "
                        "selected_method=identity; runtime calibrator is a no-op."
                    )
                payload.setdefault("artifact_warning",
                    "legacy single-family artifact (no families block); "
                    "no_score_drift will fall back to identity in runtime.")
    else:
        alerts.append(f"calibration artifact missing at {artifact_path}; runtime cannot calibrate.")
    payload["artifact_methods_by_family"] = artifact_methods
    payload["artifact_audit_by_family"] = artifact_audit

    candidate_path = candidate_dir / f"{session_date}_candidates.jsonl"
    payload["candidate_sample_path"] = str(candidate_path)
    rows = _load_jsonl(candidate_path)
    sample_metrics = _per_family_calibration_metrics(rows)
    payload["sampled_metrics_by_family"] = sample_metrics
    payload["sampled_rows_total"] = len(rows)
    for family, metrics in sample_metrics.items():
        method_counts = metrics.get("method_counts") or {}
        method_total = sum(method_counts.values())
        if method_total > 0:
            identity_share = method_counts.get("identity", 0) / method_total
            if identity_share >= 0.5:
                alerts.append(
                    f"family '{family}': {method_counts.get('identity', 0)}/"
                    f"{method_total} candidate rows used calibration_method="
                    f"identity ({identity_share:.0%}); runtime calibrator was "
                    "a no-op for this family."
                )
        n = metrics.get("rows_with_both_probs") or 0
        if n >= 25:
            mean_delta = metrics.get("mean_abs_delta")
            if mean_delta is not None and mean_delta < CALIBRATION_NEAR_IDENTITY_DELTA:
                alerts.append(
                    f"family '{family}': mean |calibrated-raw|={mean_delta:.4f} "
                    f"(< {CALIBRATION_NEAR_IDENTITY_DELTA}); calibrator behaving "
                    f"as identity over {n} sampled rows."
                )
            applied_share = metrics.get("applied_share")
            if applied_share is not None and applied_share < CALIBRATION_LOW_APPLIED_SHARE:
                shadow_share = metrics.get("shadow_share")
                if (
                    shadow_share is not None
                    and shadow_share >= CALIBRATION_SHADOW_MODE_DOMINANT_SHARE
                ):
                    notes.append(
                        f"family '{family}': calibration in shadow mode "
                        f"({shadow_share:.0%} of {sum((metrics.get('mode_counts') or {}).values())} rows); "
                        f"applied={applied_share:.0%} is expected."
                    )
                else:
                    alerts.append(
                        f"family '{family}': fair_value_calibration_applied=True share "
                        f"is {applied_share:.1%} (< {CALIBRATION_LOW_APPLIED_SHARE:.0%}); "
                        "calibration mode may be off or family-missing."
                    )

    prior_date = _shift_date(session_date, -1)
    prior_methods: Dict[str, str] = {}
    if prior_date:
        prior_review = output_root / f"{prior_date}_human_review.json"
        if prior_review.exists():
            try:
                prior_payload = _load_json(prior_review)
                prior_health = prior_payload.get("calibration_health") or {}
                prior_methods = prior_health.get("artifact_methods_by_family") or {}
                payload["prior_review_path"] = str(prior_review)
                payload["prior_artifact_methods_by_family"] = prior_methods
            except (OSError, json.JSONDecodeError):
                payload["prior_review_error"] = "failed to load prior daily review"
    method_changes: Dict[str, Dict[str, str]] = {}
    for family, today_method in artifact_methods.items():
        yesterday_method = prior_methods.get(family)
        if yesterday_method and yesterday_method != today_method:
            method_changes[family] = {"from": yesterday_method, "to": today_method}
            alerts.append(
                f"calibration method changed for family '{family}': "
                f"{yesterday_method} -> {today_method} since previous daily review."
            )
    payload["method_changes_since_prior"] = method_changes

    if artifact_path_under is not None:
        under_block = _calibration_artifact_metadata(
            artifact_path=artifact_path_under,
            session_date=session_date,
            alert_side_prefix="under: ",
        )
        prior_under_methods: Dict[str, str] = {}
        if prior_date:
            prior_review = output_root / f"{prior_date}_human_review.json"
            if prior_review.exists():
                try:
                    prior_payload = _load_json(prior_review)
                    prior_health = prior_payload.get("calibration_health") or {}
                    prior_under = (prior_health or {}).get("under") or {}
                    prior_under_methods = (
                        prior_under.get("artifact_methods_by_family") or {}
                    )
                except (OSError, json.JSONDecodeError):
                    pass
        under_method_changes: Dict[str, Dict[str, str]] = {}
        under_alerts: List[str] = under_block["alerts"]
        for family, today_method in (
            under_block.get("artifact_methods_by_family") or {}
        ).items():
            yesterday_method = prior_under_methods.get(family)
            if yesterday_method and yesterday_method != today_method:
                under_method_changes[family] = {
                    "from": yesterday_method, "to": today_method,
                }
                under_alerts.append(
                    f"under: calibration method changed for family "
                    f"'{family}': {yesterday_method} -> {today_method} "
                    "since previous daily review."
                )
        under_block["method_changes_since_prior"] = under_method_changes
        under_block["prior_artifact_methods_by_family"] = prior_under_methods
        payload["under"] = under_block
        alerts.extend(under_alerts)

    return payload



# _calibrator_enforce_shipment_health moved to
# calibrator_enforce_shipment.py on 2026-05-25. Re-exported here for
# back-compat with any caller still doing
# 'from .calibration_health import _calibrator_enforce_shipment_health'.
from .calibrator_enforce_shipment import (  # noqa: F401  (re-export)
    _calibrator_enforce_shipment_health,
)
