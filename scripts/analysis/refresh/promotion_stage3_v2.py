"""Stage-3 v2 promotion drift verdict + handler.

Detects when the daily Stage-3 v2 research fit (phase4_models.json
-> model_3_blend) has materially drifted from the currently-active
production weights (cache/team_offense_v2_weights.json) or compiled-in
defaults. Includes a verdict-stability gate so single-day fit noise
doesn't fire a promotion alert.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import (
    DEFAULT_STAGE3_V2_PROD_WEIGHTS_PATH,
    DEFAULT_STAGE3_V2_RESEARCH_FIT_PATH,
    LOGGER,
    RefreshConfig,
    STAGE3_V2_COMPILED_DEFAULTS,
    STAGE3_V2_PROMOTION_DRIFT_THRESHOLD,
    STAGE3_V2_PROMOTION_MIN_CONSECUTIVE,
    STAGE3_V2_PROMOTION_MIN_HISTORY,
    STAGE3_V2_PROMOTION_WINDOW,
    STAGE3_V2_VERDICT_STABILITY_MIN_HISTORY,
    STAGE3_V2_VERDICT_STABILITY_WINDOW,
)
from .helpers import _now_iso
from .preflight import _inline, _safe_load_json
from .promotion_stage2 import _stage2_history_row_date


def _extract_stage3_v2_research_betas(payload: object) -> Optional[Dict[str, float]]:
    """Pull the three coefficients out of phase4_models.json.

    Schema: payload["models"]["model_3_blend"]["beta_prior" | "beta_season" |
    "beta_momentum"]. Older keys ("model_3", "Model 3") are accepted for
    forward compat with promote_team_offense_v2.py's lookup.
    """
    if not isinstance(payload, dict):
        return None
    models = payload.get("models")
    if not isinstance(models, dict):
        return None
    fit = None
    for key in ("model_3_blend", "model_3", "Model 3", "model3", "phase4_model3"):
        if key in models and isinstance(models[key], dict):
            fit = models[key]
            break
    if fit is None:
        return None
    out: Dict[str, float] = {}
    for src_key, dest_key in (
        ("beta_prior", "prior_season"),
        ("prior_season", "prior_season"),
        ("beta_prior_season", "prior_season"),
        ("beta_season", "season_to_date"),
        ("season_to_date", "season_to_date"),
        ("beta_season_to_date", "season_to_date"),
        ("beta_momentum", "momentum_10"),
        ("momentum_10", "momentum_10"),
        ("beta_momentum_10", "momentum_10"),
    ):
        if src_key in fit and isinstance(fit[src_key], (int, float)):
            out.setdefault(dest_key, float(fit[src_key]))
    if set(out) != {"prior_season", "season_to_date", "momentum_10"}:
        return None
    return out


def _extract_stage3_v2_active_betas(prod_payload: object) -> Tuple[Dict[str, float], str]:
    """Return (betas_in_use, source_label).

    Prefers the production weights JSON when it exists and is well-formed;
    falls back to the compiled-in defaults so the comparison still works
    on first promotion.
    """
    if isinstance(prod_payload, dict):
        betas = prod_payload.get("betas")
        if isinstance(betas, dict):
            try:
                return (
                    {
                        "prior_season": float(betas["prior_season"]),
                        "season_to_date": float(betas["season_to_date"]),
                        "momentum_10": float(betas["momentum_10"]),
                    },
                    "production_weights_file",
                )
            except (KeyError, TypeError, ValueError):
                pass
    return dict(STAGE3_V2_COMPILED_DEFAULTS), "compiled_defaults"


def _stage3_v2_max_abs_delta(
    research: Dict[str, float], active: Dict[str, float]
) -> float:
    return max(abs(research[k] - active[k]) for k in ("prior_season", "season_to_date", "momentum_10"))


def _load_stage3_v2_drift_history(path: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    if not path.exists():
        return rows
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


def _trailing_stage3_v2_history(
    history_rows: List[Dict[str, object]],
    *,
    window: int,
    exclude_date: Optional[str] = None,
) -> List[Dict[str, object]]:
    """Same shape as Stage-2's trailing helper: dedupe same-date rows to
    the latest entry, return last `window` distinct dates oldest-first."""
    by_date: Dict[str, Dict[str, object]] = {}
    for row in history_rows:
        d = _stage2_history_row_date(row)  # same date-key fallback semantics
        if not d:
            continue
        if exclude_date and d == exclude_date:
            continue
        by_date[d] = row
    if not by_date:
        return []
    ordered = sorted(by_date.items(), key=lambda kv: kv[0])
    return [v for _, v in ordered[-window:]]


def _stage3_v2_primary_verdict(
    history_rows: List[Dict[str, object]],
    *,
    window: int,
    min_history: int,
    min_consecutive: int,
    drift_threshold: float,
    exclude_date: Optional[str],
) -> Dict[str, object]:
    """Primary count-based gate. Returns a verdict dict in the same shape
    `_stage3_v2_promotion_verdict` historically returned (insufficient_history
    / hold / promote). Pulled out so the verdict-stability gate can replay
    it against per-date slices of history."""
    trailing = _trailing_stage3_v2_history(
        history_rows, window=window, exclude_date=exclude_date
    )
    n_history = len(trailing)
    if n_history < min_history:
        return {
            "verdict": "insufficient_history",
            "n_history": n_history,
            "n_history_required": min_history,
            "n_drifting": 0,
            "n_consecutive_required": min_consecutive,
            "drift_threshold": drift_threshold,
        }
    n_drifting = 0
    for row in trailing:
        d = row.get("max_abs_delta")
        if isinstance(d, (int, float)) and float(d) >= drift_threshold:
            n_drifting += 1
    if n_drifting >= min_consecutive:
        verdict = "promote"
    else:
        verdict = "hold"
    return {
        "verdict": verdict,
        "n_history": n_history,
        "n_history_required": min_history,
        "n_drifting": n_drifting,
        "n_consecutive_required": min_consecutive,
        "drift_threshold": drift_threshold,
    }


def _stage3_v2_distinct_history_dates(
    history_rows: List[Dict[str, object]],
    *,
    exclude_date: Optional[str] = None,
) -> List[str]:
    """Sorted unique dates present in history (ignoring `exclude_date`)."""
    seen: set = set()
    for row in history_rows:
        d = _stage2_history_row_date(row)
        if d and d != exclude_date:
            seen.add(d)
    return sorted(seen)


def _stage3_v2_verdict_stability_gate(
    history_rows: List[Dict[str, object]],
    pre_override_verdict: str,
    *,
    stability_window: int,
    stability_min_history: int,
    primary_window: int,
    primary_min_history: int,
    primary_min_consecutive: int,
    primary_drift_threshold: float,
    exclude_date: Optional[str],
) -> Tuple[str, Dict[str, object]]:
    """Second-layer modal check on the verdict itself. Replays the primary
    count-based verdict on each prior distinct date (with history sliced
    to <= that date) to build a trailing verdict history, then takes the
    modal of the last `stability_window` distinct dates. If today's
    verdict differs from an unambiguous modal AND we have at least
    `stability_min_history` computable prior dates, override to the modal.

    Returns (final_verdict, audit). Audit shape mirrors the calibration
    stability-gate audit so the daily review block can render either
    one with the same template.
    """
    audit: Dict[str, object] = {
        "verdict_stability_gate_enabled": True,
        "verdict_stability_window": stability_window,
        "verdict_stability_min_history": stability_min_history,
        "verdict_stability_history": [],
        "verdict_stability_modal": None,
        "verdict_stability_gate_applied": False,
        "pre_override_verdict": pre_override_verdict,
    }
    prior_dates = _stage3_v2_distinct_history_dates(
        history_rows, exclude_date=exclude_date,
    )
    if not prior_dates:
        return pre_override_verdict, audit
    trailing_dates = prior_dates[-stability_window:]
    trailing_verdicts: List[str] = []
    for date_anchor in trailing_dates:
        sliced = [
            r for r in history_rows
            if _stage2_history_row_date(r) and _stage2_history_row_date(r) <= date_anchor
        ]
        v = _stage3_v2_primary_verdict(
            sliced,
            window=primary_window,
            min_history=primary_min_history,
            min_consecutive=primary_min_consecutive,
            drift_threshold=primary_drift_threshold,
            exclude_date=None,
        )
        trailing_verdicts.append(str(v.get("verdict") or ""))
    audit["verdict_stability_history"] = trailing_verdicts
    voting = [v for v in trailing_verdicts if v in ("promote", "hold")]
    if len(voting) < stability_min_history:
        return pre_override_verdict, audit
    counts: Dict[str, int] = {}
    for v in voting:
        counts[v] = counts.get(v, 0) + 1
    if not counts:
        return pre_override_verdict, audit
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    modal, modal_count = top[0]
    if len(top) > 1 and top[1][1] == modal_count:
        return pre_override_verdict, audit
    audit["verdict_stability_modal"] = modal
    if pre_override_verdict in ("promote", "hold") and modal != pre_override_verdict:
        audit["verdict_stability_gate_applied"] = True
        return modal, audit
    return pre_override_verdict, audit


def _stage3_v2_promotion_verdict(
    history_rows: List[Dict[str, object]],
    *,
    window: int = STAGE3_V2_PROMOTION_WINDOW,
    min_history: int = STAGE3_V2_PROMOTION_MIN_HISTORY,
    min_consecutive: int = STAGE3_V2_PROMOTION_MIN_CONSECUTIVE,
    drift_threshold: float = STAGE3_V2_PROMOTION_DRIFT_THRESHOLD,
    exclude_date: Optional[str] = None,
    stability_gate_enabled: bool = True,
    stability_window: int = STAGE3_V2_VERDICT_STABILITY_WINDOW,
    stability_min_history: int = STAGE3_V2_VERDICT_STABILITY_MIN_HISTORY,
) -> Dict[str, object]:
    """Stage-3 v2 promotion verdict with two-layer stability:
      - Primary: n_drifting >= min_consecutive of trailing `window` dates.
      - Secondary (when `stability_gate_enabled`): modal of the trailing
        `stability_window` per-date primary verdicts. If today differs
        from an unambiguous modal, override to the modal.

    The primary gate is the data-signal stability primitive (does the
    underlying drift hold?). The secondary gate prevents the 5-of-7
    boundary flap (one day's max_abs_delta crossing 0.015 swings the
    primary verdict promote <-> hold; the modal smooths that out).

    Disable with `stability_gate_enabled=False` to backfill or debug.
    """
    primary = _stage3_v2_primary_verdict(
        history_rows,
        window=window,
        min_history=min_history,
        min_consecutive=min_consecutive,
        drift_threshold=drift_threshold,
        exclude_date=exclude_date,
    )
    primary_verdict_label = str(primary.get("verdict") or "")
    if not stability_gate_enabled:
        primary["verdict_stability_gate_enabled"] = False
        return primary
    final_verdict, stability_audit = _stage3_v2_verdict_stability_gate(
        history_rows,
        primary_verdict_label,
        stability_window=stability_window,
        stability_min_history=stability_min_history,
        primary_window=window,
        primary_min_history=min_history,
        primary_min_consecutive=min_consecutive,
        primary_drift_threshold=drift_threshold,
        exclude_date=exclude_date,
    )
    primary["verdict"] = final_verdict
    primary.update(stability_audit)
    return primary


def _write_stage3_v2_drift_history_row(
    path: Path,
    *,
    research_betas: Dict[str, float],
    active_betas: Dict[str, float],
    active_source: str,
    max_abs_delta: float,
    data_max_date: Optional[str],
    generated_at_utc: str,
) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "generated_at_utc": generated_at_utc,
            "data_max_date": data_max_date,
            "research_betas": research_betas,
            "active_betas": active_betas,
            "active_source": active_source,
            "max_abs_delta": max_abs_delta,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as exc:
        LOGGER.warning(
            "Failed to append stage3-v2 drift history row to %s: %s. "
            "Promotion stability gate has nothing to read on the next refresh.",
            path, exc,
        )


@_inline("stage3_v2_promotion_check")
def _handle_stage3_v2_promotion_check(config: RefreshConfig) -> Tuple[bool, str]:
    """Detect when the daily Stage-3 v2 research fit has materially drifted
    from the currently-active production weights, with a stability gate so
    single-day fit noise doesn't fire a promotion alert.

    Never fails the refresh; descriptive only.
    """
    notes: List[str] = []
    research_path = DEFAULT_STAGE3_V2_RESEARCH_FIT_PATH
    prod_path = DEFAULT_STAGE3_V2_PROD_WEIGHTS_PATH
    history_path = config.stage3_v2_drift_history_path

    research_payload, research_err = _safe_load_json(research_path)
    if research_err:
        notes.append(
            f"Stage-3 v2 promotion check skipped: research fit unreadable ({research_err})."
        )
        return True, "\n".join(notes)
    research_betas = _extract_stage3_v2_research_betas(research_payload)
    if research_betas is None:
        notes.append(
            f"Stage-3 v2 promotion check skipped: could not extract model_3_blend "
            f"betas from {research_path.name}."
        )
        return True, "\n".join(notes)

    prod_payload, _ = _safe_load_json(prod_path)
    active_betas, active_source = _extract_stage3_v2_active_betas(prod_payload)
    max_abs_delta = _stage3_v2_max_abs_delta(research_betas, active_betas)

    notes.append(
        f"Stage-3 v2: research vs {active_source} max|delta|={max_abs_delta:.4f} "
        f"(prior {research_betas['prior_season']:+.4f} vs {active_betas['prior_season']:+.4f}, "
        f"season {research_betas['season_to_date']:+.4f} vs {active_betas['season_to_date']:+.4f}, "
        f"momentum {research_betas['momentum_10']:+.4f} vs {active_betas['momentum_10']:+.4f})."
    )

    today_date_key = (config.max_date or config.active_date or "")[:10] or None
    _write_stage3_v2_drift_history_row(
        history_path,
        research_betas=research_betas,
        active_betas=active_betas,
        active_source=active_source,
        max_abs_delta=max_abs_delta,
        data_max_date=today_date_key,
        generated_at_utc=_now_iso(),
    )
    history_rows = _load_stage3_v2_drift_history(history_path)
    verdict = _stage3_v2_promotion_verdict(
        history_rows, exclude_date=today_date_key
    )
    v_label = verdict["verdict"]
    if v_label == "promote":
        notes.append(
            f"ALERT Stage-3 v2 PROMOTION READY: max|delta| >= "
            f"{verdict['drift_threshold']:.4f} on {verdict['n_drifting']}/"
            f"{verdict['n_history']} of the last {STAGE3_V2_PROMOTION_WINDOW} "
            f"distinct dates (threshold {verdict['n_consecutive_required']}). "
            f"Promote with: python scripts/analysis/promote_team_offense_v2.py."
        )
    elif v_label == "hold":
        notes.append(
            f"ok Stage-3 v2 promotion stability gate: hold "
            f"({verdict['n_drifting']}/{verdict['n_history']} drifting days, "
            f"need {verdict['n_consecutive_required']})."
        )
    else:  # insufficient_history
        notes.append(
            f"ok Stage-3 v2 promotion stability gate: building history "
            f"({verdict['n_history']}/{verdict['n_history_required']} distinct prior dates)."
        )
    if active_source == "compiled_defaults":
        notes.append(
            "note Stage-3 v2 production weights file missing -- runtime is using "
            "compiled-in defaults. Drift is measured against those defaults until "
            "promote_team_offense_v2.py runs at least once."
        )
    return True, "\n".join(notes)
