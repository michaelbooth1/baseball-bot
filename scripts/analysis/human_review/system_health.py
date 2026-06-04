import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .constants import (
    PROJECT_DIR,
    DAEMON_STALENESS_THRESHOLD_DAYS,
    SETTLEMENT_TRUTH_STALE_AGE_DAYS,
    STALE_FILLED_ALERT_THRESHOLD,
    MISSING_MLB_DATA_RATE_ALERT_THRESHOLD,
    UNDER_BOOK_COVERAGE_STALE_AGE_DAYS,
    UNDER_BOOK_COVERAGE_WARN_THRESHOLD,
    CACHE_LINEAGE_BUILD_AGE_WARN_DAYS,
    PROMOTION_LAG_PENDING_HOURS_WARN,
    DAEMON_RETROSPECTIVE_STALE_AGE_DAYS,
    CROSS_ARTIFACT_CONSISTENCY_PATHS,
    DEFAULT_STAGE1_CACHE_PATH,
    DEFAULT_STAGE2_CACHE_PATH,
    DEFAULT_STAGE3_V2_WEIGHTS_PATH,
    DEFAULT_CALIBRATION_ARTIFACT,
    DEFAULT_CALIBRATION_ARTIFACT_UNDER,
    DEFAULT_PROMOTION_EVENTS_LOG,
    PROMOTION_LAG_LEVERS,
    PROMOTION_LAG_SESSION_ROOTS,
)

from .helpers import (
    _load_json,
    _artifact_age_days,
    _shift_date,
    _latest_session_start_utc,
)

_DAEMON_LEVER_AUDIT_NAMES: Dict[str, str] = {
    "stage2": "stage2",
    "stage3-v2": "stage3_v2",
    "stake-scaling": "stake_scaling",
    "gate-threshold": "gate_threshold",
}
_DAEMON_STALENESS_SUCCESS_ACTIONS: Tuple[str, ...] = (
    "promoted", "forced", "demoted",
)

# 2026-06-03: history-file paths for the per-lever blocker reason
# computation. These are the same files the daemon retrospective
# replays; we load them here to surface "why no promote today" in
# plain English on the daily review.
_DEFAULT_STAGE2_BRIER_HISTORY_PATH = (
    PROJECT_DIR / "data" / "analysis_output" / "calibration"
    / "stage2_brier_history.jsonl"
)
_DEFAULT_STAGE3_V2_DRIFT_HISTORY_PATH = (
    PROJECT_DIR / "data" / "analysis_output" / "calibration"
    / "stage3_v2_drift_history.jsonl"
)
# Thresholds for the blocker text. Kept here (vs imported from the
# refresh package) so this module stays self-contained and the blocker
# text can be tested without spinning up the refresh package.
_STAGE2_BLOCKER_WINDOW = 7
_STAGE2_BLOCKER_MIN_DELTA = 0.001
_STAGE2_BLOCKER_MIN_IMPROVING = 5
_STAGE3_V2_BLOCKER_DRIFT_THRESHOLD = 0.015
_STAGE3_V2_BLOCKER_WINDOW = 7


def _load_jsonl_safe(path: Path) -> List[Dict[str, Any]]:
    """Lenient JSONL reader -- malformed rows are skipped silently.
    Returns [] for missing files. Same forgiveness contract as the
    other history loaders in the codebase."""
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
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
    return rows


def _row_date(row: Dict[str, Any]) -> str:
    d = row.get("data_max_date") or row.get("date")
    if d:
        return str(d)[:10]
    g = row.get("generated_at_utc") or ""
    return str(g)[:10] if g else ""


def _trailing_distinct_dates(
    rows: List[Dict[str, Any]], *, window: int,
) -> List[Dict[str, Any]]:
    by_date: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        d = _row_date(row)
        if d:
            by_date[d] = row
    if not by_date:
        return []
    ordered = sorted(by_date.items(), key=lambda kv: kv[0])
    return [v for _, v in ordered[-window:]]


def _stage2_blocker_reason(
    history_path: Path,
) -> Tuple[str, Dict[str, Any], bool]:
    """Plain-English explanation of why stage2 isn't promoting today.

    Reads the trailing window of stage2 Brier history (staging vs
    production) and counts how many days the staging candidate beat
    production by the threshold.

    Returns (reason_text, diagnostic, would_promote_today).
    `would_promote_today` is True iff the verdict logic would emit
    `promote` -- used to decide whether flipping `--auto-daemon-mode`
    to `act` would actually act on this lever.
    """
    rows = _load_jsonl_safe(history_path)
    trailing = _trailing_distinct_dates(rows, window=_STAGE2_BLOCKER_WINDOW)
    diag = {
        "history_path": str(history_path),
        "n_history": len(trailing),
        "window": _STAGE2_BLOCKER_WINDOW,
        "min_delta": _STAGE2_BLOCKER_MIN_DELTA,
        "min_improving_required": _STAGE2_BLOCKER_MIN_IMPROVING,
    }
    if not trailing:
        return ("no stage2 brier history rows yet", diag, False)
    deltas: List[float] = []
    n_improving = 0
    for row in trailing:
        d = row.get("delta")
        if isinstance(d, (int, float)):
            deltas.append(float(d))
            if float(d) <= -_STAGE2_BLOCKER_MIN_DELTA:
                n_improving += 1
    diag["n_improving"] = n_improving
    if not deltas:
        return ("trailing rows present but no `delta` field", diag, False)
    last_delta = deltas[-1]
    avg_delta = sum(deltas) / len(deltas)
    diag["last_delta"] = round(last_delta, 6)
    diag["avg_delta_window"] = round(avg_delta, 6)
    would_promote = n_improving >= _STAGE2_BLOCKER_MIN_IMPROVING
    if would_promote:
        return (
            f"verdict=PROMOTE ({n_improving}/{len(trailing)} "
            "improving days)",
            diag,
            True,
        )
    direction = "worse than" if avg_delta > 0 else "better than"
    return (
        f"staging Brier {direction} prod by avg "
        f"{avg_delta:+.4f} over last {len(trailing)} days; "
        f"{n_improving}/{len(trailing)} days hit the "
        f"{-_STAGE2_BLOCKER_MIN_DELTA:+.3f} improving-delta threshold "
        f"(need {_STAGE2_BLOCKER_MIN_IMPROVING}/{_STAGE2_BLOCKER_WINDOW}). "
        "Staging candidate is not currently an improvement."
    ), diag, False


def _stage3_v2_blocker_reason(
    history_path: Path,
) -> Tuple[str, Dict[str, Any], bool]:
    """Plain-English explanation of why stage3-v2 isn't promoting.

    The verdict logic flags drift between the research and active
    Stage-3 v2 betas. `hold` here means "research and active are in
    sync; nothing to promote" -- the opposite of a problem.
    """
    rows = _load_jsonl_safe(history_path)
    trailing = _trailing_distinct_dates(rows, window=_STAGE3_V2_BLOCKER_WINDOW)
    diag = {
        "history_path": str(history_path),
        "n_history": len(trailing),
        "window": _STAGE3_V2_BLOCKER_WINDOW,
        "drift_threshold": _STAGE3_V2_BLOCKER_DRIFT_THRESHOLD,
    }
    if not trailing:
        return ("no stage3-v2 drift history rows yet", diag, False)
    drifts: List[float] = []
    for row in trailing:
        m = row.get("max_abs_delta")
        if isinstance(m, (int, float)):
            drifts.append(float(m))
    if not drifts:
        return (
            "trailing rows present but no `max_abs_delta` field",
            diag, False,
        )
    last_drift = drifts[-1]
    max_in_window = max(drifts)
    diag["last_max_abs_delta"] = last_drift
    diag["max_in_window"] = max_in_window
    if max_in_window >= _STAGE3_V2_BLOCKER_DRIFT_THRESHOLD:
        return (
            f"max drift in window {max_in_window:.5f} "
            f">= {_STAGE3_V2_BLOCKER_DRIFT_THRESHOLD} threshold "
            "-- promote verdict expected on next refresh",
            diag, True,
        )
    return (
        f"research vs active drift max {max_in_window:.2e} over "
        f"last {len(trailing)} days, well below "
        f"{_STAGE3_V2_BLOCKER_DRIFT_THRESHOLD:.3f} threshold. "
        "Active model is in sync with research -- no promotion needed."
    ), diag, False


def _stake_scaling_blocker_reason(
    snapshot: Optional[Dict[str, Any]],
) -> Tuple[str, Dict[str, Any], bool]:
    """Use the daemon retrospective's stake-scaling snapshot (which
    already carries n_sessions / min_sessions from the analysis
    report) to explain the data-runway status."""
    diag: Dict[str, Any] = {}
    if not snapshot:
        return ("no stake-scaling snapshot in retrospective", diag, False)
    verdict = str(snapshot.get("verdict_label") or "")
    n = snapshot.get("n_sessions")
    mn = snapshot.get("min_sessions")
    diag["verdict_label"] = verdict
    diag["n_sessions"] = n
    diag["min_sessions"] = mn
    if verdict == "promote":
        return (
            "verdict=PROMOTE -- daemon auto-actuates in act mode",
            diag, True,
        )
    if verdict == "need_more_data" and isinstance(n, int) and isinstance(mn, int):
        return (
            f"{n}/{mn} sessions of shadow data accumulated; "
            f"{mn - n} more sessions needed before the verdict can flip",
            diag, False,
        )
    return (f"verdict={verdict or 'unknown'}", diag, False)


def _gate_threshold_blocker_reason(
    snapshot: Optional[Dict[str, Any]],
) -> Tuple[str, Dict[str, Any], bool]:
    """Use the daemon retrospective's gate-threshold snapshot to
    explain the preview-only-by-design pattern + list actionable
    gates the operator should hand-promote.

    Returns would_promote_today=False ALWAYS because gate-threshold
    is preview-only-by-design even when verdict=promote (operator
    must select the threshold value per-gate). This is the design
    constraint we want to make visible in the daily review.
    """
    diag: Dict[str, Any] = {}
    if not snapshot:
        return (
            "no gate-threshold snapshot in retrospective",
            diag, False,
        )
    verdict = str(snapshot.get("verdict_label") or "")
    actionable = snapshot.get("actionable_gates") or []
    diag["verdict_label"] = verdict
    diag["n_actionable_gates"] = len(actionable)
    if verdict != "promote":
        return (f"verdict={verdict or 'unknown'}", diag, False)
    if not actionable:
        return (
            "verdict=promote but no actionable gates in snapshot",
            diag, False,
        )
    gates_summary = ", ".join(
        f"{g.get('name')} {g.get('current_threshold')} -> "
        f"{g.get('recommended_threshold')}"
        for g in actionable[:3]
    )
    more = (
        f" + {len(actionable) - 3} more"
        if len(actionable) > 3 else ""
    )
    return (
        f"preview-only-by-design: daemon does NOT auto-actuate "
        f"gate-threshold (operator runs `promote.py gate-threshold "
        f"<gate> <value>`). {len(actionable)} actionable gate(s): "
        f"{gates_summary}{more}.",
        diag, False,
    )


def _last_audit_event_for_lever(
    audit_rows: List[Dict[str, Any]], lever_underscore: str,
) -> Optional[Dict[str, Any]]:
    candidates = [
        r for r in audit_rows
        if str(r.get("lever") or "") == lever_underscore
        and str(r.get("action") or "") in _DAEMON_STALENESS_SUCCESS_ACTIONS
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: str(r.get("generated_at_utc") or ""))


def _load_audit_rows(log_path: Path) -> List[Dict[str, Any]]:
    if not log_path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
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
    return rows


def _today_verdict_for_lever(
    retrospective_report: Dict[str, Any], lever_name: str,
) -> Optional[str]:
    replays = retrospective_report.get("replays") or {}
    if lever_name in replays:
        per_date = replays[lever_name].get("per_date") or []
        if not per_date:
            return None
        return per_date[-1].get("daemon_verdict_label")
    snapshots = retrospective_report.get("snapshots") or {}
    if lever_name in snapshots:
        return snapshots[lever_name].get("verdict_label")
    return None


def _daemon_staleness_check(
    *,
    retrospective_report: Dict[str, Any],
    audit_log_path: Path,
    today: str,
    threshold_days: int = DAEMON_STALENESS_THRESHOLD_DAYS,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not today:
        return out
    audit_rows = _load_audit_rows(audit_log_path)
    cutoff = _shift_date(today, -threshold_days)
    for lever_name, audit_name in _DAEMON_LEVER_AUDIT_NAMES.items():
        verdict_label = _today_verdict_for_lever(retrospective_report, lever_name)
        if verdict_label not in ("promote", "demote"):
            continue
        last = _last_audit_event_for_lever(audit_rows, audit_name)
        last_ts = (last or {}).get("generated_at_utc") or ""
        last_date = last_ts[:10] if last_ts else ""
        if last_date and last_date >= cutoff:
            continue
        try:
            today_dt = datetime.strptime(today, "%Y-%m-%d")
        except ValueError:
            continue
        if last_date:
            try:
                last_dt = datetime.strptime(last_date, "%Y-%m-%d")
                days_since = (today_dt - last_dt).days
            except ValueError:
                days_since = None
        else:
            days_since = None
        out.append({
            "lever": lever_name,
            "verdict_label": verdict_label,
            "last_action_date": last_date or None,
            "last_action_operator": (last or {}).get("operator"),
            "last_action_label": (last or {}).get("action"),
            "days_since_last_action": days_since,
            "threshold_days": threshold_days,
        })
    return out


def _settlement_truth_health(
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
        payload["artifact_error"] = (
            "settlement_truth_report missing; check refresh step ran"
        )
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    age = _artifact_age_days(report.get("generated_at_utc", ""), session_date)
    payload["artifact_age_days"] = age
    if age is not None and age > SETTLEMENT_TRUTH_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"settlement_truth_report is {age:.1f}d old "
            f"(> {SETTLEMENT_TRUTH_STALE_AGE_DAYS}d threshold); "
            "rerun verify_settlement_truth or daily refresh."
        )

    counts = report.get("counts") or {}
    thresholds = report.get("thresholds") or {}
    n_filled = counts.get("filled_or_settled_total", 0)
    n_mismatch = counts.get("resolution_mismatch", 0)
    n_total_mismatch = counts.get("total_mismatch", 0)
    n_stale_filled = counts.get("stale_filled", 0)
    n_missing_mlb = counts.get("missing_mlb_data", 0)
    n_game_not_final = counts.get("game_not_final_yet", 0)
    payload["counts"] = {
        "filled_or_settled_total": n_filled,
        "ok": counts.get("ok", 0),
        "resolution_mismatch": n_mismatch,
        "total_mismatch": n_total_mismatch,
        "stale_filled": n_stale_filled,
        "game_not_final_yet": n_game_not_final,
        "missing_mlb_data": n_missing_mlb,
        "not_yet_settled": counts.get("not_yet_settled", 0),
    }
    payload["ok_share"] = report.get("ok_share")
    payload["missing_mlb_data_share"] = report.get("missing_mlb_data_share")
    payload["oldest_stale_filled_age_days"] = report.get(
        "oldest_stale_filled_age_days"
    )

    if n_mismatch > 0:
        payload["alerts"].append(
            f"{n_mismatch} resolution_mismatch row(s) -- engine_won "
            "disagrees with MLB final total. ROI math may be "
            "corrupted; inspect settlement_truth_report.md."
        )
    if n_total_mismatch > 0:
        payload["alerts"].append(
            f"{n_total_mismatch} total_mismatch row(s) -- engine "
            "recorded a different final_total than MLB. ROI math is "
            "preserved (same side of line), but total field is wrong."
        )
    if n_stale_filled >= thresholds.get(
        "stale_filled_alert", STALE_FILLED_ALERT_THRESHOLD,
    ):
        oldest = report.get("oldest_stale_filled_age_days")
        suffix = (
            f" (oldest {oldest}d)" if oldest is not None else ""
        )
        payload["alerts"].append(
            f"{n_stale_filled} stale_filled bet(s){suffix} -- "
            "order_status=filled but no won/loss recorded despite "
            "MLB final. Phase C v2 inventory will treat these as "
            "open forever; clean up before live UNDER actuation."
        )
    if n_game_not_final > 0:
        payload["alerts"].append(
            f"{n_game_not_final} game_not_final_yet row(s) -- bet "
            "was settled before MLB JSON showed game-final. Likely "
            "a scraper-timing issue; investigate if persistent."
        )
    if n_filled > 0:
        missing_share = n_missing_mlb / n_filled
        thresh = thresholds.get(
            "missing_mlb_data_rate_alert",
            MISSING_MLB_DATA_RATE_ALERT_THRESHOLD,
        )
        if missing_share >= thresh:
            payload["alerts"].append(
                f"missing_mlb_data share {missing_share:.1%} >= "
                f"{thresh:.0%} -- local game JSONs are missing for a "
                "large chunk of settled bets. Verifier results are "
                "degraded until the game scraper backfills."
            )

    return payload


def _under_book_coverage_health(
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
        payload["artifact_error"] = (
            "model_maturity_report missing; check refresh step ran"
        )
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    age = _artifact_age_days(report.get("generated_at_utc", ""), session_date)
    payload["artifact_age_days"] = age
    if age is not None and age > UNDER_BOOK_COVERAGE_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"model_maturity_report is {age:.1f}d old "
            f"(> {UNDER_BOOK_COVERAGE_STALE_AGE_DAYS}d threshold); "
            "rerun model_maturity_report or daily refresh."
        )

    def _opt_float(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    coverage_checks = report.get("coverage_checks") or {}
    overall = coverage_checks.get("overall") or {}
    rate = _opt_float(overall.get("under_pair_available_rate"))
    rows = overall.get("rows")
    payload["overall"] = {
        "rows": rows,
        "under_pair_available_rate": rate,
        "under_pair_available_rows": overall.get("under_pair_available_rows"),
        "under_pair_book_rate": _opt_float(overall.get("under_pair_book_rate")),
        "no_vig_market_rate": _opt_float(overall.get("no_vig_market_rate")),
    }
    payload["warn_threshold"] = UNDER_BOOK_COVERAGE_WARN_THRESHOLD

    by_family: Dict[str, Dict[str, Any]] = {}
    for family, family_payload in (coverage_checks.get("by_family") or {}).items():
        if not isinstance(family_payload, dict):
            continue
        by_family[str(family)] = {
            "rows": family_payload.get("rows"),
            "under_pair_available_rate": _opt_float(
                family_payload.get("under_pair_available_rate")
            ),
        }
    payload["by_family"] = by_family

    if rate is not None and rate < UNDER_BOOK_COVERAGE_WARN_THRESHOLD:
        payload["alerts"].append(
            f"under_pair_available_rate {rate:.2f} below warn floor "
            f"{UNDER_BOOK_COVERAGE_WARN_THRESHOLD:.2f}; UNDER offline "
            f"analysis still works (None imputed), but Phase C live "
            f"UNDER quoting needs higher pairing rate. Investigate "
            f"tick-timing variance in monitor_mlb_polymarket_ou.py."
        )
    return payload


def _cache_lineage_freshness_health(
    *,
    stage1_path: Path = DEFAULT_STAGE1_CACHE_PATH,
    stage2_path: Path = DEFAULT_STAGE2_CACHE_PATH,
    stage3_v2_path: Path = DEFAULT_STAGE3_V2_WEIGHTS_PATH,
    calibrator_path: Path = DEFAULT_CALIBRATION_ARTIFACT,
    calibrator_under_path: Path = DEFAULT_CALIBRATION_ARTIFACT_UNDER,
    build_age_warn_days: float = CACHE_LINEAGE_BUILD_AGE_WARN_DAYS,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "alerts": [],
        "artifacts": {},
        "thresholds": {
            "build_age_warn_days": build_age_warn_days,
        },
    }

    try:
        from scripts.analysis.artifact_lineage import (
            _read_lineage_from_path,
            format_lineage_summary_line,
            _age_days,
        )
    except ImportError:
        try:
            from artifact_lineage import (
                _read_lineage_from_path,
                format_lineage_summary_line,
                _age_days,
            )
        except ImportError:
            payload["alerts"].append(
                "artifact_lineage module unavailable; cache lineage "
                "freshness check skipped."
            )
            return payload

    artifact_specs = [
        ("stage1_cache", stage1_path, True),
        ("stage2_cache", stage2_path, True),
        ("stage3_v2_weights", stage3_v2_path, False),
        ("calibrator_over", calibrator_path, True),
        ("calibrator_under", calibrator_under_path, False),
    ]

    for label, path, expected in artifact_specs:
        artifact_info: Dict[str, Any] = {
            "path": str(path),
            "expected": expected,
            "exists": path.exists() if path is not None else False,
        }
        if not artifact_info["exists"]:
            artifact_info["status"] = (
                "missing_required" if expected else "missing_optional"
            )
            artifact_info["summary"] = (
                f"{label}: artifact not found"
            )
            payload["artifacts"][label] = artifact_info
            if expected:
                payload["alerts"].append(
                    f"{label} artifact not found at {path}; "
                    "engine boot would fail-closed on this cache."
                )
            continue
        lineage = _read_lineage_from_path(path)
        if lineage is None:
            artifact_info["status"] = "no_lineage_pre_v2"
            artifact_info["summary"] = format_lineage_summary_line(
                label, None,
            )
            payload["artifacts"][label] = artifact_info
            continue
        build_age = _age_days(lineage.get("built_at_utc"))
        artifact_info["status"] = "ok"
        artifact_info["built_at_utc"] = lineage.get("built_at_utc")
        artifact_info["build_age_days"] = (
            round(build_age, 2) if build_age is not None else None
        )
        artifact_info["git_sha"] = lineage.get("git_sha")
        artifact_info["git_dirty"] = lineage.get("git_dirty")
        artifact_info["git_branch"] = lineage.get("git_branch")
        artifact_info["builder_path"] = lineage.get("builder_path")
        artifact_info["input_hash_count"] = len(
            lineage.get("input_hashes") or {},
        )
        artifact_info["input_dir_count"] = len(
            lineage.get("input_dir_summaries") or {},
        )
        artifact_info["summary"] = format_lineage_summary_line(
            label, lineage,
        )
        if (
            build_age is not None
            and build_age > build_age_warn_days
        ):
            payload["alerts"].append(
                f"{label} cache built {build_age:.1f}d ago "
                f"(> {build_age_warn_days:.0f}d warn threshold); "
                "daily refresh may have skipped this builder. "
                "Check refresh_health_rollup or rerun the relevant "
                "refresh step."
            )
        payload["artifacts"][label] = artifact_info

    return payload


def _cross_artifact_consistency_health(
    *,
    project_root: Path = PROJECT_DIR,
    artifact_specs: Sequence[Any] = CROSS_ARTIFACT_CONSISTENCY_PATHS,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "alerts": [],
        "artifacts": {},
        "cross_artifact_divergences": [],
        "suppressed_transient_stale": [],
    }

    try:
        from scripts.analysis.artifact_lineage import (
            _read_lineage_from_path,
            compare_input_hash,
            CONSISTENCY_MATCH,
            CONSISTENCY_STALE,
        )
    except ImportError:
        try:
            from artifact_lineage import (
                _read_lineage_from_path,
                compare_input_hash,
                CONSISTENCY_MATCH,
                CONSISTENCY_STALE,
            )
        except ImportError:
            payload["alerts"].append(
                "artifact_lineage module unavailable; "
                "cross-artifact consistency check skipped."
            )
            return payload

    inputs_seen: Dict[str, List[Tuple[str, str, Optional[str]]]] = {}

    for spec in artifact_specs:
        # 2026-06-03: support both 2-tuple (label, path) and 3-tuple
        # (label, path, rebuilt_each_refresh) specs. 2-tuple defaults
        # to rebuilt_each_refresh=True (the safe default for legacy
        # callers -- treats transient STALE as suppressible noise).
        if len(spec) >= 3:
            label, rel_path, rebuilt_each_refresh = spec[0], spec[1], bool(spec[2])
        else:
            label, rel_path = spec[0], spec[1]
            rebuilt_each_refresh = True
        artifact_path = project_root / rel_path
        info: Dict[str, Any] = {
            "label": label,
            "path": str(artifact_path),
            "exists": artifact_path.exists(),
            "rebuilt_each_refresh": rebuilt_each_refresh,
            "status": "ok",
            "inputs": [],
        }
        if not artifact_path.exists():
            info["status"] = "missing"
            payload["artifacts"][label] = info
            continue
        try:
            lineage = _read_lineage_from_path(artifact_path)
        except Exception as exc:
            info["status"] = "check_error"
            info["error"] = repr(exc)
            payload["artifacts"][label] = info
            continue
        if lineage is None:
            info["status"] = "no_lineage_pre_v2"
            payload["artifacts"][label] = info
            continue
        input_hashes = lineage.get("input_hashes") or {}
        for ip in input_hashes.keys():
            try:
                verdict = compare_input_hash(
                    lineage, project_root / ip,
                    project_root=project_root,
                )
            except Exception as exc:
                verdict = {
                    "input_path": ip,
                    "status": "check_error",
                    "recorded_hash": input_hashes.get(ip),
                    "current_hash": None,
                    "error": repr(exc),
                }
            info["inputs"].append(verdict)
            recorded = verdict.get("recorded_hash")
            current = verdict.get("current_hash")
            if recorded is not None:
                inputs_seen.setdefault(ip, []).append(
                    (label, recorded, current),
                )
            if verdict.get("status") == CONSISTENCY_STALE:
                if rebuilt_each_refresh:
                    # Transient: the same refresh will rebuild this
                    # artifact at a later step and resolve the
                    # mismatch by end-of-refresh. Don't fire an alert;
                    # surface in `suppressed_transient_stale` for
                    # debugging. The 2026-06-03 fix that introduced
                    # this branch documented the root cause:
                    # daily_human_review runs at step 14 of the
                    # refresh, BEFORE the artifacts at steps 17-38 get
                    # rebuilt. A STALE check at step 14 is a snapshot
                    # of an intermediate state, not a real problem.
                    payload["suppressed_transient_stale"].append({
                        "artifact": label,
                        "input_path": ip,
                        "recorded_hash": recorded,
                        "current_hash": current,
                    })
                else:
                    # Promotion-gated or otherwise NOT rebuilt by the
                    # refresh -- alert is real signal. Operator needs
                    # to run the lever's promote command (or wait for
                    # the daemon in act mode) to rebuild. Phrase the
                    # alert with the right next-step language (the
                    # generic "rerun the artifact's refresh step"
                    # version was misleading for promotion-gated
                    # artifacts like stage3_v2_weights, which have
                    # NO refresh step -- only a manual promotion).
                    payload["alerts"].append(
                        f"{label} recorded hash for `{ip}` "
                        f"({(recorded or '')[:30]}) does not match "
                        f"current file hash ({(current or '')[:30]}). "
                        "The artifact is promotion-gated (NOT rebuilt "
                        "by the daily refresh) and was built against "
                        "an older version of this input. To bring it "
                        "current, run `promote.py <lever>` or wait "
                        "for the auto-daemon in act mode. Cross-check "
                        "the drift/verdict signal for this lever "
                        "before promoting -- the input may have "
                        "changed in ways that don't materially affect "
                        "the downstream model (e.g., stage3_v2 drift "
                        "may still be below threshold)."
                    )
        payload["artifacts"][label] = info

    for ip, entries in inputs_seen.items():
        if len(entries) < 2:
            continue
        unique_recorded = {rec for (_, rec, _) in entries}
        if len(unique_recorded) <= 1:
            continue
        per_hash: Dict[str, List[str]] = {}
        for lbl, rec, _ in entries:
            per_hash.setdefault(rec, []).append(lbl)
        divergence = {
            "input_path": ip,
            "groups": [
                {"recorded_hash": h, "artifacts": sorted(arts)}
                for h, arts in per_hash.items()
            ],
        }
        payload["cross_artifact_divergences"].append(divergence)
        group_descs = []
        for h, arts in per_hash.items():
            group_descs.append(
                f"[{', '.join(sorted(arts))}]={(h or '')[:20]}"
            )
        payload["alerts"].append(
            f"cross-artifact divergence on `{ip}`: artifacts disagree "
            f"on recorded hash -- {' vs '.join(group_descs)}. One "
            "group was built before a refresh updated this input; "
            "rebuild the older group's artifacts to align."
        )
    return payload


def _promotion_lag_health(
    *,
    project_root: Path = PROJECT_DIR,
    levers: Sequence[Tuple[str, str]] = PROMOTION_LAG_LEVERS,
    session_roots: Sequence[str] = PROMOTION_LAG_SESSION_ROOTS,
    pending_hours_warn: float = PROMOTION_LAG_PENDING_HOURS_WARN,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "alerts": [],
        "thresholds": {
            "pending_hours_warn": pending_hours_warn,
        },
        "last_engine_boot": None,
        "levers": {},
    }

    boot_info = _latest_session_start_utc(
        project_root=project_root, session_roots=session_roots,
    )
    if boot_info is None:
        payload["last_engine_boot"] = {
            "session_file": None,
            "epoch": None,
            "iso": None,
            "status": "no_session_history",
        }
        for lever_name, cache_rel in levers:
            cache_path = project_root / cache_rel
            payload["levers"][lever_name] = {
                "lever": lever_name,
                "cache_path": cache_rel,
                "cache_exists": cache_path.exists(),
                "status": "no_session_history",
            }
        return payload

    session_file, boot_epoch, boot_iso = boot_info
    payload["last_engine_boot"] = {
        "session_file": session_file,
        "epoch": boot_epoch,
        "iso": boot_iso,
        "status": "ok",
    }

    now_epoch = datetime.now(timezone.utc).timestamp()
    for lever_name, cache_rel in levers:
        cache_path = project_root / cache_rel
        info: Dict[str, Any] = {
            "lever": lever_name,
            "cache_path": cache_rel,
            "cache_exists": cache_path.exists(),
        }
        if not cache_path.exists():
            info["status"] = "cache_missing"
            payload["levers"][lever_name] = info
            continue
        try:
            cache_mtime_epoch = cache_path.stat().st_mtime
        except OSError as exc:
            info["status"] = "check_error"
            info["error"] = repr(exc)
            payload["levers"][lever_name] = info
            continue
        info["cache_mtime_epoch"] = cache_mtime_epoch
        info["cache_mtime_iso"] = (
            datetime.fromtimestamp(cache_mtime_epoch, tz=timezone.utc)
            .isoformat().replace("+00:00", "Z")
        )
        if cache_mtime_epoch <= boot_epoch:
            info["status"] = "effective_in_runtime"
            info["lag_hours"] = round(
                max(0.0, boot_epoch - cache_mtime_epoch) / 3600.0, 2,
            )
        else:
            info["status"] = "pending_next_session_boot"
            lag_h = round((now_epoch - cache_mtime_epoch) / 3600.0, 2)
            info["lag_hours"] = lag_h
            if lag_h > pending_hours_warn:
                payload["alerts"].append(
                    f"{lever_name} promote landed "
                    f"{info['cache_mtime_iso']} "
                    f"({lag_h:.1f}h ago) but engine has not booted "
                    f"since (last boot {boot_iso}). Restart the live "
                    "engine to pick up the new cache; the promote is "
                    "not yet in effect."
                )
        payload["levers"][lever_name] = info

    return payload


def _daemon_readiness_health(
    *,
    report_path: Path,
    session_date: str,
    audit_log_path: Path = DEFAULT_PROMOTION_EVENTS_LOG,
    stage2_history_path: Path = _DEFAULT_STAGE2_BRIER_HISTORY_PATH,
    stage3_v2_history_path: Path = _DEFAULT_STAGE3_V2_DRIFT_HISTORY_PATH,
) -> Dict[str, Any]:
    """Surface the daemon retrospective's per-lever readiness verdict.

    The retrospective (built by `daemon_retrospective.py`) replays the
    auto-daemon's promote-decision logic against history and classifies
    each (date, lever) into MATCH / DAEMON_ONLY / OPERATOR_ONLY /
    DAEMON_DISAGREED / BOTH_NO_ACTION. This block surfaces:
      - per-lever readiness label (ready_for_act /
        needs_more_history / disagreements_present)
      - overall_ready_for_act (true iff every time-series lever ready)
      - per-lever blocker_reason: plain-English explanation of why
        each lever isn't promoting today (added 2026-06-03 to replace
        the operationally-misleading "ready_for_act -> flip to act"
        alert that suggested operator action when no verdict was
        actually saying go)
      - alerts when stale, when disagreements present, or (positive
        signal) when at least one lever's verdict is currently promote
        AND the daemon would auto-actuate it in act mode

    Surfaces under top-level `notes` with prefix "Daemon-readiness:".
    """
    payload: Dict[str, Any] = {
        "artifact_path": str(report_path),
        "artifact_present": report_path.exists(),
        "alerts": [],
    }
    if not report_path.exists():
        payload["artifact_error"] = (
            "daemon retrospective report missing; check refresh step ran"
        )
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    payload["config"] = report.get("config")

    age = _artifact_age_days(report.get("generated_at_utc", ""), session_date)
    payload["artifact_age_days"] = age
    if age is not None and age > DAEMON_RETROSPECTIVE_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"retrospective is {age:.1f}d old "
            f"(> {DAEMON_RETROSPECTIVE_STALE_AGE_DAYS}d threshold); "
            "rerun daemon_retrospective or daily refresh."
        )

    levers: Dict[str, Dict[str, Any]] = {}
    replays = report.get("replays") or {}
    all_ready = bool(replays)
    for lever_name, replay in replays.items():
        s = replay.get("summary") or {}
        readiness = s.get("readiness_for_act")
        levers[lever_name] = {
            "readiness_for_act": readiness,
            "n_dates_evaluated": s.get("n_dates_evaluated"),
            "match_count": s.get("match_count", 0),
            "daemon_only_count": s.get("daemon_only_count", 0),
            "operator_only_count": s.get("operator_only_count", 0),
            "daemon_disagreed_count": s.get("daemon_disagreed_count", 0),
            "both_no_action_count": s.get("both_no_action_count", 0),
            "last_disagreement_date": s.get("last_disagreement_date"),
        }
        if readiness != "ready_for_act":
            all_ready = False
        if readiness == "disagreements_present":
            payload["alerts"].append(
                f"{lever_name}: {s.get('daemon_disagreed_count', 0)} "
                f"disagreement(s) + {s.get('daemon_only_count', 0)} "
                f"daemon-only action(s); inspect retrospective before "
                f"flipping to act mode (last_disagreement="
                f"{s.get('last_disagreement_date')})."
            )
    payload["levers"] = levers

    snap_summary: Dict[str, Dict[str, Any]] = {}
    snapshots_raw = report.get("snapshots") or {}
    for lever_name, snap in snapshots_raw.items():
        snap_summary[lever_name] = {
            "verdict_label": snap.get("verdict_label"),
            "actuated_by_daemon": snap.get("actuated_by_daemon"),
        }
    payload["snapshots"] = snap_summary

    # 2026-06-03: per-lever blocker_reason. Replaces the old
    # "ready_for_act -> flip --auto-daemon-mode act" alert that misled
    # the operator into thinking the daemon was stuck. The real story
    # is per-lever: each lever has a specific reason it isn't promoting
    # today (staging-worse-than-prod, in-sync research/active, runway,
    # preview-only-by-design). Surface that reason so the daily review
    # is self-explanatory.
    blockers: Dict[str, Dict[str, Any]] = {}
    # stage2 + stage3-v2: blocker text derived from the history files
    s2_reason, s2_diag, s2_promote = _stage2_blocker_reason(
        stage2_history_path,
    )
    s3_reason, s3_diag, s3_promote = _stage3_v2_blocker_reason(
        stage3_v2_history_path,
    )
    blockers["stage2"] = {
        "reason": s2_reason, "diag": s2_diag,
        "would_promote_today": s2_promote,
    }
    blockers["stage3-v2"] = {
        "reason": s3_reason, "diag": s3_diag,
        "would_promote_today": s3_promote,
    }
    # stake-scaling + gate-threshold: blocker text from the
    # retrospective's snapshot (which already carries the relevant
    # diagnostic fields from the source verdict reports).
    sk_reason, sk_diag, sk_promote = _stake_scaling_blocker_reason(
        snapshots_raw.get("stake-scaling"),
    )
    gt_reason, gt_diag, gt_promote = _gate_threshold_blocker_reason(
        snapshots_raw.get("gate-threshold"),
    )
    blockers["stake-scaling"] = {
        "reason": sk_reason, "diag": sk_diag,
        "would_promote_today": sk_promote,
    }
    blockers["gate-threshold"] = {
        "reason": gt_reason, "diag": gt_diag,
        "would_promote_today": gt_promote,
    }
    # Decorate the lever entries we already produced from replays so a
    # single per-lever lookup carries everything.
    for lever_name, info in blockers.items():
        levers.setdefault(lever_name, {})
        levers[lever_name]["blocker_reason"] = info["reason"]
        levers[lever_name]["blocker_diag"] = info["diag"]
        levers[lever_name]["would_promote_today"] = info["would_promote_today"]
    payload["per_lever_blocker_reasons"] = {
        k: v["reason"] for k, v in blockers.items()
    }

    payload["overall_ready_for_act"] = all_ready
    # `actionable_today` = levers where the verdict says go AND the
    # daemon would auto-actuate in `act` mode. Gate-threshold is
    # excluded by construction (its blocker function returns
    # would_promote_today=False even when verdict=promote, because
    # the daemon is preview-only-by-design on this lever). This list
    # is what makes flipping `--auto-daemon-mode act` USEFUL today
    # vs. a no-op.
    actionable_today = [
        lever for lever, info in blockers.items()
        if info["would_promote_today"]
    ]
    payload["actionable_today"] = actionable_today

    # 2026-06-03: replace the operationally-misleading
    # "ready_for_act -> flip to act mode" alert with one of three
    # outcomes:
    #   1. Some lever IS actionable -> tell the operator to flip and
    #      what would happen
    #   2. all_ready (retrospective agrees) but nothing actionable ->
    #      explain that flipping the mode would not promote anything
    #      and surface the per-lever reasons
    #   3. not all_ready (disagreements in history) -> existing
    #      per-lever disagreement alerts already cover this
    if actionable_today:
        readout = []
        for lever in actionable_today:
            readout.append(f"{lever}: {blockers[lever]['reason']}")
        payload["alerts"].append(
            f"daemon would auto-actuate "
            f"{', '.join(actionable_today)} TODAY if flipped to "
            f"`--auto-daemon-mode act` ({'; '.join(readout)}). "
            "Cross-check the retrospective's per-date table before "
            "flipping the mode."
        )
    elif all_ready:
        blocker_summary = "; ".join(
            f"{lever}={blockers[lever]['reason']}"
            for lever in (
                "stage2", "stage3-v2", "stake-scaling", "gate-threshold",
            )
        )
        payload["alerts"].append(
            "retrospective shows daemon agrees with operator on all "
            "evaluated dates (overall_ready_for_act=true), but no "
            "auto-actuatable lever has a `promote` verdict today, so "
            "flipping `--auto-daemon-mode act` would not promote "
            f"anything. Per-lever status: {blocker_summary}."
        )

    staleness_records = _daemon_staleness_check(
        retrospective_report=report,
        audit_log_path=audit_log_path,
        today=session_date,
        threshold_days=DAEMON_STALENESS_THRESHOLD_DAYS,
    )
    payload["staleness_records"] = staleness_records
    for rec in staleness_records:
        if rec.get("last_action_date"):
            tail = (
                f"last action {rec['days_since_last_action']}d ago "
                f"({rec.get('last_action_operator')} {rec.get('last_action_label')} "
                f"on {rec['last_action_date']})"
            )
        else:
            tail = "no successful action ever"
        # 2026-06-03: gate-threshold is preview-only-by-design, so a
        # stale promote-verdict on it isn't a daemon configuration
        # bug -- it just means the operator hasn't run
        # `promote.py gate-threshold <gate> <value>` yet. Use
        # lever-specific suffix so the alert points at the right fix.
        if rec["lever"] == "gate-threshold":
            suffix = (
                "Preview-only-by-design: daemon does NOT auto-actuate "
                "this lever. Operator runs `promote.py gate-threshold "
                "<gate> <value>` (the daemon retrospective's "
                "snapshot.actionable_gates lists the candidates)."
            )
        else:
            suffix = "Check daemon mode, cooldown, opt-out flags."
        payload["alerts"].append(
            f"{rec['lever']} verdict={rec['verdict_label']} but "
            f"{tail}; > {rec['threshold_days']}d staleness threshold. "
            f"{suffix}"
        )

    return payload


def _refresh_staleness_health(
    *,
    session_date: str,
    startup_refresh_dir: Optional[Path] = None,
    warn_hours: float = None,
    alert_hours: float = None,
) -> Dict[str, Any]:
    """Surface when the daily refresh hasn't run recently.

    The refresh writes a `<date>_startup_refresh.json` artifact under
    `data/analysis_output/startup_refresh/`. If the newest such
    artifact's effective date is older than the configured threshold,
    fire an alert -- the calibrator, concept_drift_report, training
    table, and cross-artifact consistency block all silently rot
    when the refresh is missing.

    Effective date is parsed from the filename (the `<date>` slug),
    not file mtime, because mtime can be touched by manual edits.
    Ignores `_plan` artifacts (dry-runs without execution).
    """
    from .constants import (
        DEFAULT_STARTUP_REFRESH_DIR,
        REFRESH_STALENESS_HOURS_WARN,
        REFRESH_STALENESS_HOURS_ALERT,
    )
    refresh_dir = startup_refresh_dir or DEFAULT_STARTUP_REFRESH_DIR
    warn_hours = warn_hours if warn_hours is not None else REFRESH_STALENESS_HOURS_WARN
    alert_hours = alert_hours if alert_hours is not None else REFRESH_STALENESS_HOURS_ALERT

    payload: Dict[str, Any] = {
        "artifact_dir": str(refresh_dir),
        "alerts": [],
        "thresholds": {
            "warn_hours": warn_hours,
            "alert_hours": alert_hours,
        },
    }
    if not refresh_dir.exists():
        payload["status"] = "check_error"
        payload["error"] = "startup_refresh directory missing"
        return payload

    # Collect <date>_startup_refresh.json filenames, parse the date
    # slug. Ignore plan-only artifacts (suffix _plan) -- the operator
    # may dry-run without actually executing the steps.
    dated = []
    for f in refresh_dir.glob("*_startup_refresh.json"):
        name = f.name
        if "_plan" in name:
            continue
        date_str = name.split("_")[0]
        if len(date_str) == 10 and date_str[4] == "-" and date_str[7] == "-":
            dated.append((date_str, f))
    if not dated:
        payload["status"] = "no_refresh_artifacts"
        payload["last_refresh_date"] = None
        return payload
    # Sort ascending by date string (lexicographic == chronological for ISO).
    # Skip future-dated artifacts (data anomaly; e.g. a stale 2026-06-15
    # artifact in 2026-05-21's dataset would lie about freshness).
    dated.sort(key=lambda x: x[0])
    valid = [(d, f) for d, f in dated if d <= session_date]
    if not valid:
        payload["status"] = "all_future_dated"
        payload["last_refresh_date"] = None
        return payload
    last_date, last_file = valid[-1]
    payload["last_refresh_date"] = last_date
    payload["last_refresh_artifact"] = last_file.name

    # Hours between last_date midnight and session_date midnight is
    # the floor; add 12h to approximate "the refresh was run sometime
    # that day" so a same-day refresh shows ~0-12h age, not exactly 0.
    try:
        sd = datetime.fromisoformat(session_date)
        ld = datetime.fromisoformat(last_date)
        delta_hours = max(0.0, (sd - ld).total_seconds() / 3600.0)
    except (ValueError, TypeError):
        payload["status"] = "check_error"
        payload["error"] = "could not parse session_date or last_date"
        return payload
    payload["hours_since_last_refresh"] = round(delta_hours, 1)

    if delta_hours >= alert_hours:
        payload["status"] = "alert"
        payload["alerts"].append(
            f"daily refresh STALE: last successful refresh was "
            f"{last_date} ({delta_hours:.0f}h ago, >= "
            f"{alert_hours:.0f}h alert threshold). The calibrator, "
            "concept_drift_report, signal_training_table, and "
            "cross-artifact consistency are all rotting silently. "
            "Run `python scripts/analysis/run_daily_refresh.py` "
            "(or check Windows Task Scheduler / your cron) to "
            "restore the daily pipeline."
        )
    elif delta_hours >= warn_hours:
        payload["status"] = "warn"
        payload["alerts"].append(
            f"daily refresh getting stale: last successful refresh was "
            f"{last_date} ({delta_hours:.0f}h ago, >= "
            f"{warn_hours:.0f}h warn threshold). If today's run also "
            "skips, downstream artifacts will start firing false "
            "drift alerts. Trigger the refresh manually if scheduled "
            "task isn't running."
        )
    else:
        payload["status"] = "ok"

    return payload

