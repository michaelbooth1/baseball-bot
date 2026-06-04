#!/usr/bin/env python3
"""
live_pricing.py -- Limit-price computation, Kelly sizing, and fill-cost helpers.

Free functions extracted from live_engine.LiveTradingEngine (Tier 3 refactor,
2026-05-01). Pure pricing math; no IO. Engine retains thin method wrappers
(`_compute_limit_price`, `_compute_stake`, `_kelly_components`,
`_filled_notional`) so test stubs and call-sites in `_place_bet` /
`session_serialization` continue to work unchanged.

Surfaces:
  - compute_limit_price(engine, ask, bid, fair_value, line_val) -> Optional[float]
  - filled_notional(engine, bet) -> float
  - kelly_components(engine, fair_value, limit_price) -> (raw_f, eff_f, eff_edge)
  - compute_stake(engine, fair_value, limit_price) -> float

Engine attrs read:
  - trade_args (edge_threshold, edge_threshold_high_line, high_line_cutoff, stake)
  - live_args  (spread_factor, kelly_max_edge, stake_mode, daily_budget,
                kelly_fraction, min_order_size, kelly_max_bet_fraction,
                kelly_floor_to_min)
"""

from __future__ import annotations

from typing import Optional, Tuple, TYPE_CHECKING

from signal_config import DEFAULT_COOLDOWN_TICKS  # noqa: F401  (kept for parity; not used here)

if TYPE_CHECKING:
    from live_engine import LiveTradingEngine
    from models import LiveBetRecord


# Defaults imported lazily to keep import surface minimal; see live_engine.py
# for the canonical constants and CLI bindings.
def _kelly_defaults():
    from live_engine import (
        DEFAULT_KELLY_MAX_EDGE,
        DEFAULT_KELLY_FLOOR_TO_MIN,
        DEFAULT_LIVE_STAKE,
        DEFAULT_STAKE_MODE,
    )
    return (
        DEFAULT_KELLY_MAX_EDGE,
        DEFAULT_KELLY_FLOOR_TO_MIN,
        DEFAULT_LIVE_STAKE,
        DEFAULT_STAKE_MODE,
    )


# ---------------------------------------------------------------------------
# Limit-price calculation
# ---------------------------------------------------------------------------

def compute_limit_price(
    engine: "LiveTradingEngine",
    ask: float,
    bid: float,
    fair_value: float,
    line_val: float,
) -> Optional[float]:
    """Compute limit BUY price from current book state and model FV.

    Formula: bid + spread * spread_factor
    Caps and floors to preserve model edge and stay inside the spread.
    Returns None if no valid limit price exists.
    """
    if bid >= ask:
        return None

    spread = ask - bid
    spread_factor = engine.live_args.spread_factor

    # Select the applicable min_edge for this line
    if line_val >= engine.trade_args.high_line_cutoff:
        min_edge = engine.trade_args.edge_threshold_high_line
    else:
        min_edge = engine.trade_args.edge_threshold

    limit_raw = bid + spread * spread_factor
    edge_cap = fair_value - min_edge   # don't pay more than FV - min_edge
    limit = min(limit_raw, edge_cap)   # cap: preserve edge
    limit = min(limit, ask - 0.01)     # cap: must stay inside spread
    # 2026-06-03 fill-optimization fix: floor the limit at
    # `ask - max_limit_gap_below_ask`. The pre-fix bid+spread*factor
    # logic let the limit fall 6-10c below ask in wide-spread regimes,
    # which historically filled only ~50% of the time. Floor pulls
    # the limit back up so it sits in the high-fill (~80-94%) zone.
    # Audit data (34d live, 28 cancels): 27 of 28 cancels would have
    # WON had they filled at ask; the gap was the only blocker.
    # If max_limit_gap_below_ask is None or <= 0, no floor is applied
    # (back-compat for callers built before this fix).
    max_gap = getattr(
        engine.live_args, "max_limit_gap_below_ask", None,
    )
    if max_gap is not None and max_gap > 0:
        limit = max(limit, ask - float(max_gap))
    limit = max(limit, bid + 0.01)     # floor: must improve on bid
    limit = round(limit, 2)

    # Final invariant: check we still have edge at this limit
    if limit >= ask:
        return None
    if (fair_value - limit) < min_edge:
        return None

    return limit


# ---------------------------------------------------------------------------
# Fill-cost accounting
# ---------------------------------------------------------------------------

def filled_notional(engine: "LiveTradingEngine", bet: "LiveBetRecord") -> float:
    """Return realized USDC cost for filled orders (supports partial fills)."""
    if bet.order_status == "filled":
        fill_cost = getattr(bet, "fill_cost", None)
        if fill_cost is not None:
            return float(fill_cost)
        fill_size = getattr(bet, "fill_size", None)
        fill_price = getattr(bet, "actual_fill_price", None) or getattr(bet, "fill_price", None)
        if fill_size is not None and fill_price is not None:
            return float(fill_size) * float(fill_price)
    return float(bet.stake)


# ---------------------------------------------------------------------------
# Kelly sizing
# ---------------------------------------------------------------------------

def kelly_components(
    engine: "LiveTradingEngine",
    fair_value: float,
    limit_price: float,
) -> Tuple[float, float, float]:
    """Return (raw_f_star, effective_f_star, effective_edge_at_limit).

    Applies kelly_max_edge cap to reduce stake in extreme high-edge states.
    """
    denom = 1.0 - limit_price
    if denom <= 0:
        return 0.0, 0.0, 0.0
    raw_edge = fair_value - limit_price
    raw_f_star = raw_edge / denom

    DEFAULT_KELLY_MAX_EDGE, _, _, _ = _kelly_defaults()
    max_edge = max(0.0, getattr(engine.live_args, "kelly_max_edge", DEFAULT_KELLY_MAX_EDGE))
    effective_edge = min(raw_edge, max_edge) if max_edge > 0 else raw_edge
    effective_f_star = effective_edge / denom
    return raw_f_star, effective_f_star, effective_edge


def compute_stake(
    engine: "LiveTradingEngine",
    fair_value: float,
    limit_price: float,
) -> float:
    """Compute bet stake using the configured stake_mode.

    flat (default): fixed --stake dollars per bet.
    kelly: quarter-Kelly on daily_budget, sized by signal edge with cap.
    """
    DEFAULT_KELLY_MAX_EDGE, DEFAULT_KELLY_FLOOR_TO_MIN, DEFAULT_LIVE_STAKE, DEFAULT_STAKE_MODE = _kelly_defaults()
    mode = getattr(engine.live_args, "stake_mode", DEFAULT_STAKE_MODE)
    if mode == "kelly":
        _, kelly_full, _ = kelly_components(engine, fair_value, limit_price)
        stake_raw = kelly_full * engine.live_args.daily_budget * engine.live_args.kelly_fraction
        min_s = engine.live_args.min_order_size
        max_s = engine.live_args.daily_budget * engine.live_args.kelly_max_bet_fraction
        stake = min(max_s, max(0.0, stake_raw))
        if bool(getattr(engine.live_args, "kelly_floor_to_min", DEFAULT_KELLY_FLOOR_TO_MIN)):
            stake = max(min_s, stake)
        return round(stake, 2)
    else:
        # --stake is a trade_arg, not a live_arg. Read from trade_args first;
        # fall back to DEFAULT_LIVE_STAKE only if truly absent.
        return float(getattr(engine.trade_args, "stake",
                     getattr(engine.live_args, "stake", DEFAULT_LIVE_STAKE)))


# ---------------------------------------------------------------------------
# Calibrated-edge stake scaling (Active #6 part 2, shipped 2026-05-12).
# ---------------------------------------------------------------------------


def calibrated_stake_multiplier(
    *,
    calibrated_edge: float,
    min_multiplier: float,
    max_multiplier: float,
    ramp_top_edge: float,
) -> float:
    """Linear-ramp multiplier in [min, max] from calibrated edge.

    Anchors:
      edge <= 0         -> min_multiplier
      edge == ramp_top  -> max_multiplier
      edge >= ramp_top  -> max_multiplier (clamped)

    Between 0 and ramp_top the multiplier is linear:
      mult = min + (max - min) * (edge / ramp_top)

    Behavior is deliberately conservative: at zero calibrated edge we cut
    stake in half, only growing past 1.0 when the calibrated FV is
    materially above the ask. Calibration consistently shrinks raw FV
    (Platt found raw was 27% overconfident on average), so calibrated
    edge < raw edge -- the multiplier penalizes overconfident raw signals.
    """
    if ramp_top_edge <= 0:
        return float(min_multiplier)
    if calibrated_edge <= 0:
        return float(min_multiplier)
    if calibrated_edge >= ramp_top_edge:
        return float(max_multiplier)
    span = float(max_multiplier) - float(min_multiplier)
    return float(min_multiplier) + span * (float(calibrated_edge) / float(ramp_top_edge))


def resolve_calibrated_edge(
    engine: "LiveTradingEngine",
    *,
    raw_or_final_fv: float,
    decision_ask: float,
    model_family: str,
) -> Optional[float]:
    """Compute (calibrated_fv - decision_ask), or None if no calibrator.

    ``raw_or_final_fv`` is whatever the engine's signal pipeline returned --
    raw FV when prob_calibration_mode is off/shadow, calibrated FV when
    enforce. We work backward: in enforce mode the value is already
    calibrated; otherwise we route it through the calibrator. This avoids
    double-calibration without plumbing the raw FV through every call site.
    """
    calibrator = getattr(engine, "_prob_calibrator", None)
    if calibrator is None:
        return None
    mode = getattr(engine, "_prob_calibration_mode", "off")
    if mode == "enforce":
        calibrated_fv = float(raw_or_final_fv)
    else:
        try:
            calibrated_fv = float(calibrator.calibrate(
                float(raw_or_final_fv), model_family=model_family
            ))
        except Exception:
            return None
    return calibrated_fv - float(decision_ask)
