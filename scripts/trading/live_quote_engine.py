"""live_quote_engine.py -- Two-sided quote engine (Phase C C1 + C3 + C4 shadow).

Phase C of the Bidirectional-trading roadmap (2026-05-17). Replaces
the unilateral `_place_bet` model with a two-sided market-maker
quote: post BOTH a bid (buy Over at FV - half_spread) AND an ask
(sell Over / equivalently buy Under at FV + half_spread). If both
fill, capture spread with zero directional exposure.

**Shadow-mode only in this initial shipment.** This module COMPUTES
the would-have-quoted bid + ask + hedge opportunity per tick and
RETURNS them as a decision object. Callers in shadow mode write the
decision to a ledger; NO order is ever placed. The existing OVER
`_place_bet` path is untouched.

Layered safety:
  - This module has NO order-placement code path. It cannot reach the
    CLOB SDK even by mistake.
  - The decision object includes both the quoted prices AND every
    reason a quote would skip (max inventory, under_pair_unavailable,
    FV unavailable, etc.) so the shadow ledger gives the operator a
    full audit of what the engine considered.
  - Inventory tracker is read-only (Phase C C2) and is NOT mutated
    by shadow quotes -- only real placed bets affect it. This makes
    "what shadow quoted vs what really happened" a clean comparison.

Math:
  Half-spread = configurable (default 2 cents)
  Bid  = max(over_best_bid + 0.01,  FV - half_spread - inv_shade)
  Ask  = min(over_best_ask - 0.01,  FV + half_spread - inv_shade)
  Inv shade = sign(net_over) * min(|net_over|/max_inventory, 1.0) * max_shade
              (positive when long Over -> shade both quotes DOWN
               so the bid is less aggressive AND the ask invites
               flattening trades; negative when short)
  Hedge trigger:
    long Over + Under_ask < (1 - Over_FV - hedge_premium)  -> hedge attractive
    short Over + Over_ask < (Over_FV - hedge_premium)      -> hedge attractive

Decisions are emitted at the same trigger points as the existing
OVER candidate evaluator (Phase D will introduce continuous quoting).

External API:
  - QuoteEngineConfig dataclass (params + thresholds)
  - QuoteDecision dataclass (per-tick output)
  - compute_quote_decision(ctx) -> QuoteDecision  (pure function)
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


LOGGER = logging.getLogger("live_quote_engine")


# Schema marker for shadow-ledger rows; bump if the decision dict
# shape changes incompatibly.
QUOTE_DECISION_SCHEMA_VERSION = 1


@dataclass
class QuoteEngineConfig:
    """Tunable parameters for the two-sided quote engine.

    Defaults chosen to be safe shadow defaults:
      - half_spread 2c: typical MM spread on liquid prediction-market
        contracts. Wider than the natural book spread (avg ~3c) to
        ensure we are providing liquidity, not paying for it.
      - max_inventory_per_game 50 shares: ~$35 of nominal exposure at
        the typical 0.70 ask. Below the current per-line cap so we
        cannot end up MORE exposed via MM mode than via taker mode.
      - max_shade 5c: at max inventory, both quotes shift 5c toward
        the inventory-flattening direction.
      - hedge_premium 1c: we will pay 1c over the natural fair value
        to flatten an uncomfortable directional exposure.
    """
    half_spread: float = 0.02
    max_inventory_per_game: float = 50.0
    max_shade: float = 0.05
    hedge_premium: float = 0.01
    # Minimum tick we will quote relative to the book to avoid
    # quoting through the existing market (no immediate self-fill).
    min_book_offset: float = 0.01


@dataclass
class QuoteDecisionContext:
    """Inputs to one quote-decision computation. Constructed by the
    caller from the existing TickContext + a fresh inventory snapshot."""
    game_pk: int
    line: str
    over_best_bid: Optional[float]
    over_best_ask: Optional[float]
    under_best_bid: Optional[float]
    under_best_ask: Optional[float]
    over_fair_value: Optional[float]
    under_fair_value: Optional[float]
    under_pair_available: bool
    net_inventory_over_shares: float
    config: QuoteEngineConfig = field(default_factory=QuoteEngineConfig)


@dataclass
class QuoteDecision:
    """Per-tick two-sided quote decision produced by the engine.

    `would_quote_bid` / `would_quote_ask` are the prices the engine
    WOULD post in non-shadow mode. They are None when the
    corresponding side is skipped (with the reason recorded in
    `bid_skipped_reason` / `ask_skipped_reason`).

    Skip reasons are strings (not enums) so operator-readable in the
    shadow ledger without a lookup. Stable set:
      - "ok": the side was quoted
      - "missing_fair_value": no over_fair_value to anchor off
      - "under_pair_unavailable": no under-side book to derive
        the under-quote complement (ask side is OVER ask =
        sell Over = buy Under at 1-ask; without an UNDER ask
        reference we can't verify our quote isn't through the book)
      - "missing_over_book": over_best_bid/ask is None
      - "max_inventory_long": net Over inventory at or above
        max_inventory_per_game -> bid is blocked (no more long)
      - "max_inventory_short": net Over inventory at or below
        -max_inventory_per_game -> ask is blocked (no more short)
      - "quote_inverted_book": computed quote would cross the
        existing book (priced through), so skipped
    """
    schema_version: int = QUOTE_DECISION_SCHEMA_VERSION
    game_pk: int = 0
    line: str = ""
    # Inputs (echoed back into the row for self-contained audit)
    over_best_bid: Optional[float] = None
    over_best_ask: Optional[float] = None
    under_best_bid: Optional[float] = None
    under_best_ask: Optional[float] = None
    over_fair_value: Optional[float] = None
    under_fair_value: Optional[float] = None
    under_pair_available: bool = False
    net_inventory_over_shares: float = 0.0
    # Computed quote prices
    would_quote_bid: Optional[float] = None
    would_quote_ask: Optional[float] = None
    # Shading metadata
    inventory_shade: float = 0.0   # signed: + when long
    half_spread: float = 0.0
    bid_anchor_price: Optional[float] = None    # FV - half_spread - shade
    ask_anchor_price: Optional[float] = None    # FV + half_spread - shade
    # Skip reasons (str), default "ok" when the side IS quoted
    bid_skipped_reason: str = "ok"
    ask_skipped_reason: str = "ok"
    # Hedge analysis (C4)
    hedge_opportunity: bool = False
    hedge_side: Optional[str] = None        # "buy_under" or "buy_over"
    hedge_target_price: Optional[float] = None
    hedge_max_price: Optional[float] = None
    hedge_reason: str = "none"
    # Config snapshot so the ledger row is self-contained.
    config_snapshot: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _safe_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _inventory_shade(
    net_inventory: float, max_inventory: float, max_shade: float
) -> float:
    """Compute the signed shading offset.

    Returns:
      0.0 when flat or max_inventory <= 0 (disabled)
      Positive when long Over (shifts both quotes DOWN to encourage
        flattening: bid less aggressive, ask invites contra trades)
      Negative when short Over (symmetric)
      Clamped to ±max_shade
    """
    if max_inventory <= 0:
        return 0.0
    ratio = net_inventory / max_inventory
    if ratio > 1.0:
        ratio = 1.0
    elif ratio < -1.0:
        ratio = -1.0
    return ratio * max_shade


def _compute_hedge(
    ctx: QuoteDecisionContext,
) -> Tuple[bool, Optional[str], Optional[float], Optional[float], str]:
    """Decide whether a hedge trade would be attractive right now.

    Returns (opportunity, side, target_price, max_price, reason):
      - opportunity: True if the inventory + opposite-side ask makes
        a flattening trade attractive
      - side: "buy_under" when long Over, "buy_over" when short Over
      - target_price: the at-fair price we'd be willing to pay
        (1 - over_fv for under; over_fv for over)
      - max_price: target + hedge_premium (the most we'd pay)
      - reason: human-readable explanation
    """
    cfg = ctx.config
    inv = ctx.net_inventory_over_shares
    fv = ctx.over_fair_value
    if fv is None:
        return False, None, None, None, "missing_fair_value"
    # Need some inventory to be at risk of needing a hedge.
    if abs(inv) < 1.0:
        return False, None, None, None, "no_inventory_to_hedge"
    if inv > 0:
        # Long Over -> want to buy Under (sell Over directionally)
        under_ask = ctx.under_best_ask
        if not ctx.under_pair_available or under_ask is None:
            return (
                False, "buy_under", None, None,
                "under_book_unavailable",
            )
        # Fair under = 1 - fv. We'd pay up to fair + premium.
        target = 1.0 - fv
        max_price = target + cfg.hedge_premium
        if under_ask <= max_price:
            return (
                True, "buy_under", target, max_price,
                f"long_over_{inv:.1f}_shares_under_ask_{under_ask:.3f}_<=_{max_price:.3f}",
            )
        return (
            False, "buy_under", target, max_price,
            f"under_ask_{under_ask:.3f}_above_max_{max_price:.3f}",
        )
    # Short Over -> want to buy Over (cover the short).
    over_ask = ctx.over_best_ask
    if over_ask is None:
        return False, "buy_over", None, None, "missing_over_book"
    target = fv
    max_price = target + cfg.hedge_premium
    if over_ask <= max_price:
        return (
            True, "buy_over", target, max_price,
            f"short_over_{inv:.1f}_shares_over_ask_{over_ask:.3f}_<=_{max_price:.3f}",
        )
    return (
        False, "buy_over", target, max_price,
        f"over_ask_{over_ask:.3f}_above_max_{max_price:.3f}",
    )


def compute_quote_decision(ctx: QuoteDecisionContext) -> QuoteDecision:
    """Compute a two-sided quote decision (no I/O, pure function).

    All inputs are passed in via `ctx`. Returns a `QuoteDecision`
    that the caller serializes to the shadow ledger. The decision
    NEVER triggers an order placement.
    """
    cfg = ctx.config
    decision = QuoteDecision(
        game_pk=ctx.game_pk,
        line=ctx.line,
        over_best_bid=_safe_float(ctx.over_best_bid),
        over_best_ask=_safe_float(ctx.over_best_ask),
        under_best_bid=_safe_float(ctx.under_best_bid),
        under_best_ask=_safe_float(ctx.under_best_ask),
        over_fair_value=_safe_float(ctx.over_fair_value),
        under_fair_value=_safe_float(ctx.under_fair_value),
        under_pair_available=bool(ctx.under_pair_available),
        net_inventory_over_shares=float(ctx.net_inventory_over_shares),
        half_spread=cfg.half_spread,
        config_snapshot={
            "half_spread": cfg.half_spread,
            "max_inventory_per_game": cfg.max_inventory_per_game,
            "max_shade": cfg.max_shade,
            "hedge_premium": cfg.hedge_premium,
            "min_book_offset": cfg.min_book_offset,
        },
    )

    # ---- Compute shading (C3) ----
    shade = _inventory_shade(
        decision.net_inventory_over_shares,
        cfg.max_inventory_per_game,
        cfg.max_shade,
    )
    decision.inventory_shade = round(shade, 4)

    # ---- C4: hedge opportunity ----
    (
        hedge_op, hedge_side, hedge_target, hedge_max, hedge_reason
    ) = _compute_hedge(ctx)
    decision.hedge_opportunity = hedge_op
    decision.hedge_side = hedge_side
    decision.hedge_target_price = (
        round(hedge_target, 4) if hedge_target is not None else None
    )
    decision.hedge_max_price = (
        round(hedge_max, 4) if hedge_max is not None else None
    )
    decision.hedge_reason = hedge_reason

    # ---- C1: two-sided quote computation ----
    fv = decision.over_fair_value
    if fv is None:
        decision.bid_skipped_reason = "missing_fair_value"
        decision.ask_skipped_reason = "missing_fair_value"
        return decision

    if decision.over_best_bid is None or decision.over_best_ask is None:
        # No book reference -> can't safely quote without risk of
        # quoting through the market.
        decision.bid_skipped_reason = "missing_over_book"
        decision.ask_skipped_reason = "missing_over_book"
        return decision

    if not decision.under_pair_available:
        # Without the under-side book we can't verify symmetry, so
        # skip both sides for shadow visibility. The decision still
        # records the over book + FV so the operator can audit what
        # the engine considered.
        decision.bid_skipped_reason = "under_pair_unavailable"
        decision.ask_skipped_reason = "under_pair_unavailable"
        return decision

    # ---- Bid (buy Over) ----
    bid_anchor = fv - cfg.half_spread - shade
    decision.bid_anchor_price = round(bid_anchor, 4)
    if decision.net_inventory_over_shares >= cfg.max_inventory_per_game:
        decision.bid_skipped_reason = "max_inventory_long"
    else:
        # Don't quote through the market: bid must be at or below the
        # existing best_ask - min_book_offset (otherwise we'd cross).
        bid_price = bid_anchor
        ceiling = decision.over_best_ask - cfg.min_book_offset
        if bid_price > ceiling:
            bid_price = ceiling
        # Round to cent precision (Polymarket tick).
        bid_price = round(bid_price, 2)
        if bid_price <= 0.0 or bid_price >= 1.0:
            decision.bid_skipped_reason = "quote_inverted_book"
        else:
            decision.would_quote_bid = bid_price

    # ---- Ask (sell Over = effectively buy Under) ----
    ask_anchor = fv + cfg.half_spread - shade
    decision.ask_anchor_price = round(ask_anchor, 4)
    if decision.net_inventory_over_shares <= -cfg.max_inventory_per_game:
        decision.ask_skipped_reason = "max_inventory_short"
    else:
        ask_price = ask_anchor
        floor = decision.over_best_bid + cfg.min_book_offset
        if ask_price < floor:
            ask_price = floor
        ask_price = round(ask_price, 2)
        if ask_price <= 0.0 or ask_price >= 1.0:
            decision.ask_skipped_reason = "quote_inverted_book"
        else:
            decision.would_quote_ask = ask_price

    return decision


# ---------------------------------------------------------------------------
# Shadow ledger writer
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def shadow_ledger_path(root: Path, session_date: str) -> Path:
    """Per-date shadow ledger location.

    Lives under `data/{live,paper}_trading/quote_engine_shadow/`
    so it sits alongside the existing per-date trading artifacts
    without polluting the production ledger streams.
    """
    return root / "quote_engine_shadow" / f"{session_date}_quotes.jsonl"


def append_shadow_decision(
    path: Path, decision: QuoteDecision, *, ts: Optional[str] = None,
) -> bool:
    """Append one QuoteDecision row to the shadow ledger.

    Best-effort: write failures log a warning but do not raise --
    a corrupted shadow ledger must NEVER block live trading.
    Returns True on success, False otherwise so tests / callers
    can assert behavior.
    """
    row = decision.to_dict()
    row.setdefault("ts", ts or _now_iso())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return True
    except OSError as exc:
        LOGGER.warning(
            "Shadow ledger append failed at %s: %s (shadow only -- "
            "live trading unaffected)", path, exc,
        )
        return False
