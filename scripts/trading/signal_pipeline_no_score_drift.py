#!/usr/bin/env python3
"""
signal_pipeline_no_score_drift.py -- Shadow no-score drift candidate writer.

Free function extracted from signal_pipeline.py (Tier 4 refactor, 2026-05-01).
Hosts the standalone observability writer that records one
`shadow_no_score_drift` candidate per same-score segment when the ask has
decayed but current-state FV still shows value. These rows never place
orders -- they exist to measure a possible second strategy arm.

Surface:
  - maybe_record_no_score_drift_candidate(engine, ctx) -> None

Engine attrs read:
  - engine.trade_args.shadow_no_score_drift_*
  - engine.cache, engine.stage2_model, engine.offense_model
                                              (via compute_state_value_snapshot)
  - engine._next_candidate_id, engine._record_candidate_decision
                                              (via build_base_candidate_payload)
Engine state mutated:
  - ctx.state.score_segment_shadow_logged (set True after writing)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from signal_config import (
    DEFAULT_LOOKBACK,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_ENABLED,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_ASK,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_INNING,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_SPREAD,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_ASK,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_DRAWDOWN,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_EMP_EDGE,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_INNING,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_PO_EDGE,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_AGE_SECS,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_TICKS,
    INACTIVE_INNING_STATES,
)
from model_families import NO_SCORE_DRIFT
from signal_pipeline_payload import build_base_candidate_payload
from signal_pipeline_state_value import compute_state_value_snapshot

if TYPE_CHECKING:
    from signal_engine import SignalEngine
    from signal_pipeline import TickContext


def maybe_record_no_score_drift_candidate(
    engine: "SignalEngine",
    ctx: "TickContext",
) -> None:
    """Write one shadow no-score drift candidate per same-score segment."""
    if not bool(getattr(
        engine.trade_args,
        "shadow_no_score_drift_enabled",
        DEFAULT_SHADOW_NO_SCORE_DRIFT_ENABLED,
    )):
        return

    state = ctx.state
    if bool(getattr(state, "score_segment_shadow_logged", False)):
        return

    min_inning = int(getattr(
        engine.trade_args,
        "shadow_no_score_drift_min_inning",
        DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_INNING,
    ))
    max_inning = int(getattr(
        engine.trade_args,
        "shadow_no_score_drift_max_inning",
        DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_INNING,
    ))
    if ctx.inning < min_inning or ctx.inning > max_inning:
        return
    if ctx.inning_state.lower() in INACTIVE_INNING_STATES:
        return
    if ctx.current_total > ctx.line_val:
        return

    segment_start = getattr(state, "score_segment_started_at", None)
    segment_high = getattr(state, "score_segment_high_ask", None)
    segment_ticks = int(getattr(state, "score_segment_ticks", 0) or 0)
    if segment_start is None or segment_high is None:
        return
    segment_age = max(0.0, ctx.now - float(segment_start))
    drawdown = float(segment_high) - ctx.ask

    min_age = float(getattr(
        engine.trade_args,
        "shadow_no_score_drift_min_segment_age_secs",
        DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_AGE_SECS,
    ))
    min_ticks = int(getattr(
        engine.trade_args,
        "shadow_no_score_drift_min_segment_ticks",
        DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_TICKS,
    ))
    min_drawdown = float(getattr(
        engine.trade_args,
        "shadow_no_score_drift_min_drawdown",
        DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_DRAWDOWN,
    ))
    min_ask = float(getattr(
        engine.trade_args,
        "shadow_no_score_drift_min_ask",
        DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_ASK,
    ))
    max_ask = float(getattr(
        engine.trade_args,
        "shadow_no_score_drift_max_ask",
        DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_ASK,
    ))
    max_spread = float(getattr(
        engine.trade_args,
        "shadow_no_score_drift_max_spread",
        DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_SPREAD,
    ))
    if segment_age < min_age or segment_ticks < min_ticks or drawdown < min_drawdown:
        return
    if ctx.ask < min_ask or ctx.ask > max_ask:
        return
    if ctx.best_bid is None or (ctx.ask - ctx.best_bid) > max_spread:
        return

    snapshot = compute_state_value_snapshot(engine, ctx)
    if not snapshot or snapshot.get("state_value_fv_raw") is None:
        return
    po_edge = snapshot.get("state_value_edge")
    emp_edge = snapshot.get("state_value_empirical_edge")
    min_po_edge = float(getattr(
        engine.trade_args,
        "shadow_no_score_drift_min_po_edge",
        DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_PO_EDGE,
    ))
    min_emp_edge = float(getattr(
        engine.trade_args,
        "shadow_no_score_drift_min_emp_edge",
        DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_EMP_EDGE,
    ))
    po_pass = po_edge is not None and float(po_edge) >= min_po_edge
    emp_pass = emp_edge is not None and float(emp_edge) >= min_emp_edge
    if not (po_pass or emp_pass):
        return

    payload = build_base_candidate_payload(engine, ctx)
    payload.update({f"current_{key}": value for key, value in snapshot.items()})
    payload.update(
        {
            "decision": "shadow_no_score_drift",
            "decision_reason": "state_value_no_score_drift",
            "signal_model_family": NO_SCORE_DRIFT,
            "state_value_strategy": "no_score_drift",
            "fair_value": snapshot.get("state_value_fv_raw"),
            "base_fair_value": snapshot.get("state_value_base_poisson"),
            "fair_value_raw": snapshot.get("state_value_fv_raw"),
            "edge": po_edge,
            "min_edge_effective": min_po_edge,
            "score_segment_key": f"{ctx.away_score}-{ctx.home_score}",
            "score_segment_age_secs": round(segment_age, 3),
            "score_segment_ticks": segment_ticks,
            "score_segment_high_ask": float(segment_high),
            "score_segment_drawdown": drawdown,
            "shadow_no_score_drift_min_age_secs": min_age,
            "shadow_no_score_drift_min_ticks": min_ticks,
            "shadow_no_score_drift_min_drawdown": min_drawdown,
            "shadow_no_score_drift_min_ask": min_ask,
            "shadow_no_score_drift_max_ask": max_ask,
            "shadow_no_score_drift_max_spread": max_spread,
            "shadow_no_score_drift_min_po_edge": min_po_edge,
            "shadow_no_score_drift_min_emp_edge": min_emp_edge,
            "shadow_no_score_drift_trigger": (
                "both" if po_pass and emp_pass else "po_edge" if po_pass else "empirical_edge"
            ),
            "baseline_ask": getattr(state, "baseline_ask", None),
            "ask_jump": state.ask_jump(getattr(engine.trade_args, "lookback_ticks", DEFAULT_LOOKBACK)),
            "lookback_ticks": getattr(engine.trade_args, "lookback_ticks", DEFAULT_LOOKBACK),
        }
    )
    engine._record_candidate_decision(payload)
    state.score_segment_shadow_logged = True
