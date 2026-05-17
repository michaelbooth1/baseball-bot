"""Book snapshot rows, horizon features, and simulated fill features."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from scripts.analysis.unified_signal_table.schema import CaptureData
from scripts.analysis.unified_signal_table.utils import _coalesce, _safe_float, _safe_int

def _extract_book_levels(book: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    best_bid = _safe_float(book.get("best_bid"))
    best_ask = _safe_float(book.get("best_ask"))
    spread = _safe_float(book.get("spread"))
    mid = _safe_float(book.get("mid"))
    if spread is None and best_bid is not None and best_ask is not None:
        spread = round(best_ask - best_bid, 4)
    if mid is None and best_bid is not None and best_ask is not None:
        mid = round((best_ask + best_bid) / 2.0, 4)
    return best_bid, best_ask, spread, mid


def _build_snapshot_rows(mode: str, captures: Dict[str, CaptureData]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for bet_id, cap in sorted(captures.items()):
        for snap in cap.snapshots:
            book = snap.get("book", {}) or {}
            best_bid, best_ask, spread, mid = _extract_book_levels(book)
            out.append(
                {
                    "mode": mode,
                    "bet_id": bet_id,
                    "seq": _safe_int(snap.get("seq")),
                    "elapsed_s": _safe_float(snap.get("elapsed_s")),
                    "ts": snap.get("ts"),
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spread": spread,
                    "mid": mid,
                    "ltp": _safe_float(book.get("ltp")),
                    "latency_ms": _safe_float(book.get("latency_ms")),
                    "total_bid_depth": _safe_float(book.get("total_bid_depth")),
                    "total_ask_depth": _safe_float(book.get("total_ask_depth")),
                    "ok": bool(book.get("ok")) if book.get("ok") is not None else None,
                    "error": book.get("error"),
                }
            )
    return out


def _capture_points(cap: Optional[CaptureData]) -> List[Dict[str, Optional[float]]]:
    if not cap:
        return []
    points: List[Dict[str, Optional[float]]] = []
    for snap in cap.snapshots:
        elapsed_s = _safe_float(snap.get("elapsed_s"))
        if elapsed_s is None:
            continue
        book = snap.get("book", {}) or {}
        best_bid, best_ask, spread, _mid = _extract_book_levels(book)
        points.append(
            {
                "elapsed_s": elapsed_s,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": spread,
            }
        )
    points.sort(key=lambda p: float(p["elapsed_s"] or 0.0))
    return points


def _nearest_point_value(
    points: List[Dict[str, Optional[float]]],
    target_elapsed_s: float,
    key: str,
) -> Optional[float]:
    if not points:
        return None
    min_elapsed = _safe_float(points[0].get("elapsed_s"))
    max_elapsed = _safe_float(points[-1].get("elapsed_s"))
    if min_elapsed is None or max_elapsed is None:
        return None
    # Do not extrapolate far beyond recorded capture coverage.
    if target_elapsed_s > max_elapsed + 1.0:
        return None

    best_value: Optional[float] = None
    best_delta: Optional[float] = None
    best_elapsed: Optional[float] = None
    for p in points:
        val = _safe_float(p.get(key))
        elapsed = _safe_float(p.get("elapsed_s"))
        if val is None or elapsed is None:
            continue
        delta = abs(elapsed - target_elapsed_s)
        if best_delta is None:
            best_value = val
            best_delta = delta
            best_elapsed = elapsed
            continue
        if delta < best_delta or (math.isclose(delta, best_delta) and best_elapsed is not None and elapsed < best_elapsed):
            best_value = val
            best_delta = delta
            best_elapsed = elapsed
    return best_value


def _window_extrema(
    points: List[Dict[str, Optional[float]]],
    key: str,
    window_s: float,
) -> Tuple[Optional[float], Optional[float]]:
    vals: List[float] = []
    for p in points:
        elapsed = _safe_float(p.get("elapsed_s"))
        val = _safe_float(p.get(key))
        if elapsed is None or val is None:
            continue
        if elapsed <= window_s + 1e-9:
            vals.append(val)
    if not vals:
        return None, None
    return min(vals), max(vals)


def _simulate_fill(
    points: List[Dict[str, Optional[float]]],
    limit_price: Optional[float],
    fill_window_secs: int,
) -> Tuple[Optional[bool], Optional[float]]:
    if not points or limit_price is None:
        return None, None
    for p in points:
        elapsed = _safe_float(p.get("elapsed_s"))
        ask = _safe_float(p.get("best_ask"))
        if elapsed is None or ask is None:
            continue
        if elapsed > float(fill_window_secs) + 1e-9:
            break
        if ask <= limit_price + 1e-9:
            return True, elapsed
    return False, None


def _compute_phase2_capture_features(
    cap: Optional[CaptureData],
    horizons: List[int],
    fill_window_secs: int,
    t0_best_ask: Optional[float],
    entry_ask: Optional[float],
    limit_price: Optional[float],
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for h in horizons:
        out[f"ask_{h}s"] = None
        out[f"bid_{h}s"] = None
    out.update(
        {
            "ask_move_2s": None,
            "ask_move_5s": None,
            "ask_velocity_5s_cents": None,
            "min_ask_30s": None,
            "max_ask_30s": None,
            "min_spread_30s": None,
            "max_spread_30s": None,
            f"sim_fill_time_{fill_window_secs}s": None,
            f"sim_filled_{fill_window_secs}s": None,
            f"sim_cents_saved_vs_taker_{fill_window_secs}s": None,
            f"sim_fill_time_{fill_window_secs}s_p1c": None,
            f"sim_filled_{fill_window_secs}s_p1c": None,
            f"sim_cents_saved_vs_taker_{fill_window_secs}s_p1c": None,
            f"sim_fill_time_{fill_window_secs}s_p2c": None,
            f"sim_filled_{fill_window_secs}s_p2c": None,
            f"sim_cents_saved_vs_taker_{fill_window_secs}s_p2c": None,
        }
    )

    points = _capture_points(cap)
    if not points:
        return out

    for h in horizons:
        out[f"ask_{h}s"] = _nearest_point_value(points, float(h), "best_ask")
        out[f"bid_{h}s"] = _nearest_point_value(points, float(h), "best_bid")

    base_ask = _coalesce([entry_ask, t0_best_ask])
    ask_2 = _safe_float(out.get("ask_2s"))
    ask_5 = _safe_float(out.get("ask_5s"))
    if base_ask is not None and ask_2 is not None:
        out["ask_move_2s"] = round(ask_2 - base_ask, 6)
    if base_ask is not None and ask_5 is not None:
        out["ask_move_5s"] = round(ask_5 - base_ask, 6)
        # Positive means ask dropped toward our passive buy (favorable for fill).
        out["ask_velocity_5s_cents"] = round(((base_ask - ask_5) / 5.0) * 100.0, 6)

    min_ask_30s, max_ask_30s = _window_extrema(points, "best_ask", 30.0)
    min_spread_30s, max_spread_30s = _window_extrema(points, "spread", 30.0)
    out["min_ask_30s"] = min_ask_30s
    out["max_ask_30s"] = max_ask_30s
    out["min_spread_30s"] = min_spread_30s
    out["max_spread_30s"] = max_spread_30s

    sim_filled, sim_fill_time = _simulate_fill(points, limit_price=limit_price, fill_window_secs=fill_window_secs)
    out[f"sim_fill_time_{fill_window_secs}s"] = sim_fill_time
    out[f"sim_filled_{fill_window_secs}s"] = sim_filled
    if sim_filled:
        taker_price = _coalesce([entry_ask, t0_best_ask])
        if taker_price is not None and limit_price is not None:
            out[f"sim_cents_saved_vs_taker_{fill_window_secs}s"] = round((taker_price - limit_price) * 100.0, 6)

    # Shadow adaptive repricer metrics (observational only).
    # These simulate whether +1c / +2c more aggressive limits would have filled
    # inside the same capture window, without changing live execution behavior.
    for cents, suffix in ((1, "p1c"), (2, "p2c")):
        if limit_price is None:
            continue
        shadow_limit = round(limit_price + (cents / 100.0), 6)
        shadow_filled, shadow_fill_time = _simulate_fill(
            points,
            limit_price=shadow_limit,
            fill_window_secs=fill_window_secs,
        )
        out[f"sim_fill_time_{fill_window_secs}s_{suffix}"] = shadow_fill_time
        out[f"sim_filled_{fill_window_secs}s_{suffix}"] = shadow_filled
        if shadow_filled:
            taker_price = _coalesce([entry_ask, t0_best_ask])
            if taker_price is not None:
                out[f"sim_cents_saved_vs_taker_{fill_window_secs}s_{suffix}"] = round(
                    (taker_price - shadow_limit) * 100.0,
                    6,
                )
    return out

