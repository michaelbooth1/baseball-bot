#!/usr/bin/env python3
"""
shadow_post_tr20.py -- Post-TR20 shadow gate probes.

These fields are diagnostics only. They do not participate in live/paper
decision logic; candidate logging calls this module after the real decision is
known so the next testing window can audit hypothetical post-TR20 filters
without changing the stable gate stack.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

from line_state import _ask_edge_boost
from signal_config import (
    DEFAULT_EDGE_THRESHOLD,
    DEFAULT_EDGE_THRESHOLD_HIGH_LINE,
    DEFAULT_HIGH_LINE_CUTOFF,
    DEFAULT_MIN_CURRENT_TOTAL,
)
from signal_gates import min_current_total_relax_pass


SHADOW_POST_TR20_EXTREME_EDGE_MAX = 0.20
SHADOW_POST_TR20_ASK_RAMP_V2_START = 0.70
SHADOW_POST_TR20_ASK_RAMP_V2_END = 0.85
SHADOW_POST_TR20_ASK_RAMP_V2_MAX_BOOST = 0.08

SHADOW_POST_TR20_FIELDS = (
    "shadow_post_tr20_extreme_020_pass",
    "shadow_post_tr20_ask_ramp_v2_pass",
    "shadow_post_tr20_gate6_relax_enforce_pass",
    "shadow_post_tr20_combined_pass",
)


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
        return int(value)
    except (TypeError, ValueError):
        return None


def _runs_needed(row: Dict[str, object]) -> Optional[float]:
    explicit = _safe_float(row.get("runs_needed"))
    if explicit is not None:
        return explicit
    line = _safe_float(row.get("line"))
    current_total = _safe_float(row.get("current_total"))
    if line is None or current_total is None:
        return None
    return line - current_total


def _lead_abs(row: Dict[str, object]) -> Optional[int]:
    explicit = _safe_int(row.get("lead_abs"))
    if explicit is not None:
        return explicit
    away = _safe_int(row.get("away_score_before"))
    home = _safe_int(row.get("home_score_before"))
    if away is None or home is None:
        return None
    return abs(away - home)


def _min_edge_base(row: Dict[str, object], trade_args: Any) -> Optional[float]:
    explicit = _safe_float(row.get("min_edge_base"))
    if explicit is not None:
        return explicit
    line = _safe_float(row.get("line"))
    if line is None:
        return None
    high_line_cutoff = float(getattr(trade_args, "high_line_cutoff", DEFAULT_HIGH_LINE_CUTOFF))
    if line >= high_line_cutoff:
        return float(getattr(trade_args, "edge_threshold_high_line", DEFAULT_EDGE_THRESHOLD_HIGH_LINE))
    return float(getattr(trade_args, "edge_threshold", DEFAULT_EDGE_THRESHOLD))


def _gate6_relax_enforce_pass(row: Dict[str, object], trade_args: Any) -> Optional[bool]:
    current_total = _safe_int(row.get("current_total"))
    if current_total is None:
        return None
    min_current_total = int(getattr(trade_args, "min_current_total", DEFAULT_MIN_CURRENT_TOTAL))
    if current_total >= min_current_total:
        return True

    inning = _safe_int(row.get("inning"))
    ask = _safe_float(row.get("decision_ask"))
    lead = _lead_abs(row)
    runs_needed = _runs_needed(row)
    if inning is None or ask is None or lead is None:
        return None
    return bool(
        min_current_total_relax_pass(
            trade_args=trade_args,
            inning=inning,
            current_total=current_total,
            ask=ask,
            lead_abs=lead,
            runs_needed=runs_needed,
        )
    )


def attach_post_tr20_shadow_fields(row: Dict[str, object], trade_args: Any) -> None:
    """Attach hypothetical post-TR20 pass/fail probes to a candidate row."""
    edge = _safe_float(row.get("edge"))
    decision_reason = str(row.get("decision_reason") or "")
    has_post_fv_context = edge is not None
    has_gate6_context = decision_reason == "gate_min_current_total" or has_post_fv_context
    if not has_post_fv_context and not has_gate6_context:
        return

    gate6_pass = _gate6_relax_enforce_pass(row, trade_args) if has_gate6_context else None
    if gate6_pass is not None:
        row["shadow_post_tr20_gate6_relax_enforce_pass"] = gate6_pass

    extreme_pass: Optional[bool] = None
    ramp_pass: Optional[bool] = None
    if edge is not None:
        extreme_pass = edge <= SHADOW_POST_TR20_EXTREME_EDGE_MAX
        row["shadow_post_tr20_extreme_020_pass"] = extreme_pass

        ask = _safe_float(row.get("decision_ask"))
        min_edge_base = _min_edge_base(row, trade_args)
        if ask is not None and min_edge_base is not None:
            ramp_boost = _ask_edge_boost(
                ask=ask,
                start=SHADOW_POST_TR20_ASK_RAMP_V2_START,
                end=SHADOW_POST_TR20_ASK_RAMP_V2_END,
                max_boost=SHADOW_POST_TR20_ASK_RAMP_V2_MAX_BOOST,
            )
            ramp_pass = edge >= (min_edge_base + ramp_boost)
            row["shadow_post_tr20_ask_ramp_v2_pass"] = ramp_pass

    components = (extreme_pass, ramp_pass, gate6_pass)
    if all(value is not None for value in components):
        row["shadow_post_tr20_combined_pass"] = all(bool(value) for value in components)
