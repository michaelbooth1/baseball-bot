#!/usr/bin/env python3
"""
signal_pipeline_payload.py -- Candidate row schema + shadow risk tag attachers.

Free functions extracted from signal_pipeline.py (Tier 4 refactor, 2026-05-01).
Hosts every helper that constructs or decorates the canonical candidate row,
keeping schema-touching code in one place to prevent drift between the early-
skip writer, late-skip writer, and trade-row writer.

Surfaces:
  - STATE_VALUE_BET_DIAGNOSTIC_KEYS  (constant)
  - compact_state_value_bet_diagnostics(payload) -> dict
        Subset of state-value diagnostic fields that belong in bet ledgers.
  - attach_shadow_risk_tags(*, trade_args, book, candidate_payload, ask, edge)
        Risk tags for post-session diagnostics. extreme_edge is currently
        enforced later by the post-FV gate; ltp_ask_gap remains shadow-only.
  - build_base_candidate_payload(engine, ctx) -> dict
        Canonical schema for one candidate row. All fields populated to None
        by default so downstream rollups can rely on missing-safe access.
  - record_early_skip(engine, ctx, reason, extra_fields=None)
        Convenience for Gates 1-5 that fire before any model work.

Engine attrs read:
  - engine._next_candidate_id(game_pk, line)
  - engine._prob_calibration_mode
  - engine._record_candidate_decision(payload)
"""

from __future__ import annotations

from typing import Any, Dict, Optional, TYPE_CHECKING

from line_state import _now_iso
from model_families import SCORE_EVENT_TRANSITION
from models import signal_context_fields
from remaining_opportunity import (
    REMAINING_OPPORTUNITY_FIELD_KEYS,
    compute_remaining_opportunity_fields,
)
from scoring_path_features import SCORING_PATH_FIELD_KEYS, compute_scoring_path_fields
from shadow_diagnostic_features import compute_shadow_diagnostic_fields
from signal_config import DEFAULT_EXTREME_EDGE_MAX, DEFAULT_LTP_ASK_GAP_MAX
from stage1_support import STAGE1_SUPPORT_SUFFIXES, stage1_support_field_names
from weather_client import WEATHER_FEATURE_FIELD_KEYS

if TYPE_CHECKING:
    from signal_engine import SignalEngine
    from signal_pipeline import TickContext


# Diagnostic keys that flow through into placed-bet ledgers (subset of the
# full shadow-risk decoration applied to every candidate row).
INFERENCE_PANEL_FIELD_KEYS = (
    "inference_panel_runs_considered",
    "inference_panel_selected_rule",
    "inference_panel_selected_runs",
    *(
        f"inference_run{runs}_{suffix}"
        for runs in (1, 2, 3)
        for suffix in (
            "selected",
            "away_score",
            "home_score",
            "total",
            "base_poisson",
            "base_empirical",
            "poisson_minus_empirical",
            "distance_to_ask",
            "empirical_distance_to_ask",
            "n",
            "n_samples",
            "effective_n",
            *(f"support_{suffix}" for suffix in STAGE1_SUPPORT_SUFFIXES),
            "fallback_level",
            "fallback_label",
            "cell_key",
            "line_fallback_mode",
            "line_source_key",
            "empirical_line_fallback_mode",
            "empirical_line_source_key",
        )
    ),
)

MARKET_COMPLEMENT_FIELD_KEYS = (
    "over_token_id",
    "under_token_id",
    "over_best_bid",
    "over_best_ask",
    "over_mid",
    "over_spread",
    "over_ltp",
    "over_book_source",
    "decision_mid",
    "under_pair_available",
    "under_best_bid",
    "under_best_ask",
    "under_mid",
    "under_spread",
    "under_ltp",
    "under_book_source",
    "over_under_ask_sum",
    "over_under_bid_sum",
    "over_under_mid_sum",
    "over_mid_no_vig",
    "under_mid_no_vig",
    "decision_market_mid_no_vig",
)

STATE_VALUE_BET_DIAGNOSTIC_KEYS = (
    "state_value_strategy",
    "inferred_state_base_poisson",
    "inferred_state_base_empirical",
    "inferred_state_poisson_minus_empirical",
    "inferred_state_empirical_edge",
    "inferred_state_n",
    "inferred_state_n_samples",
    "inferred_state_weighted_n",
    "inferred_state_effective_n",
    *stage1_support_field_names("inferred_state"),
    "inferred_state_fallback_level",
    "inferred_state_fallback_label",
    "inferred_state_cell_key",
    "inferred_state_line_key_poisson",
    "inferred_state_line_key_empirical",
    "inferred_state_line_fallback_mode",
    "inferred_state_empirical_line_fallback_mode",
    "inferred_state_empirical_line_source_key",
    "inferred_state_empirical_line_source_key_low",
    "inferred_state_empirical_line_source_key_high",
    "inferred_state_used_fallback",
    "inferred_state_base_source",
    "current_state_value_base_poisson",
    "current_state_value_base_empirical",
    "current_state_value_line_key_poisson",
    "current_state_value_line_key_empirical",
    "current_state_value_used_fallback",
    "current_state_value_state_fallback_level",
    "current_state_value_state_fallback_label",
    "current_state_value_state_cell_key",
    "current_state_value_line_fallback_mode",
    "current_state_value_line_source_key",
    "current_state_value_empirical_line_fallback_mode",
    "current_state_value_empirical_line_source_key",
    *stage1_support_field_names("current_state_value"),
    "current_state_value_edge",
    "current_state_value_fv_raw",
    "current_state_value_stage2_run_env_delta",
    "current_state_value_team_offense_delta",
    "current_state_value_empirical_edge",
    "current_state_value_away_score",
    "current_state_value_home_score",
    "current_state_value_total",
    "shadow_fv_current_state",
    "shadow_fv_after_inferred_score",
    "shadow_fv_inferred_lift",
    "shadow_no_event_edge",
    "shadow_after_event_edge",
    "shadow_p_score_event_proxy",
    "shadow_phantom_risk_score",
    "shadow_phantom_risk_band",
    "shadow_transition_model",
    "shadow_transition_inferred_runs",
    "shadow_extreme_edge",
    "shadow_extreme_edge_threshold",
    "shadow_ltp_at_signal",
    "shadow_ltp_ask_gap",
    "shadow_ltp_ask_gap_threshold",
    "shadow_ltp_ask_gap_exceeded",
    "shadow_risk_tags",
    "shadow_low_ask_high_edge",
    "shadow_runs_needed_exact_3p5",
    "shadow_current_state_edge_bucket",
    "shadow_phantom_risk_bucket",
    "shadow_current_phantom_combo_bucket",
    "shadow_inning_bucket",
    "shadow_inning_runs_needed_bucket",
    "shadow_bottom9_home_lead_context",
    "shadow_home_skip_bottom9_risk_bucket",
    *REMAINING_OPPORTUNITY_FIELD_KEYS,
    *SCORING_PATH_FIELD_KEYS,
    "score_segment_key",
    "score_segment_age_secs",
    "score_segment_ticks",
    "score_segment_high_ask",
    "score_segment_drawdown",
    "shadow_no_score_drift_min_age_secs",
    "shadow_no_score_drift_min_ticks",
    "shadow_no_score_drift_min_drawdown",
    "shadow_no_score_drift_min_ask",
    "shadow_no_score_drift_max_ask",
    "shadow_no_score_drift_max_spread",
    "shadow_no_score_drift_min_po_edge",
    "shadow_no_score_drift_min_emp_edge",
    "shadow_no_score_drift_trigger",
    "baseline_ask",
    "ask_jump",
    "lookback_ticks",
    *WEATHER_FEATURE_FIELD_KEYS,
)


def _safe_float(value: object) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _midpoint(bid: object, ask: object) -> Optional[float]:
    bid_f = _safe_float(bid)
    ask_f = _safe_float(ask)
    if bid_f is None or ask_f is None:
        return None
    return (bid_f + ask_f) / 2.0


def _spread(bid: object, ask: object) -> Optional[float]:
    bid_f = _safe_float(bid)
    ask_f = _safe_float(ask)
    if bid_f is None or ask_f is None:
        return None
    return ask_f - bid_f


def _market_complement_fields(ctx: "TickContext") -> Dict[str, object]:
    """Decision-time market fields for Over plus its Under complement."""
    book = ctx.book if isinstance(ctx.book, dict) else {}
    over_bid = _safe_float(ctx.best_bid)
    over_ask = _safe_float(ctx.ask)
    over_mid = _midpoint(over_bid, over_ask)
    under_bid = _safe_float(book.get("under_best_bid"))
    under_ask = _safe_float(book.get("under_best_ask"))
    under_mid = _midpoint(under_bid, under_ask)
    mid_sum = (
        over_mid + under_mid
        if over_mid is not None and under_mid is not None and (over_mid + under_mid) > 0
        else None
    )
    return {
        "over_token_id": getattr(ctx.market, "over_token_id", None),
        "under_token_id": getattr(ctx.market, "under_token_id", None),
        "over_best_bid": over_bid,
        "over_best_ask": over_ask,
        "over_mid": over_mid,
        "over_spread": _spread(over_bid, over_ask),
        "over_ltp": _safe_float(book.get("ltp")),
        "over_book_source": book.get("source"),
        "decision_mid": over_mid,
        "under_pair_available": bool(book.get("under_pair_available")),
        "under_best_bid": under_bid,
        "under_best_ask": under_ask,
        "under_mid": under_mid,
        "under_spread": _spread(under_bid, under_ask),
        "under_ltp": _safe_float(book.get("under_ltp")),
        "under_book_source": book.get("under_source"),
        "over_under_ask_sum": (
            over_ask + under_ask if over_ask is not None and under_ask is not None else None
        ),
        "over_under_bid_sum": (
            over_bid + under_bid if over_bid is not None and under_bid is not None else None
        ),
        "over_under_mid_sum": mid_sum,
        "over_mid_no_vig": over_mid / mid_sum if mid_sum else None,
        "under_mid_no_vig": under_mid / mid_sum if mid_sum else None,
        "decision_market_mid_no_vig": over_mid / mid_sum if mid_sum else None,
    }


def compact_state_value_bet_diagnostics(
    candidate_payload: Dict[str, object],
) -> Dict[str, object]:
    """Return the placed-order diagnostics that belong in bet ledgers."""
    enriched_payload = dict(candidate_payload)
    for key, value in compute_shadow_diagnostic_fields(enriched_payload).items():
        if enriched_payload.get(key) in (None, ""):
            enriched_payload[key] = value
    out: Dict[str, object] = {}
    for key in STATE_VALUE_BET_DIAGNOSTIC_KEYS:
        value = enriched_payload.get(key)
        if value is not None and value != "":
            out[key] = value
    return out


def attach_shadow_risk_tags(
    *,
    trade_args: Any,
    book: Dict[str, Any],
    candidate_payload: Dict[str, object],
    ask: float,
    edge: float,
) -> None:
    """Attach risk tags used for post-session diagnostics.

    Note: extreme_edge is also enforced by the post-FV gate stack as TR17.
    This helper only decorates the candidate row; it does not make the
    decision itself. ltp_ask_gap remains shadow-only.
    """
    tags = []
    extreme_edge_max = float(getattr(trade_args, "extreme_edge_max", DEFAULT_EXTREME_EDGE_MAX))
    shadow_extreme_edge = edge > extreme_edge_max
    if shadow_extreme_edge:
        tags.append("extreme_edge")
    candidate_payload["shadow_extreme_edge"] = shadow_extreme_edge
    candidate_payload["shadow_extreme_edge_threshold"] = extreme_edge_max

    ltp_ask_gap_max = float(getattr(trade_args, "ltp_ask_gap_max", DEFAULT_LTP_ASK_GAP_MAX))
    ltp = book.get("ltp") if isinstance(book, dict) else None
    ltp_val = None
    ltp_ask_gap = None
    shadow_ltp_ask_gap = False
    if ltp is not None:
        try:
            ltp_val = float(ltp)
            ltp_ask_gap = abs(ask - ltp_val)
            shadow_ltp_ask_gap = ltp_ask_gap > ltp_ask_gap_max
        except (TypeError, ValueError):
            ltp_val = None
            ltp_ask_gap = None
    if shadow_ltp_ask_gap:
        tags.append("ltp_ask_gap")
    candidate_payload["shadow_ltp_at_signal"] = ltp_val
    candidate_payload["shadow_ltp_ask_gap"] = ltp_ask_gap
    candidate_payload["shadow_ltp_ask_gap_threshold"] = ltp_ask_gap_max
    candidate_payload["shadow_ltp_ask_gap_exceeded"] = shadow_ltp_ask_gap
    candidate_payload["shadow_risk_tags"] = ",".join(tags)


def build_base_candidate_payload(engine: "SignalEngine", ctx: "TickContext") -> Dict[str, object]:
    """Canonical candidate-row schema. All fields default to None so downstream
    rollups can rely on missing-safe access."""
    remaining_opportunity_fields = compute_remaining_opportunity_fields(
        away_score=ctx.away_score,
        home_score=ctx.home_score,
        inning=ctx.inning,
        inning_state=ctx.inning_state,
    )
    scoring_path_fields = compute_scoring_path_fields(
        away_inning_runs=getattr(ctx, "away_inning_runs", ()),
        home_inning_runs=getattr(ctx, "home_inning_runs", ()),
        current_inning=ctx.inning,
    )
    payload = {
        "candidate_id": engine._next_candidate_id(ctx.game.game_pk, ctx.market.line),
        "ts": _now_iso(),
        "game_pk": ctx.game.game_pk,
        "away_abbrev": ctx.game.away_abbrev,
        "home_abbrev": ctx.game.home_abbrev,
        "line": ctx.market.line,
        "signal_model_family": SCORE_EVENT_TRANSITION,
        "inning": ctx.inning,
        "inning_state": ctx.inning_state,
        "outs": ctx.outs,
        "runners_on": ctx.runners_on,
        "away_score_before": ctx.away_score,
        "home_score_before": ctx.home_score,
        "current_total": ctx.current_total,
        # Tier-1 player/count/scheduling capture (2026-05-28): pitcher
        # identity/ERA, count, and scheduling metadata already in memory on
        # the ScheduledGame but previously dropped. Shared with the bet
        # record via models.signal_context_fields so both stay in lockstep.
        **signal_context_fields(ctx.game),
        **remaining_opportunity_fields,
        **scoring_path_fields,
        **_market_complement_fields(ctx),
        "decision_ask": ctx.ask,
        "best_bid": ctx.best_bid,
        "spread": (ctx.ask - ctx.best_bid) if ctx.best_bid is not None else None,
        "decision": None,
        "decision_reason": None,
        "bet_id": None,
        "runs_needed": None,
        "lead_abs": None,
        "inferred_runs": None,
        "fair_value": None,
        "base_fair_value": None,
        "stage2_run_env_delta": None,
        "team_offense_delta": None,
        "edge": None,
        "min_edge_base": None,
        "min_edge_ask_boost": None,
        "min_edge_effective": None,
        "fair_value_raw": None,
        "fair_value_calibrated": None,
        "fair_value_calibration_delta": None,
        "fair_value_calibration_method": None,
        "fair_value_calibration_mode": getattr(engine, "_prob_calibration_mode", "off"),
        "fair_value_calibration_family": None,
        "fair_value_calibration_applied": None,
        "inferred_state_base_poisson": None,
        "inferred_state_base_empirical": None,
        "inferred_state_poisson_minus_empirical": None,
        "inferred_state_empirical_edge": None,
        "inferred_state_n": None,
        "inferred_state_n_samples": None,
        "inferred_state_weighted_n": None,
        "inferred_state_effective_n": None,
        **{key: None for key in stage1_support_field_names("inferred_state")},
        "inferred_state_fallback_level": None,
        "inferred_state_fallback_label": None,
        "inferred_state_cell_key": None,
        "inferred_state_line_key_poisson": None,
        "inferred_state_line_key_empirical": None,
        "inferred_state_line_fallback_mode": None,
        "inferred_state_empirical_line_fallback_mode": None,
        "inferred_state_empirical_line_source_key": None,
        "inferred_state_empirical_line_source_key_low": None,
        "inferred_state_empirical_line_source_key_high": None,
        "inferred_state_used_fallback": None,
        "inferred_state_base_source": None,
        "inference_used_fallback": None,
        "inference_state_fallback_level": None,
        "inference_state_fallback_label": None,
        "inference_state_cell_key": None,
        "inference_line_requested_key": None,
        "inference_line_fallback_mode": None,
        "inference_line_source_key": None,
        "inference_line_source_key_low": None,
        "inference_line_source_key_high": None,
        **{key: None for key in INFERENCE_PANEL_FIELD_KEYS},
        "shadow_relaxed_evaluated": False,
        "shadow_relaxed_would_pass": None,
        "shadow_relaxed_reason": None,
        "shadow_relaxed_value": None,
        "shadow_relaxed_threshold": None,
        "shadow_relaxed_comparator": None,
        "shadow_relaxed_secondary_evaluated": False,
        "shadow_relaxed_secondary_would_pass": None,
        "shadow_relaxed_secondary_reason": None,
        "shadow_relaxed_secondary_value": None,
        "shadow_relaxed_secondary_threshold": None,
        "shadow_relaxed_secondary_comparator": None,
        "conditional_relax_gate": None,
        "conditional_relax_mode": None,
        "conditional_relax_arm": None,
        "conditional_relax_would_pass": None,
        "conditional_relax_applied": None,
    }
    weather_getter = getattr(engine, "_weather_fields_for_game", None)
    if callable(weather_getter):
        payload.update(weather_getter(ctx.game.game_pk))
    game_meta_getter = getattr(engine, "_game_meta_fields_for_game", None)
    if callable(game_meta_getter):
        payload.update(game_meta_getter(ctx.game.game_pk))
    return payload


def record_early_skip(
    engine: "SignalEngine",
    ctx: "TickContext",
    reason: str,
    extra_fields: Optional[Dict[str, object]] = None,
) -> None:
    """Convenience writer for Gates 1-5 that fire before any model work."""
    payload = build_base_candidate_payload(engine, ctx)
    payload["decision"] = "skip"
    payload["decision_reason"] = reason
    if extra_fields:
        payload.update(extra_fields)
    engine._record_candidate_decision(payload)
