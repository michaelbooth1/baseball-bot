#!/usr/bin/env python3
"""
live_session_loading.py -- Resume LiveTradingEngine state from prior session JSON.

Free function extracted from live_engine.LiveTradingEngine (Tier 3 refactor,
2026-05-01). Reads `<date>_live_session.json` if present, rebuilds the
`_bets` list, dedup state (`_last_bet_ts`, `_last_bet_edge`,
`_last_bet_inning`, `_last_bet_edge_by_line`), and re-registers open orders
for crash-recovery polling. The engine retains a thin
`_load_existing_session` method wrapper for backward compat.

Surfaces:
  - load_existing_session(engine) -> None

Engine attrs read:
  - engine._live_session_path, engine.live_args.daily_budget
Engine attrs written:
  - engine._bets (appended)
  - engine._last_bet_ts, engine._last_bet_edge,
    engine._last_bet_inning, engine._last_bet_edge_by_line (set)
  - engine._open_orders (re-registered for monitoring)
External calls:
  - engine._filled_notional(bet)
  - engine._check_open_orders()  (immediate sync poll if open orders found)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from line_state import DEFAULT_COOLDOWN_TICKS, LineState, _now_ts
from models import LiveBetRecord
from order_status import (
    is_exposure_counted_status as _is_exposure_counted_status,
    normalize_order_status as _normalize_order_status,
)

if TYPE_CHECKING:
    from live_engine import LiveTradingEngine

LOGGER = logging.getLogger("live_engine")


def _should_restore_dedup_state(bet: LiveBetRecord) -> bool:
    """Resume dedup only for real placed exposure, not failed/missed attempts."""
    status = _normalize_order_status(getattr(bet, "order_status", ""))
    return status == "filled" or _is_exposure_counted_status(status)


def _restore_open_line_lock(engine: "LiveTradingEngine", bet: LiveBetRecord) -> None:
    """Mirror LineState.reset_after_bet() for an order restored mid-session."""
    key = (bet.game_pk, bet.line)
    state = engine._line_states.setdefault(
        key,
        LineState(game_pk=bet.game_pk, line=bet.line),
    )
    state.bet_open = True
    state.cooldown_remaining = max(
        int(getattr(state, "cooldown_remaining", 0) or 0),
        DEFAULT_COOLDOWN_TICKS,
    )
    state.baseline_ask = None
    state.baseline_candidate = None
    state.stable_count = 0
    state.pending_signal = False
    state.pending_ticks_remaining = 0
    state.pending_jump_ask = None


def load_existing_session(engine: "LiveTradingEngine") -> None:
    """Restore state from today's session file if the bot is being restarted mid-day.

    Rebuilds the dedup state (last_bet_ts, last_bet_inning, etc.) and
    re-populates _open_orders so live order monitoring continues.
    """
    if not engine._live_session_path.exists():
        return

    try:
        with open(engine._live_session_path, encoding="utf-8") as f:
            session = json.load(f)
    except Exception as exc:
        LOGGER.warning(
            "Could not load existing session file %s: %s -- starting fresh",
            engine._live_session_path, exc,
        )
        return

    bets_data = session.get("bets", [])
    live_fields = {f for f in LiveBetRecord.__dataclass_fields__}
    loaded = 0
    deployed = 0.0

    def _parse_iso_to_ts(raw: Optional[str]) -> Optional[float]:
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw).rstrip("Z"))
        except Exception:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.timestamp()

    for bet_dict in bets_data:
        try:
            kwargs = {k: v for k, v in bet_dict.items() if k in live_fields}
            bet = LiveBetRecord(**kwargs)
            if bet.filled_shares is None and bet.fill_size is not None:
                bet.filled_shares = float(bet.fill_size)
            if bet.fill_cost_usdc is None and bet.fill_cost is not None:
                bet.fill_cost_usdc = float(bet.fill_cost)
            if bet.payout_usdc is None and bet.payout is not None:
                bet.payout_usdc = float(bet.payout)
            engine._bets.append(bet)
            loaded += 1

            dedup_ts = (
                _parse_iso_to_ts(getattr(bet, "order_placed_at", ""))
                or _parse_iso_to_ts(getattr(bet, "placed_at", ""))
                or _now_ts()
            )

            # Rebuild dedup state only from real placed exposure. Failed
            # placements and unfilled cancellations remain in _bets for audit,
            # but must not suppress future valid candidates after a restart.
            key = (bet.game_pk, bet.line)
            if _should_restore_dedup_state(bet):
                prev_ts = engine._last_bet_ts.get(bet.game_pk)
                if prev_ts is None or dedup_ts >= prev_ts:
                    engine._last_bet_ts[bet.game_pk] = dedup_ts
                    engine._last_bet_edge[bet.game_pk] = bet.edge

                prev_inning = engine._last_bet_inning.get(key, -1)
                if int(getattr(bet, "inning", -1)) >= int(prev_inning):
                    engine._last_bet_inning[key] = bet.inning
                    engine._last_bet_edge_by_line[key] = bet.edge

            # Re-register open orders for monitoring
            if _is_exposure_counted_status(bet.order_status) and bet.order_id:
                engine._open_orders[bet.order_id] = bet
                _restore_open_line_lock(engine, bet)

            if bet.order_status == "filled":
                deployed += engine._filled_notional(bet)
            elif _is_exposure_counted_status(bet.order_status) and bet.stake:
                deployed += float(bet.stake)

        except Exception as exc:
            LOGGER.warning("Skipping malformed bet record during resume: %s", exc)

    remaining = max(0.0, engine.live_args.daily_budget - deployed)
    LOGGER.info(
        "RESUMED session from %s: %d bet(s) loaded  deployed=$%.2f  remaining=$%.2f of $%.0f",
        engine._live_session_path, loaded, deployed, remaining, engine.live_args.daily_budget,
    )

    if engine._open_orders:
        LOGGER.info(
            "Crash recovery: %d open order(s) restored -- running immediate sync poll",
            len(engine._open_orders),
        )
        try:
            engine._check_open_orders()
        except Exception as exc:
            LOGGER.warning("Crash recovery open-order sync failed: %s", exc)
