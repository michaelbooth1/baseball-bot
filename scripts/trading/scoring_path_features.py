#!/usr/bin/env python3
"""
scoring_path_features.py -- Non-enforcing scoring-timing diagnostics.

These fields describe *how* the current score was reached: steady scoring,
burst concentration, recent scoring share, and scoreless streaks. They are
shadow/modeling features only and must not change fair value, gates, sizing, or
execution decisions without explicit promotion.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence


SCORING_PATH_FIELD_KEYS = (
    "scoring_path_available",
    "scoring_path_innings_observed",
    "scoring_path_runs_observed",
    "scoring_path_inning_runs",
    "scoring_inning_rate",
    "scoring_half_rate",
    "burst_share",
    "scoreless_streak",
    "recent2_run_share",
    "weighted_run_inning_norm",
    "inning_run_slope",
)

SCORING_PATH_MODEL_FIELD_KEYS = (
    "scoring_inning_rate",
    "scoring_half_rate",
    "burst_share",
    "scoreless_streak",
    "recent2_run_share",
    "weighted_run_inning_norm",
    "inning_run_slope",
)


def _safe_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return int(value)
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _clean_runs(values: Iterable[Any]) -> List[int]:
    out: List[int] = []
    for value in values or []:
        parsed = _safe_int(value)
        out.append(max(0, parsed or 0))
    return out


def _trailing_scoreless(values: Sequence[int]) -> int:
    count = 0
    for value in reversed(values):
        if int(value or 0) == 0:
            count += 1
        else:
            break
    return count


def _weighted_mean_index(values: Sequence[int]) -> Optional[float]:
    total = sum(values)
    if total <= 0:
        return None
    return sum((idx + 1) * value for idx, value in enumerate(values)) / total


def _linear_slope(values: Sequence[int]) -> Optional[float]:
    if len(values) < 2:
        return None
    xs = list(range(1, len(values) + 1))
    x_mean = sum(xs) / len(xs)
    y_mean = sum(values) / len(values)
    denom = sum((x - x_mean) ** 2 for x in xs)
    if denom <= 0:
        return None
    return sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values)) / denom


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    if value is None:
        return None
    try:
        value = float(value)
        if not math.isfinite(value):
            return None
        return round(value, digits)
    except (TypeError, ValueError):
        return None


def empty_scoring_path_fields() -> Dict[str, object]:
    return {key: None for key in SCORING_PATH_FIELD_KEYS}


def compute_scoring_path_fields(
    *,
    away_inning_runs: Iterable[Any] = (),
    home_inning_runs: Iterable[Any] = (),
    current_inning: Optional[int] = None,
) -> Dict[str, object]:
    """Compute compact scoring-path features from known inning runs.

    Inputs may include the current partial inning; this is intentional for live
    diagnostics because those runs are already visible in the current score.
    Missing inning rows are treated as zero up to the observed path length.
    """

    away = _clean_runs(away_inning_runs)
    home = _clean_runs(home_inning_runs)
    n = max(len(away), len(home))
    if current_inning is not None:
        parsed_inning = _safe_int(current_inning)
        if parsed_inning is not None and parsed_inning > 0:
            n = min(n, parsed_inning)
    if n <= 0:
        return empty_scoring_path_fields()

    away = (away + [0] * n)[:n]
    home = (home + [0] * n)[:n]
    inning_totals = [a + h for a, h in zip(away, home)]
    half_totals: List[int] = []
    for a, h in zip(away, home):
        half_totals.extend([a, h])

    current_total = sum(inning_totals)
    scoring_innings = sum(1 for value in inning_totals if value > 0)
    scoring_halves = sum(1 for value in half_totals if value > 0)
    recent2 = sum(inning_totals[-2:])
    weighted_idx = _weighted_mean_index(inning_totals)
    slope = _linear_slope(inning_totals)

    return {
        "scoring_path_available": True,
        "scoring_path_innings_observed": n,
        "scoring_path_runs_observed": current_total,
        "scoring_path_inning_runs": "-".join(str(v) for v in inning_totals),
        "scoring_inning_rate": _round(scoring_innings / n if n else None),
        "scoring_half_rate": _round(scoring_halves / (2 * n) if n else None),
        "burst_share": _round(max(inning_totals) / current_total if current_total > 0 else None),
        "scoreless_streak": _trailing_scoreless(inning_totals),
        "recent2_run_share": _round(recent2 / current_total if current_total > 0 else None),
        "weighted_run_inning_norm": _round(weighted_idx / n if weighted_idx is not None and n else None),
        "inning_run_slope": _round(slope),
    }
