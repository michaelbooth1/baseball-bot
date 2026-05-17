"""inventory_tracker.py -- Per-game net exposure tracking.

Phase C C2 (2026-05-17). Foundation for the two-sided quote engine
(Phase C C1) which needs to know, at decision time, how much OVER or
UNDER inventory we already hold on a given game so it can shade quotes
away from the over-exposed side.

Today the live engine only buys OVER, so inventory is implicitly 0 or
+N_over_shares per game. As Phase C ships UNDER trading, this module
becomes the authoritative net-exposure source.

This module is READ-ONLY in Phase C shadow mode: it computes
inventory from the existing `live_orders_ledger.jsonl` (real placed
bets) and an optional in-memory open-orders snapshot. Shadow quotes
that the quote engine WOULD have posted do NOT mutate the inventory
tracker -- if they did, the shadow output would be self-confounded
("the shadow inventory built up because the shadow quoted").

Inventory accounting:
  - For each filled bet that is NOT yet settled: add fill_size shares
    on its side. (Filled+settled = position resolved; subtracted out.)
  - For each placed-but-not-yet-filled open order: add stake/limit_price
    shares on its side. (At-risk capital; matters for inventory-shading
    even before fill.)

Net inventory per game = open_over_shares - open_under_shares
                       + filled_over_shares - filled_under_shares

(All "open" and "filled" totals are NET of settled bets.)

External API:
  - InventorySnapshot dataclass (per-game state)
  - build_inventory_snapshot(ledger_rows, open_orders=()) -> Dict[int, InventorySnapshot]
  - net_inventory_for_game(snapshot, game_pk) -> float
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


LOGGER = logging.getLogger("inventory_tracker")


@dataclass
class GameInventoryRow:
    """Per-game inventory state. All share counts are non-negative;
    the side is encoded in the field name, not the sign."""
    game_pk: int
    filled_over_shares: float = 0.0
    filled_under_shares: float = 0.0
    open_over_shares: float = 0.0
    open_under_shares: float = 0.0
    n_filled_open: int = 0       # filled bets not yet settled
    n_orders_open: int = 0       # placed orders not yet filled
    last_event_ts: str = ""      # most recent ledger row that touched this game

    @property
    def net_over_shares(self) -> float:
        """Net OVER exposure. Positive when long Over."""
        return (
            self.filled_over_shares + self.open_over_shares
            - self.filled_under_shares - self.open_under_shares
        )

    @property
    def total_filled_shares(self) -> float:
        return self.filled_over_shares + self.filled_under_shares

    @property
    def total_open_shares(self) -> float:
        return self.open_over_shares + self.open_under_shares

    def to_dict(self) -> Dict[str, Any]:
        return {
            "game_pk": self.game_pk,
            "filled_over_shares": round(self.filled_over_shares, 4),
            "filled_under_shares": round(self.filled_under_shares, 4),
            "open_over_shares": round(self.open_over_shares, 4),
            "open_under_shares": round(self.open_under_shares, 4),
            "net_over_shares": round(self.net_over_shares, 4),
            "n_filled_open": self.n_filled_open,
            "n_orders_open": self.n_orders_open,
            "last_event_ts": self.last_event_ts,
        }


@dataclass
class InventorySnapshot:
    """Whole-account inventory across all games at one point in time."""
    by_game: Dict[int, GameInventoryRow] = field(default_factory=dict)
    n_ledger_rows: int = 0
    n_skipped: int = 0
    generated_at_utc: str = ""

    def for_game(self, game_pk: int) -> GameInventoryRow:
        """Return the inventory row for game_pk, or a zeroed row if
        the game has no recorded position."""
        if game_pk in self.by_game:
            return self.by_game[game_pk]
        return GameInventoryRow(game_pk=game_pk)

    def net_inventory_for_game(self, game_pk: int) -> float:
        """Net OVER share exposure for a single game. Positive when
        long Over, negative when long Under, 0 when flat / unknown."""
        return self.for_game(game_pk).net_over_shares

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at_utc": self.generated_at_utc,
            "n_ledger_rows": self.n_ledger_rows,
            "n_skipped": self.n_skipped,
            "n_games_with_inventory": len(self.by_game),
            "by_game": {
                str(gpk): row.to_dict() for gpk, row in sorted(self.by_game.items())
            },
        }


def _safe_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> Optional[int]:
    f = _safe_float(v)
    return None if f is None else int(f)


def _row_event(row: Dict[str, Any]) -> str:
    """Stable event tag with backward-compatible fallback to
    `order_status` for legacy rows that predate the `_event` field."""
    return str(row.get("_event") or row.get("order_status") or "").lower()


def _row_side(row: Dict[str, Any]) -> str:
    return str(row.get("side") or "over").lower()


def _fill_shares(row: Dict[str, Any]) -> float:
    """Returns shares filled. Prefers `fill_size`, falls back to
    `filled_shares`, finally to stake/fill_price arithmetic when both
    are absent. Returns 0.0 when nothing computable.
    """
    n = _safe_float(row.get("fill_size"))
    if n is not None and n > 0:
        return n
    n = _safe_float(row.get("filled_shares"))
    if n is not None and n > 0:
        return n
    stake = _safe_float(row.get("stake"))
    price = _safe_float(row.get("actual_fill_price")) or _safe_float(row.get("fill_price"))
    if stake is not None and price is not None and price > 0:
        return stake / price
    return 0.0


def _open_order_shares(row: Dict[str, Any]) -> float:
    """For a placed-but-not-filled order, shares-at-risk = stake / limit_price.
    Falls back to entry_ask when limit_price absent (the engine's
    typical limit is at or below entry_ask)."""
    stake = _safe_float(row.get("stake"))
    price = (
        _safe_float(row.get("limit_price"))
        or _safe_float(row.get("posted_limit"))
        or _safe_float(row.get("entry_ask"))
    )
    if stake is not None and price is not None and price > 0:
        return stake / price
    return 0.0


def _latest_event_per_bet(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Collapse the per-bet event stream to one row per bet_id =
    the LAST-written event (most recent state). Stable tie-break on
    ts for replay determinism. The ledger is append-only with one
    row per state change, so "last row per bet" = current state.
    """
    latest: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        bet_id = str(row.get("bet_id") or "")
        if not bet_id:
            continue
        prev = latest.get(bet_id)
        if prev is None:
            latest[bet_id] = row
            continue
        # Prefer the row with the later ts; ledger order tie-breaks.
        prev_ts = str(prev.get("ts") or prev.get("placed_at") or "")
        cur_ts = str(row.get("ts") or row.get("placed_at") or "")
        if cur_ts >= prev_ts:
            latest[bet_id] = row
    return latest


def build_inventory_snapshot(
    ledger_rows: Iterable[Dict[str, Any]],
    *,
    open_orders: Iterable[Dict[str, Any]] = (),
    generated_at_utc: str = "",
) -> InventorySnapshot:
    """Build a fresh per-game inventory snapshot.

    `ledger_rows`: rows from `live_orders_ledger.jsonl` (one per
    state transition per bet). The function collapses to one row
    per bet (the latest event) before computing inventory.

    `open_orders`: optional rows representing orders the engine
    currently has open in memory but which may not yet appear in the
    ledger (e.g., just placed, not yet ack'd). Each row is treated
    as a placed-but-not-filled order. Pass an empty iterable when
    open orders aren't available -- inventory falls back to
    ledger-only accounting.

    Settled bets are excluded from BOTH filled and open totals.
    Cancelled bets are excluded. Error bets are excluded.
    """
    snapshot = InventorySnapshot(generated_at_utc=generated_at_utc)
    rows = list(ledger_rows)
    snapshot.n_ledger_rows = len(rows)

    latest = _latest_event_per_bet(rows)
    for bet_id, row in latest.items():
        game_pk = _safe_int(row.get("game_pk"))
        if game_pk is None:
            snapshot.n_skipped += 1
            continue
        event = _row_event(row)
        # Skip terminal states that closed out the position.
        if event in {"settled", "cancelled", "missed", "expired", "error"}:
            continue
        side = _row_side(row)
        ts = str(row.get("ts") or row.get("placed_at") or "")

        # Compute shares BEFORE touching by_game so a zero-share row
        # doesn't materialize an empty game entry.
        if event in {"filled", "reconciled_filled"}:
            shares = _fill_shares(row)
            is_fill = True
        else:
            shares = _open_order_shares(row)
            is_fill = False
        if shares <= 0:
            snapshot.n_skipped += 1
            continue

        gi = snapshot.by_game.setdefault(
            game_pk, GameInventoryRow(game_pk=game_pk)
        )
        if ts > gi.last_event_ts:
            gi.last_event_ts = ts

        if is_fill:
            if side == "under":
                gi.filled_under_shares += shares
            else:
                gi.filled_over_shares += shares
            gi.n_filled_open += 1
        else:
            # Non-terminal, non-filled = open order. Legacy rows lack
            # `_event` and just carry `order_status` -- _row_event
            # already normalises that fallback.
            if side == "under":
                gi.open_under_shares += shares
            else:
                gi.open_over_shares += shares
            gi.n_orders_open += 1

    # In-memory open orders (not yet in ledger). Add on top.
    for row in open_orders:
        game_pk = _safe_int(row.get("game_pk"))
        if game_pk is None:
            continue
        side = _row_side(row)
        shares = _open_order_shares(row)
        if shares <= 0:
            continue
        gi = snapshot.by_game.setdefault(
            game_pk, GameInventoryRow(game_pk=game_pk)
        )
        if side == "under":
            gi.open_under_shares += shares
        else:
            gi.open_over_shares += shares
        gi.n_orders_open += 1

    return snapshot


def load_ledger_rows(path: Path) -> List[Dict[str, Any]]:
    """Read `live_orders_ledger.jsonl`. Best-effort -- malformed
    lines are skipped silently (this is research data, not a
    contract; corruption shouldn't crash the snapshot)."""
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError as exc:
        LOGGER.warning("Failed to read ledger %s: %s", path, exc)
    return rows


def net_inventory_for_game(
    snapshot: InventorySnapshot, game_pk: int
) -> float:
    """Convenience accessor used by the quote engine."""
    return snapshot.net_inventory_for_game(game_pk)
