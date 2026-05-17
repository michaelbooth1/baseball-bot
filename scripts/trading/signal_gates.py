#!/usr/bin/env python3
"""
signal_gates.py -- Gate and shadow-relaxed evaluation helpers for SignalEngine.

Extracted from signal_engine.py in Phase 3 refactor.
Behavior is intentionally unchanged.
"""

from __future__ import annotations

import hashlib
from typing import Dict, Optional, Tuple

from signal_config import (
    DEFAULT_BLOWOUT_RELAX_MAX_INNING,
    DEFAULT_BLOWOUT_RELAX_MAX_RUNS_NEEDED,
    DEFAULT_BLOWOUT_RELAX_MIN_ASK,
    DEFAULT_BLOWOUT_RELAX_NEAR_LINE_MIN_ASK,
    DEFAULT_BLOWOUT_RELAX_NEAR_LINE_RUNS_NEEDED,
    DEFAULT_GATE_RELAX_AB_FRACTION,
    DEFAULT_MIN_CURRENT_TOTAL_RELAX_ASK_MIN,
    DEFAULT_MIN_CURRENT_TOTAL_RELAX_ENABLED,
    DEFAULT_MIN_CURRENT_TOTAL_RELAX_FLOOR,
    DEFAULT_MIN_CURRENT_TOTAL_RELAX_INNING,
    DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_LEAD,
    DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_RUNS_NEEDED,
    DEFAULT_SHADOW_RELAXED_ENABLED,
    DEFAULT_SHADOW_RELAXED_MAX_BASE_FV_V2,
    DEFAULT_SHADOW_RELAXED_PACE_BUFFER,
)


def is_relax_ab_treatment(
    *,
    trade_args,
    date_str: str,
    gate_name: str,
    game_pk: int,
    line: str,
) -> bool:
    frac = float(getattr(trade_args, "gate_relax_ab_fraction", DEFAULT_GATE_RELAX_AB_FRACTION))
    frac = max(0.0, min(1.0, frac))
    if frac <= 0.0:
        return False
    if frac >= 1.0:
        return True
    key = f"{date_str}|{gate_name}|{game_pk}|{line}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) / float(0xFFFFFFFF)
    return bucket < frac


def resolve_relax_mode_action(
    *,
    trade_args,
    date_str: str,
    mode: str,
    gate_name: str,
    game_pk: int,
    line: str,
    relax_pass: bool,
) -> Tuple[bool, str]:
    if not relax_pass:
        return False, "strict_block"
    mode_norm = str(mode or "shadow").strip().lower()
    if mode_norm == "enforce":
        return True, "enforce"
    if mode_norm == "ab":
        if is_relax_ab_treatment(
            trade_args=trade_args,
            date_str=date_str,
            gate_name=gate_name,
            game_pk=game_pk,
            line=line,
        ):
            return True, "ab_treatment"
        return False, "ab_control"
    if mode_norm == "shadow":
        return False, "shadow"
    return False, "off"


def min_current_total_relax_pass(
    *,
    trade_args,
    inning: int,
    current_total: int,
    ask: float,
    lead_abs: int,
    runs_needed: Optional[float],
) -> bool:
    if not bool(getattr(trade_args, "min_current_total_relax_enabled", DEFAULT_MIN_CURRENT_TOTAL_RELAX_ENABLED)):
        return False
    if inning != int(getattr(trade_args, "min_current_total_relax_inning", DEFAULT_MIN_CURRENT_TOTAL_RELAX_INNING)):
        return False
    if current_total < int(getattr(trade_args, "min_current_total_relax_floor", DEFAULT_MIN_CURRENT_TOTAL_RELAX_FLOOR)):
        return False
    if ask < float(getattr(trade_args, "min_current_total_relax_ask_min", DEFAULT_MIN_CURRENT_TOTAL_RELAX_ASK_MIN)):
        return False
    if lead_abs > int(getattr(trade_args, "min_current_total_relax_max_lead", DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_LEAD)):
        return False
    if runs_needed is not None and runs_needed > float(
        getattr(
            trade_args,
            "min_current_total_relax_max_runs_needed",
            DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_RUNS_NEEDED,
        )
    ):
        return False
    return True


def blowout_relax_pass(
    *,
    trade_args,
    inning: int,
    ask: float,
    runs_needed: Optional[float],
) -> bool:
    if runs_needed is not None:
        near_line_runs_needed = float(getattr(
            trade_args,
            "blowout_relax_near_line_runs_needed",
            DEFAULT_BLOWOUT_RELAX_NEAR_LINE_RUNS_NEEDED,
        ))
        near_line_min_ask = float(getattr(
            trade_args,
            "blowout_relax_near_line_min_ask",
            DEFAULT_BLOWOUT_RELAX_NEAR_LINE_MIN_ASK,
        ))
        if runs_needed <= near_line_runs_needed and ask >= near_line_min_ask:
            return True
    if inning > int(getattr(trade_args, "blowout_relax_max_inning", DEFAULT_BLOWOUT_RELAX_MAX_INNING)):
        return False
    if ask < float(getattr(trade_args, "blowout_relax_min_ask", DEFAULT_BLOWOUT_RELAX_MIN_ASK)):
        return False
    if runs_needed is not None and runs_needed > float(
        getattr(trade_args, "blowout_relax_max_runs_needed", DEFAULT_BLOWOUT_RELAX_MAX_RUNS_NEEDED)
    ):
        return False
    return True


def evaluate_shadow_relaxed(
    *,
    trade_args,
    reason: str,
    values: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    out: Dict[str, object] = {
        "shadow_relaxed_evaluated": False,
        "shadow_relaxed_would_pass": None,
        "shadow_relaxed_reason": reason,
        "shadow_relaxed_value": None,
        "shadow_relaxed_threshold": None,
        "shadow_relaxed_comparator": None,
    }
    if not getattr(trade_args, "shadow_relaxed_enabled", DEFAULT_SHADOW_RELAXED_ENABLED):
        return out

    data = values or {}

    def _as_float(key: str) -> Optional[float]:
        raw = data.get(key)
        if raw is None or raw == "":
            return None
        try:
            return float(raw)
        except Exception:
            return None

    def _as_int(key: str) -> Optional[int]:
        raw = data.get(key)
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except Exception:
            return None

    def _finalize(*, would_pass: bool, value: object, threshold: object, comparator: str) -> Dict[str, object]:
        out["shadow_relaxed_evaluated"] = True
        out["shadow_relaxed_would_pass"] = bool(would_pass)
        out["shadow_relaxed_value"] = value
        out["shadow_relaxed_threshold"] = threshold
        out["shadow_relaxed_comparator"] = comparator
        return out

    if reason == "gate_min_current_total":
        current_total = _as_int("current_total")
        threshold = int(trade_args.shadow_relaxed_min_current_total)
        if current_total is None:
            return out
        return _finalize(
            would_pass=current_total >= threshold,
            value=current_total,
            threshold=threshold,
            comparator=">=",
        )

    if reason == "gate_min_current_total_conditional_v1":
        current_total = _as_int("current_total")
        inning = _as_int("inning")
        lead_abs = _as_int("lead_abs")
        decision_ask = _as_float("decision_ask")
        runs_needed = _as_float("runs_needed")
        if (
            current_total is None
            or inning is None
            or lead_abs is None
            or decision_ask is None
        ):
            return out
        strict_blocked = current_total < int(trade_args.min_current_total)
        relax_pass = min_current_total_relax_pass(
            trade_args=trade_args,
            inning=inning,
            current_total=current_total,
            ask=decision_ask,
            lead_abs=lead_abs,
            runs_needed=runs_needed,
        )
        would_pass = (not strict_blocked) or relax_pass
        return _finalize(
            would_pass=would_pass,
            value=(
                f"total={current_total},inn={inning},lead={lead_abs},"
                f"ask={decision_ask:.3f},rn={runs_needed if runs_needed is not None else 'n/a'}"
            ),
            threshold=(
                f"strict(total<{int(trade_args.min_current_total)}) + "
                f"relax(total>={int(getattr(trade_args, 'min_current_total_relax_floor', DEFAULT_MIN_CURRENT_TOTAL_RELAX_FLOOR))},"
                f"inn={int(getattr(trade_args, 'min_current_total_relax_inning', DEFAULT_MIN_CURRENT_TOTAL_RELAX_INNING))},"
                f"ask>={float(getattr(trade_args, 'min_current_total_relax_ask_min', DEFAULT_MIN_CURRENT_TOTAL_RELAX_ASK_MIN)):.3f},"
                f"lead<={int(getattr(trade_args, 'min_current_total_relax_max_lead', DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_LEAD))},"
                f"rn<={float(getattr(trade_args, 'min_current_total_relax_max_runs_needed', DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_RUNS_NEEDED)):.2f})"
            ),
            comparator="would_pass_conditional_relax",
        )

    if reason == "gate_runs_needed_max":
        runs_needed = _as_float("runs_needed")
        threshold = float(trade_args.shadow_relaxed_runs_needed_max)
        if runs_needed is None:
            return out
        return _finalize(
            would_pass=runs_needed <= threshold,
            value=round(runs_needed, 4),
            threshold=threshold,
            comparator="<=",
        )

    if reason == "gate_close_game_runs_needed":
        runs_needed = _as_float("runs_needed")
        threshold = float(trade_args.shadow_relaxed_min_close_game_rn)
        if runs_needed is None:
            return out
        return _finalize(
            would_pass=runs_needed < threshold,
            value=round(runs_needed, 4),
            threshold=threshold,
            comparator="<",
        )

    if reason == "gate_inning5_runs_needed":
        runs_needed = _as_float("runs_needed")
        threshold = float(trade_args.shadow_relaxed_inn5_rn_max)
        if runs_needed is None:
            return out
        return _finalize(
            would_pass=runs_needed < threshold,
            value=round(runs_needed, 4),
            threshold=threshold,
            comparator="<",
        )

    if reason == "gate_inning6_runs_needed":
        runs_needed = _as_float("runs_needed")
        threshold = float(trade_args.shadow_relaxed_inn6_rn_max)
        if runs_needed is None:
            return out
        return _finalize(
            would_pass=runs_needed < threshold,
            value=round(runs_needed, 4),
            threshold=threshold,
            comparator="<",
        )

    if reason == "gate_blowout":
        lead = _as_int("lead_abs")
        inning = _as_int("inning")
        trailing_runs = _as_int("trailing_runs")
        if lead is None or inning is None or trailing_runs is None:
            return out
        blocked_relaxed = trailing_runs <= 1 and inning >= 6 and (
            lead >= int(trade_args.shadow_relaxed_blowout_lead_min)
            or (lead >= int(trade_args.shadow_relaxed_blowout_adj_lead_min) and inning >= 7)
        )
        return _finalize(
            would_pass=not blocked_relaxed,
            value=f"lead={lead},inn={inning},trail={trailing_runs}",
            threshold=(
                f"lead>={int(trade_args.shadow_relaxed_blowout_lead_min)}@inn>=6 "
                f"or lead>={int(trade_args.shadow_relaxed_blowout_adj_lead_min)}@inn>=7"
            ),
            comparator="not blocked",
        )

    if reason == "gate_blowout_conditional_v1":
        lead = _as_int("lead_abs")
        inning = _as_int("inning")
        trailing_runs = _as_int("trailing_runs")
        decision_ask = _as_float("decision_ask")
        runs_needed = _as_float("runs_needed")
        if lead is None or inning is None or trailing_runs is None or decision_ask is None:
            return out
        strict_blocked = trailing_runs <= 1 and inning >= 6 and (
            lead >= int(trade_args.blowout_lead_min)
            or (lead >= int(trade_args.blowout_adj_lead_min) and inning >= 7)
        )
        relax_pass = blowout_relax_pass(
            trade_args=trade_args,
            inning=inning,
            ask=decision_ask,
            runs_needed=runs_needed,
        )
        would_pass = (not strict_blocked) or relax_pass
        max_inning = int(getattr(trade_args, "blowout_relax_max_inning", DEFAULT_BLOWOUT_RELAX_MAX_INNING))
        min_ask = float(getattr(trade_args, "blowout_relax_min_ask", DEFAULT_BLOWOUT_RELAX_MIN_ASK))
        max_runs_needed = float(getattr(
            trade_args,
            "blowout_relax_max_runs_needed",
            DEFAULT_BLOWOUT_RELAX_MAX_RUNS_NEEDED,
        ))
        near_line_runs_needed = float(getattr(
            trade_args,
            "blowout_relax_near_line_runs_needed",
            DEFAULT_BLOWOUT_RELAX_NEAR_LINE_RUNS_NEEDED,
        ))
        near_line_min_ask = float(getattr(
            trade_args,
            "blowout_relax_near_line_min_ask",
            DEFAULT_BLOWOUT_RELAX_NEAR_LINE_MIN_ASK,
        ))
        return _finalize(
            would_pass=would_pass,
            value=(
                f"lead={lead},inn={inning},trail={trailing_runs},"
                f"ask={decision_ask:.3f},rn={runs_needed if runs_needed is not None else 'n/a'}"
            ),
            threshold=(
                f"strict_blowout + relax(near_line rn<={near_line_runs_needed:.2f} "
                f"ask>={near_line_min_ask:.3f}; or inn<={max_inning},"
                f"ask>={min_ask:.3f},rn<={max_runs_needed:.2f})"
            ),
            comparator="would_pass_conditional_relax",
        )

    if reason == "gate_stage2_suppression":
        s2_delta = _as_float("stage2_run_env_delta")
        inning = _as_int("inning")
        if s2_delta is None or inning is None:
            return out
        threshold = float(trade_args.shadow_relaxed_s2_suppress_max)
        min_inning = int(trade_args.shadow_relaxed_s2_suppress_min_inning)
        blocked_relaxed = s2_delta <= threshold and inning >= min_inning
        return _finalize(
            would_pass=not blocked_relaxed,
            value=f"s2={s2_delta:.4f},inn={inning}",
            threshold=f"s2<={threshold:.4f} and inn>={min_inning}",
            comparator="not blocked",
        )

    if reason == "gate_fv_saturation":
        base_fv = _as_float("base_fair_value")
        if base_fv is None:
            return out
        threshold = float(trade_args.shadow_relaxed_max_base_fv)
        return _finalize(
            would_pass=base_fv < threshold,
            value=round(base_fv, 4),
            threshold=threshold,
            comparator="<",
        )

    if reason == "gate_fv_saturation_v2":
        base_fv = _as_float("base_fair_value")
        if base_fv is None:
            return out
        threshold = float(getattr(
            trade_args, "shadow_relaxed_max_base_fv_v2",
            DEFAULT_SHADOW_RELAXED_MAX_BASE_FV_V2,
        ))
        return _finalize(
            would_pass=base_fv < threshold,
            value=round(base_fv, 4),
            threshold=threshold,
            comparator="<",
        )

    if reason == "gate_fv_ask_gap":
        edge = _as_float("edge")
        inning = _as_int("inning")
        if edge is None or inning is None:
            return out
        threshold = float(trade_args.shadow_relaxed_fv_ask_gap_max)
        min_inning = int(trade_args.shadow_relaxed_fv_ask_gap_min_inning)
        blocked_relaxed = edge > threshold and inning >= min_inning
        return _finalize(
            would_pass=not blocked_relaxed,
            value=f"edge={edge:.4f},inn={inning}",
            threshold=f"edge>{threshold:.4f} and inn>={min_inning}",
            comparator="not blocked",
        )

    if reason == "gate_min_edge":
        edge = _as_float("edge")
        min_edge = _as_float("min_edge")
        if edge is None or min_edge is None:
            return out
        relaxed_min_edge = min_edge - float(trade_args.shadow_relaxed_min_edge_offset)
        return _finalize(
            would_pass=edge >= relaxed_min_edge,
            value=round(edge, 4),
            threshold=round(relaxed_min_edge, 4),
            comparator=">=",
        )

    if reason == "gate_sp_era":
        edge = _as_float("edge")
        min_edge = _as_float("min_edge")
        if edge is None or min_edge is None:
            return out
        relaxed_boost = float(trade_args.shadow_relaxed_sp_era_edge_boost)
        relaxed_min_edge = min_edge + relaxed_boost
        return _finalize(
            would_pass=edge >= relaxed_min_edge,
            value=round(edge, 4),
            threshold=round(relaxed_min_edge, 4),
            comparator=">=",
        )

    if reason == "gate_runs_pace":
        current_total = _as_int("current_total")
        inning = _as_int("inning")
        line_str = data.get("line", "")
        if current_total is None or inning is None or not line_str:
            return out
        try:
            line_val = float(line_str)
        except (ValueError, TypeError):
            return out
        if inning <= 0:
            return out
        pace_per_9 = (current_total / inning) * 9
        relaxed_buffer = float(getattr(
            trade_args, "shadow_relaxed_pace_buffer",
            DEFAULT_SHADOW_RELAXED_PACE_BUFFER,
        ))
        relaxed_threshold = line_val - relaxed_buffer
        return _finalize(
            would_pass=pace_per_9 >= relaxed_threshold,
            value=round(pace_per_9, 2),
            threshold=round(relaxed_threshold, 2),
            comparator=">=",
        )

    return out


__all__ = [
    "is_relax_ab_treatment",
    "resolve_relax_mode_action",
    "min_current_total_relax_pass",
    "blowout_relax_pass",
    "evaluate_shadow_relaxed",
]
