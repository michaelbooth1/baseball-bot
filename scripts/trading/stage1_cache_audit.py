#!/usr/bin/env python3
"""
stage1_cache_audit.py -- Helpers for Stage-1 cache diagnostic lookups.

Runtime FV decisions still use the production cache's Poisson `poXX` lookup.
This module is intentionally diagnostic-only: it resolves sibling empirical
`oXX` probabilities from the same cache cell, using the same style of
line-level exact/interpolate/extrapolate fallback so audit fields are not blank
for lines outside the cache grid.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Tuple


def line_code(line: Any) -> Optional[str]:
    """Convert 8.5 -> "85"."""
    try:
        return str(int(round(float(line) * 10)))
    except Exception:
        return None


def line_value_from_key(raw_key: Any) -> Optional[float]:
    """Convert keys like o85/po85 into 8.5."""
    key = str(raw_key or "").strip().lower()
    if key.startswith("po"):
        key = key[2:]
    elif key.startswith("o"):
        key = key[1:]
    if not key:
        return None
    try:
        return int(key) / 10.0
    except Exception:
        return None


def _clip_prob(value: Any) -> Optional[float]:
    try:
        prob = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(prob):
        return None
    return min(max(prob, 1e-6), 1.0 - 1e-6)


def _logit(prob: float) -> float:
    prob = min(max(float(prob), 1e-6), 1.0 - 1e-6)
    return math.log(prob / (1.0 - prob))


def _inv_logit(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _available_points(
    cell: Mapping[str, Any],
    *,
    prefix: str,
) -> List[Tuple[float, float, str]]:
    points: List[Tuple[float, float, str]] = []
    wanted = prefix.lower()
    for raw_key, raw_prob in cell.items():
        key = str(raw_key or "").strip()
        if not key.lower().startswith(wanted):
            continue
        line_val = line_value_from_key(key)
        prob = _clip_prob(raw_prob)
        if line_val is None or prob is None:
            continue
        points.append((line_val, prob, key))
    points.sort(key=lambda item: item[0])
    return points


def resolve_cell_line_probability(
    cell: Mapping[str, Any],
    *,
    requested_line: Any,
    prefix: str,
) -> Tuple[Optional[float], Dict[str, Any]]:
    """Resolve a probability from one Stage-1 cache cell.

    Args:
        cell: One cache cell payload.
        requested_line: Market line, e.g. "8.5".
        prefix: "po" for Poisson keys or "o" for empirical keys.

    Returns:
        (probability, metadata). Metadata mirrors the runtime line fallback
        language but is prefix-specific, so empirical support can be audited
        independently of the production Poisson line lookup.
    """
    code = line_code(requested_line)
    requested_key = f"{prefix}{code}" if code else None
    meta: Dict[str, Any] = {
        "line_requested_key": requested_key,
        "line_fallback_mode": "none",
        "line_source_key": None,
        "line_source_key_low": None,
        "line_source_key_high": None,
    }
    if not requested_key:
        meta["line_fallback_mode"] = "invalid_line"
        return None, meta

    exact = _clip_prob(cell.get(requested_key))
    if exact is not None:
        meta.update(
            {
                "line_fallback_mode": "exact",
                "line_source_key": requested_key,
                "line_source_key_low": requested_key,
                "line_source_key_high": requested_key,
            }
        )
        return exact, meta

    points = _available_points(cell, prefix=prefix)
    if not points:
        meta["line_fallback_mode"] = "no_line_points"
        return None, meta

    try:
        target = float(requested_line)
    except Exception:
        target = line_value_from_key(requested_key)
    if target is None:
        target = points[-1][0]

    if target <= points[0][0] + 1e-9:
        if len(points) == 1:
            prob = points[0][1]
            source = points[0][2]
            mode = "clamp_low"
            low = high = source
        else:
            x0, p0, k0 = points[0]
            x1, p1, k1 = points[1]
            slope = (_logit(p1) - _logit(p0)) / (x1 - x0) if abs(x1 - x0) > 1e-9 else 0.0
            prob = _inv_logit(_logit(p0) + slope * (target - x0))
            source = k0
            low, high = k0, k1
            mode = "extrapolate_low"
        meta.update(
            {
                "line_fallback_mode": mode,
                "line_source_key": source,
                "line_source_key_low": low,
                "line_source_key_high": high,
            }
        )
        return _clip_prob(prob), meta

    if target >= points[-1][0] - 1e-9:
        if len(points) == 1:
            prob = points[-1][1]
            source = points[-1][2]
            mode = "clamp_high"
            low = high = source
        else:
            x0, p0, k0 = points[-2]
            x1, p1, k1 = points[-1]
            slope = (_logit(p1) - _logit(p0)) / (x1 - x0) if abs(x1 - x0) > 1e-9 else 0.0
            prob = _inv_logit(_logit(p1) + slope * (target - x1))
            source = k1
            low, high = k0, k1
            mode = "extrapolate_high"
        meta.update(
            {
                "line_fallback_mode": mode,
                "line_source_key": source,
                "line_source_key_low": low,
                "line_source_key_high": high,
            }
        )
        return _clip_prob(prob), meta

    for idx in range(1, len(points)):
        lo_x, lo_p, lo_k = points[idx - 1]
        hi_x, hi_p, hi_k = points[idx]
        if lo_x <= target <= hi_x:
            if abs(hi_x - lo_x) <= 1e-9:
                prob = lo_p
                mode = "interpolate_flat"
            else:
                weight = (target - lo_x) / (hi_x - lo_x)
                prob = _inv_logit(_logit(lo_p) + weight * (_logit(hi_p) - _logit(lo_p)))
                mode = "interpolate"
            meta.update(
                {
                    "line_fallback_mode": mode,
                    "line_source_key": lo_k,
                    "line_source_key_low": lo_k,
                    "line_source_key_high": hi_k,
                }
            )
            return _clip_prob(prob), meta

    meta["line_fallback_mode"] = "unresolved"
    return None, meta
