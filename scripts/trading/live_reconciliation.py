"""live_reconciliation.py -- Orphan-fill detection via Polymarket data-api.

Background
----------
The CLOB SDK's ``get_order`` and ``get_trades`` calls filter by ``maker_address``,
which silently misses any fill recorded under a different identity (taker-side
fills, post-resolution lookups where the index was cleared, etc.). On 2026-05-10
the bot logged ``MISSED  profit=+0.00`` for MIN@CLE Over 7.5 even though the
share showed up in the wallet at the bot's exact limit price (0.79). Both
``get_order`` (returned ``live`` for the entire game) and ``get_trades_for_order``
(returned 0 rows) failed to surface the fill.

This module is the safety net. After the live engine's normal cancel /
recovery paths run, we ask the public Polymarket data-api the only question
that matters: *does the wallet currently own shares of this token?* If yes,
and we have a bet for that token marked anything other than filled, the fill
is orphaned -- we patch the LiveBetRecord, re-settle, and append a corrected
ledger row.

Designed to fail open: any data-api error logs a warning and leaves the
existing ledger untouched. Reconciliation never blocks shutdown.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from models import bet_traded_token_id

if TYPE_CHECKING:
    from live_engine import LiveTradingEngine
    from models import LiveBetRecord

LOGGER = logging.getLogger("live_reconciliation")

# Positions smaller than this are treated as floating-point noise / dust from
# rounding, not real shares the bet should claim.
MIN_RECONCILABLE_SHARES = 0.01

# Statuses that indicate the engine believes the order did not fill. These are
# the candidates for orphan-fill recovery.
UNFILLED_STATUSES = frozenset({"cancelled", "canceled", "expired", "shutdown", "missed", "error"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _placed_at_ts(bet: "LiveBetRecord") -> Optional[float]:
    raw = getattr(bet, "order_placed_at", None) or getattr(bet, "placed_at", None)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).rstrip("Z")).replace(
            tzinfo=timezone.utc
        ).timestamp()
    except Exception:
        return None


def _bet_is_unfilled(bet: "LiveBetRecord") -> bool:
    """Return True when the engine recorded no fill for this bet.

    Treats both spellings of "cancelled" so the reconciler stays consistent
    even if the SDK starts returning the American spelling.
    """
    status = str(getattr(bet, "order_status", "") or "").strip().lower()
    if status == "filled":
        return False
    if status in UNFILLED_STATUSES:
        return True
    # Pending / live / unknown / blank -- treat as unfilled for reconciliation
    # purposes only when the bet has been settled (game already final). An
    # in-flight live order is the normal lifecycle, not an orphan.
    return bool(getattr(bet, "settled", False))


def _candidate_bets(engine: "LiveTradingEngine") -> List["LiveBetRecord"]:
    """Live bets that look unfilled but might actually have a position."""
    bets = list(getattr(engine, "_bets", []) or [])
    out: List["LiveBetRecord"] = []
    for bet in bets:
        if not getattr(bet, "order_id", None):
            continue
        if not bet_traded_token_id(bet):
            continue
        if _bet_is_unfilled(bet):
            out.append(bet)
    return out


def _patch_bet_with_orphan_fill(
    bet: "LiveBetRecord",
    *,
    fill_price: float,
    fill_size: float,
    fill_timestamp_iso: str,
    source: str,
    trade_id: Optional[str] = None,
) -> None:
    """Mutate a LiveBetRecord so it looks like the fill we discovered.

    Mirrors the field set written by ``check_open_orders`` /
    ``try_recover_fill``. Settlement (won / profit) is left to the existing
    ``_settle_finished_games`` path; this only marks the order as filled with
    realized cost so the next save + ledger append carries correct numbers.
    """
    bet.order_status = "filled"
    bet.fill_price = fill_price
    bet.fill_size = fill_size
    bet.fill_cost = round(float(fill_size) * float(fill_price), 2)
    bet.filled_at = fill_timestamp_iso
    bet.actual_fill_price = fill_price
    bet.filled_shares = float(fill_size)
    bet.fill_cost_usdc = bet.fill_cost
    bet.cancel_reason = ""  # no longer cancelled
    bet.cancelled_at = None
    # Record provenance so the reconciled lifecycle row is auditable.
    setattr(bet, "reconciliation_source", source)
    if trade_id:
        setattr(bet, "reconciliation_trade_id", trade_id)


def _resolve_orphan_via_trades(
    engine: "LiveTradingEngine",
    bet: "LiveBetRecord",
) -> Optional[Tuple[float, float, str, Optional[str]]]:
    """Look up the actual fill price/size/time via the data-api trade history.

    Returns ``(price, size, ts_iso, trade_id)`` or ``None`` if no usable trade
    found. Falls back gracefully when the wallet-keyed trade-history endpoint
    is unreachable -- the caller can still synthesize a fill from the
    position size and the bet's limit price.
    """
    clob = getattr(engine, "_clob", None)
    if clob is None or not hasattr(clob, "get_user_trades_for_market"):
        return None
    after_ts = _placed_at_ts(bet)
    trades = clob.get_user_trades_for_market(bet_traded_token_id(bet), after_ts=after_ts)
    if not trades:
        return None
    # Prefer trades at our limit price or better; otherwise the largest one.
    limit_price = float(getattr(bet, "limit_price", 0.0) or 0.0)
    sized: List[Tuple[float, dict]] = []
    for t in trades:
        try:
            price = float(t.get("price"))
            size = float(t.get("size"))
        except (TypeError, ValueError):
            continue
        if price <= 0 or size <= 0:
            continue
        # On a BUY order, an executed price <= limit_price is a valid fill.
        if limit_price > 0 and price > limit_price + 1e-6:
            continue
        sized.append((size, t))
    if not sized:
        return None
    _, match = max(sized, key=lambda kv: kv[0])
    price = float(match.get("price"))
    size = float(match.get("size"))
    ts_iso = str(match.get("timestamp_iso") or _now_iso())
    return price, size, ts_iso, match.get("trade_id")


def _apply_orphan_fill(
    engine: "LiveTradingEngine",
    bet: "LiveBetRecord",
    *,
    price: float,
    size: float,
    ts_iso: str,
    source: str,
    trade_id: Optional[str],
    token_id: str,
    summary: Dict[str, int],
) -> bool:
    """Patch ``bet`` with the discovered fill, log loudly, and append a
    ``reconciled_filled`` lifecycle row to the ledger. Returns True on
    success, False on any error (errors are counted into ``summary``)."""
    LOGGER.warning(
        "ORPHAN FILL RECONCILED [%s] order_id=%s  token=%s..%s  "
        "fill_price=%.4f  size=%.4f  source=%s  trade_id=%s -- "
        "engine had marked order_status=%s",
        bet.bet_id, getattr(bet, "order_id", "?"),
        token_id[:10], token_id[-6:],
        price, size, source, trade_id or "n/a",
        getattr(bet, "order_status", "?"),
    )

    _patch_bet_with_orphan_fill(
        bet,
        fill_price=price,
        fill_size=size,
        fill_timestamp_iso=ts_iso,
        source=source,
        trade_id=trade_id,
    )
    try:
        row = asdict(bet)
        row["_event"] = "reconciled_filled"
        row["reconciliation_source"] = source
        if trade_id:
            row["reconciliation_trade_id"] = trade_id
        writer = getattr(engine, "_write_live_ledger_row", None)
        if writer is not None:
            writer(row)
    except Exception as exc:
        LOGGER.warning(
            "Reconciliation ledger append failed [%s]: %s", bet.bet_id, exc,
        )
        summary["errors"] += 1
        return False
    return True


def reconcile_orphan_fills(engine: "LiveTradingEngine") -> Dict[str, int]:
    """Walk current LiveBetRecords; recover any fill the engine missed.

    Two-pass strategy:

      Pass 1 (position-based, fast). Snapshot the wallet's open positions
      (one HTTP call). For each unfilled bet whose token shows up in the
      snapshot, locate the matching trade row (or synthesize at limit
      price), patch the bet, and append a ``reconciled_filled`` ledger
      row. Catches WINNING orphan fills (shares still held).

      Pass 2 (trade-history, 2026-06-07). For each remaining unfilled
      candidate, hit the data-api ``/trades`` endpoint scoped to its
      token + post-placement timestamp. This catches LOSING orphan
      fills -- the case the position check is structurally blind to:
      order filled, game resolved against us, position auto-resolved
      to $0, wallet no longer holds the token. Without pass 2 every
      losing-side race-condition fill stays silent (real money lost,
      ``profit=$0`` in the ledger). 2026-06-06 CWS@PHI 11.5 was the
      flagship incident that motivated this; see promotion_events.jsonl
      lever=``orphan_fill_recovery``.

    Returns a small counter dict for the caller's summary log:
        {"checked": N, "orphans": N, "errors": N,
         "orphans_position_only": N, "orphans_trade_only": N}
    """
    summary: Dict[str, int] = {
        "checked": 0,
        "orphans": 0,
        "errors": 0,
        "orphans_position_only": 0,
        "orphans_trade_only": 0,
    }

    candidates = _candidate_bets(engine)
    if not candidates:
        return summary
    summary["checked"] = len(candidates)

    clob = getattr(engine, "_clob", None)
    if clob is None or not hasattr(clob, "get_user_positions"):
        LOGGER.debug(
            "Reconciliation skipped: CLOB client missing or does not expose "
            "get_user_positions; %d unfilled bet(s) unchecked.",
            len(candidates),
        )
        return summary

    positions = clob.get_user_positions()
    # Track which candidates pass 1 already reconciled, so pass 2 doesn't
    # double-process. Identity by bet_id (stable across the function).
    reconciled_ids: set = set()

    # ---- Pass 1: position-based ----
    if positions is None:
        # Best-effort: data-api unreachable. Already warned by the client.
        # Don't abort; pass 2 still has a chance via the /trades endpoint.
        summary["errors"] += 1
        positions = {}

    if positions:
        LOGGER.info(
            "Reconciliation pass 1 (position-based): checking %d unfilled bet(s) "
            "against %d wallet position(s).",
            len(candidates), len(positions),
        )

    for bet in candidates:
        try:
            token_id = str(bet_traded_token_id(bet))
            held = float(positions.get(token_id, 0.0) or 0.0)
            if held < MIN_RECONCILABLE_SHARES:
                continue

            recovered = _resolve_orphan_via_trades(engine, bet)
            if recovered is not None:
                price, size, ts_iso, trade_id = recovered
                source = "data_api_trades"
            else:
                price = float(getattr(bet, "limit_price", 0.0) or 0.0)
                if price <= 0:
                    LOGGER.warning(
                        "Reconciliation: position found for [%s] token=%s..%s but "
                        "no trade detail and bet has no limit_price -- cannot patch.",
                        bet.bet_id, token_id[:10], token_id[-6:],
                    )
                    summary["errors"] += 1
                    continue
                size = held
                ts_iso = _now_iso()
                trade_id = None
                source = "data_api_position_only"

            ok = _apply_orphan_fill(
                engine, bet,
                price=price, size=size, ts_iso=ts_iso,
                source=source, trade_id=trade_id, token_id=token_id,
                summary=summary,
            )
            if ok:
                summary["orphans"] += 1
                summary["orphans_position_only"] += 1
                reconciled_ids.add(getattr(bet, "bet_id", ""))
        except Exception as exc:
            LOGGER.warning(
                "Reconciliation pass 1 failed for bet [%s]: %s",
                getattr(bet, "bet_id", "?"), exc,
            )
            summary["errors"] += 1

    # ---- Pass 2: trade-history-only (catches LOSING orphans) ----
    # Only candidates not already recovered by pass 1, and only if the
    # CLOB client exposes the per-market trades endpoint.
    if not hasattr(clob, "get_user_trades_for_market"):
        LOGGER.debug(
            "Reconciliation pass 2 skipped: CLOB client does not expose "
            "get_user_trades_for_market.",
        )
    else:
        remaining = [
            b for b in candidates
            if getattr(b, "bet_id", "") not in reconciled_ids
        ]
        if remaining:
            LOGGER.info(
                "Reconciliation pass 2 (trade-history): checking %d remaining "
                "unfilled bet(s) for losing-side orphan fills.",
                len(remaining),
            )
        for bet in remaining:
            try:
                token_id = str(bet_traded_token_id(bet))
                recovered = _resolve_orphan_via_trades(engine, bet)
                if recovered is None:
                    continue
                price, size, ts_iso, trade_id = recovered
                # Tag distinctly so the daily-review reconciler_summary
                # block can break out losing-side recoveries from the
                # standard wallet-position path.
                ok = _apply_orphan_fill(
                    engine, bet,
                    price=price, size=size, ts_iso=ts_iso,
                    source="data_api_trades_only",
                    trade_id=trade_id, token_id=token_id,
                    summary=summary,
                )
                if ok:
                    summary["orphans"] += 1
                    summary["orphans_trade_only"] += 1
            except Exception as exc:
                LOGGER.warning(
                    "Reconciliation pass 2 failed for bet [%s]: %s",
                    getattr(bet, "bet_id", "?"), exc,
                )
                summary["errors"] += 1

    if summary["orphans"] > 0:
        LOGGER.warning(
            "Reconciliation summary: checked=%d orphans_recovered=%d "
            "(position=%d trade_only=%d) errors=%d -- "
            "see data/live_trading/live_orders_ledger.jsonl for `reconciled_filled` rows.",
            summary["checked"], summary["orphans"],
            summary["orphans_position_only"], summary["orphans_trade_only"],
            summary["errors"],
        )
    else:
        LOGGER.info(
            "Reconciliation summary: checked=%d orphans_recovered=0 errors=%d.",
            summary["checked"], summary["errors"],
        )

    return summary
