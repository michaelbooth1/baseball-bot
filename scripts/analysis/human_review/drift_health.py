import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .constants import (
    DRIFT_MIN_TODAY_SAMPLE,
    DRIFT_MIN_BASELINE_SAMPLE,
    DRIFT_REGIME_MIX_TVD,
    CONCEPT_DRIFT_STALE_AGE_DAYS,
    DRIFT_IN_DRIFT_STALE_AGE_DAYS,
    PROMOTION_ATTRIBUTION_WINDOW_DAYS,
    MODEL_UPGRADES,
)
from .helpers import (
    _load_json,
    _artifact_age_days,
    _drift_ask_bucket,
    _drift_current_state_edge_bucket,
    _drift_phantom_band_bucket,
    _shift_date,
)


def _bet_distributions(bet_rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    dims: Dict[str, Counter] = {
        "ask_bucket": Counter(),
        "current_state_edge_bucket": Counter(),
        "phantom_risk_band": Counter(),
    }
    for bet in bet_rows:
        dims["ask_bucket"][_drift_ask_bucket(bet.get("entry_ask"))] += 1
        dims["current_state_edge_bucket"][
            _drift_current_state_edge_bucket(bet.get("current_state_value_edge"))
        ] += 1
        dims["phantom_risk_band"][
            _drift_phantom_band_bucket(bet.get("phantom_risk_band"))
        ] += 1
    return {dim: dict(counter) for dim, counter in dims.items()}


def _total_variation_distance(
    today: Dict[str, int], base: Dict[str, int]
) -> Optional[float]:
    today_total = sum(today.values())
    base_total = sum(base.values())
    if today_total <= 0 or base_total <= 0:
        return None
    keys = set(today) | set(base)
    diff = 0.0
    for key in keys:
        diff += abs(
            (today.get(key, 0) / today_total) - (base.get(key, 0) / base_total)
        )
    return round(diff / 2.0, 4)


def _regime_mix_health(
    *,
    today_bet_rows: List[Dict[str, Any]],
    trailing_reviews: List[Dict[str, Any]],
) -> Dict[str, Any]:
    today_distributions = _bet_distributions(today_bet_rows)
    today_total_bets = (
        sum(today_distributions["ask_bucket"].values())
        if today_distributions
        else 0
    )

    base_distributions: Dict[str, Counter] = {
        dim: Counter() for dim in today_distributions
    }
    days_in_baseline = 0
    for review in trailing_reviews:
        prior = (review.get("regime_mix_health") or {}).get("today_distributions") or {}
        if not prior:
            continue
        contributed = False
        for dim, counts in prior.items():
            if dim not in base_distributions or not isinstance(counts, dict):
                continue
            for bucket, raw_count in counts.items():
                try:
                    base_distributions[dim][str(bucket)] += int(raw_count)
                    contributed = True
                except (TypeError, ValueError):
                    continue
        if contributed:
            days_in_baseline += 1
    base_distributions_out = {
        dim: dict(counter) for dim, counter in base_distributions.items()
    }
    baseline_total_bets = (
        sum(base_distributions_out.get("ask_bucket", {}).values())
    )

    tvd_by_dimension: Dict[str, Optional[float]] = {}
    for dim, today_counts in today_distributions.items():
        tvd_by_dimension[dim] = _total_variation_distance(
            today_counts, base_distributions_out.get(dim, {})
        )

    alerts: List[str] = []
    if (
        today_total_bets >= DRIFT_MIN_TODAY_SAMPLE
        and baseline_total_bets >= DRIFT_MIN_BASELINE_SAMPLE
    ):
        for dim, tvd_val in tvd_by_dimension.items():
            if tvd_val is None or tvd_val < DRIFT_REGIME_MIX_TVD:
                continue
            today_dist = today_distributions[dim]
            base_dist = base_distributions_out.get(dim, {})
            today_top_bucket, today_top_count = max(
                today_dist.items(), key=lambda item: item[1]
            )
            base_top_bucket, base_top_count = (
                max(base_dist.items(), key=lambda item: item[1])
                if base_dist
                else ("?", 0)
            )
            alerts.append(
                f"{dim}: TVD={tvd_val:.2f} (>= {DRIFT_REGIME_MIX_TVD}); "
                f"today top={today_top_bucket} "
                f"({today_top_count}/{today_total_bets}), "
                f"baseline top={base_top_bucket} "
                f"({base_top_count}/{baseline_total_bets}); "
                "trading a different cohort than recent sessions."
            )

    return {
        "today_distributions": today_distributions,
        "today_total_bets": today_total_bets,
        "baseline_distributions": base_distributions_out,
        "baseline_total_bets": baseline_total_bets,
        "days_in_baseline": days_in_baseline,
        "tvd_by_dimension": tvd_by_dimension,
        "thresholds": {
            "min_today_sample": DRIFT_MIN_TODAY_SAMPLE,
            "min_baseline_sample": DRIFT_MIN_BASELINE_SAMPLE,
            "max_tvd": DRIFT_REGIME_MIX_TVD,
        },
        "alerts": alerts,
    }


def _concept_drift_health(
    *,
    report_path: Path,
    session_date: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "artifact_path": str(report_path),
        "artifact_present": report_path.exists(),
        "alerts": [],
    }
    if not report_path.exists():
        payload["artifact_error"] = "concept-drift report missing; check refresh step ran"
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    payload["report_active_date"] = report.get("active_date")
    payload["current_window"] = report.get("current_window")
    payload["baseline_window"] = report.get("baseline_window")
    payload["thresholds"] = report.get("thresholds")

    age = _artifact_age_days(report.get("generated_at_utc", ""), session_date)
    payload["artifact_age_days"] = age
    if age is not None and age > CONCEPT_DRIFT_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"concept-drift report is {age:.1f}d old "
            f"(> {CONCEPT_DRIFT_STALE_AGE_DAYS}d threshold); "
            "rerun build_concept_drift_report or daily refresh."
        )

    feature_verdicts: Dict[str, Dict[str, Any]] = {}
    baseline_window = report.get("baseline_window") or {}
    base_start = baseline_window.get("start")
    base_end = baseline_window.get("end")
    for fname, info in (report.get("features") or {}).items():
        verdict_entry: Dict[str, Any] = {
            "kind": info.get("kind"),
            "metric": info.get("metric"),
            "value": info.get("value"),
            "verdict": info.get("verdict"),
            "current_n": info.get("current_n"),
            "baseline_n": info.get("baseline_n"),
        }
        # Hygiene #23: attribute PSI-major shifts to known model
        # upgrades when the upgrade date falls within the baseline
        # window. Without this annotation the alert reads as
        # "untrustworthy calibrator" when the actual cause is a
        # planned model improvement (TR20/TR21 etc.).
        if (
            str(info.get("verdict") or "") == "major"
            and base_start
            and base_end
        ):
            attributions: List[Dict[str, Any]] = []
            for upg in MODEL_UPGRADES:
                affected = upg.get("affected_features") or {}
                if fname not in affected:
                    continue
                upg_date = str(upg.get("date") or "")
                if base_start <= upg_date <= base_end:
                    attributions.append({
                        "name": upg.get("name"),
                        "date": upg_date,
                        "description": upg.get("description"),
                        "attribution_kind": affected.get(fname),
                    })
            if attributions:
                verdict_entry["upgrade_attributions"] = attributions
        feature_verdicts[fname] = verdict_entry
    payload["feature_verdicts"] = feature_verdicts

    # Aggregate attribution status: are ALL major-PSI features
    # attributable to known upgrades? If so the calibrator drift
    # alert reword can flip the alert from "alarming" to "benign".
    major_features = [
        f for f, v in feature_verdicts.items()
        if str(v.get("verdict") or "") == "major"
    ]
    attributed_features = [
        f for f in major_features
        if feature_verdicts[f].get("upgrade_attributions")
    ]
    payload["upgrade_attribution_summary"] = {
        "major_features": major_features,
        "attributed_features": attributed_features,
        "fully_attributed": (
            bool(major_features)
            and len(attributed_features) == len(major_features)
        ),
    }

    for alert_text in report.get("alerts") or []:
        payload["alerts"].append(str(alert_text))

    # T7 (2026-06-15): PSI watchpoint. After the 2026-06-07->10 live-root gap,
    # concept-drift ran on too few rows (insufficient_data) exactly when the
    # calibrator-staleness question needed it. With the continuity engine
    # running the trailing window refills; fire a ONE-TIME nudge the first day
    # PSI becomes computable so the operator reads the verdict (it gates the
    # enforce_min_raw + Stage-1 decisions). A marker file makes it one-shot
    # and auto-re-arms if PSI goes insufficient again (a future gap).
    thresholds = report.get("thresholds") or {}
    cur_window = report.get("current_window") or {}
    computable = [
        f for f, v in feature_verdicts.items()
        if str(v.get("verdict") or "") not in ("insufficient_data", "", "None")
    ]
    psi_computable = bool(computable)
    payload["psi_watchpoint"] = {
        "current_window_n_rows": cur_window.get("n_rows"),
        "min_rows_per_feature": thresholds.get("min_rows_per_feature"),
        "n_computable_features": len(computable),
        "n_features": len(feature_verdicts),
        "psi_computable": psi_computable,
    }
    marker = report_path.parent / ".psi_watchpoint_fired"
    if psi_computable and feature_verdicts:
        if not marker.exists():
            payload["alerts"].append(
                f"concept-drift PSI is computable again "
                f"({len(computable)}/{len(feature_verdicts)} features; current "
                f"window {cur_window.get('n_rows')} rows >= "
                f"{thresholds.get('min_rows_per_feature')}). After the "
                "live-root gap this is the first day the calibrator-staleness "
                "question is answerable -- read "
                "concept_drift_health.feature_verdicts (esp. "
                "stage2_run_env_delta / team_offense_delta) before acting on "
                "enforce_min_raw or the Stage-1 decision."
            )
            try:
                marker.write_text(session_date, encoding="utf-8")
            except OSError:
                pass
    else:
        # Re-arm the one-shot for the next recovery after a gap.
        try:
            if marker.exists():
                marker.unlink()
        except OSError:
            pass

    return payload


def _drift_in_drift_health(
    *,
    report_path: Path,
    session_date: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "artifact_path": str(report_path),
        "artifact_present": report_path.exists(),
        "alerts": [],
    }
    if not report_path.exists():
        payload["artifact_error"] = "drift-in-drift report missing; check refresh step ran"
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    payload["report_active_date"] = report.get("active_date")
    payload["history_window_days"] = report.get("history_window_days")
    payload["projection_horizon_days"] = report.get("projection_horizon_days")
    payload["min_points_for_trend"] = report.get("min_points_for_trend")
    payload["n_history_rows_in_window"] = report.get("n_history_rows_in_window")
    payload["n_features_evaluated"] = report.get("n_features_evaluated")
    payload["thresholds"] = report.get("thresholds")

    age = _artifact_age_days(report.get("generated_at_utc", ""), session_date)
    payload["artifact_age_days"] = age
    if age is not None and age > DRIFT_IN_DRIFT_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"drift-in-drift report is {age:.1f}d old "
            f"(> {DRIFT_IN_DRIFT_STALE_AGE_DAYS}d threshold); "
            "rerun build_drift_in_drift_report or daily refresh."
        )

    feature_verdicts: Dict[str, Dict[str, Any]] = {}
    for fname, info in (report.get("features") or {}).items():
        feature_verdicts[fname] = {
            "n_points": info.get("n_points"),
            "current_psi": info.get("current_psi"),
            "slope_per_day": info.get("slope_per_day"),
            "r_squared": info.get("r_squared"),
            "projected_psi": info.get("projected_psi"),
            "verdict": info.get("verdict"),
        }
    payload["feature_verdicts"] = feature_verdicts

    for alert_text in report.get("alerts") or []:
        payload["alerts"].append(str(alert_text))

    return payload


def _recent_events_by_direction(
    *,
    today: str,
    log_path: Path,
    direction: str,
    success_actions: Tuple[str, ...],
    window_days: int = PROMOTION_ATTRIBUTION_WINDOW_DAYS,
) -> List[Dict[str, Any]]:
    """Return audit-log events of one direction (promote|demote) in the
    last `window_days`. Backward-compat: rows without `direction` predate
    the field and are treated as promotions (so demote queries skip them
    cleanly)."""
    if not log_path.exists():
        return []
    try:
        rows = []
        with open(log_path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rows.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    cutoff = _shift_date(today, -window_days)
    out: List[Dict[str, Any]] = []
    for r in rows:
        row_direction = str(r.get("direction") or "promote")
        if row_direction != direction:
            continue
        if str(r.get("action") or "") not in success_actions:
            continue
        ts = str(r.get("generated_at_utc") or "")
        if not ts:
            continue
        ts_date = ts[:10]
        if ts_date < cutoff or ts_date > today:
            continue
        out.append(r)
    return out


def _recent_promotions(
    *,
    today: str,
    log_path: Path,
    window_days: int = PROMOTION_ATTRIBUTION_WINDOW_DAYS,
) -> List[Dict[str, Any]]:
    """Return promotion events that completed in the last `window_days`
    days (relative to `today`). Used to attribute drift alerts to recent
    promotions ("this cohort started losing right after we promoted X").
    """
    return _recent_events_by_direction(
        today=today, log_path=log_path,
        direction="promote", success_actions=("promoted", "forced"),
        window_days=window_days,
    )


def _recent_demotions(
    *,
    today: str,
    log_path: Path,
    window_days: int = PROMOTION_ATTRIBUTION_WINDOW_DAYS,
) -> List[Dict[str, Any]]:
    """Return demotion events that completed in the last `window_days`
    days. Used to add a "follows" suffix to drift alerts so the operator
    can see whether a recent demote DID NOT fix the cohort (different
    semantic from promotion-attribution -- demote was supposed to help)."""
    return _recent_events_by_direction(
        today=today, log_path=log_path,
        direction="demote", success_actions=("demoted", "forced"),
        window_days=window_days,
    )


def _attribute_alert_to_promotions(
    alert: str,
    promotions: List[Dict[str, Any]],
    *,
    today: str,
) -> str:
    """If any recent promotion exists, append a hint to the alert text.
    The hint lists the promoted lever(s) and how many days ago. We don't
    try to causally link to a specific lever (that would need feature-
    level attribution) -- just call out the temporal coincidence so the
    operator can investigate.
    """
    if not promotions:
        return alert
    # Group by lever, take the most recent per lever for compact display.
    by_lever: Dict[str, str] = {}
    for p in promotions:
        lever = str(p.get("lever") or "?")
        ts = str(p.get("generated_at_utc") or "")
        if lever not in by_lever or ts > by_lever[lever]:
            by_lever[lever] = ts
    parts: List[str] = []
    for lever, ts in sorted(by_lever.items(), key=lambda kv: kv[1], reverse=True):
        ts_date = ts[:10]
        try:
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            ts_dt = datetime.strptime(ts_date, "%Y-%m-%d")
            days_ago = (today_dt - ts_dt).days
            parts.append(f"{lever} promotion {days_ago}d ago")
        except ValueError:
            parts.append(f"{lever} promotion at {ts_date}")
    return alert + f"  [coincides with: {', '.join(parts)}]"


def _attribute_alert_to_demotions(
    alert: str,
    demotions: List[Dict[str, Any]],
    *,
    today: str,
) -> str:
    """If any recent demotion exists, append a "[follows: ...]" suffix.
    Different verb from the promotion suffix ("coincides with") on
    purpose: a demote was supposed to *help* the cohort, so an alert
    that fires after a demote means the demote either didn't help or
    the cohort has a different root cause. The operator should treat
    the follow-up signal differently than a fresh coincidence."""
    if not demotions:
        return alert
    # Group by lever, take the most recent per lever for compact display.
    by_lever: Dict[str, str] = {}
    for d in demotions:
        lever = str(d.get("lever") or "?")
        ts = str(d.get("generated_at_utc") or "")
        if lever not in by_lever or ts > by_lever[lever]:
            by_lever[lever] = ts
    parts: List[str] = []
    for lever, ts in sorted(by_lever.items(), key=lambda kv: kv[1], reverse=True):
        ts_date = ts[:10]
        try:
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            ts_dt = datetime.strptime(ts_date, "%Y-%m-%d")
            days_ago = (today_dt - ts_dt).days
            parts.append(f"{lever} demotion {days_ago}d ago")
        except ValueError:
            parts.append(f"{lever} demotion at {ts_date}")
    return alert + f"  [follows: {', '.join(parts)}]"


CONCEPT_DRIFT_ATTRIBUTION_TOP_N = 5


def _major_drift_features(
    concept_drift_health: Optional[Dict[str, Any]],
) -> List[Tuple[str, str, float]]:
    """Extract features with verdict='major' (PSI >= 0.25 / TVD past its
    own threshold) from the concept-drift health block. Returns a list of
    (feature_name, metric_label, value) tuples sorted by value desc, so
    the most-shifted feature appears first in the attribution suffix.
    """
    if not concept_drift_health:
        return []
    feature_verdicts = concept_drift_health.get("feature_verdicts") or {}
    out: List[Tuple[str, str, float]] = []
    for fname, info in feature_verdicts.items():
        if str(info.get("verdict") or "") != "major":
            continue
        metric = str(info.get("metric") or "PSI")
        value = info.get("value")
        if value is None:
            continue
        try:
            out.append((str(fname), metric, float(value)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda t: t[2], reverse=True)
    return out


def _attribute_alert_to_concept_drift(
    alert: str,
    drift_features: List[Tuple[str, str, float]],
    *,
    top_n: int = CONCEPT_DRIFT_ATTRIBUTION_TOP_N,
) -> str:
    """If any major-drift features exist, append a candidate-root-cause
    suffix to the alert. Format mirrors the promotion-attribution
    suffix: `[concept-drift: <feature> <metric> <value>, ...]`. We do
    not try to claim a causal link (the input shifted, the cohort lost
    money -- they MIGHT be related); the suffix just shrinks the
    operator's investigation surface from "all 7 features" to "these
    are the ones whose input distribution shifted".
    """
    if not drift_features:
        return alert
    top = drift_features[:top_n]
    parts = [f"{f} {m} {v:.2f}" for (f, m, v) in top]
    extra = len(drift_features) - len(top)
    suffix = ", ".join(parts)
    if extra > 0:
        suffix += f", (+{extra} more)"
    return alert + f"  [concept-drift: {suffix}]"

