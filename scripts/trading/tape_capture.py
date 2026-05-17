"""
tape_capture.py -- Trade-tape fetcher and Family A feature computation.

Polymarket exposes a public trades endpoint at data-api.polymarket.com.
At signal time we fetch the most recent trades for the OVER token, then
compute Family A features (recent flow / tape velocity) per the design
in model_improvements/fill_model_feature.txt.

This module is pure: no engine state, no order placement, no IO besides
the single HTTP fetch. Imported by signal_pipeline.py.

Family A features (from fill_model_feature.txt):
    A1. trades_last_5s_count
    A2. trades_last_30s_count
    A3. signed_volume_last_30s
    A4. ltp_minus_ask_last_3_trades
    A5. seconds_since_last_trade
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from typing import Any, Dict, List, Optional

import requests

LOGGER = logging.getLogger("tape_capture")

DATA_API_BASE = "https://data-api.polymarket.com"
DEFAULT_TRADE_LIMIT = 100        # ample for 60s windows on liquid markets
DEFAULT_FETCH_TIMEOUT = 2.0      # seconds; do not block bet placement


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_unix_ts(raw: Any) -> Optional[float]:
    """Convert a Polymarket trade timestamp to a unix epoch float.

    Polymarket trade records may carry timestamps as:
      - int seconds (10 digits)
      - int milliseconds (13 digits)
      - ISO 8601 string
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        if v > 1e12:    # milliseconds
            v /= 1000.0
        return v
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            v = float(s)
            if v > 1e12:
                v /= 1000.0
            return v
        try:
            iso = s.rstrip("Z").replace("+00:00", "")
            return _dt.datetime.fromisoformat(iso).replace(
                tzinfo=_dt.timezone.utc
            ).timestamp()
        except ValueError:
            return None
    return None


def _normalize_side(raw: Any) -> Optional[str]:
    """Normalize the trade-side label to 'BUY' or 'SELL' (taker side)."""
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if s in ("BUY", "B", "BID", "TAKER_BUY"):
        return "BUY"
    if s in ("SELL", "S", "ASK", "TAKER_SELL"):
        return "SELL"
    return None


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

def fetch_recent_trades(
    token_id: str,
    limit: int = DEFAULT_TRADE_LIMIT,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Fetch the most recent trades for one Polymarket token.

    Returns a dict:
        {
          "ok": bool,
          "error": Optional[str],
          "latency_ms": float,
          "trades": List[Dict] -- normalized records sorted oldest-first,
        }

    Each normalized trade has: ts (unix sec), price (float), size (float),
    side ('BUY'|'SELL'|None), raw (the original record for forensics).
    """
    out: Dict[str, Any] = {
        "ok": False,
        "error": None,
        "latency_ms": 0.0,
        "trades": [],
    }
    sess = session or requests
    t0 = time.time()
    try:
        resp = sess.get(
            f"{DATA_API_BASE}/trades",
            params={"market": token_id, "limit": int(limit)},
            timeout=timeout,
        )
        out["latency_ms"] = round((time.time() - t0) * 1000.0, 2)
        if resp.status_code != 200:
            out["error"] = f"http_{resp.status_code}"
            return out
        payload = resp.json()
    except Exception as exc:
        out["latency_ms"] = round((time.time() - t0) * 1000.0, 2)
        out["error"] = str(exc)
        return out

    raw_list: List[Any]
    if isinstance(payload, list):
        raw_list = payload
    elif isinstance(payload, dict):
        raw_list = (
            payload.get("data")
            or payload.get("trades")
            or payload.get("results")
            or []
        )
    else:
        raw_list = []

    normalized: List[Dict[str, Any]] = []
    for rec in raw_list:
        if not isinstance(rec, dict):
            continue
        ts = _to_unix_ts(
            rec.get("timestamp")
            or rec.get("ts")
            or rec.get("created_at")
            or rec.get("time")
            or rec.get("matchTime")
        )
        if ts is None:
            continue
        price = _safe_float(rec.get("price"))
        size = _safe_float(rec.get("size") or rec.get("amount"))
        if price is None or size is None or size <= 0:
            continue
        side = _normalize_side(rec.get("side") or rec.get("takerSide"))
        normalized.append({
            "ts": ts,
            "price": price,
            "size": size,
            "side": side,
        })

    normalized.sort(key=lambda r: r["ts"])
    out["trades"] = normalized
    out["ok"] = True
    return out


# ---------------------------------------------------------------------------
# Family A feature computation
# ---------------------------------------------------------------------------

def compute_family_a_features(
    trades: List[Dict[str, Any]],
    signal_ts: float,
    current_ask: Optional[float],
) -> Dict[str, Any]:
    """Compute Family A features over the trade tape ending at signal_ts.

    Args:
        trades:      Normalized trades (output of fetch_recent_trades).
        signal_ts:   Unix epoch seconds of the signal (decision moment).
        current_ask: Best ask at signal time, used for A4.

    Returns a dict with the five Family A features. Values are None when
    the underlying data is insufficient (e.g. no trades in the window).
    Always includes a 'window_trade_count' diagnostic and the count of
    raw trades considered.
    """
    out: Dict[str, Any] = {
        "trades_last_5s_count": None,
        "trades_last_30s_count": None,
        "signed_volume_last_30s": None,
        "ltp_minus_ask_last_3_trades": None,
        "seconds_since_last_trade": None,
        "raw_trade_count": len(trades),
    }
    if not trades:
        return out

    # Pre-signal window only: trades must be at or before signal_ts.
    pre_signal = [t for t in trades if t["ts"] <= signal_ts]
    if not pre_signal:
        return out

    # A1, A2: simple counts in time windows.
    cutoff_5 = signal_ts - 5.0
    cutoff_30 = signal_ts - 30.0
    last_5 = [t for t in pre_signal if t["ts"] >= cutoff_5]
    last_30 = [t for t in pre_signal if t["ts"] >= cutoff_30]
    out["trades_last_5s_count"] = len(last_5)
    out["trades_last_30s_count"] = len(last_30)

    # A3: signed volume in last 30s. BUY = +size, SELL = -size, unknown = 0.
    signed = 0.0
    have_any_signed = False
    for t in last_30:
        side = t.get("side")
        if side == "BUY":
            signed += t["size"]
            have_any_signed = True
        elif side == "SELL":
            signed -= t["size"]
            have_any_signed = True
    out["signed_volume_last_30s"] = round(signed, 4) if have_any_signed else None

    # A4: avg(price - current_ask) over the last 3 trades. Negative means
    # recent prints were below ask -> aggressors crossing the spread on the
    # sell side -> informed-flow indicator.
    if current_ask is not None and pre_signal:
        last_n = pre_signal[-3:]
        if last_n:
            gap = sum(t["price"] - current_ask for t in last_n) / len(last_n)
            out["ltp_minus_ask_last_3_trades"] = round(gap, 4)

    # A5: staleness — seconds between most recent pre-signal trade and signal.
    most_recent_ts = pre_signal[-1]["ts"]
    out["seconds_since_last_trade"] = round(max(0.0, signal_ts - most_recent_ts), 3)

    return out


# ---------------------------------------------------------------------------
# Combined convenience entry point
# ---------------------------------------------------------------------------

def capture_tape_and_features(
    token_id: str,
    signal_ts: float,
    current_ask: Optional[float],
    limit: int = DEFAULT_TRADE_LIMIT,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """One-shot helper: fetch tape + compute features. Used by signal_pipeline.

    Returns a single dict with both the raw fetch outcome and the computed
    features; the caller can persist the whole thing to a sidecar file.
    """
    fetch = fetch_recent_trades(
        token_id=token_id,
        limit=limit,
        timeout=timeout,
        session=session,
    )
    features = compute_family_a_features(
        trades=fetch.get("trades", []),
        signal_ts=signal_ts,
        current_ask=current_ask,
    )
    features["fetch_ok"] = fetch.get("ok", False)
    features["fetch_error"] = fetch.get("error")
    features["fetch_latency_ms"] = fetch.get("latency_ms", 0.0)
    return {
        "token_id": token_id,
        "signal_ts": signal_ts,
        "current_ask": current_ask,
        "trades": fetch.get("trades", []),
        "features": features,
    }
