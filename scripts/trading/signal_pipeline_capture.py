#!/usr/bin/env python3
"""
signal_pipeline_capture.py -- Family A-E feature attachment + skip-row recorder.

Free functions extracted from signal_pipeline.process_tick (Tier 4 refactor,
2026-05-01). Hosts the three closures that were nested inside process_tick
(`_record_skip`, `_track_shadow_result`, `_attach_skip_features`) plus the
post-placement Family A/B/C/D/E + gate-proximity block. Centralizes all
feature-attachment so the skip-with-features and trade-row paths stay in
schema lock-step.

Surfaces:
  - record_skip(engine, ctx, candidate_payload, reason, ...)
        Main skip writer used by every gate phase. Handles shadow-relax
        evaluation, late-stage skip-with-features capture (LATE_STAGE_SKIP_GATES),
        and writes the candidate row.
  - attach_skip_features(engine, ctx, payload, reason)
        Compute Family A-E for a late-stage skip and attach to payload.
        Synthesizes a hypothetical limit price using the same formula
        live_engine uses (bid + spread*spread_factor) so Family B/D features
        are comparable to trade-path captures.
  - attach_trade_features(engine, ctx, *, placed_bet, candidate_trade_payload,
                          fv_phase, runs_needed, lead, trailing_runs)
        Attach Family A-E + gate-proximity to a placed-bet trade row.

Engine attrs read:
  - engine.live_args / engine.trade_args (spread_factor)
  - engine.trade_args (capture_depth, max_base_fv,
                       fv_ask_gap_max, fv_ask_gap_min_inning, s2_suppress_max,
                       s2_suppress_min_inning, runs_needed_max,
                       blowout_lead_min, blowout_adj_lead_min)
  - engine._evaluate_shadow_relaxed(reason=, values=)
  - engine._record_candidate_decision(payload)
  - engine._start_tape_capture / _start_family_b_capture / _start_family_c_capture
Engine state mutated:
  - engine._shadow_relaxed_evaluated, engine._shadow_relaxed_would_pass,
    engine._shadow_relaxed_blocked
  - engine._shadow_relaxed_eval_by_reason, engine._shadow_relaxed_would_pass_by_reason
  - engine._skip_features_seen, engine._skip_features_dedup_suppressed,
    engine._skip_features_captured
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, TYPE_CHECKING

from pricing_features import compute_family_d_features, resolve_spread_factor
from signal_interaction_features import compute_family_e_features

if TYPE_CHECKING:
    from signal_engine import SignalEngine
    from signal_pipeline import FvPhaseResult, TickContext

LOGGER = logging.getLogger("signal_engine")


# Skip reasons that warrant Family A-E feature capture for fill-model training.
# These fire AFTER the FV model is computed, so candidate_payload has fair_value,
# edge, etc. populated. Earlier gates (gate_min_inning, gate_min_entry_ask) are
# environmental/timing skips with no signal content -- excluded to avoid 7,000+
# wasted HTTP calls per session.
#
# Deduplication uses (reason, game_pk, line, inning, inning_state, outs,
# current_total) so we get one feature snapshot per unique game state per gate
# rather than identical snapshots every 2.5s tick.
LATE_STAGE_SKIP_GATES = frozenset({
    "gate_min_edge",
    "gate_fv_saturation",
    "gate_fv_ask_gap",
    "gate_extreme_edge",                  # [TR17] enforced 2026-05-01 (any-inning phantom-run protection)
    "gate_runs_needed_max",
    "gate_runs_pace",
    "gate_close_game_runs_needed",
    "gate_inning5_runs_needed",
    "gate_inning6_runs_needed",
    "gate_blowout",
    "gate_stage2_suppression",
    "gate_sp_era",
})


def _track_shadow_result(
    engine: "SignalEngine",
    reason_key: str,
    result: Dict[str, object],
) -> None:
    """Update shadow-relax counters for a single (reason, result) pair."""
    if not bool(result.get("shadow_relaxed_evaluated")):
        return
    engine._shadow_relaxed_evaluated += 1
    engine._shadow_relaxed_eval_by_reason[reason_key] += 1
    if bool(result.get("shadow_relaxed_would_pass")):
        engine._shadow_relaxed_would_pass += 1
        engine._shadow_relaxed_would_pass_by_reason[reason_key] += 1
    else:
        engine._shadow_relaxed_blocked += 1


def attach_skip_features(
    engine: "SignalEngine",
    ctx: "TickContext",
    payload: Dict[str, object],
    reason: str,
) -> None:
    """Compute Family A-E for a late-stage skip and attach to payload.

    Synthesizes a hypothetical limit price using the same formula
    live_engine uses (bid + spread*spread_factor) so Family B/D features
    are comparable to trade-path captures. Marks the row so downstream
    analysis can distinguish skip-path features from filled-bet features.
    """
    candidate_id = str(payload.get("candidate_id") or "no_id")
    skip_id = f"skip_{candidate_id}"

    # Hypothetical limit price -- same formula as live_engine._compute_limit
    spread_factor = resolve_spread_factor(engine)
    if ctx.best_bid is not None and ctx.ask is not None and ctx.ask > ctx.best_bid:
        hypothetical_limit = round(ctx.best_bid + (ctx.ask - ctx.best_bid) * spread_factor, 4)
    else:
        hypothetical_limit = None
    payload["hypothetical_limit_price"] = hypothetical_limit
    payload["skip_features_capture_id"] = skip_id

    token_id = ctx.market.over_token_id

    try:
        family_a = engine._start_tape_capture(
            bet_id=skip_id,
            token_id=token_id,
            signal_ts=ctx.now,
            current_ask=ctx.ask,
        )
    except Exception as exc:
        LOGGER.warning("skip-features Family A failed for %s: %s", skip_id, exc)
        family_a = None
    payload["family_a_features"] = family_a

    try:
        family_b = engine._start_family_b_capture(
            bet_id=skip_id,
            token_id=token_id,
            limit_price=hypothetical_limit,
            depth=int(getattr(engine.trade_args, "capture_depth", 5) or 5),
        )
    except Exception as exc:
        LOGGER.warning("skip-features Family B failed for %s: %s", skip_id, exc)
        family_b = None
    payload["family_b_features"] = family_b

    try:
        family_c = engine._start_family_c_capture(
            bet_id=skip_id,
            line_state=ctx.state,
            signal_ts=ctx.now,
        )
    except Exception as exc:
        LOGGER.warning("skip-features Family C failed for %s: %s", skip_id, exc)
        family_c = None
    payload["family_c_features"] = family_c

    payload["family_d_features"] = compute_family_d_features(
        limit_price=hypothetical_limit,
        bid=ctx.best_bid,
        ask=ctx.ask,
        ltp=ctx.book.get("ltp"),
    )

    payload["family_e_features"] = compute_family_e_features(
        fv=payload.get("fair_value"),
        ask=ctx.ask,
        ltp=ctx.book.get("ltp"),
        inning=ctx.inning,
        runs_needed=payload.get("runs_needed"),
        runners_on=ctx.runners_on,
    )


def record_skip(
    engine: "SignalEngine",
    ctx: "TickContext",
    candidate_payload: Dict[str, object],
    reason: str,
    shadow_values: Optional[Dict[str, object]] = None,
    secondary_shadow_reason: Optional[str] = None,
    secondary_shadow_values: Optional[Dict[str, object]] = None,
    extra_fields: Optional[Dict[str, object]] = None,
) -> None:
    """Main skip writer for every gate phase.

    Steps:
      1. Clone candidate_payload into a skip-specific row.
      2. Run primary shadow-relax evaluation; merge result fields.
      3. Run secondary shadow-relax evaluation if requested.
      4. If reason is in LATE_STAGE_SKIP_GATES and not yet seen for this
         (reason, game, line, inning, inning_state, outs, current_total),
         capture Family A-E features and flip decision to skip_with_features.
      5. Write candidate row via engine._record_candidate_decision.
    """
    payload = dict(candidate_payload)
    payload["decision"] = "skip"
    payload["decision_reason"] = reason
    if extra_fields:
        payload.update(extra_fields)
    shadow_result = engine._evaluate_shadow_relaxed(
        reason=reason,
        values=shadow_values,
    )
    payload.update(shadow_result)
    _track_shadow_result(engine, reason, shadow_result)

    if secondary_shadow_reason:
        secondary_result = engine._evaluate_shadow_relaxed(
            reason=secondary_shadow_reason,
            values=secondary_shadow_values,
        )
        payload.update(
            {
                "shadow_relaxed_secondary_evaluated": secondary_result.get("shadow_relaxed_evaluated"),
                "shadow_relaxed_secondary_would_pass": secondary_result.get("shadow_relaxed_would_pass"),
                "shadow_relaxed_secondary_reason": secondary_result.get("shadow_relaxed_reason"),
                "shadow_relaxed_secondary_value": secondary_result.get("shadow_relaxed_value"),
                "shadow_relaxed_secondary_threshold": secondary_result.get("shadow_relaxed_threshold"),
                "shadow_relaxed_secondary_comparator": secondary_result.get("shadow_relaxed_comparator"),
            }
        )
        _track_shadow_result(engine, secondary_shadow_reason, secondary_result)

    # ---- Late-stage skip-with-features capture ----
    # On a small set of "looks like a real signal" gate skips, fire the
    # Family A-E captures so the fill-model has training data even when
    # nothing crosses the trade threshold. Dedup at game-state level so
    # idle markets don't trigger 100+ identical snapshots.
    if reason in LATE_STAGE_SKIP_GATES:
        dedup_key = (
            reason,
            ctx.game.game_pk,
            str(ctx.market.line),
            int(ctx.inning),
            str(ctx.inning_state),
            int(ctx.outs),
            int(ctx.current_total),
        )
        if dedup_key in engine._skip_features_seen:
            engine._skip_features_dedup_suppressed += 1
        else:
            engine._skip_features_seen.add(dedup_key)
            engine._skip_features_captured += 1
            attach_skip_features(engine, ctx, payload, reason)
            payload["decision"] = "skip_with_features"

    engine._record_candidate_decision(payload)


def attach_trade_features(
    engine: "SignalEngine",
    ctx: "TickContext",
    *,
    placed_bet: Any,
    candidate_trade_payload: Dict[str, object],
    fv_phase: "FvPhaseResult",
    runs_needed: float,
    lead: int,
    trailing_runs: int,
) -> None:
    """Attach Family A-E features + gate proximity to a placed-bet trade row.

    Called after engine._place_bet returns a non-None bet. Mutates
    candidate_trade_payload in place so the caller can pass it to
    engine._record_candidate_decision.
    """
    ltp_at_signal = ctx.book.get("ltp")
    candidate_trade_payload["decision"] = "trade"
    candidate_trade_payload["decision_reason"] = "placed_bet"
    candidate_trade_payload["bet_id"] = placed_bet.bet_id
    candidate_trade_payload["posted_limit"] = getattr(placed_bet, "limit_price", None)
    candidate_trade_payload["execution_bid"] = getattr(placed_bet, "execution_bid", None)
    candidate_trade_payload["execution_ask"] = getattr(placed_bet, "execution_ask", None)
    _exec_bid = candidate_trade_payload["execution_bid"]
    _exec_ask = candidate_trade_payload["execution_ask"]
    candidate_trade_payload["execution_spread"] = (
        (_exec_ask - _exec_bid) if _exec_ask is not None and _exec_bid is not None else None
    )
    candidate_trade_payload["ltp_at_signal"] = ltp_at_signal
    candidate_trade_payload["ltp_ask_gap"] = (
        round(ctx.ask - ltp_at_signal, 4) if ltp_at_signal is not None else None
    )

    # --- Family A features (tape / recent-flow proxies for adverse-selection)
    # One-shot tape fetch + feature compute; sidecar JSON persisted by the engine.
    # Failure does not block; features dict carries None values + error string.
    family_a = engine._start_tape_capture(
        bet_id=placed_bet.bet_id,
        token_id=ctx.market.over_token_id,
        signal_ts=ctx.now,
        current_ask=ctx.ask,
    )
    candidate_trade_payload["family_a_features"] = family_a

    # --- Family B features (book state at decision: depth / imbalance / queue)
    # One depth-aware /book fetch; sidecar JSON persisted by the engine.
    # Uses placed_bet.limit_price for B5/B6 queue-position proxies.
    family_b = engine._start_family_b_capture(
        bet_id=placed_bet.bet_id,
        token_id=ctx.market.over_token_id,
        limit_price=getattr(placed_bet, "limit_price", None),
        depth=int(getattr(engine.trade_args, "capture_depth", 5) or 5),
    )
    candidate_trade_payload["family_b_features"] = family_b

    # --- Family C features (book velocity / drift from in-memory tick buffer)
    # No HTTP -- reads the per-line tick buffer the monitor populates each tick.
    family_c = engine._start_family_c_capture(
        bet_id=placed_bet.bet_id,
        line_state=ctx.state,
        signal_ts=ctx.now,
    )
    candidate_trade_payload["family_c_features"] = family_c

    # --- Family D features (pricing position) -- pure arithmetic, no IO.
    # Encodes the spread_factor=0.65 heuristic as a learnable feature, plus
    # the limit-vs-bid/ask/ltp lineup for the EV / fill model.
    candidate_trade_payload["family_d_features"] = compute_family_d_features(
        limit_price=getattr(placed_bet, "limit_price", None),
        bid=ctx.best_bid,
        ask=ctx.ask,
        ltp=ltp_at_signal,
    )

    # --- Family E features (signal interaction) -- pure arithmetic, no IO.
    # E2 (fv_minus_ltp) is the edge-vs-adverse-selection separator the
    # design doc highlights; E3 packages inning/runs_needed/runners so the
    # fill model gets the full game-state context as one self-contained dict.
    candidate_trade_payload["family_e_features"] = compute_family_e_features(
        fv=fv_phase.fair_value,
        ask=ctx.ask,
        ltp=ltp_at_signal,
        inning=ctx.inning,
        runs_needed=runs_needed,
        runners_on=ctx.runners_on,
    )

    # --- Gate proximity for placed bets (tightening direction analysis) ---
    # Records how close this placed bet was to being blocked by each gate.
    # Positive = gate did not fire and had this much margin.
    # Near-zero positive = almost blocked; useful for identifying over-tight gates.
    # Join this record to the outcomes JSONL to see if near-blocked bets lost money,
    # which signals that tightening a specific gate threshold is warranted.
    _ta = engine.trade_args
    candidate_trade_payload["gate_proximity"] = {
        # How far the signal was from FV saturation limit (base_fv < max_base_fv)
        "fv_saturation_margin": round(_ta.max_base_fv - fv_phase.base_fair_value, 4),
        # How far edge is from the late-inning gap trigger (lower = closer to block)
        "fv_ask_gap_margin": round(_ta.fv_ask_gap_max - fv_phase.edge, 4)
            if ctx.inning >= _ta.fv_ask_gap_min_inning else None,
        # How far S2 delta is from extreme suppression (negative margin = closer to block)
        "s2_suppress_margin": round(fv_phase.stage2_run_env_delta - _ta.s2_suppress_max, 4)
            if ctx.inning >= _ta.s2_suppress_min_inning else None,
        # How much edge exceeded the minimum required (lower = barely passed gate)
        "min_edge_margin": round(fv_phase.edge - fv_phase.min_edge, 4),
        # How many runs short of the runs-needed max (lower = closer to block)
        "runs_needed_margin": round(_ta.runs_needed_max - runs_needed, 4),
        # How far the lead is below the full blowout threshold (negative = no risk)
        "blowout_lead_margin": (
            _ta.blowout_lead_min - lead if trailing_runs <= 1 and ctx.inning >= 6 else None
        ),
        # How far the lead is below the adjacent blowout threshold (inning >= 7 only)
        "blowout_adj_lead_margin": (
            _ta.blowout_adj_lead_min - lead if trailing_runs <= 1 and ctx.inning >= 7 else None
        ),
    }
