#!/usr/bin/env python3
"""
capture_helpers.py -- Sidecar capture functions for placed bets and skip-with-features.

Free functions extracted from signal_engine.py (Tier 1 refactor, 2026-05-01).
Each helper takes the engine instance to access trade_args, date_str, and
book_client; behavior is identical to the original engine methods. The engine
keeps thin method wrappers so test stubs (e.g. `engine._start_tape_capture =
lambda **kwargs: ...`) and any future subclass overrides still work.

Capture surfaces:
  - fetch_depth_snapshot       -- one-shot depth-N book snapshot (Family B + book capture)
  - start_book_capture         -- background thread, post-signal book depth time series
  - start_tape_capture         -- one-shot trade-tape fetch + Family A features
  - start_family_b_capture     -- one-shot depth snapshot + Family B features
  - start_family_c_capture     -- in-memory tick-buffer Family C velocity features

Sidecar persistence layout (unchanged):
  data/<paper|live>_trading/book_captures/<date>/<bet_id>.jsonl
  data/<paper|live>_trading/tape_captures/<date>/<bet_id>.json
  data/<paper|live>_trading/book_decision_snapshots/<date>/<bet_id>.json
  data/<paper|live>_trading/velocity_snapshots/<date>/<bet_id>.json

For skip-with-features captures bet_id is "skip_<candidate_id>".
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional, TYPE_CHECKING

PROJECT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_DIR / "scripts" / "monitor"))

from monitor_mlb_polymarket_ou import CLOB_BASE, _safe_float  # noqa: E402

from book_features import compute_family_b_features  # noqa: E402
from book_velocity import compute_family_c_features  # noqa: E402
from line_state import _now_iso  # noqa: E402
from tape_capture import capture_tape_and_features  # noqa: E402

if TYPE_CHECKING:  # avoid runtime circular import with signal_engine
    from line_state import LineState
    from models import BetRecord
    from signal_engine import SignalEngine

LOGGER = logging.getLogger("signal_engine")  # match the engine logger name


# ---------------------------------------------------------------------------
# Family B / book capture helpers
# ---------------------------------------------------------------------------

def fetch_depth_snapshot(engine: "SignalEngine", token_id: str, depth: int) -> dict:
    """Fetch one book snapshot with top-N bid/ask levels plus derived analytics.

    Uses the engine's thread-local HTTP session pool (book_client._session()).
    Behavior identical to the original SignalEngine._fetch_depth_snapshot.

    Returns a dict ready to be written as a JSONL record. Fields:
      best_bid/ask, best_bid/ask_size, ltp           -- standard top-of-book
      top_bids/top_asks                              -- depth levels [{price, size}]
      spread                                         -- best_ask - best_bid
      mid                                            -- (best_bid + best_ask) / 2
      total_bid_depth / total_ask_depth              -- sum of size within top depth
    """
    import requests as _req  # local import to keep startup time clean
    out: dict = {
        "ok": False,
        "latency_ms": 0.0,
        "error": "",
        "best_bid": None,
        "best_bid_size": None,
        "best_ask": None,
        "best_ask_size": None,
        "ltp": None,
        "spread": None,
        "mid": None,
        "total_bid_depth": None,
        "total_ask_depth": None,
        "top_bids": [],
        "top_asks": [],
    }
    t0 = time.time()
    try:
        sess = engine.book_client._session()  # thread-local session reuse
        resp = sess.get(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            timeout=3.0,
        )
        out["latency_ms"] = round((time.time() - t0) * 1000.0, 2)
        if resp.status_code != 200:
            out["error"] = f"http_{resp.status_code}"
            return out
        data = resp.json()
        out["ltp"] = _safe_float(data.get("last_trade_price"))
        bids = sorted(
            data.get("bids", []) or [],
            key=lambda x: float(x.get("price", 0)),
            reverse=True,
        )
        asks = sorted(
            data.get("asks", []) or [],
            key=lambda x: float(x.get("price", 0)),
        )
        if bids:
            out["best_bid"] = _safe_float(bids[0].get("price"))
            out["best_bid_size"] = _safe_float(bids[0].get("size"))
            out["top_bids"] = [
                {"price": _safe_float(b.get("price")), "size": _safe_float(b.get("size"))}
                for b in bids[:depth]
            ]
            out["total_bid_depth"] = round(
                sum(_safe_float(b.get("size")) or 0.0 for b in bids[:depth]), 2
            )
        if asks:
            out["best_ask"] = _safe_float(asks[0].get("price"))
            out["best_ask_size"] = _safe_float(asks[0].get("size"))
            out["top_asks"] = [
                {"price": _safe_float(a.get("price")), "size": _safe_float(a.get("size"))}
                for a in asks[:depth]
            ]
            out["total_ask_depth"] = round(
                sum(_safe_float(a.get("size")) or 0.0 for a in asks[:depth]), 2
            )
        if out["best_bid"] is not None and out["best_ask"] is not None:
            out["spread"] = round(out["best_ask"] - out["best_bid"], 4)
            out["mid"] = round((out["best_bid"] + out["best_ask"]) / 2.0, 4)
        out["ok"] = True
    except Exception as exc:
        out["latency_ms"] = round((time.time() - t0) * 1000.0, 2)
        out["error"] = str(exc)
    return out


def start_book_capture(
    engine: "SignalEngine",
    bet: "BetRecord",
    token_id: str,
    initial_book: dict,
    signal_ts: float,
) -> None:
    """Launch a daemon thread capturing book depth snapshots for a placed bet.

    t=0 snapshot is written synchronously from `initial_book` (no extra
    API call). Subsequent snapshots are fetched at `capture_interval`
    intervals for `capture_duration` seconds total.

    Output: data/<paper|live>_trading/book_captures/{date}/{bet_id}.jsonl
      Line 1: {"type": "signal", ...full signal context...}
      Line 2: {"type": "snapshot", "seq": 0, "elapsed_s": 0.0, "book": {...}}
      Line N: {"type": "snapshot", "seq": N-2, "elapsed_s": N-2.0, "book": {...}}
    """
    bet_id = bet.bet_id
    capture_root = engine.trade_args.paper_root / "book_captures" / engine.date_str
    capture_root.mkdir(parents=True, exist_ok=True)
    capture_path = capture_root / f"{bet_id}.jsonl"

    duration = engine.trade_args.capture_duration
    interval = engine.trade_args.capture_interval
    depth = engine.trade_args.capture_depth

    header = {
        "type": "signal",
        "bet_id": bet_id,
        "ts": bet.placed_at,
        "game_pk": bet.game_pk,
        "away_abbrev": bet.away_abbrev,
        "home_abbrev": bet.home_abbrev,
        "line": bet.line,
        "token_id": token_id,
        "inning": bet.inning,
        "inning_state": bet.inning_state,
        "outs": bet.outs,
        "away_score_before": bet.away_score_before,
        "home_score_before": bet.home_score_before,
        "entry_ask": bet.entry_ask,
        "fair_value": bet.fair_value,
        "base_fair_value": bet.base_fair_value,
        "stage2_run_env_delta": bet.stage2_run_env_delta,
        "team_offense_delta": bet.team_offense_delta,
        "edge": bet.edge,
        "capture_duration_s": duration,
        "capture_interval_s": interval,
        "capture_depth": depth,
    }
    t0_snapshot = {
        "type": "snapshot",
        "seq": 0,
        "elapsed_s": 0.0,
        "ts": bet.placed_at,
        "book": {
            "ok": initial_book.get("ok", False),
            "latency_ms": initial_book.get("latency_ms", 0.0),
            "best_bid": initial_book.get("best_bid"),
            "best_bid_size": initial_book.get("best_bid_size"),
            "best_ask": initial_book.get("best_ask"),
            "best_ask_size": initial_book.get("best_ask_size"),
            "ltp": initial_book.get("ltp"),
            "top_bids": [],
            "top_asks": [],
        },
    }
    try:
        with open(capture_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(header) + "\n")
            fh.write(json.dumps(t0_snapshot) + "\n")
    except Exception as exc:
        LOGGER.warning("Book capture header write failed for %s: %s", bet_id, exc)
        return  # don't start thread if we can't write

    LOGGER.debug(
        "Book capture started: %s  token=%s...%s  duration=%.0fs interval=%.1fs",
        bet_id, token_id[:12], token_id[-6:], duration, interval,
    )

    def _capture_loop() -> None:
        n_snapshots = int(duration / interval)
        for seq in range(1, n_snapshots + 1):
            target_t = signal_ts + seq * interval
            sleep_s = max(0.0, target_t - time.time())
            if sleep_s > 0:
                time.sleep(sleep_s)

            elapsed = round(time.time() - signal_ts, 3)
            book = fetch_depth_snapshot(engine, token_id, depth)
            snapshot = {
                "type": "snapshot",
                "seq": seq,
                "elapsed_s": elapsed,
                "ts": _now_iso(),
                "book": book,
            }
            try:
                with open(capture_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(snapshot) + "\n")
            except Exception as exc:
                LOGGER.warning(
                    "Book capture write error seq=%d %s: %s", seq, bet_id, exc
                )
                break

        LOGGER.debug("Book capture complete: %s  (%d snapshots)", bet_id, n_snapshots + 1)

    t = threading.Thread(
        target=_capture_loop,
        daemon=True,
        name=f"capture_{bet_id}",
    )
    t.start()


# ---------------------------------------------------------------------------
# Family A: trade-tape capture
# ---------------------------------------------------------------------------

def start_tape_capture(
    engine: "SignalEngine",
    *,
    bet_id: str,
    token_id: str,
    signal_ts: float,
    current_ask: Optional[float],
) -> Dict[str, object]:
    """One-shot trade-tape fetch + Family A feature computation.

    Called once per placed bet OR per late-stage skip-with-features.
    Synchronous: one HTTP call (~100ms) to data-api.polymarket.com/trades.
    Failure modes never block the caller; on error the features dict carries
    None values and an error string, but the sidecar file is still written
    for forensic review.

    Persistence:
      data/<paper|live>_trading/tape_captures/<date>/<bet_id>.json
      (bet_id is "skip_<candidate_id>" for skip-path captures)
    Returns the features dict so the caller can attach it to the candidate
    row for downstream training.
    """
    empty_features = {
        "trades_last_5s_count": None,
        "trades_last_30s_count": None,
        "signed_volume_last_30s": None,
        "ltp_minus_ask_last_3_trades": None,
        "seconds_since_last_trade": None,
        "raw_trade_count": 0,
        "fetch_ok": False,
        "fetch_error": "not_attempted",
        "fetch_latency_ms": 0.0,
    }
    if not token_id:
        return empty_features

    try:
        result = capture_tape_and_features(
            token_id=token_id,
            signal_ts=signal_ts,
            current_ask=current_ask,
        )
    except Exception as exc:
        LOGGER.warning("Tape capture failed for %s: %s", bet_id, exc)
        features = dict(empty_features)
        features["fetch_error"] = f"exception:{exc}"
        return features

    features = result.get("features", empty_features)

    try:
        capture_root = engine.trade_args.paper_root / "tape_captures" / engine.date_str
        capture_root.mkdir(parents=True, exist_ok=True)
        capture_path = capture_root / f"{bet_id}.json"
        sidecar = {
            "bet_id": bet_id,
            "token_id": token_id,
            "signal_ts": signal_ts,
            "signal_ts_iso": _now_iso(),
            "current_ask": current_ask,
            "features": features,
            "trades": result.get("trades", []),
        }
        with open(capture_path, "w", encoding="utf-8") as fh:
            json.dump(sidecar, fh)
    except Exception as exc:
        LOGGER.warning("Tape capture sidecar write failed for %s: %s", bet_id, exc)

    LOGGER.debug(
        "Tape capture %s: trades_5s=%s trades_30s=%s signed_30s=%s "
        "ltp_ask_gap_3=%s stale=%ss raw_n=%d fetch_ok=%s",
        bet_id,
        features.get("trades_last_5s_count"),
        features.get("trades_last_30s_count"),
        features.get("signed_volume_last_30s"),
        features.get("ltp_minus_ask_last_3_trades"),
        features.get("seconds_since_last_trade"),
        features.get("raw_trade_count"),
        features.get("fetch_ok"),
    )
    return features


# ---------------------------------------------------------------------------
# Family B: book state at decision (depth-aware snapshot + features)
# ---------------------------------------------------------------------------

def start_family_b_capture(
    engine: "SignalEngine",
    *,
    bet_id: str,
    token_id: str,
    limit_price: Optional[float],
    depth: int = 5,
) -> Dict[str, object]:
    """One-shot depth-snapshot fetch + Family B feature computation.

    Called per placed bet OR per late-stage skip-with-features. Synchronous:
    one HTTP call to the CLOB /book endpoint (~150ms). Failure modes never
    block the caller; on error the features dict carries None values and a
    fetch_error string. The full depth snapshot is persisted as a sidecar
    for offline analysis.

    Persistence:
      data/<paper|live>_trading/book_decision_snapshots/<date>/<bet_id>.json
      (bet_id is "skip_<candidate_id>" for skip-path captures; for those,
      limit_price is the *hypothetical* limit derived from bid+spread*spread_factor)
    Returns the features dict so the caller can attach it to the candidate
    row for downstream training.
    """
    empty_features = {
        "ask_depth_at_touch": None,
        "bid_depth_at_touch": None,
        "book_imbalance_top1": None,
        "book_imbalance_top5": None,
        "depth_below_our_limit": None,
        "depth_at_limit_L": None,
        "limit_price_used": (
            round(float(limit_price), 4) if limit_price is not None else None
        ),
        "top_n": int(depth),
        "book_ok": False,
        "fetch_error": "not_attempted",
        "fetch_latency_ms": 0.0,
    }
    if not token_id:
        return empty_features

    t_fetch = time.time()
    try:
        snapshot = fetch_depth_snapshot(engine, token_id, depth)
    except Exception as exc:
        LOGGER.warning("Family B depth fetch failed for %s: %s", bet_id, exc)
        features = dict(empty_features)
        features["fetch_error"] = f"exception:{exc}"
        features["fetch_latency_ms"] = round((time.time() - t_fetch) * 1000.0, 2)
        return features
    fetch_ms = round((time.time() - t_fetch) * 1000.0, 2)

    features = compute_family_b_features(
        book=snapshot,
        limit_price=limit_price,
        top_n=depth,
    )
    features["fetch_error"] = (
        None if snapshot.get("ok") else (snapshot.get("error") or "fetch_failed")
    )
    features["fetch_latency_ms"] = fetch_ms

    try:
        capture_root = (
            engine.trade_args.paper_root / "book_decision_snapshots" / engine.date_str
        )
        capture_root.mkdir(parents=True, exist_ok=True)
        capture_path = capture_root / f"{bet_id}.json"
        sidecar = {
            "bet_id": bet_id,
            "token_id": token_id,
            "captured_at": _now_iso(),
            "limit_price": limit_price,
            "features": features,
            "snapshot": snapshot,
        }
        with open(capture_path, "w", encoding="utf-8") as fh:
            json.dump(sidecar, fh)
    except Exception as exc:
        LOGGER.warning("Family B sidecar write failed for %s: %s", bet_id, exc)

    LOGGER.debug(
        "Family B capture %s: ask_touch=%s bid_touch=%s imb1=%s imb5=%s "
        "depth_below=%s depth_at=%s book_ok=%s",
        bet_id,
        features.get("ask_depth_at_touch"),
        features.get("bid_depth_at_touch"),
        features.get("book_imbalance_top1"),
        features.get("book_imbalance_top5"),
        features.get("depth_below_our_limit"),
        features.get("depth_at_limit_L"),
        features.get("book_ok"),
    )
    return features


# ---------------------------------------------------------------------------
# Family C: book velocity / drift (in-memory tick buffer, no HTTP)
# ---------------------------------------------------------------------------

def start_family_c_capture(
    engine: "SignalEngine",
    *,
    bet_id: str,
    line_state: "LineState",
    signal_ts: float,
) -> Dict[str, object]:
    """Compute Family C features from the LineState tick buffer.

    Called per placed bet OR per late-stage skip-with-features. No HTTP
    call -- reads the in-memory buffer maintained by LineState.push_tick
    during normal monitor cadence. Persists a sidecar containing the
    computed features and the raw tick slice used (for offline forensics).

    Persistence:
      data/<paper|live>_trading/velocity_snapshots/<date>/<bet_id>.json
      (bet_id is "skip_<candidate_id>" for skip-path captures)
    Returns the features dict.
    """
    empty_features = {
        "ask_velocity_5s": None,
        "ask_velocity_30s": None,
        "mid_drift_30s": None,
        "spread_now_vs_spread_30s_avg": None,
        "ask_volatility_30s": None,
        "buffer_n": 0,
        "window_30s_n": 0,
        "buffer_oldest_age_s": None,
    }
    ticks = list(getattr(line_state, "tick_buffer", []) or [])
    if not ticks:
        return empty_features

    try:
        features = compute_family_c_features(
            tick_buffer=ticks,
            signal_ts=signal_ts,
        )
    except Exception as exc:
        LOGGER.warning("Family C compute failed for %s: %s", bet_id, exc)
        features = dict(empty_features)
        features["compute_error"] = f"exception:{exc}"
        return features

    try:
        capture_root = (
            engine.trade_args.paper_root / "velocity_snapshots" / engine.date_str
        )
        capture_root.mkdir(parents=True, exist_ok=True)
        capture_path = capture_root / f"{bet_id}.json"
        sidecar = {
            "bet_id": bet_id,
            "captured_at": _now_iso(),
            "signal_ts": signal_ts,
            "features": features,
            "tick_buffer": ticks,
        }
        with open(capture_path, "w", encoding="utf-8") as fh:
            json.dump(sidecar, fh)
    except Exception as exc:
        LOGGER.warning("Family C sidecar write failed for %s: %s", bet_id, exc)

    LOGGER.debug(
        "Family C capture %s: ask_v5=%s ask_v30=%s mid_drift=%s "
        "spread_ratio=%s ask_vol=%s buffer_n=%d window_30s_n=%d",
        bet_id,
        features.get("ask_velocity_5s"),
        features.get("ask_velocity_30s"),
        features.get("mid_drift_30s"),
        features.get("spread_now_vs_spread_30s_avg"),
        features.get("ask_volatility_30s"),
        features.get("buffer_n"),
        features.get("window_30s_n"),
    )
    return features
