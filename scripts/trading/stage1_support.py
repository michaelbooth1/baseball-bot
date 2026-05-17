#!/usr/bin/env python3
"""
stage1_support.py -- Stage-1 lookup support and trust diagnostics.

These helpers are diagnostic-only. They do not change fair value; they expose
how much support sits underneath a Stage-1 lookup so calibration/reporting can
later decide how much to trust model-vs-market disagreement.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional


STAGE1_SUPPORT_SUFFIXES = (
    "effective_n_proxy",
    "stage1_trust_weight",
    "stage1_support_bucket",
    "exact_cell_support",
    "poisson_line_exact",
    "empirical_line_exact",
    "empirical_sample_support",
    "empirical_sample_bucket",
    "state_fallback_penalty",
    "line_fallback_penalty",
)


def stage1_support_field_names(prefix: str) -> tuple[str, ...]:
    return tuple(f"{prefix}_{suffix}" for suffix in STAGE1_SUPPORT_SUFFIXES)


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _state_fallback_penalty(level: Any) -> float:
    level_i = _safe_int(level)
    if level_i is None:
        return 0.25
    if level_i <= 0:
        return 1.0
    if level_i == 1:
        return 0.85
    if level_i == 2:
        return 0.70
    if level_i == 3:
        return 0.55
    if level_i == 4:
        return 0.40
    if level_i == 5:
        return 0.30
    return 0.20


def _line_fallback_penalty(mode: Any) -> float:
    text = str(mode or "").strip().lower()
    if text == "exact":
        return 1.0
    if text in {"interpolate", "interpolate_flat"}:
        return 0.80
    if text in {"clamp_low", "clamp_high"}:
        return 0.55
    if text in {"extrapolate_low", "extrapolate_high"}:
        return 0.35
    if text in {"invalid_line", "no_line_points", "unresolved", "no_state_match"}:
        return 0.10
    return 0.50


def _support_bucket(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    if value < 20:
        return "<20"
    if value < 50:
        return "20-50"
    if value < 100:
        return "50-100"
    if value < 250:
        return "100-250"
    return ">=250"


def _trust_weight(effective_n_proxy: Optional[float]) -> Optional[float]:
    if effective_n_proxy is None:
        return None
    # Saturating curve: ~0.22 at 20, ~0.46 at 50, ~0.71 at 100, ~0.96 at 250.
    return round(1.0 - math.exp(-max(0.0, effective_n_proxy) / 80.0), 6)


def _cell_support_mass(cell: Optional[Mapping[str, Any]]) -> Optional[float]:
    if not isinstance(cell, Mapping):
        return None
    for key in ("effective_n", "weighted_n", "n"):
        value = _safe_float(cell.get(key))
        if value is not None:
            return value
    return None


def _empirical_sample_support(cell: Optional[Mapping[str, Any]]) -> Optional[float]:
    if not isinstance(cell, Mapping):
        return None
    for key in ("effective_n_samples", "weighted_n_samples", "n_samples"):
        value = _safe_float(cell.get(key))
        if value is not None:
            return value
    return None


def stage1_support_diagnostics(
    *,
    cell: Optional[Mapping[str, Any]],
    state_fallback_level: Any,
    poisson_line_fallback_mode: Any,
    empirical_line_fallback_mode: Any,
) -> Dict[str, Any]:
    """Return support/trust diagnostics for one Stage-1 lookup."""
    support_mass = _cell_support_mass(cell)
    empirical_samples = _empirical_sample_support(cell)
    return stage1_support_diagnostics_from_values(
        support_mass=support_mass,
        empirical_sample_support=empirical_samples,
        state_fallback_level=state_fallback_level,
        poisson_line_fallback_mode=poisson_line_fallback_mode,
        empirical_line_fallback_mode=empirical_line_fallback_mode,
    )


def stage1_support_diagnostics_from_values(
    *,
    support_mass: Any,
    empirical_sample_support: Any,
    state_fallback_level: Any,
    poisson_line_fallback_mode: Any,
    empirical_line_fallback_mode: Any,
) -> Dict[str, Any]:
    """Return support/trust diagnostics from already-extracted support values."""
    support_mass_f = _safe_float(support_mass)
    empirical_samples = _safe_float(empirical_sample_support)
    state_level = _safe_int(state_fallback_level)
    poisson_mode = str(poisson_line_fallback_mode or "").strip().lower()
    empirical_mode = str(empirical_line_fallback_mode or "").strip().lower()
    state_penalty = _state_fallback_penalty(state_fallback_level)
    poisson_penalty = _line_fallback_penalty(poisson_line_fallback_mode)
    empirical_penalty = _line_fallback_penalty(empirical_line_fallback_mode)
    line_penalty = min(poisson_penalty, empirical_penalty)
    effective_n_proxy = (
        round(max(0.0, support_mass_f) * state_penalty * line_penalty, 4)
        if support_mass_f is not None
        else None
    )
    return {
        "effective_n_proxy": effective_n_proxy,
        "stage1_trust_weight": _trust_weight(effective_n_proxy),
        "stage1_support_bucket": _support_bucket(effective_n_proxy),
        "exact_cell_support": (state_level == 0 if state_level is not None else None),
        "poisson_line_exact": (poisson_mode == "exact" if poisson_mode else None),
        "empirical_line_exact": (empirical_mode == "exact" if empirical_mode else None),
        "empirical_sample_support": empirical_samples,
        "empirical_sample_bucket": _support_bucket(empirical_samples),
        "state_fallback_penalty": (
            round(state_penalty, 4)
            if support_mass_f is not None or state_level is not None
            else None
        ),
        "line_fallback_penalty": (
            round(line_penalty, 4)
            if support_mass_f is not None or poisson_mode or empirical_mode
            else None
        ),
    }


def prefixed_stage1_support_fields(
    *,
    prefix: str,
    cell: Optional[Mapping[str, Any]],
    state_fallback_level: Any,
    poisson_line_fallback_mode: Any,
    empirical_line_fallback_mode: Any,
) -> Dict[str, Any]:
    return {
        f"{prefix}_{key}": value
        for key, value in stage1_support_diagnostics(
            cell=cell,
            state_fallback_level=state_fallback_level,
            poisson_line_fallback_mode=poisson_line_fallback_mode,
            empirical_line_fallback_mode=empirical_line_fallback_mode,
        ).items()
    }
