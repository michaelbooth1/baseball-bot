"""live_engine_session_io.py -- Session JSON + lifecycle-ledger writers.

Extracted from `live_engine.py` (2026-05-12) to keep the engine file under
the LLM-friendly 1200-line threshold. Owns the two persistent durable
artifacts produced by a live trading run:

  1. `<date>_session.json` -- one file per session, written at every save
     point. Snapshot of engine state, bet records, shadow diagnostics,
     and the post-save per-game summary log. Payload assembly is in
     `session_serialization.build_live_session_payload`; this module
     handles the throttling, write-to-disk, and post-save log emission.

  2. `live_orders_ledger.jsonl` + `master_ledger.jsonl` -- append-only
     lifecycle event streams. Each meaningful state transition
     (placed -> filled, cancelled, missed, reconciled_filled) emits one
     tagged row. Dedup keys are bootstrapped from any existing file at
     startup so warm-starts don't duplicate rows.

Live-engine methods on `LiveTradingEngine` are thin wrappers that delegate
here, mirroring the same pattern already used by `live_pricing`,
`live_order_lifecycle`, `live_diagnostics`, and `live_reconciliation`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Tuple

from order_status import (
    is_exposure_counted_status as _is_exposure_counted_status,
    normalize_order_status as _normalize_order_status,
)

if TYPE_CHECKING:
    from live_engine import LiveTradingEngine
    from models import LiveBetRecord
    from signal_engine import BetRecord


LOGGER = logging.getLogger("live_engine")


# ---------------------------------------------------------------------------
# Lifecycle-ledger helpers (live_orders_ledger.jsonl + master_ledger.jsonl)
# ---------------------------------------------------------------------------


def append_to_ledger(_engine: "LiveTradingEngine", _bet: "BetRecord") -> None:
    """No-op override of the parent SignalEngine ledger writer.

    Live trading uses :func:`append_to_live_ledger` for all ledger writes,
    with explicit event tagging. SignalEngine's parent path would write an
    untagged row, which would conflict with the tagged-event dedup logic
    below.
    """
    pass


def live_bet_event_type(bet: "LiveBetRecord") -> str:
    """Map a LiveBetRecord's `order_status` to a stable ledger event tag."""
    status = _normalize_order_status(getattr(bet, "order_status", ""))
    if status == "filled":
        return "filled"
    if status in {"cancelled", "expired"}:
        return "missed" if bool(getattr(bet, "settled", False)) else "cancelled"
    if status == "error":
        return "error"
    if status == "dry_run":
        return "dry_run"
    return status or "order"


def append_to_live_ledger(
    engine: "LiveTradingEngine", bet: "LiveBetRecord"
) -> None:
    """Append a LiveBetRecord lifecycle row to both live ledger streams."""
    row = asdict(bet)
    row.setdefault("_event", live_bet_event_type(bet))
    write_live_ledger_row(engine, row)


def live_ledger_event_key(row: Dict[str, Any]) -> Tuple[str, str, str]:
    """Stable (bet_id, order_id, event) key used to dedup ledger rows."""
    return (
        str(row.get("bet_id") or ""),
        str(row.get("order_id") or ""),
        str(row.get("_event") or row.get("event_type") or row.get("order_status") or ""),
    )


def bootstrap_live_ledger_event_keys(engine: "LiveTradingEngine") -> None:
    """Hydrate the in-memory dedup set from any existing ledger on disk.

    Called once on first ledger write per session so a warm-start (engine
    process restarted mid-day) doesn't replay events that already landed on
    disk. Best-effort: a malformed line just gets skipped.
    """
    if bool(getattr(engine, "_live_ledger_event_keys_bootstrapped", False)):
        return
    engine._live_ledger_event_keys_bootstrapped = True
    seen = getattr(engine, "_live_ledger_event_keys_written", None)
    if seen is None:
        seen = set()
        engine._live_ledger_event_keys_written = seen
    path = getattr(engine, "_live_orders_path", None)
    if path is None or not Path(path).exists():
        return
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    existing = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(existing, dict):
                    continue
                key = live_ledger_event_key(existing)
                if key[0] and key[2]:
                    seen.add(key)
    except Exception as exc:
        LOGGER.warning("Failed to bootstrap live ledger dedup keys: %s", exc)


def write_live_ledger_row(
    engine: "LiveTradingEngine", row: Dict[str, Any]
) -> bool:
    """Write one tagged live lifecycle row, suppressing duplicate events.

    Writes to both `_live_orders_path` (the session-scoped tagged ledger)
    and `_live_ledger_path` (the all-time master ledger).
    """
    bootstrap_live_ledger_event_keys(engine)
    key = live_ledger_event_key(row)
    seen = getattr(engine, "_live_ledger_event_keys_written", None)
    if seen is None:
        seen = set()
        engine._live_ledger_event_keys_written = seen
    dedupe_enabled = bool(key[0] and key[2])
    if dedupe_enabled and key in seen:
        LOGGER.debug(
            "Suppressed duplicate live ledger event bet_id=%s order_id=%s event=%s",
            key[0], key[1], key[2],
        )
        return False
    try:
        for path in (engine._live_orders_path, engine._live_ledger_path):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        if dedupe_enabled:
            seen.add(key)
        return True
    except Exception as exc:
        LOGGER.error("Failed to write row to live ledger [%s]: %s", key[0], exc)
        return False


# ---------------------------------------------------------------------------
# Session JSON saver
# ---------------------------------------------------------------------------


def save_session(engine: "LiveTradingEngine", force: bool = False) -> None:
    """Write `<date>_session.json` and emit post-save summary logs.

    Throttling: by default coalesces burst writes via
    `_session_save_min_interval_secs`. Pass `force=True` to bypass.

    On every save we also write the candidate-rollup sidecar. On the final
    save of the day (`force=True` AND `_all_games_done()`), we emit the
    shadow-order, shadow-feature, and current-state-edge-band diagnostics
    to the log so they show up in the session log without opening the JSON.
    """
    # Lazy imports to avoid a circular module dependency at startup -- both
    # `session_serialization` and the diagnostic helpers historically lived
    # next to `live_engine` and import each other transitively.
    from session_serialization import (
        build_live_session_payload as _build_live_session_payload,
    )
    from signal_engine import _now_ts as _signal_now_ts

    if not force:
        min_interval = max(0.0, float(engine._session_save_min_interval_secs))
        if (
            min_interval > 0
            and engine._last_session_save_ts > 0
            and (_signal_now_ts() - engine._last_session_save_ts) < min_interval
        ):
            engine._session_save_pending = True
            return
    try:
        engine._flush_expired_score_confirmations()
        filled_settled = [
            b for b in engine._bets if b.settled and engine._is_bet_executable(b)
        ]
        missed_settled = [
            b for b in engine._bets if b.settled and not engine._is_bet_executable(b)
        ]
        filled = [
            b for b in engine._bets if getattr(b, "order_status", "") == "filled"
        ]
        deployed = sum(engine._filled_notional(b) for b in filled)
        reserved = sum(
            (b.stake or 0) for b in engine._bets
            if _is_exposure_counted_status(getattr(b, "order_status", ""))
        )
        shadow_order_diagnostics = engine._build_shadow_order_diagnostics()
        shadow_feature_diagnostics = engine._build_shadow_feature_diagnostics()
        current_state_edge_band_diagnostics = (
            engine._build_current_state_edge_band_diagnostics()
        )

        session = _build_live_session_payload(
            engine,
            filled_settled=filled_settled,
            missed_settled=missed_settled,
            filled=filled,
            deployed=deployed,
            reserved=reserved,
            shadow_order_diagnostics=shadow_order_diagnostics,
            shadow_feature_diagnostics=shadow_feature_diagnostics,
            current_state_edge_band_diagnostics=current_state_edge_band_diagnostics,
        )
        with open(engine._live_session_path, "w", encoding="utf-8") as f:
            json.dump(session, f, indent=2)
        engine._write_candidate_rollup()
        engine._last_session_save_ts = _signal_now_ts()
        engine._session_save_pending = False

        _emit_post_save_summary_logs(
            engine,
            filled_settled=filled_settled,
            missed_settled=missed_settled,
        )

        if force and engine._all_games_done():
            engine._log_shadow_order_diagnostics(shadow_order_diagnostics)
            engine._log_shadow_feature_diagnostics(shadow_feature_diagnostics)
            engine._log_current_state_edge_band_diagnostics(
                current_state_edge_band_diagnostics
            )

        if force:
            engine._log_runtime_debug_rollups(force=True)

    except Exception as exc:
        LOGGER.error("Failed to save session: %s", exc)


def _emit_post_save_summary_logs(
    engine: "LiveTradingEngine",
    *,
    filled_settled: list,
    missed_settled: list,
) -> None:
    """Per-game and order-lifecycle summary log lines.

    Runs every save so end-of-day state is visible in the log without
    opening the session JSON. Zero-trade days still get a game-final
    rollup so the day isn't invisible.
    """
    all_settled = filled_settled + missed_settled
    if all_settled or engine._open_orders:
        game_lines: dict = {}
        for b in all_settled:
            key = (b.away_abbrev, b.home_abbrev, b.game_pk)
            if key not in game_lines:
                game_lines[key] = {
                    "final": f"{b.final_away}-{b.final_home}" if b.final_away is not None else "?",
                    "total": b.final_total,
                    "bets": [],
                }
            result = "W" if b.won else ("L" if b.won is False else "?")
            fill = "FILL" if engine._is_bet_executable(b) else "MISS"
            profit_str = f"{b.profit:+.2f}" if b.profit is not None else "+0.00"
            game_lines[key]["bets"].append(
                f"O{b.line}({fill},{result},{profit_str})"
            )
        if game_lines:
            # [TR14] Order-lifecycle summary surfaces fill quality and wasted
            # budget reservation. A high never-filled rate usually means our
            # limit pricing is too passive or we're posting against stale
            # ltp/wide books.
            n_placed = len([b for b in engine._bets if getattr(b, "order_id", None)])
            n_filled = sum(
                1 for b in engine._bets if getattr(b, "order_status", "") == "filled"
            )
            n_never_filled = sum(
                1 for b in engine._bets
                if getattr(b, "cancel_reason", "") == "game_final"
                and getattr(b, "fill_price", None) is None
            )
            n_other_cancel = sum(
                1 for b in engine._bets
                if getattr(b, "order_status", "") == "cancelled"
                and getattr(b, "cancel_reason", "") != "game_final"
            )
            fill_rate = (n_filled / n_placed) if n_placed > 0 else 0.0
            reserved_on_misses = sum(
                float(getattr(b, "stake", 0.0) or 0.0)
                for b in engine._bets
                if getattr(b, "cancel_reason", "") == "game_final"
                and getattr(b, "fill_price", None) is None
            )
            LOGGER.info(
                "=== ORDER LIFECYCLE SUMMARY (%s) -- placed=%d filled=%d (%.0f%%) "
                "never_filled=%d other_cancel=%d reserved_on_misses=$%.2f ===",
                engine.date_str, n_placed, n_filled, fill_rate * 100.0,
                n_never_filled, n_other_cancel, reserved_on_misses,
            )

            LOGGER.info("=== GAME SUMMARY (%s) ===", engine.date_str)
            for (away, home, _), gdata in sorted(game_lines.items()):
                bets_str = "  ".join(gdata["bets"])
                total_str = f"total={gdata['total']}" if gdata["total"] else "final=?"
                LOGGER.info(
                    "  %s@%s  final=%s (%s) | %s",
                    away, home, gdata["final"], total_str, bets_str,
                )
    elif not engine._bets and engine.games:
        # Zero-trade day: log finalized game outcomes so the day is visible
        # in logs without opening the session JSON.
        final_games = [
            (gpk, g) for gpk, g in sorted(engine.games.items())
            if g.is_final()
            and g.score.away is not None
            and g.score.home is not None
        ]
        if final_games:
            listed_line_count = 0
            listed_over_count = 0
            for _, g in final_games:
                match = engine.matches.get(g.game_pk)
                if not match:
                    continue
                total = g.score.away + g.score.home
                for market in getattr(match, "markets", []) or []:
                    try:
                        line_value = float(market.line)
                    except Exception:
                        continue
                    listed_line_count += 1
                    if total > line_value:
                        listed_over_count += 1
            LOGGER.info(
                "=== GAME SUMMARY (%s) -- 0 bets placed | "
                "%d games final | %d/%d listed lines went over ===",
                engine.date_str, len(final_games),
                listed_over_count, listed_line_count,
            )
            for _, g in final_games:
                total = g.score.away + g.score.home
                LOGGER.info(
                    "  %s@%s  final=%d-%d (total=%d) | no bet",
                    g.away_abbrev, g.home_abbrev,
                    g.score.away, g.score.home, total,
                )
