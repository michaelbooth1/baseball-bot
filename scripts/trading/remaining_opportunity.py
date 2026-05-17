#!/usr/bin/env python3
"""
remaining_opportunity.py -- Late-game batting opportunity diagnostics.

These fields make the "home team may skip the bottom 9th" effect explicit for
candidate logs and modeling tables. They are observability/modeling features
only; no gate logic should depend on this module directly.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


REMAINING_OPPORTUNITY_FIELD_KEYS: Tuple[str, ...] = (
    "home_leading_late",
    "batting_team_is_home",
    "bottom9_available_if_needed",
    "expected_remaining_half_innings",
    "expected_remaining_pa_bucket",
    "home_skip_bottom9_risk",
)


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _half_code(inning_state: Any) -> Optional[str]:
    text = str(inning_state or "").strip().lower()
    if text.startswith("top") or text == "t":
        return "T"
    if text.startswith("bottom") or text == "b":
        return "B"
    return None


def _pa_bucket(expected_half_innings: Optional[float]) -> str:
    if expected_half_innings is None:
        return "unknown"
    expected_pa = expected_half_innings * 4.3
    if expected_pa <= 5.0:
        return "<=5"
    if expected_pa <= 10.0:
        return "6-10"
    if expected_pa <= 18.0:
        return "11-18"
    return "19+"


def _expected_remaining_half_innings(
    *,
    inning: int,
    half: str,
    home_leading: bool,
) -> float:
    """Return structural remaining half-innings including the current half.

    For regulation innings, this assumes the current score relationship holds
    through the top 9th. That intentionally captures the shorter opportunity
    set for home-leading states without pretending to forecast future scoring.
    Extras are treated as current top+bottom or current bottom only.
    """
    if inning < 9:
        remaining = (9 - inning) * 2 + (2 if half == "T" else 1)
        if home_leading:
            remaining -= 1
        return float(max(1, remaining))
    if inning == 9:
        if half == "B":
            return 1.0
        return 1.0 if home_leading else 2.0
    return 2.0 if half == "T" else 1.0


def compute_remaining_opportunity_fields(
    *,
    away_score: Any,
    home_score: Any,
    inning: Any,
    inning_state: Any,
) -> Dict[str, object]:
    """Compute explicit remaining-offense diagnostics from the scoreboard state."""
    away = _safe_int(away_score)
    home = _safe_int(home_score)
    inn = _safe_int(inning)
    half = _half_code(inning_state)

    if away is None or home is None or inn is None or half is None:
        return {
            "home_leading_late": None,
            "batting_team_is_home": None,
            "bottom9_available_if_needed": None,
            "expected_remaining_half_innings": None,
            "expected_remaining_pa_bucket": "unknown",
            "home_skip_bottom9_risk": None,
        }

    home_leading = home > away
    expected_halves = _expected_remaining_half_innings(
        inning=inn,
        half=half,
        home_leading=home_leading,
    )

    return {
        "home_leading_late": bool(home_leading and 8 <= inn <= 9),
        "batting_team_is_home": half == "B",
        "bottom9_available_if_needed": inn <= 9,
        "expected_remaining_half_innings": expected_halves,
        "expected_remaining_pa_bucket": _pa_bucket(expected_halves),
        "home_skip_bottom9_risk": (
            1.0
            if home_leading and 8 <= inn <= 9 and not (inn == 9 and half == "B")
            else 0.0
        ),
    }
