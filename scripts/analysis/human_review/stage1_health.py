import json
from pathlib import Path
from typing import Any, Dict, List

from .constants import (
    STAGE1_CELL_LOSS_MIN_ABS_BIAS,
    STAGE1_CELL_LOSS_FALLBACK_RATE_NOTES_FLOOR,
    STAGE1_CELL_LOSS_STALE_AGE_DAYS,
    STAGE1_SHADOW_OVERRIDE_STALE_AGE_DAYS,
    STAGE1_ALT_A_STAGING_AGE_WARN_DAYS,
    DEFAULT_STAGE1_ALT_A_STAGING_PATH,
    DEFAULT_STAGE1_CACHE_PATH,
)

from .helpers import (
    _load_json,
    _artifact_age_days,
)


def _stage1_cell_loss_health(
    *,
    report_path: Path,
    session_date: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "artifact_path": str(report_path),
        "artifact_present": report_path.exists(),
        "alerts": [],
        "trailing_30d": None,
        "top_culprits_30d": [],
    }
    if not report_path.exists():
        payload["artifact_error"] = (
            "stage1_cell_loss_attribution missing; check refresh step ran"
        )
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    age = _artifact_age_days(
        report.get("generated_at_utc", ""), session_date,
    )
    payload["artifact_age_days"] = age
    if age is not None and age > STAGE1_CELL_LOSS_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"stage1_cell_loss_attribution report is {age:.1f}d old "
            f"(> {STAGE1_CELL_LOSS_STALE_AGE_DAYS}d threshold); "
            "rerun build_stage1_cell_loss_attribution or daily refresh."
        )

    windows = report.get("windows") or {}
    w30 = windows.get("trailing_30d") or {}
    agg = w30.get("aggregate") or {}
    if agg.get("n", 0) == 0:
        return payload

    payload["trailing_30d"] = {
        "n": agg.get("n"),
        "stage1_bias": agg.get("stage1_bias"),
        "mean_p0": agg.get("mean_p0"),
        "mean_won": agg.get("mean_won"),
        "fallback_rate": agg.get("fallback_rate"),
        "mean_poisson_minus_empirical": agg.get(
            "mean_poisson_minus_empirical",
        ),
        "n_with_empirical": agg.get("n_with_empirical"),
        "date_range": w30.get("date_range"),
    }
    payload["top_culprits_30d"] = w30.get("top_culprits") or []

    abs_bias = abs(agg.get("stage1_bias") or 0.0)
    fallback_rate = agg.get("fallback_rate") or 0.0
    if (
        abs_bias >= STAGE1_CELL_LOSS_MIN_ABS_BIAS
        and fallback_rate >= STAGE1_CELL_LOSS_FALLBACK_RATE_NOTES_FLOOR
    ):
        n = agg.get("n")
        bias_pp = (agg.get("stage1_bias") or 0.0) * 100
        gap = agg.get("mean_poisson_minus_empirical")
        culprit_msg = ""
        culprits = payload["top_culprits_30d"]
        if culprits:
            top = culprits[0]
            culprit_msg = (
                f" Top culprit: `{top['dimension']}={top['bucket']}` "
                f"(bias {(top['stage1_bias'] or 0) * 100:+.1f}pp, "
                f"n={top['n']}, "
                f"ratio_vs_agg="
                f"{(top.get('stage1_bias_vs_aggregate_ratio') or 0):.2f}x)."
            )
        gap_msg = ""
        if gap is not None:
            gap_pp = gap * 100
            if abs(gap_pp) >= 5:
                gap_msg = (
                    f" Poisson smoothing diverges from empirical by "
                    f"{gap_pp:+.1f}pp on average -- candidate fix is the "
                    "Stage-1 smoothing, not the fallback path."
                )
        payload["alerts"].append(
            f"trailing-30d Stage-1 bias {bias_pp:+.1f}pp on n={n} bets "
            f"with fallback_rate={fallback_rate * 100:.0f}% -- "
            "Active #8 retrain surface narrows to the Stage-1 "
            "fallback path." + culprit_msg + gap_msg
        )

    return payload


def _stage1_shadow_override_health(
    *,
    report_path: Path,
    session_date: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "artifact_path": str(report_path),
        "artifact_present": report_path.exists(),
        "alerts": [],
        "trailing_30d": None,
        "recommendations_30d": [],
    }
    if not report_path.exists():
        payload["artifact_error"] = (
            "stage1_shadow_override_report missing; "
            "check refresh step ran"
        )
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    age = _artifact_age_days(
        report.get("generated_at_utc", ""), session_date,
    )
    payload["artifact_age_days"] = age
    if age is not None and age > STAGE1_SHADOW_OVERRIDE_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"stage1_shadow_override_report is {age:.1f}d old "
            f"(> {STAGE1_SHADOW_OVERRIDE_STALE_AGE_DAYS}d threshold)."
        )

    windows = report.get("windows") or {}
    w30 = windows.get("trailing_30d") or {}
    n_bets = w30.get("n_bets", 0)
    if n_bets == 0:
        return payload

    prod = w30.get("production") or {}
    alt_a = w30.get("alt_a_empirical_when_available") or {}
    alt_b = w30.get("alt_b_block_fallback_level_2plus") or {}
    payload["trailing_30d"] = {
        "n_bets": n_bets,
        "production_bias": prod.get("bias"),
        "alt_a_bias": alt_a.get("bias"),
        "alt_a_bias_delta_pp": alt_a.get("bias_delta_vs_prod_pp"),
        "alt_a_n_changed": alt_a.get("n_changed"),
        "alt_a_coverage_rate": alt_a.get("n_coverage_rate"),
        "alt_b_n_blocked": alt_b.get("n_blocked"),
        "alt_b_n_kept": alt_b.get("n_kept"),
        "alt_b_counterfactual_profit_delta_usd": (
            alt_b.get("counterfactual_profit_delta_usd")
        ),
        "alt_b_blocked_n_wins": alt_b.get("blocked_n_wins"),
        "alt_b_blocked_n_losses": alt_b.get("blocked_n_losses"),
        "date_range": w30.get("date_range"),
    }
    payload["recommendations_30d"] = w30.get("recommendations") or []

    for rec in payload["recommendations_30d"]:
        alt = rec.get("alt", "?")
        payload["alerts"].append(
            f"{alt}: {rec.get('verdict')} -- " + rec.get("rationale", "")
        )

    cohort = report.get("cohort_breakdown_trailing_30d") or {}
    if cohort.get("n_bets_total"):
        top = cohort.get("top_cohorts") or {}
        payload["cohort_breakdown_30d"] = {
            "n_bets_total": cohort.get("n_bets_total"),
            "min_n_per_cohort": cohort.get("min_n_per_cohort"),
            "most_improved": (top.get("most_improved") or [])[:3],
            "regressions": (top.get("regressions") or [])[:3],
            "highest_coverage": (top.get("highest_coverage") or [])[:3],
            "largest_alt_b_savings": [
                e for e in (top.get("largest_alt_b_savings") or [])
                if (e.get("alt_b_counterfactual_usd") or 0.0) > 0
            ][:3],
        }
        most_improved = top.get("most_improved") or []
        if most_improved:
            best = most_improved[0]
            delta = best.get("bias_delta_vs_prod_pp") or 0.0
            if delta >= 1.0:
                cov = best.get("coverage_rate")
                cov_str = (
                    f"{cov * 100:.0f}%" if cov is not None else "n/a"
                )
                payload["alerts"].append(
                    f"cohort `{best.get('dimension')}="
                    f"{best.get('bucket')}` (n={best.get('n_bets')}) "
                    f"sees Alt A reduce bias by {delta:+.2f}pp "
                    f"(coverage={cov_str}). "
                    "Consider a scoped promotion on this cohort "
                    "before flipping Alt A globally."
                )
        regressions = top.get("regressions") or []
        if regressions:
            worst = regressions[0]
            delta = worst.get("bias_delta_vs_prod_pp") or 0.0
            if delta <= -2.0:
                payload["alerts"].append(
                    f"cohort `{worst.get('dimension')}="
                    f"{worst.get('bucket')}` (n={worst.get('n_bets')}) "
                    f"REGRESSES under Alt A: bias delta {delta:+.2f}pp "
                    "(Alt A makes bias worse here). A global Alt A "
                    "promote would hurt this cohort; consider scoped "
                    "promotion that excludes it."
                )

    return payload


def _stage1_alt_a_staging_health(
    *,
    staging_path: Path = DEFAULT_STAGE1_ALT_A_STAGING_PATH,
    production_path: Path = DEFAULT_STAGE1_CACHE_PATH,
    age_warn_days: float = STAGE1_ALT_A_STAGING_AGE_WARN_DAYS,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "alerts": [],
        "thresholds": {
            "age_warn_days": age_warn_days,
        },
        "staging": {
            "path": str(staging_path),
            "exists": staging_path.exists(),
        },
        "production": {
            "path": str(production_path),
            "exists": production_path.exists(),
        },
    }

    if not staging_path.exists():
        payload["staging"]["status"] = "missing"
        payload["alerts"].append(
            "Stage-1 Alt-A staging cache not found at "
            f"{staging_path.name}. Run the daily refresh step "
            "`stage1_ou_cache_alt_a` (or rebuild manually with "
            "`python cache/build_mlb_ou_cache.py --smoothing-mode "
            "empirical_when_available --out <path>`)."
        )
        return payload

    try:
        with open(staging_path, encoding="utf-8") as f:
            staging_cache = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        payload["staging"]["status"] = "check_error"
        payload["staging"]["error"] = repr(exc)
        payload["alerts"].append(
            f"Stage-1 Alt-A staging cache unreadable: {exc!r}. "
            "Delete the corrupt file; the next daily refresh will "
            "rebuild it."
        )
        return payload

    staging_meta = staging_cache.get("meta") or {}
    alt_a_summary = staging_meta.get("alt_a_smoothing") or {}
    payload["staging"]["status"] = "ok"
    payload["staging"]["alt_a_smoothing"] = alt_a_summary
    payload["staging"]["history_start_date"] = staging_meta.get(
        "history_start_date"
    )
    payload["staging"]["history_end_date"] = staging_meta.get(
        "history_end_date"
    )
    payload["staging"]["total_games"] = staging_meta.get("total_games")
    payload["staging"]["valid_cells"] = staging_meta.get("valid_cells")

    try:
        from scripts.analysis.artifact_lineage import (
            _read_lineage_from_path,
            _age_days,
        )
    except ImportError:
        try:
            from artifact_lineage import (
                _read_lineage_from_path,
                _age_days,
            )
        except ImportError:
            _read_lineage_from_path = None
            _age_days = None

    if _read_lineage_from_path is not None:
        staging_lineage = _read_lineage_from_path(staging_path) or {}
        payload["staging"]["lineage"] = {
            "built_at_utc": staging_lineage.get("built_at_utc"),
            "git_sha": staging_lineage.get("git_sha"),
            "git_dirty": staging_lineage.get("git_dirty"),
            "builder_path": staging_lineage.get("builder_path"),
        }
        if _age_days is not None and staging_lineage:
            age = _age_days(staging_lineage.get("built_at_utc"))
            payload["staging"]["build_age_days"] = (
                round(age, 2) if age is not None else None
            )
            if age is not None and age > age_warn_days:
                payload["alerts"].append(
                    f"Stage-1 Alt-A staging cache built {age:.1f}d ago "
                    f"(> {age_warn_days:.0f}d warn); daily refresh may "
                    "have skipped this builder. Check refresh status."
                )

    mode = alt_a_summary.get("mode") or ""
    if alt_a_summary.get("enabled") is not True:
        payload["alerts"].append(
            f"Stage-1 Alt-A staging cache built in mode `{mode or 'unknown'}`, "
            "not `empirical_when_available`. The staging artifact is "
            "supposed to differ from production; rerun with "
            "`--smoothing-mode empirical_when_available`."
        )

    if (
        _read_lineage_from_path is not None
        and production_path.exists()
        and staging_path.exists()
    ):
        prod_lineage = _read_lineage_from_path(production_path) or {}
        staging_lineage_for_compare = _read_lineage_from_path(staging_path) or {}
        prod_inputs = prod_lineage.get("input_hashes") or {}
        staging_inputs = staging_lineage_for_compare.get("input_hashes") or {}
        shared_paths = set(prod_inputs.keys()) & set(staging_inputs.keys())
        divergences: List[Dict[str, Any]] = []
        for ip in sorted(shared_paths):
            if prod_inputs.get(ip) != staging_inputs.get(ip):
                divergences.append(
                    {
                        "input_path": ip,
                        "production_hash": prod_inputs.get(ip),
                        "staging_hash": staging_inputs.get(ip),
                    }
                )
        payload["input_divergences"] = divergences
        if divergences:
            payload["alerts"].append(
                f"Stage-1 Alt-A staging and production caches disagree "
                f"on {len(divergences)} input hash(es); one of them is "
                "built on a stale game corpus. Trigger the daily refresh "
                "to rebuild both against the current data/games/."
            )

    return payload
