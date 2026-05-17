#!/usr/bin/env python3
"""
live_order_lifecycle.py -- Open-order polling, cancellation, and FV recomputation.

Free functions extracted from live_engine.LiveTradingEngine (Tier 3 refactor,
2026-05-01). All open-order maintenance lives here: fill polling, fill recovery
via trade history, stale-order timeout, ask-reversal cancellation, FV
recomputation against live game state, FV-decay cancellation, end-of-game
cancellation, and per-line lock release after cancellation. The engine retains
thin method wrappers so existing call-sites in `_on_tick_batch`,
`_settle_finished_games`, `_shutdown_gracefully`, and `_place_bet` keep
working unchanged.

Surfaces:
  - check_open_orders(engine)         -- poll CLOB for fill/cancel status
  - try_recover_fill(engine, bet, order_id) -> bool
  - cancel_stale_orders(engine)       -- safety-net timeout cancel
  - check_ask_reversal(engine)        -- 5s post-placement ask-drop cancel
  - recompute_fv(engine, bet) -> Optional[float]
  - check_fv_decay(engine)            -- cancel when current_fv decays below limit
  - cancel_orders_for_game(engine, game_pk) -- end-of-game order cleanup
  - release_line_after_unfilled_order(engine, bet, *, reason) -- reset line state

Engine attrs read:
  - engine._open_orders (mutated; orders removed on resolve)
  - engine._line_states, engine._dry_run, engine.live_args, engine.games
  - engine.cache, engine.stage2_model, engine.offense_model
  - engine._clob (CLOB order client)
Engine method calls (preserved as method calls so subclass overrides work):
  - engine._fetch_depth_snapshot(...)
  - engine._calibrate_fair_value(...)
  - engine._save_session()
  - engine._append_to_live_ledger(bet)

All cancellation paths funnel through release_line_after_unfilled_order so the
line lock is released and a future event on the same line can be reconsidered
after the cooldown window.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from line_state import _now_iso, _now_ts
from signal_config import DEFAULT_COOLDOWN_TICKS
from stage2_run_env_model import RunEnvContext
from weather_client import weather_v2_run_env_game_data
from polymarket_client import _is_transient_error
from order_status import (
    is_poll_filled_status as _is_poll_filled_status,
    normalize_order_status as _normalize_order_status,
)

if TYPE_CHECKING:
    from live_engine import LiveTradingEngine
    from models import LiveBetRecord

LOGGER = logging.getLogger("live_engine")


# ---------------------------------------------------------------------------
# Per-line lock release after cancellation
# ---------------------------------------------------------------------------

def release_line_after_unfilled_order(
    engine: "LiveTradingEngine",
    bet: "LiveBetRecord",
    *,
    reason: str,
) -> None:
    """Allow a later event on the same line to be evaluated after cancellation."""
    key = (bet.game_pk, bet.line)
    state = engine._line_states.get(key)
    if state is None:
        return
    state.bet_open = False
    state.cooldown_remaining = DEFAULT_COOLDOWN_TICKS
    state.baseline_ask = None
    state.baseline_candidate = None
    state.stable_count = 0
    state.pending_signal = False
    state.pending_ticks_remaining = 0
    state.pending_jump_ask = None
    LOGGER.debug(
        "Line state released after %s [%s] game_pk=%s line=%s cooldown_ticks=%d",
        reason, bet.bet_id, bet.game_pk, bet.line, DEFAULT_COOLDOWN_TICKS,
    )


# ---------------------------------------------------------------------------
# Poll open orders for fill/cancel
# ---------------------------------------------------------------------------

def check_open_orders(engine: "LiveTradingEngine") -> None:
    """Poll CLOB for fill status of all open orders.

    Called every ORDER_POLL_EVERY_N_CYCLES cycles (approx every 4-10s).
    On fill: records fill price/size, settles the bet with realized P&L.
    """
    if not engine._open_orders:
        return

    filled_ids = []
    for order_id, bet in list(engine._open_orders.items()):
        status = engine._clob.get_order(order_id)
        norm_status = _normalize_order_status(status.status) or "unknown"

        if _is_poll_filled_status(norm_status):
            fill_price = status.price or bet.limit_price
            fill_size  = status.size_matched or bet.order_size_shares or (bet.stake / bet.limit_price)
            LOGGER.info(
                "ORDER FILLED [%s] order_id=%s  fill_price=%.3f  size=%.2f",
                bet.bet_id, order_id, fill_price, fill_size,
            )
            bet.order_status = "filled"
            bet.fill_price   = fill_price
            bet.fill_size    = fill_size
            bet.fill_cost    = round(float(fill_size) * float(fill_price), 2)
            bet.filled_at    = _now_iso()
            bet.actual_fill_price = fill_price
            bet.filled_shares = float(fill_size)
            bet.fill_cost_usdc = bet.fill_cost
            filled_ids.append(order_id)
            engine._append_to_live_ledger(bet)
            engine._save_session()

        elif norm_status in ("cancelled", "expired", "error"):
            LOGGER.info(
                "ORDER %s [%s] order_id=%s",
                norm_status.upper(), bet.bet_id, order_id,
            )
            bet.order_status = norm_status
            if not bet.cancel_reason:
                bet.cancel_reason = norm_status
            bet.cancelled_at = _now_iso()
            filled_ids.append(order_id)
            release_line_after_unfilled_order(engine, bet, reason=norm_status)
            engine._append_to_live_ledger(bet)
            engine._save_session()

        elif norm_status == "unknown" and status.error:
            # Check if the error looks transient (5xx, timeout, connection).
            # During the deposit wallet rollout or CLOB maintenance windows,
            # poll failures are expected. Skip the order this cycle instead of
            # running fill recovery (which would also likely fail).
            if _is_transient_error(Exception(status.error)):
                LOGGER.warning(
                    "Transient CLOB error polling [%s] order_id=%s -- skipping this cycle: %s",
                    bet.bet_id, order_id, status.error,
                )
                continue
            if try_recover_fill(engine, bet, order_id):
                filled_ids.append(order_id)

    for oid in filled_ids:
        engine._open_orders.pop(oid, None)


# ---------------------------------------------------------------------------
# Fill recovery via trade history
# ---------------------------------------------------------------------------

def try_recover_fill(
    engine: "LiveTradingEngine",
    bet: "LiveBetRecord",
    order_id: str,
) -> bool:
    """Attempt to recover a missed fill by querying the CLOB trade history.

    Returns True if fill was confirmed and the bet was updated.
    Used when get_order() returns unknown/error but the market may have
    cleared (e.g., post-settlement). Falls back to trade history search.
    """
    try:
        placed_at_ts = datetime.fromisoformat(
            bet.order_placed_at.rstrip("Z")
        ).replace(tzinfo=timezone.utc).timestamp() if bet.order_placed_at else None
    except Exception:
        placed_at_ts = None

    try:
        trade = engine._clob.get_trades_for_order(
            token_id=bet.over_token_id,
            order_id=order_id,
            placed_at_ts=placed_at_ts,
        )
    except Exception as exc:
        LOGGER.warning(
            "Fill recovery query failed [%s]: %s", bet.bet_id, exc,
        )
        return False

    if trade is None:
        return False

    fill_price = trade.price
    fill_size  = trade.size
    LOGGER.info(
        "FILL RECOVERED [%s] order_id=%s via trade history  price=%.3f  size=%.2f",
        bet.bet_id, order_id, fill_price, fill_size,
    )
    bet.order_status  = "filled"
    bet.fill_price    = fill_price
    bet.fill_size     = fill_size
    bet.fill_cost     = round(float(fill_size) * float(fill_price), 2)
    bet.filled_at     = trade.timestamp_iso
    bet.actual_fill_price = fill_price
    bet.filled_shares = float(fill_size)
    bet.fill_cost_usdc = bet.fill_cost
    engine._append_to_live_ledger(bet)
    engine._save_session()
    return True


# ---------------------------------------------------------------------------
# Stale order timeout
# ---------------------------------------------------------------------------

def cancel_stale_orders(engine: "LiveTradingEngine") -> None:
    """Safety-net: cancel open orders older than order_timeout_secs.

    This is the last resort for abandoned orders (monitor restart, game
    suspended, etc.). FV decay is the primary cancellation mechanism.
    """
    from live_engine import DEFAULT_ORDER_TIMEOUT_SECS

    now = _now_ts()
    timeout = getattr(engine.live_args, "order_timeout_secs", DEFAULT_ORDER_TIMEOUT_SECS)

    for order_id, bet in list(engine._open_orders.items()):
        if not bet.order_placed_at:
            continue
        try:
            placed_ts = datetime.fromisoformat(
                bet.order_placed_at.rstrip("Z")
            ).replace(tzinfo=timezone.utc).timestamp()
            age_secs = now - placed_ts
        except Exception:
            continue

        if age_secs < timeout:
            continue

        LOGGER.info(
            "STALE ORDER cancel [%s] order_id=%s  age=%.0fs > timeout=%.0fs",
            bet.bet_id, order_id, age_secs, timeout,
        )
        if try_recover_fill(engine, bet, order_id):
            del engine._open_orders[order_id]
            engine._save_session()
            continue

        if not engine._dry_run:
            engine._clob.cancel_order(order_id)
        bet.order_status  = "cancelled"
        bet.cancelled_at  = _now_iso()
        bet.cancel_reason = "timeout"
        del engine._open_orders[order_id]
        release_line_after_unfilled_order(engine, bet, reason="timeout")
        engine._append_to_live_ledger(bet)
        engine._save_session()


# ---------------------------------------------------------------------------
# Ask-reversal early-cancel
# ---------------------------------------------------------------------------

def check_ask_reversal(engine: "LiveTradingEngine") -> None:
    """Cancel new orders where the ask has dropped sharply since placement.

    Called every poll cycle (not throttled) because the window is only 5
    seconds -- a throttled 4-10s cadence would miss it entirely.

    Two actions, both logged to file:
      1. Cancel + record  -- if ask drop >= threshold within reversal_window
      2. Snapshot + log   -- once the window expires, record ask_5s and ask_drop_5s
                            on the bet regardless of cancellation so we have
                            data on every order for later gate tuning.

    Confirmed data pattern (2026-04-20 session):
      Wins:  ask drift at 5s = -0.02 to +0.05 (flat / confirming)
      Losses: ask drift at 5s = -0.11         (immediate reversal)
    """
    from live_engine import (
        DEFAULT_ASK_REVERSAL_DROP,
        DEFAULT_ASK_REVERSAL_WINDOW,
        DEFAULT_ASK_REVERSAL_WINDOW_BUFFER,
    )

    if not engine._open_orders:
        return

    now = _now_ts()
    drop_threshold = getattr(engine.live_args, "ask_reversal_drop",   DEFAULT_ASK_REVERSAL_DROP)
    window         = getattr(engine.live_args, "ask_reversal_window",  DEFAULT_ASK_REVERSAL_WINDOW)

    for order_id, bet in list(engine._open_orders.items()):
        if not bet.order_placed_at:
            continue

        try:
            placed_ts = datetime.fromisoformat(
                bet.order_placed_at.rstrip("Z")
            ).replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            continue

        age = now - placed_ts

        # Only relevant within the first window + small buffer (for the end-of-window snapshot)
        if age > window + DEFAULT_ASK_REVERSAL_WINDOW_BUFFER:
            continue

        # Fetch current best ask -- one lightweight API call per young open order
        try:
            book = engine._fetch_depth_snapshot(bet.over_token_id, 1)
            current_ask = book.get("best_ask")
        except Exception as exc:
            LOGGER.debug("Ask-reversal fetch failed [%s]: %s", bet.bet_id, exc)
            continue

        if current_ask is None:
            continue

        ask_drop = bet.entry_ask - current_ask  # positive = ask fell

        LOGGER.debug(
            "Ask-reversal monitor [%s] age=%.1fs  entry_ask=%.3f  "
            "current_ask=%.3f  drop=%+.3f  threshold=%.3f",
            bet.bet_id, age, bet.entry_ask, current_ask, ask_drop, drop_threshold,
        )

        # --- Gate: cancel if within window and drop is large enough ---
        if age <= window and ask_drop >= drop_threshold:
            LOGGER.info(
                "ASK REVERSAL cancel [%s] order_id=%s | "
                "entry_ask=%.3f -> current_ask=%.3f  drop=%+.3f >= threshold=%.3f | "
                "age=%.1fs (within %.1fs window) | "
                "Signal likely phantom run or API artefact",
                bet.bet_id, order_id,
                bet.entry_ask, current_ask, ask_drop, drop_threshold,
                age, window,
            )
            bet.ask_5s = current_ask
            bet.ask_drop_5s = round(ask_drop, 4)
            bet.ask_reversal_recorded = True

            if try_recover_fill(engine, bet, order_id):
                del engine._open_orders[order_id]
                engine._save_session()
                continue

            if not engine._dry_run:
                engine._clob.cancel_order(order_id)
            bet.order_status  = "cancelled"
            bet.cancelled_at  = _now_iso()
            bet.cancel_reason = "ask_reversal"
            bet.ask_at_cancel = current_ask
            del engine._open_orders[order_id]
            release_line_after_unfilled_order(engine, bet, reason="ask_reversal")
            engine._append_to_live_ledger(bet)
            engine._save_session()
            continue

        # --- End-of-window snapshot: record ask_5s for ALL orders ---
        if age > window and not bet.ask_reversal_recorded:
            bet.ask_5s = current_ask
            bet.ask_drop_5s = round(ask_drop, 4)
            bet.ask_reversal_recorded = True
            LOGGER.info(
                "Ask-reversal window expired [%s] entry_ask=%.3f  "
                "window=%.1fs  current_ask=%.3f  drop=%+.3f  "
                "(threshold=%.3f  window=%.0fs -- no cancel triggered)",
                bet.bet_id,
                bet.entry_ask, window, current_ask, ask_drop,
                drop_threshold, window,
            )
            engine._append_to_live_ledger(bet)
            engine._save_session()


# ---------------------------------------------------------------------------
# FV recomputation against live game state
# ---------------------------------------------------------------------------

def recompute_fv(engine: "LiveTradingEngine", bet: "LiveBetRecord") -> Optional[float]:
    """Recompute current model FV for an open order using live game state.

    Returns None when the game state is unavailable (API gap, game not
    started, etc.) -- caller should skip cancellation in that case.
    """
    game = engine.games.get(bet.game_pk)
    if game is None:
        return None

    score = game.score
    if score.away is None or score.home is None:
        return None
    if score.inning is None or score.outs is None:
        return None
    if not score.inning_state or score.inning_state.lower() in ("end", "middle"):
        return None

    fv = engine.cache.lookup(
        away_score=score.away,
        home_score=score.home,
        inning=score.inning,
        inning_state=score.inning_state,
        outs=score.outs,
        line=bet.line,
        runners_on=score.runners_on,
    )
    if fv is None:
        return None

    # Apply Stage-2 park/weather adjustment
    if engine.stage2_model is not None:
        try:
            weather_features = {}
            weather_getter = getattr(engine, "_weather_fields_for_game", None)
            if callable(weather_getter):
                weather_features = weather_getter(game.game_pk)
            env_context = RunEnvContext.from_game_data(
                weather_v2_run_env_game_data(
                    weather_features,
                    venue_name=game.venue_name,
                    game_date_utc=game.game_date,
                )
            )
            fv = engine.stage2_model.adjust_line(
                line=bet.line, base_prob=fv, context=env_context
            )
        except Exception:
            pass

    # Apply Stage-3 team offense adjustment
    try:
        game_date = game.game_date[:10]
        fv = engine.offense_model.adjust_fv(
            base_fv=fv,
            away_abbrev=bet.away_abbrev,
            home_abbrev=bet.home_abbrev,
            game_date=game_date,
            inning=score.inning,
        )
    except Exception:
        pass

    try:
        fv, _ = engine._calibrate_fair_value(
            raw_prob=fv,
            line=bet.line,
            inning=score.inning,
            decision_ask=None,
        )
    except Exception:
        pass

    return fv


# ---------------------------------------------------------------------------
# FV-decay cancellation
# ---------------------------------------------------------------------------

def check_fv_decay(engine: "LiveTradingEngine") -> None:
    """Cancel open orders where current FV has decayed to near our limit price.

    At signal time, edge-at-limit was 15-17pp.  We cancel when:
        current_fv - bet.limit_price < FV_CANCEL_MIN_EDGE (3pp)
    and
        entry_ask - current_ask >= fv_decay_min_ask_drop (market confirms decay)

    The minimum age guard (DEFAULT_FV_DECAY_MIN_AGE_SECS = 90s) ensures FV
    decay does not fire during the inning-ending repricing window (60-90s of
    bid collapse/recovery observed in book capture data from 2026-04-22).

    [P0 Fix #1] Score-over-line guard: if the running total already exceeds
    the line, the bet has effectively won. Never cancel in this case.
    [P0 Fix #2] Min age raised 30s -> 90s (covers inning-ending repricing).
    [P0 Fix #3] Min edge lowered 5pp -> 3pp (prevents marginal-edge cancels).
    """
    from live_engine import (
        DEFAULT_FV_DECAY_MIN_AGE_SECS,
        DEFAULT_FV_DECAY_MIN_ASK_DROP,
        FV_CANCEL_MIN_EDGE,
    )

    if not engine._open_orders:
        return

    min_edge = getattr(engine.live_args, "fv_cancel_min_edge", FV_CANCEL_MIN_EDGE)
    min_age_secs = getattr(
        engine.live_args, "fv_decay_min_age_secs", DEFAULT_FV_DECAY_MIN_AGE_SECS
    )
    min_ask_drop = getattr(
        engine.live_args, "fv_decay_min_ask_drop", DEFAULT_FV_DECAY_MIN_ASK_DROP
    )

    for order_id, bet in list(engine._open_orders.items()):
        age_secs = None
        if bet.order_placed_at:
            try:
                placed_ts = datetime.fromisoformat(
                    bet.order_placed_at.rstrip("Z")
                ).replace(tzinfo=timezone.utc).timestamp()
                age_secs = _now_ts() - placed_ts
            except Exception:
                age_secs = None

        # --- [P0 Fix #1] Guard: current score already exceeds the line ---
        # If the running total is already over the line, the bet has effectively
        # won regardless of remaining innings. Never cancel. This fires for live
        # (non-final) games -- final games are settled separately by
        # _cancel_orders_for_game() / _settle_finished_games().
        # Source: 2026-04-22 dry run -- all 3 signals would have filled if this
        # guard existed. FV decay fired during inning-ending repricing even though
        # the games were still heading well over the line.
        _game = engine.games.get(bet.game_pk)
        if _game is not None and not _game.is_final():
            _s_away = _game.score.away
            _s_home = _game.score.home
            if _s_away is not None and _s_home is not None:
                _live_total = _s_away + _s_home
                if _live_total > float(bet.line):
                    LOGGER.info(
                        "FV decay LOCKED [%s] order_id=%s -- "
                        "current score %d-%d (total=%d) already exceeds line=%.1f. "
                        "Bet cannot lose on the over; holding until fill or game Final.",
                        bet.bet_id, order_id,
                        _s_away, _s_home, _live_total, float(bet.line),
                    )
                    continue

        if age_secs is not None and age_secs < min_age_secs:
            LOGGER.debug(
                "FV check [%s] age=%.1fs < min_age=%.1fs -- hold period active",
                bet.bet_id, age_secs, min_age_secs,
            )
            continue

        current_fv = recompute_fv(engine, bet)
        if current_fv is None:
            # Game state unavailable (API gap, game not started, etc.) -- skip
            continue

        edge_at_limit = current_fv - bet.limit_price

        if edge_at_limit >= min_edge:
            LOGGER.debug(
                "FV check [%s] current_fv=%.3f  limit=%.3f  edge_at_limit=%.3f  OK",
                bet.bet_id, current_fv, bet.limit_price, edge_at_limit,
            )
            continue

        # Edge collapsed by model. Before canceling, require market confirmation:
        # if ask has not weakened meaningfully, this can be a transient MLB API
        # state oscillation and we hold the order.
        current_ask = None
        ask_drop = None
        try:
            book = engine._fetch_depth_snapshot(bet.over_token_id, 1)
            current_ask = book.get("best_ask")
            if current_ask is not None:
                ask_drop = bet.entry_ask - current_ask
        except Exception as exc:
            LOGGER.debug("FV-decay ask-confirmation fetch failed [%s]: %s", bet.bet_id, exc)

        if ask_drop is not None and ask_drop < min_ask_drop:
            LOGGER.info(
                "FV decay hold [%s] order_id=%s  edge_at_limit=%.3f < min=%.3f  "
                "but ask_drop=%+.3f < required=%.3f (entry_ask=%.3f current_ask=%.3f)",
                bet.bet_id, order_id,
                edge_at_limit, min_edge,
                ask_drop, min_ask_drop, bet.entry_ask, current_ask,
            )
            continue

        ask_drop_msg = "n/a" if ask_drop is None else f"{ask_drop:+.3f}"
        LOGGER.info(
            "FV decay cancel [%s] order_id=%s  "
            "entry_fv=%.3f -> current_fv=%.3f  limit=%.3f  "
            "edge_at_limit=%.3f < min=%.3f  ask_drop=%s",
            bet.bet_id, order_id,
            bet.fair_value, current_fv, bet.limit_price,
            edge_at_limit, min_edge, ask_drop_msg,
        )

        # Attempt fill recovery -- order may have been filled since last poll
        if try_recover_fill(engine, bet, order_id):
            del engine._open_orders[order_id]
            engine._save_session()
            continue

        if not engine._dry_run:
            engine._clob.cancel_order(order_id)
        bet.order_status  = "cancelled"
        bet.cancelled_at  = _now_iso()
        bet.cancel_reason = "fv_decay"
        bet.fv_at_cancel  = current_fv
        bet.ask_at_cancel = current_ask
        del engine._open_orders[order_id]
        release_line_after_unfilled_order(engine, bet, reason="fv_decay")
        engine._append_to_live_ledger(bet)
        engine._save_session()


# ---------------------------------------------------------------------------
# End-of-game cleanup
# ---------------------------------------------------------------------------

def cancel_orders_for_game(engine: "LiveTradingEngine", game_pk: int) -> None:
    """Cancel all open orders for a game that has gone Final.

    Always does a final fill-check before cancelling -- an order may have
    been filled between the last poll cycle and game resolution.
    """
    for order_id, bet in list(engine._open_orders.items()):
        if bet.game_pk != game_pk:
            continue

        status = engine._clob.get_order(order_id)
        norm_status = _normalize_order_status(status.status)

        # Skip transient CLOB errors during end-of-game cleanup.
        # The stale-order timeout will catch any orders missed here.
        if norm_status == "unknown" and status.error and _is_transient_error(Exception(status.error)):
            LOGGER.warning(
                "Transient CLOB error during game-final check [%s] order_id=%s -- "
                "skipping, will retry next cycle: %s",
                bet.bet_id, order_id, status.error,
            )
            continue

        if norm_status in ("filled", "matched"):
            fill_price = status.price or bet.limit_price
            fill_size  = status.size_matched or bet.order_size_shares or (bet.stake / bet.limit_price)
            bet.order_status  = "filled"
            bet.fill_price    = fill_price
            bet.fill_size     = fill_size
            bet.fill_cost     = round(float(fill_size) * float(fill_price), 2)
            bet.filled_at     = _now_iso()
            bet.actual_fill_price = fill_price
            bet.filled_shares = float(fill_size)
            bet.fill_cost_usdc = bet.fill_cost
            LOGGER.info(
                "ORDER FILLED (at game Final) [%s] order_id=%s  fill_price=%.3f",
                bet.bet_id, order_id, fill_price,
            )
            del engine._open_orders[order_id]
            engine._append_to_live_ledger(bet)
            engine._save_session()
            continue

        if try_recover_fill(engine, bet, order_id):
            del engine._open_orders[order_id]
            engine._save_session()
            continue

        LOGGER.info(
            "Cancelling open order for Final game [%s] order_id=%s",
            bet.bet_id, order_id,
        )
        if not engine._dry_run:
            engine._clob.cancel_order(order_id)
        bet.order_status  = "cancelled"
        bet.cancelled_at  = _now_iso()
        bet.cancel_reason = "game_final"
        del engine._open_orders[order_id]
        release_line_after_unfilled_order(engine, bet, reason="game_final")
        engine._append_to_live_ledger(bet)
        engine._save_session()
