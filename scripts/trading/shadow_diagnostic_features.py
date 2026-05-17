#!/usr/bin/env python3
"""
shadow_diagnostic_features.py -- Non-enforcing model-risk buckets.

These helpers make repeatable audit cuts visible without changing gate logic:
low-ask/high-edge disagreement, exact 3.5 runs-needed contexts, current-state
edge crossed with phantom risk, inning crossed with runs-needed, and late home
lead opportunity loss. The fields are diagnostics/model features only.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional


LOW_ASK_HIGH_EDGE_ASK_MAX = 0.70
LOW_ASK_HIGH_EDGE_EDGE_MIN = 0.20
RUNS_NEEDED_EXACT_TRAP = 3.5
CURRENT_EDGE_DANGER_MAX = 0.03
CURRENT_EDGE_STRONG_MIN = 0.08
PHANTOM_MEDIUM_MIN = 0.40
PHANTOM_HIGH_MIN = 0.70


def safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def safe_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def runs_needed_bucket(value: Any) -> str:
    rn = safe_float(value)
    if rn is None:
        return "missing"
    if rn <= 1.5:
        return "<=1.5"
    if rn <= 2.5:
        return "1.5-2.5"
    if rn <= 3.5:
        return "2.5-3.5"
    return ">3.5"


def inning_bucket(value: Any) -> str:
    inning = safe_float(value)
    if inning is None:
        return "missing"
    inn = int(inning)
    if inn <= 4:
        return "<=4"
    if inn == 5:
        return "5"
    if inn == 6:
        return "6"
    if inn == 7:
        return "7"
    return ">=8"


def current_state_edge_bucket(value: Any) -> str:
    edge = safe_float(value)
    if edge is None:
        return "missing"
    if edge < CURRENT_EDGE_DANGER_MAX:
        return "current_edge<0.03"
    if edge < CURRENT_EDGE_STRONG_MIN:
        return "current_edge_0.03-0.08"
    return "current_edge>=0.08"


def phantom_risk_bucket(*, score: Any = None, band: Any = None) -> str:
    band_text = str(band or "").strip().lower()
    if band_text in {"low", "medium", "high"}:
        return band_text
    value = safe_float(score)
    if value is None:
        return "missing"
    if value >= PHANTOM_HIGH_MIN:
        return "high"
    if value >= PHANTOM_MEDIUM_MIN:
        return "medium"
    return "low"


def bottom9_home_lead_context(row: Dict[str, Any]) -> str:
    home_leading_late = safe_bool(row.get("home_leading_late"))
    batting_home = safe_bool(row.get("batting_team_is_home"))
    skip_risk = safe_float(row.get("home_skip_bottom9_risk"))
    expected_pa_bucket = str(row.get("expected_remaining_pa_bucket") or "unknown")

    if home_leading_late is None and batting_home is None and skip_risk is None:
        return "missing"
    if skip_risk is not None and skip_risk >= 1.0:
        return f"home_leading_late_skip_bottom9_risk|pa_{expected_pa_bucket}"
    if home_leading_late and batting_home:
        return f"home_leading_late_batting_bottom|pa_{expected_pa_bucket}"
    if home_leading_late:
        return f"home_leading_late_low_skip_risk|pa_{expected_pa_bucket}"
    if batting_home:
        return f"home_batting_no_late_lead|pa_{expected_pa_bucket}"
    return f"no_home_late_lead|pa_{expected_pa_bucket}"


def home_skip_bottom9_risk_bucket(value: Any) -> str:
    risk = safe_float(value)
    if risk is None:
        return "missing"
    if risk >= 1.0:
        return "skip_bottom9_risk"
    if risk > 0.0:
        return "partial_skip_bottom9_risk"
    return "no_skip_bottom9_risk"


def no_score_edge_bucket(value: Any) -> str:
    edge = safe_float(value)
    if edge is None:
        return "missing"
    if edge < 0.0:
        return "<0.00"
    if edge < 0.05:
        return "0.00-0.05"
    if edge < 0.10:
        return "0.05-0.10"
    if edge < 0.15:
        return "0.10-0.15"
    return ">=0.15"


def no_score_ask_bucket(value: Any) -> str:
    ask = safe_float(value)
    if ask is None:
        return "missing"
    if ask < 0.40:
        return "<0.40"
    if ask < 0.55:
        return "0.40-0.55"
    if ask < 0.70:
        return "0.55-0.70"
    if ask < 0.85:
        return "0.70-0.85"
    return ">=0.85"


def no_score_drawdown_bucket(value: Any) -> str:
    drawdown = safe_float(value)
    if drawdown is None:
        return "missing"
    if drawdown < 0.03:
        return "<0.03"
    if drawdown < 0.06:
        return "0.03-0.06"
    if drawdown < 0.10:
        return "0.06-0.10"
    if drawdown < 0.20:
        return "0.10-0.20"
    return ">=0.20"


def compute_shadow_diagnostic_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """Return shadow-only diagnostics derived from a candidate or bet row."""
    ask = safe_float(row.get("decision_ask", row.get("entry_ask")))
    edge = safe_float(row.get("edge", row.get("edge_at_ask")))
    rn = safe_float(row.get("runs_needed"))
    current_state_edge = safe_float(row.get("current_state_value_edge"))
    empirical_edge = safe_float(row.get("current_state_value_empirical_edge"))
    score_segment_drawdown = safe_float(row.get("score_segment_drawdown"))
    current_bucket = current_state_edge_bucket(row.get("current_state_value_edge"))
    phantom_bucket = phantom_risk_bucket(
        score=row.get("shadow_phantom_risk_score"),
        band=row.get("shadow_phantom_risk_band"),
    )
    inn_bucket = inning_bucket(row.get("inning"))
    rn_bucket = runs_needed_bucket(rn)
    poisson_edge_bucket = no_score_edge_bucket(current_state_edge)
    empirical_edge_bucket = no_score_edge_bucket(empirical_edge)
    ask_bucket = no_score_ask_bucket(ask)
    drawdown_bucket = no_score_drawdown_bucket(score_segment_drawdown)

    return {
        "shadow_low_ask_high_edge": (
            ask < LOW_ASK_HIGH_EDGE_ASK_MAX and edge > LOW_ASK_HIGH_EDGE_EDGE_MIN
            if ask is not None and edge is not None
            else None
        ),
        "shadow_runs_needed_exact_3p5": (
            abs(rn - RUNS_NEEDED_EXACT_TRAP) < 1e-9 if rn is not None else None
        ),
        "shadow_score_event_current_edge_strong_ask_lt_085": (
            current_state_edge >= CURRENT_EDGE_STRONG_MIN and ask < 0.85
            if current_state_edge is not None and ask is not None
            else None
        ),
        "shadow_current_state_edge_bucket": current_bucket,
        "shadow_phantom_risk_bucket": phantom_bucket,
        "shadow_current_phantom_combo_bucket": (
            f"{current_bucket}|phantom_{phantom_bucket}"
            if current_bucket != "missing" and phantom_bucket != "missing"
            else "missing"
        ),
        "shadow_inning_bucket": inn_bucket,
        "shadow_inning_runs_needed_bucket": (
            f"inn_{inn_bucket}|rn_{rn_bucket}"
            if inn_bucket != "missing" and rn_bucket != "missing"
            else "missing"
        ),
        "shadow_bottom9_home_lead_context": bottom9_home_lead_context(row),
        "shadow_home_skip_bottom9_risk_bucket": home_skip_bottom9_risk_bucket(
            row.get("home_skip_bottom9_risk")
        ),
        "shadow_no_score_poisson_edge_bucket": poisson_edge_bucket,
        "shadow_no_score_empirical_edge_bucket": empirical_edge_bucket,
        "shadow_no_score_ask_bucket": ask_bucket,
        "shadow_no_score_drawdown_bucket": drawdown_bucket,
        "shadow_no_score_poisson_empirical_ask_drawdown_bucket": (
            f"poisson_{poisson_edge_bucket}|empirical_{empirical_edge_bucket}|"
            f"ask_{ask_bucket}|drawdown_{drawdown_bucket}"
            if (
                poisson_edge_bucket != "missing"
                and empirical_edge_bucket != "missing"
                and ask_bucket != "missing"
                and drawdown_bucket != "missing"
            )
            else "missing"
        ),
    }
