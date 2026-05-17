"""
book_features.py -- Family B (book state at decision) feature computation.

Computes resting-depth and imbalance features over a single book snapshot
captured at signal time. Per the design in
model_improvements/fill_model_feature.txt:

    B1. ask_depth_at_touch
    B2. bid_depth_at_touch
    B3. book_imbalance_top1   = bid_at_touch / (bid_at_touch + ask_at_touch)
    B4. book_imbalance_top5   = sum_bid_top5 / (sum_bid_top5 + sum_ask_top5)
    B5. depth_below_our_limit = total ask-side size at prices <= L
    B6. depth_at_limit_L      = ask-side size resting exactly at L

This module is pure: takes a book dict (matching the schema produced by
SignalEngine._fetch_depth_snapshot) and an optional intended limit price
L, returns a features dict. No IO, no engine state.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("book_features")

# Polymarket prices are quoted in 2-decimal increments. Use a half-tick
# tolerance for "is this level at L" equality checks to absorb float dust.
_PRICE_TOL = 0.005


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _norm_levels(levels: Any) -> List[Dict[str, float]]:
    """Normalize a price-ladder list into [{price, size}] form, dropping bad rows."""
    out: List[Dict[str, float]] = []
    if not isinstance(levels, list):
        return out
    for lvl in levels:
        if not isinstance(lvl, dict):
            continue
        p = _safe_float(lvl.get("price"))
        s = _safe_float(lvl.get("size"))
        if p is None or s is None or s <= 0:
            continue
        out.append({"price": p, "size": s})
    return out


def compute_family_b_features(
    book: Dict[str, Any],
    limit_price: Optional[float],
    top_n: int = 5,
) -> Dict[str, Any]:
    """Compute Family B features from a depth-aware book snapshot.

    Args:
        book:         A snapshot in the schema produced by
                      SignalEngine._fetch_depth_snapshot. Required keys:
                      best_bid_size, best_ask_size, top_bids, top_asks.
        limit_price:  Our intended limit price L. If None, B5 and B6 are
                      returned as None (they require L to be defined).
        top_n:        Imbalance window for B4 (default 5).

    Returns dict with the six Family B fields plus a 'book_ok' diagnostic.
    Fields are None when the underlying input is missing.
    """
    out: Dict[str, Any] = {
        "ask_depth_at_touch": None,
        "bid_depth_at_touch": None,
        "book_imbalance_top1": None,
        "book_imbalance_top5": None,
        "depth_below_our_limit": None,
        "depth_at_limit_L": None,
        "limit_price_used": (
            round(float(limit_price), 4) if limit_price is not None else None
        ),
        "top_n": int(top_n),
        "book_ok": bool(book.get("ok")) if isinstance(book, dict) else False,
    }
    if not isinstance(book, dict):
        return out

    bid_at_touch = _safe_float(book.get("best_bid_size"))
    ask_at_touch = _safe_float(book.get("best_ask_size"))
    out["bid_depth_at_touch"] = bid_at_touch
    out["ask_depth_at_touch"] = ask_at_touch

    # B3: top-of-book imbalance
    if (
        bid_at_touch is not None
        and ask_at_touch is not None
        and (bid_at_touch + ask_at_touch) > 0
    ):
        out["book_imbalance_top1"] = round(
            bid_at_touch / (bid_at_touch + ask_at_touch), 4
        )

    top_bids = _norm_levels(book.get("top_bids"))
    top_asks = _norm_levels(book.get("top_asks"))

    # B4: top-N imbalance — defensive against missing depth ladders.
    if top_bids and top_asks:
        sum_bid = sum(b["size"] for b in top_bids[:top_n])
        sum_ask = sum(a["size"] for a in top_asks[:top_n])
        if (sum_bid + sum_ask) > 0:
            out["book_imbalance_top5"] = round(
                sum_bid / (sum_bid + sum_ask), 4
            )

    # B5, B6: queue-position proxies. Need our limit and at least one
    # ask level to be meaningful.
    if limit_price is not None and top_asks:
        L = float(limit_price)
        # B5 = total ask-side size at price <= L (within half-tick tolerance).
        depth_below = sum(
            a["size"] for a in top_asks if a["price"] <= L + _PRICE_TOL
        )
        out["depth_below_our_limit"] = round(depth_below, 4)

        # B6 = size resting exactly at L (within half-tick tolerance).
        depth_at = sum(
            a["size"] for a in top_asks if abs(a["price"] - L) <= _PRICE_TOL
        )
        out["depth_at_limit_L"] = round(depth_at, 4)

    return out
