"""
pricing_features.py -- Family D (pricing position) feature computation.

Pure arithmetic over decision-time price state. Per the design in
model_improvements/fill_model_feature.txt:

    D1. limit_minus_bid          = L - bid
    D2. limit_minus_ask          = L - ask
                                   (< 0: passive  / 0: at touch / > 0: crossing)
    D3. spread_normalized_limit  = (L - bid) / spread
                                   (the hard-coded spread_factor 0.65 made
                                    explicit and learnable per regime)
    D4. ltp_minus_limit          = LTP - L

No HTTP, no buffer, no engine state — all inputs are scalars already in
scope at signal time. Feature module exists so backtest and EV-policy
training reuse the exact runtime definitions.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def compute_family_d_features(
    limit_price: Optional[float],
    bid: Optional[float],
    ask: Optional[float],
    ltp: Optional[float],
) -> Dict[str, Any]:
    """Compute Family D features from decision-time pricing scalars.

    Returns dict with the four Family D fields plus a 'limit_price_used'
    diagnostic. Per-feature None when a required input is missing or
    when div-by-zero would occur (D3 with zero spread).
    """
    L = _safe_float(limit_price)
    b = _safe_float(bid)
    a = _safe_float(ask)
    p = _safe_float(ltp)

    out: Dict[str, Any] = {
        "limit_minus_bid": None,
        "limit_minus_ask": None,
        "spread_normalized_limit": None,
        "ltp_minus_limit": None,
        "limit_price_used": (round(L, 4) if L is not None else None),
    }

    if L is None:
        return out

    if b is not None:
        out["limit_minus_bid"] = round(L - b, 6)

    if a is not None:
        out["limit_minus_ask"] = round(L - a, 6)

    if b is not None and a is not None:
        spread = a - b
        if spread > 1e-9:
            out["spread_normalized_limit"] = round((L - b) / spread, 6)

    if p is not None:
        out["ltp_minus_limit"] = round(p - L, 6)

    return out


def resolve_spread_factor(engine: Any, default: float = 0.65) -> float:
    """Return the spread factor used by live execution for this engine.

    Live trading owns `--spread-factor` on `live_args`; paper/test stubs may
    still carry it on `trade_args`. Feature logging should mirror the live
    execution path whenever `live_args` is present.
    """
    live_args = getattr(engine, "live_args", None)
    value = _safe_float(getattr(live_args, "spread_factor", None))
    if value is not None:
        return value
    trade_args = getattr(engine, "trade_args", None)
    value = _safe_float(getattr(trade_args, "spread_factor", None))
    if value is not None:
        return value
    return float(default)
