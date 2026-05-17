"""
book_velocity.py -- Family C (book velocity / drift) feature computation.

Computes velocity, drift, and volatility features from a per-line tick
buffer maintained by LineState. Per the design in
model_improvements/fill_model_feature.txt:

    C1. ask_velocity_5s              = (ask_now - ask_5s_ago) / 5
    C2. ask_velocity_30s             = (ask_now - ask_30s_ago) / 30
    C3. mid_drift_30s                = mid_now - mid_30s_ago
    C4. spread_now_vs_spread_30s_avg = spread_now / mean(spreads_30s)
    C5. ask_volatility_30s           = stdev(asks in last 30s)

Pure module: takes a list of tick dicts and a signal timestamp, returns
a features dict. No IO, no engine state, no HTTP calls.

Tick buffer schema (built by LineState.push_tick):
    {"ts": float, "bid": float, "ask": float, "mid": float, "spread": float}
Ticks are expected oldest-first. Buffer cadence is set by the monitor
poll interval (~2.5s), so a 30s window holds ~12 entries.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("book_velocity")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pick_anchor_tick(
    ticks: List[Dict[str, Any]],
    target_ts: float,
    tolerance: float,
) -> Optional[Dict[str, Any]]:
    """Return the tick whose ts is closest to target_ts within tolerance.

    Used to anchor the "N seconds ago" lookback for velocity calculations.
    Cadence jitter (~2.5s nominal but variable) means we won't have an
    exact tick at signal_ts - N; pick the closest one within `tolerance`.

    Returns None if the closest candidate is outside tolerance, indicating
    the buffer doesn't span the requested window.
    """
    if not ticks:
        return None
    best = min(ticks, key=lambda t: abs(t["ts"] - target_ts))
    if abs(best["ts"] - target_ts) > tolerance:
        return None
    return best


def _stdev(values: List[float]) -> Optional[float]:
    """Sample standard deviation; None if fewer than 2 samples."""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var)


# ---------------------------------------------------------------------------
# Feature computation
# ---------------------------------------------------------------------------

def compute_family_c_features(
    tick_buffer: List[Dict[str, Any]],
    signal_ts: float,
    *,
    velocity_short_s: float = 5.0,
    velocity_long_s: float = 30.0,
    velocity_anchor_tolerance: float = 4.0,
) -> Dict[str, Any]:
    """Compute Family C features over the tick buffer at signal_ts.

    Args:
        tick_buffer:                List of tick dicts (oldest-first), each
                                    with keys: ts, bid, ask, mid, spread.
        signal_ts:                  Unix epoch seconds of the signal.
        velocity_short_s:           Window for C1 (default 5s).
        velocity_long_s:            Window for C2/C3/C4/C5 (default 30s).
        velocity_anchor_tolerance:  Max |delta| from target window edge
                                    when picking the anchor tick (handles
                                    monitor cadence jitter).

    Returns dict with C1-C5 plus diagnostics (buffer_n, window_n, etc.).
    Per-feature None when underlying data is insufficient.
    """
    out: Dict[str, Any] = {
        "ask_velocity_5s": None,
        "ask_velocity_30s": None,
        "mid_drift_30s": None,
        "spread_now_vs_spread_30s_avg": None,
        "ask_volatility_30s": None,
        "buffer_n": len(tick_buffer),
        "window_30s_n": 0,
        "buffer_oldest_age_s": None,
    }
    if not tick_buffer:
        return out

    # Pre-signal window only — defensive against ticks ahead of signal_ts.
    pre = [t for t in tick_buffer if t.get("ts") is not None and t["ts"] <= signal_ts]
    if not pre:
        return out

    latest = pre[-1]
    out["buffer_oldest_age_s"] = round(signal_ts - pre[0]["ts"], 3)

    # 30-second slice for C2/C3/C4/C5.
    cutoff_30 = signal_ts - velocity_long_s
    last_30 = [t for t in pre if t["ts"] >= cutoff_30]
    out["window_30s_n"] = len(last_30)

    # --- C1: ask velocity over short window ---
    anchor_short = _pick_anchor_tick(
        pre, signal_ts - velocity_short_s, velocity_anchor_tolerance
    )
    latest_ask = latest.get("ask")
    if (
        anchor_short is not None
        and anchor_short.get("ask") is not None
        and latest_ask is not None
    ):
        delta = latest_ask - anchor_short["ask"]
        # Use actual elapsed time, not nominal window, for true velocity.
        elapsed = max(0.001, latest["ts"] - anchor_short["ts"])
        out["ask_velocity_5s"] = round(delta / elapsed, 6)

    # --- C2: ask velocity over long window ---
    anchor_long = _pick_anchor_tick(
        pre, signal_ts - velocity_long_s, velocity_anchor_tolerance
    )
    if (
        anchor_long is not None
        and anchor_long.get("ask") is not None
        and latest_ask is not None
    ):
        delta = latest_ask - anchor_long["ask"]
        elapsed = max(0.001, latest["ts"] - anchor_long["ts"])
        out["ask_velocity_30s"] = round(delta / elapsed, 6)

    # --- C3: mid drift over long window ---
    if (
        anchor_long is not None
        and anchor_long.get("mid") is not None
        and latest.get("mid") is not None
    ):
        out["mid_drift_30s"] = round(latest["mid"] - anchor_long["mid"], 6)

    # --- C4: spread now vs mean spread over last 30s ---
    spreads_30 = [
        t["spread"] for t in last_30
        if t.get("spread") is not None and math.isfinite(t["spread"])
    ]
    spread_now = latest.get("spread")
    if (
        spread_now is not None
        and math.isfinite(spread_now)
        and len(spreads_30) >= 2
    ):
        mean_spread = sum(spreads_30) / len(spreads_30)
        if mean_spread > 1e-9:
            out["spread_now_vs_spread_30s_avg"] = round(spread_now / mean_spread, 4)

    # --- C5: ask volatility over last 30s ---
    asks_30 = [
        t["ask"] for t in last_30
        if t.get("ask") is not None and math.isfinite(t["ask"])
    ]
    sd = _stdev(asks_30)
    if sd is not None:
        out["ask_volatility_30s"] = round(sd, 6)

    return out
