#!/usr/bin/env python3
"""
Build a paper policy ledger for no-score drift shadow candidates.

This is an offline research tool for the state-value objective. It extends the
shadow no-score drift evaluator into an order-like paper ledger with:
  - daily budget accounting
  - per-game and per-game-line caps
  - first-candidate-per-score-segment dedup
  - explicit fill/price assumptions
  - USDC/share/payout/profit fields matching live settlement math

It does not place live orders and does not change gate behavior.

Inputs:
  data/live_trading/candidate_universe/*_candidates.jsonl
  data/live_trading/candidate_universe/*_outcomes.jsonl
  data/paper_trading/candidate_universe/*_candidates.jsonl
  data/paper_trading/candidate_universe/*_outcomes.jsonl

Outputs:
  data/analysis_output/no_score_drift_paper_ledger/
    no_score_drift_paper_ledger_summary.json
    no_score_drift_paper_ledger_rows.jsonl
    no_score_drift_paper_ledger_rows.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import evaluate_no_score_drift_policy as nsd


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "no_score_drift_paper_ledger"

DEFAULT_STAKE_USDC = 10.0
DEFAULT_DAILY_BUDGET_USDC = 80.0
DEFAULT_PER_GAME_BUDGET_FRACTION = 0.40
DEFAULT_MAX_ORDERS_PER_GAME = 2
DEFAULT_MAX_ORDERS_PER_GAME_LINE = 1
DEFAULT_TOUCH_WINDOW_SECONDS = 900.0
DEFAULT_PRICE_OFFSET_CENTS = 1.0

DEFAULT_SUPPORT_REGIMES = (
    "poisson_and_empirical_support",
    "poisson_only_support",
    "empirical_only_support",
)
POLICY_VARIANT_SUPPORT_REGIMES = {
    "both_only": ("poisson_and_empirical_support",),
    "empirical_or_both": (
        "poisson_and_empirical_support",
        "empirical_only_support",
    ),
    "poisson_only": ("poisson_only_support",),
}

LEDGER_COLUMNS = [
    "ledger_id",
    "paper_order_id",
    "mode",
    "session_date",
    "ts",
    "candidate_id",
    "dedup_key",
    "duplicate_candidate_rows",
    "decision",
    "skip_reason",
    "support_regime",
    "shadow_no_score_drift_trigger",
    "signal_model_family",
    "game_pk",
    "away_abbrev",
    "home_abbrev",
    "line",
    "inning",
    "inning_state",
    "outs",
    "runners_on",
    "away_score_before",
    "home_score_before",
    "current_total",
    "home_leading_late",
    "batting_team_is_home",
    "bottom9_available_if_needed",
    "expected_remaining_half_innings",
    "expected_remaining_pa_bucket",
    "home_skip_bottom9_risk",
    *nsd.SCORING_PATH_FIELD_KEYS,
    *nsd.WEATHER_FEATURE_FIELD_KEYS,
    "shadow_bottom9_home_lead_context",
    "shadow_home_skip_bottom9_risk_bucket",
    "shadow_no_score_poisson_edge_bucket",
    "shadow_no_score_empirical_edge_bucket",
    "shadow_no_score_ask_bucket",
    "shadow_no_score_drawdown_bucket",
    "shadow_no_score_poisson_empirical_ask_drawdown_bucket",
    "score_segment_key",
    "score_segment_age_secs",
    "score_segment_ticks",
    "score_segment_drawdown",
    "price_policy",
    "fill_assumption",
    "decision_ask",
    "best_bid",
    "spread",
    "limit_price",
    "fill_price",
    "filled",
    "fill_source",
    "fill_seconds",
    "fill_ts",
    "settled",
    "over_hit",
    "final_total",
    "final_away",
    "final_home",
    "stake_usdc",
    "filled_shares",
    "fill_cost_usdc",
    "payout_usdc",
    "profit_usdc",
    "roi",
    "daily_budget_usdc",
    "daily_spent_before",
    "daily_reserved_before",
    "daily_committed_before",
    "daily_spent_after_submit",
    "daily_reserved_after_submit",
    "daily_committed_after_submit",
    "game_budget_usdc",
    "game_committed_before",
    "game_committed_after_submit",
    "game_line_open_or_filled_before",
    "game_open_or_filled_before",
    "fair_value",
    "edge",
    "current_state_value_edge",
    "current_state_value_empirical_edge",
    "shadow_inning_runs_needed_bucket",
    "shadow_current_phantom_combo_bucket",
]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build no-score drift paper policy ledger.")
    p.add_argument("--mode", choices=["live", "paper", "both"], default="live")
    p.add_argument("--min-date", type=str, default="", help="Inclusive YYYY-MM-DD.")
    p.add_argument("--max-date", type=str, default="", help="Inclusive YYYY-MM-DD.")
    p.add_argument("--stake", type=float, default=DEFAULT_STAKE_USDC)
    p.add_argument("--daily-budget", type=float, default=DEFAULT_DAILY_BUDGET_USDC)
    p.add_argument(
        "--per-game-budget-fraction",
        type=float,
        default=DEFAULT_PER_GAME_BUDGET_FRACTION,
        help=f"Per-game committed budget as fraction of daily budget (default: {DEFAULT_PER_GAME_BUDGET_FRACTION}).",
    )
    p.add_argument("--max-orders-per-game", type=int, default=DEFAULT_MAX_ORDERS_PER_GAME)
    p.add_argument("--max-orders-per-game-line", type=int, default=DEFAULT_MAX_ORDERS_PER_GAME_LINE)
    p.add_argument(
        "--support-regimes",
        type=str,
        default=",".join(DEFAULT_SUPPORT_REGIMES),
        help="Comma-separated support regimes allowed to submit paper orders.",
    )
    p.add_argument(
        "--price-policy",
        choices=["taker", "ask_minus_cents", "bid_plus_cents"],
        default="taker",
        help="Paper order price policy (default: taker at decision_ask).",
    )
    p.add_argument(
        "--fill-assumption",
        choices=["immediate", "touch_within_segment"],
        default="immediate",
        help="For non-taker price policies, how resting fills are simulated.",
    )
    p.add_argument("--price-offset-cents", type=float, default=DEFAULT_PRICE_OFFSET_CENTS)
    p.add_argument("--touch-window-seconds", type=float, default=DEFAULT_TOUCH_WINDOW_SECONDS)
    p.add_argument("--min-poisson-edge", type=float, default=nsd.DEFAULT_MIN_POISSON_EDGE)
    p.add_argument("--min-empirical-edge", type=float, default=nsd.DEFAULT_MIN_EMPIRICAL_EDGE)
    p.add_argument("--include-unsettled", action="store_true")
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--strict", action="store_true", help="Fail if no ledger rows are produced.")
    return p.parse_args(argv)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        if math.isnan(out):
            return None
        return out
    except Exception:
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def _safe_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"true", "1", "yes", "y"}:
        return True
    if s in {"false", "0", "no", "n"}:
        return False
    return None


def _parse_ts(raw: Any) -> Optional[float]:
    if raw in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).timestamp()


def _round_price(price: float) -> float:
    return round(min(0.99, max(0.01, price)), 2)


def _round_money(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 2)


def _dedup_key_from_segment(row: Dict[str, Any]) -> str:
    return "|".join(str(part) for part in nsd.segment_key(row))


def _sort_key(row: Dict[str, Any]) -> Tuple[str, str]:
    return (
        str(row.get("ts") or row.get("recorded_at") or ""),
        str(row.get("candidate_id") or ""),
    )


def _line_key(value: Any) -> str:
    return nsd._line_key(value)  # Reuse evaluator formatting for exact joins.


def _policy_game_key(row: Dict[str, Any]) -> Tuple[str, str, int]:
    return (
        str(row.get("mode") or ""),
        str(row.get("session_date") or ""),
        int(_safe_int(row.get("game_pk")) or -1),
    )


def _policy_game_line_key(row: Dict[str, Any]) -> Tuple[str, str, int, str]:
    mode, date, game_pk = _policy_game_key(row)
    return mode, date, game_pk, _line_key(row.get("line"))


def _allowed_support_regimes(raw: str) -> Tuple[str, ...]:
    regimes = tuple(part.strip() for part in raw.split(",") if part.strip())
    return regimes or DEFAULT_SUPPORT_REGIMES


def _build_segment_traces(candidates: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    traces: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        if not nsd.is_no_score_drift_candidate(row):
            continue
        traces[_dedup_key_from_segment(row)].append(row)
    for rows in traces.values():
        rows.sort(key=_sort_key)
    return traces


def _price_for_policy(row: Dict[str, Any], *, price_policy: str, offset_cents: float) -> Tuple[Optional[float], str]:
    ask = _safe_float(row.get("decision_ask"))
    bid = _safe_float(row.get("best_bid"))
    offset = max(0.0, offset_cents) / 100.0

    if ask is None or ask <= 0 or ask >= 1:
        return None, "invalid_decision_ask"
    if price_policy == "taker":
        return _round_price(ask), ""
    if price_policy == "ask_minus_cents":
        return _round_price(ask - offset), ""
    if price_policy == "bid_plus_cents":
        if bid is None or bid <= 0 or bid >= 1:
            return None, "invalid_best_bid"
        return _round_price(min(ask, bid + offset)), ""
    return None, "unknown_price_policy"


def _find_touch_fill(
    row: Dict[str, Any],
    segment_traces: Dict[str, List[Dict[str, Any]]],
    *,
    limit_price: float,
    touch_window_seconds: float,
) -> Tuple[bool, Optional[float], Optional[str], str]:
    ask0 = _safe_float(row.get("decision_ask"))
    start_ts = _parse_ts(row.get("ts"))
    if ask0 is not None and limit_price >= ask0:
        return True, 0.0, row.get("ts"), "marketable_at_entry"

    if start_ts is None:
        return False, None, None, "missing_start_ts"

    horizon = start_ts + max(0.0, touch_window_seconds)
    trace = segment_traces.get(str(row.get("dedup_key") or ""), [])
    for tick in trace:
        tick_ts = _parse_ts(tick.get("ts"))
        if tick_ts is None or tick_ts < start_ts or tick_ts > horizon:
            continue
        ask = _safe_float(tick.get("decision_ask"))
        if ask is not None and ask <= limit_price:
            return True, round(tick_ts - start_ts, 3), tick.get("ts"), "segment_touch"
    return False, None, None, "not_touched_within_segment"


@dataclass
class OpenPaperOrder:
    fill_ts: Optional[float]
    expire_ts: float
    filled: bool
    stake_usdc: float
    fill_cost_usdc: Optional[float]
    game_key: Tuple[str, str, int]
    game_line_key: Tuple[str, str, int, str]


@dataclass
class PaperBudgetState:
    daily_budget_usdc: float
    per_game_budget_fraction: float
    daily_spent: Dict[Tuple[str, str], float] = field(default_factory=lambda: defaultdict(float))
    daily_reserved: Dict[Tuple[str, str], float] = field(default_factory=lambda: defaultdict(float))
    game_spent: Dict[Tuple[str, str, int], float] = field(default_factory=lambda: defaultdict(float))
    game_reserved: Dict[Tuple[str, str, int], float] = field(default_factory=lambda: defaultdict(float))
    game_filled_count: Dict[Tuple[str, str, int], int] = field(default_factory=lambda: defaultdict(int))
    game_line_filled_count: Dict[Tuple[str, str, int, str], int] = field(default_factory=lambda: defaultdict(int))
    game_open_count: Dict[Tuple[str, str, int], int] = field(default_factory=lambda: defaultdict(int))
    game_line_open_count: Dict[Tuple[str, str, int, str], int] = field(default_factory=lambda: defaultdict(int))
    open_orders: List[OpenPaperOrder] = field(default_factory=list)

    def _day_key(self, row: Dict[str, Any]) -> Tuple[str, str]:
        return str(row.get("mode") or ""), str(row.get("session_date") or "")

    def release_until(self, now_ts: Optional[float]) -> None:
        if now_ts is None:
            return
        keep: List[OpenPaperOrder] = []
        for order in self.open_orders:
            done_ts = order.fill_ts if order.filled and order.fill_ts is not None else order.expire_ts
            if done_ts is None or done_ts > now_ts:
                keep.append(order)
                continue
            day_key = (order.game_key[0], order.game_key[1])
            self.daily_reserved[day_key] = max(0.0, self.daily_reserved[day_key] - order.stake_usdc)
            self.game_reserved[order.game_key] = max(0.0, self.game_reserved[order.game_key] - order.stake_usdc)
            self.game_open_count[order.game_key] = max(0, self.game_open_count[order.game_key] - 1)
            self.game_line_open_count[order.game_line_key] = max(0, self.game_line_open_count[order.game_line_key] - 1)
            if order.filled:
                cost = float(order.fill_cost_usdc or order.stake_usdc)
                self.daily_spent[day_key] += cost
                self.game_spent[order.game_key] += cost
                self.game_filled_count[order.game_key] += 1
                self.game_line_filled_count[order.game_line_key] += 1
        self.open_orders = keep

    def daily_committed(self, day_key: Tuple[str, str]) -> float:
        return self.daily_spent[day_key] + self.daily_reserved[day_key]

    def game_committed(self, game_key: Tuple[str, str, int]) -> float:
        return self.game_spent[game_key] + self.game_reserved[game_key]

    def game_open_or_filled(self, game_key: Tuple[str, str, int]) -> int:
        return self.game_filled_count[game_key] + self.game_open_count[game_key]

    def game_line_open_or_filled(self, game_line_key: Tuple[str, str, int, str]) -> int:
        return self.game_line_filled_count[game_line_key] + self.game_line_open_count[game_line_key]

    def can_submit(
        self,
        row: Dict[str, Any],
        *,
        stake_usdc: float,
        max_orders_per_game: int,
        max_orders_per_game_line: int,
    ) -> Tuple[bool, str, Dict[str, float]]:
        day_key = self._day_key(row)
        game_key = _policy_game_key(row)
        game_line_key = _policy_game_line_key(row)
        daily_committed = self.daily_committed(day_key)
        game_committed = self.game_committed(game_key)
        game_budget = self.daily_budget_usdc * self.per_game_budget_fraction
        game_count = self.game_open_or_filled(game_key)
        line_count = self.game_line_open_or_filled(game_line_key)

        snapshot = {
            "daily_spent_before": self.daily_spent[day_key],
            "daily_reserved_before": self.daily_reserved[day_key],
            "daily_committed_before": daily_committed,
            "game_budget_usdc": game_budget,
            "game_committed_before": game_committed,
            "game_open_or_filled_before": float(game_count),
            "game_line_open_or_filled_before": float(line_count),
        }
        if daily_committed + stake_usdc > self.daily_budget_usdc + 1e-9:
            return False, "daily_budget_exhausted", snapshot
        if game_committed + stake_usdc > game_budget + 1e-9:
            return False, "per_game_budget_exhausted", snapshot
        if max_orders_per_game > 0 and game_count >= max_orders_per_game:
            return False, "max_orders_per_game", snapshot
        if max_orders_per_game_line > 0 and line_count >= max_orders_per_game_line:
            return False, "max_orders_per_game_line", snapshot
        return True, "", snapshot

    def submit(
        self,
        row: Dict[str, Any],
        *,
        stake_usdc: float,
        filled: bool,
        fill_seconds: Optional[float],
        fill_cost_usdc: Optional[float],
        touch_window_seconds: float,
    ) -> Dict[str, float]:
        day_key = self._day_key(row)
        game_key = _policy_game_key(row)
        game_line_key = _policy_game_line_key(row)
        start_ts = _parse_ts(row.get("ts"))

        if filled and (fill_seconds is None or fill_seconds <= 0):
            cost = float(fill_cost_usdc or stake_usdc)
            self.daily_spent[day_key] += cost
            self.game_spent[game_key] += cost
            self.game_filled_count[game_key] += 1
            self.game_line_filled_count[game_line_key] += 1
        else:
            self.daily_reserved[day_key] += stake_usdc
            self.game_reserved[game_key] += stake_usdc
            self.game_open_count[game_key] += 1
            self.game_line_open_count[game_line_key] += 1
            expire_ts = (start_ts or 0.0) + max(0.0, touch_window_seconds)
            fill_ts = None
            if filled and fill_seconds is not None and start_ts is not None:
                fill_ts = start_ts + fill_seconds
            self.open_orders.append(OpenPaperOrder(
                fill_ts=fill_ts,
                expire_ts=expire_ts,
                filled=filled,
                stake_usdc=stake_usdc,
                fill_cost_usdc=fill_cost_usdc,
                game_key=game_key,
                game_line_key=game_line_key,
            ))

        return {
            "daily_spent_after_submit": self.daily_spent[day_key],
            "daily_reserved_after_submit": self.daily_reserved[day_key],
            "daily_committed_after_submit": self.daily_committed(day_key),
            "game_committed_after_submit": self.game_committed(game_key),
        }


def _settlement_fields(
    *,
    stake_usdc: float,
    fill_price: Optional[float],
    filled: bool,
    outcome_available: bool,
    over_hit: Optional[bool],
) -> Dict[str, Any]:
    if not filled or fill_price is None:
        return {
            "settled": bool(outcome_available),
            "filled_shares": None,
            "fill_cost_usdc": None,
            "payout_usdc": None,
            "profit_usdc": None,
            "roi": None,
        }
    shares = stake_usdc / fill_price
    cost = stake_usdc
    if not outcome_available or over_hit is None:
        return {
            "settled": False,
            "filled_shares": round(shares, 6),
            "fill_cost_usdc": round(cost, 2),
            "payout_usdc": None,
            "profit_usdc": None,
            "roi": None,
        }
    payout = shares if over_hit else 0.0
    profit = payout - cost
    return {
        "settled": True,
        "filled_shares": round(shares, 6),
        "fill_cost_usdc": round(cost, 2),
        "payout_usdc": round(payout, 6),
        "profit_usdc": round(profit, 2),
        "roi": round(profit / cost, 6) if cost > 0 else None,
    }


def build_ledger_rows(
    policy_rows: Sequence[Dict[str, Any]],
    *,
    raw_candidates: Sequence[Dict[str, Any]],
    allowed_support_regimes: Sequence[str] = DEFAULT_SUPPORT_REGIMES,
    stake_usdc: float = DEFAULT_STAKE_USDC,
    daily_budget_usdc: float = DEFAULT_DAILY_BUDGET_USDC,
    per_game_budget_fraction: float = DEFAULT_PER_GAME_BUDGET_FRACTION,
    max_orders_per_game: int = DEFAULT_MAX_ORDERS_PER_GAME,
    max_orders_per_game_line: int = DEFAULT_MAX_ORDERS_PER_GAME_LINE,
    price_policy: str = "taker",
    fill_assumption: str = "immediate",
    price_offset_cents: float = DEFAULT_PRICE_OFFSET_CENTS,
    touch_window_seconds: float = DEFAULT_TOUCH_WINDOW_SECONDS,
    include_unsettled: bool = False,
) -> List[Dict[str, Any]]:
    segment_traces = _build_segment_traces(raw_candidates)
    allowed = set(allowed_support_regimes)
    state = PaperBudgetState(
        daily_budget_usdc=daily_budget_usdc,
        per_game_budget_fraction=per_game_budget_fraction,
    )
    ledger_rows: List[Dict[str, Any]] = []

    for idx, row in enumerate(sorted(policy_rows, key=_sort_key), start=1):
        row = dict(row)
        row_ts = _parse_ts(row.get("ts"))
        state.release_until(row_ts)

        day_key = (str(row.get("mode") or ""), str(row.get("session_date") or ""))
        game_key = _policy_game_key(row)
        game_line_key = _policy_game_line_key(row)
        outcome_available = bool(row.get("outcome_available"))
        over_hit = _safe_bool(row.get("over_hit"))
        decision = "submitted"
        skip_reason = ""

        price, price_error = _price_for_policy(
            row,
            price_policy=price_policy,
            offset_cents=price_offset_cents,
        )
        can_submit, budget_reason, budget_snapshot = state.can_submit(
            row,
            stake_usdc=stake_usdc,
            max_orders_per_game=max_orders_per_game,
            max_orders_per_game_line=max_orders_per_game_line,
        )

        if row.get("support_regime") not in allowed:
            decision = "skipped"
            skip_reason = "support_regime_not_allowed"
        elif not outcome_available and not include_unsettled:
            decision = "skipped"
            skip_reason = "outcome_unavailable"
        elif price is None:
            decision = "skipped"
            skip_reason = price_error or "invalid_price"
        elif not can_submit:
            decision = "skipped"
            skip_reason = budget_reason

        filled = False
        fill_source = ""
        fill_seconds: Optional[float] = None
        fill_ts: Optional[str] = None
        fill_price: Optional[float] = None
        settlement = _settlement_fields(
            stake_usdc=stake_usdc,
            fill_price=None,
            filled=False,
            outcome_available=outcome_available,
            over_hit=over_hit,
        )
        after_submit = {
            "daily_spent_after_submit": state.daily_spent[day_key],
            "daily_reserved_after_submit": state.daily_reserved[day_key],
            "daily_committed_after_submit": state.daily_committed(day_key),
            "game_committed_after_submit": state.game_committed(game_key),
        }

        if decision == "submitted":
            ask = _safe_float(row.get("decision_ask"))
            if price_policy == "taker":
                filled = True
                fill_source = "taker_at_decision_ask"
                fill_seconds = 0.0
                fill_ts = row.get("ts")
                fill_price = price
            elif fill_assumption == "immediate":
                filled = bool(ask is not None and price is not None and price >= ask)
                fill_source = "marketable_at_entry" if filled else "not_marketable_immediate"
                if filled:
                    fill_seconds = 0.0
                    fill_ts = row.get("ts")
                    fill_price = min(price, ask) if ask is not None else price
            else:
                filled, fill_seconds, fill_ts, fill_source = _find_touch_fill(
                    row,
                    segment_traces,
                    limit_price=price,
                    touch_window_seconds=touch_window_seconds,
                )
                if filled:
                    fill_price = min(price, ask) if ask is not None and price >= ask else price

            settlement = _settlement_fields(
                stake_usdc=stake_usdc,
                fill_price=fill_price,
                filled=filled,
                outcome_available=outcome_available,
                over_hit=over_hit,
            )
            after_submit = state.submit(
                row,
                stake_usdc=stake_usdc,
                filled=filled,
                fill_seconds=fill_seconds,
                fill_cost_usdc=settlement.get("fill_cost_usdc"),
                touch_window_seconds=touch_window_seconds,
            )
            if not filled:
                decision = "submitted_unfilled"

        ledger_row: Dict[str, Any] = {
            "ledger_id": f"nsdpl_{idx:06d}",
            "paper_order_id": f"paper_nsd_{idx:06d}" if decision.startswith("submitted") else "",
            "mode": row.get("mode"),
            "session_date": row.get("session_date"),
            "ts": row.get("ts"),
            "candidate_id": row.get("candidate_id"),
            "dedup_key": row.get("dedup_key"),
            "duplicate_candidate_rows": row.get("duplicate_candidate_rows"),
            "decision": decision,
            "skip_reason": skip_reason,
            "support_regime": row.get("support_regime"),
            "shadow_no_score_drift_trigger": row.get("shadow_no_score_drift_trigger"),
            "signal_model_family": row.get("signal_model_family") or "no_score_drift",
            "game_pk": row.get("game_pk"),
            "away_abbrev": row.get("away_abbrev"),
            "home_abbrev": row.get("home_abbrev"),
            "line": row.get("line"),
            "inning": row.get("inning"),
            "inning_state": row.get("inning_state"),
            "outs": row.get("outs"),
            "runners_on": row.get("runners_on"),
            "away_score_before": row.get("away_score_before"),
            "home_score_before": row.get("home_score_before"),
            "current_total": row.get("current_total"),
            "home_leading_late": row.get("home_leading_late"),
            "batting_team_is_home": row.get("batting_team_is_home"),
            "bottom9_available_if_needed": row.get("bottom9_available_if_needed"),
            "expected_remaining_half_innings": row.get("expected_remaining_half_innings"),
            "expected_remaining_pa_bucket": row.get("expected_remaining_pa_bucket"),
            "home_skip_bottom9_risk": row.get("home_skip_bottom9_risk"),
            **{key: row.get(key) for key in nsd.SCORING_PATH_FIELD_KEYS},
            "shadow_bottom9_home_lead_context": row.get("shadow_bottom9_home_lead_context"),
            "shadow_home_skip_bottom9_risk_bucket": row.get("shadow_home_skip_bottom9_risk_bucket"),
            "shadow_no_score_poisson_edge_bucket": row.get("shadow_no_score_poisson_edge_bucket"),
            "shadow_no_score_empirical_edge_bucket": row.get("shadow_no_score_empirical_edge_bucket"),
            "shadow_no_score_ask_bucket": row.get("shadow_no_score_ask_bucket"),
            "shadow_no_score_drawdown_bucket": row.get("shadow_no_score_drawdown_bucket"),
            "shadow_no_score_poisson_empirical_ask_drawdown_bucket": row.get(
                "shadow_no_score_poisson_empirical_ask_drawdown_bucket"
            ),
            "score_segment_key": row.get("score_segment_key"),
            "score_segment_age_secs": row.get("score_segment_age_secs"),
            "score_segment_ticks": row.get("score_segment_ticks"),
            "score_segment_drawdown": row.get("score_segment_drawdown"),
            "price_policy": price_policy,
            "fill_assumption": fill_assumption,
            "decision_ask": row.get("decision_ask"),
            "best_bid": row.get("best_bid"),
            "spread": row.get("spread"),
            "limit_price": price,
            "fill_price": fill_price,
            "filled": filled,
            "fill_source": fill_source,
            "fill_seconds": fill_seconds,
            "fill_ts": fill_ts,
            "over_hit": over_hit,
            "final_total": row.get("final_total"),
            "final_away": row.get("final_away"),
            "final_home": row.get("final_home"),
            "stake_usdc": round(stake_usdc, 2),
            "daily_budget_usdc": round(daily_budget_usdc, 2),
            "game_budget_usdc": round(daily_budget_usdc * per_game_budget_fraction, 2),
            "daily_spent_before": _round_money(budget_snapshot.get("daily_spent_before")),
            "daily_reserved_before": _round_money(budget_snapshot.get("daily_reserved_before")),
            "daily_committed_before": _round_money(budget_snapshot.get("daily_committed_before")),
            "daily_spent_after_submit": _round_money(after_submit.get("daily_spent_after_submit")),
            "daily_reserved_after_submit": _round_money(after_submit.get("daily_reserved_after_submit")),
            "daily_committed_after_submit": _round_money(after_submit.get("daily_committed_after_submit")),
            "game_committed_before": _round_money(budget_snapshot.get("game_committed_before")),
            "game_committed_after_submit": _round_money(after_submit.get("game_committed_after_submit")),
            "game_open_or_filled_before": int(budget_snapshot.get("game_open_or_filled_before") or 0),
            "game_line_open_or_filled_before": int(budget_snapshot.get("game_line_open_or_filled_before") or 0),
            "fair_value": row.get("fair_value"),
            "edge": row.get("edge"),
            "current_state_value_edge": row.get("current_state_value_edge"),
            "current_state_value_empirical_edge": row.get("current_state_value_empirical_edge"),
            "shadow_inning_runs_needed_bucket": row.get("shadow_inning_runs_needed_bucket"),
            "shadow_current_phantom_combo_bucket": row.get("shadow_current_phantom_combo_bucket"),
        }
        for key in nsd.WEATHER_FEATURE_FIELD_KEYS:
            ledger_row[key] = row.get(key)
        ledger_row.update(settlement)
        ledger_rows.append(ledger_row)

    # Close out delayed paper orders so aggregate state is deterministic for tests.
    state.release_until(math.inf)
    return ledger_rows


def _numeric(rows: Iterable[Dict[str, Any]], field: str) -> List[float]:
    values: List[float] = []
    for row in rows:
        value = _safe_float(row.get(field))
        if value is not None:
            values.append(value)
    return values


def _mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return round(mean(values), 6)


def summarize_ledger(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    submitted = [row for row in rows if str(row.get("decision", "")).startswith("submitted")]
    filled = [row for row in submitted if row.get("filled") is True]
    settled = [row for row in filled if row.get("settled") is True]
    wins = [row for row in settled if row.get("over_hit") is True]
    losses = [row for row in settled if row.get("over_hit") is False]
    profits = _numeric(settled, "profit_usdc")
    costs = _numeric(filled, "fill_cost_usdc")
    skipped = [row for row in rows if row.get("decision") == "skipped"]
    unfilled = [row for row in rows if row.get("decision") == "submitted_unfilled"]

    return {
        "rows": len(rows),
        "submitted_orders": len(submitted),
        "skipped": len(skipped),
        "submitted_unfilled": len(unfilled),
        "filled_orders": len(filled),
        "settled_filled_orders": len(settled),
        "wins": len(wins),
        "losses": len(losses),
        "fill_rate": round(len(filled) / len(submitted), 6) if submitted else None,
        "win_rate": round(len(wins) / len(settled), 6) if settled else None,
        "stake_filled_usdc": round(sum(costs), 2) if costs else 0.0,
        "profit_usdc": round(sum(profits), 2) if profits else 0.0,
        "roi": round(sum(profits) / sum(costs), 6) if costs and sum(costs) > 0 else None,
        "avg_decision_ask": _mean(_numeric(submitted, "decision_ask")),
        "avg_fill_price": _mean(_numeric(filled, "fill_price")),
        "avg_current_state_value_edge": _mean(_numeric(submitted, "current_state_value_edge")),
        "avg_current_state_value_empirical_edge": _mean(_numeric(submitted, "current_state_value_empirical_edge")),
    }


def _group_summary(rows: Sequence[Dict[str, Any]], field: str) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "missing")].append(row)
    return {key: summarize_ledger(group) for key, group in sorted(groups.items())}


def build_summary(
    ledger_rows: Sequence[Dict[str, Any]],
    *,
    policy_counts: Dict[str, Any],
    args: argparse.Namespace,
    support_regimes: Sequence[str],
    policy_variants: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "generated_at_utc": _now_iso(),
        "description": (
            "Offline paper ledger for no-score drift shadow candidates. "
            "No live trading or gate enforcement."
        ),
        "config": {
            "mode": args.mode,
            "min_date": args.min_date or None,
            "max_date": args.max_date or None,
            "stake_usdc": args.stake,
            "daily_budget_usdc": args.daily_budget,
            "per_game_budget_fraction": args.per_game_budget_fraction,
            "max_orders_per_game": args.max_orders_per_game,
            "max_orders_per_game_line": args.max_orders_per_game_line,
            "support_regimes": list(support_regimes),
            "price_policy": args.price_policy,
            "fill_assumption": args.fill_assumption,
            "price_offset_cents": args.price_offset_cents,
            "touch_window_seconds": args.touch_window_seconds,
            "min_poisson_edge": args.min_poisson_edge,
            "min_empirical_edge": args.min_empirical_edge,
            "include_unsettled": bool(args.include_unsettled),
        },
        "input_counts": policy_counts,
        "overall": summarize_ledger(ledger_rows),
        "by_support_regime": _group_summary(ledger_rows, "support_regime"),
        "by_trigger": _group_summary(ledger_rows, "shadow_no_score_drift_trigger"),
        "by_session_date": _group_summary(ledger_rows, "session_date"),
        "by_decision": _group_summary(ledger_rows, "decision"),
        "by_skip_reason": _group_summary(ledger_rows, "skip_reason"),
        "by_poisson_empirical_ask_drawdown": _group_summary(
            ledger_rows,
            "shadow_no_score_poisson_empirical_ask_drawdown_bucket",
        ),
        "by_poisson_edge_bucket": _group_summary(ledger_rows, "shadow_no_score_poisson_edge_bucket"),
        "by_empirical_edge_bucket": _group_summary(ledger_rows, "shadow_no_score_empirical_edge_bucket"),
        "by_ask_bucket": _group_summary(ledger_rows, "shadow_no_score_ask_bucket"),
        "by_drawdown_bucket": _group_summary(ledger_rows, "shadow_no_score_drawdown_bucket"),
        "policy_variants": policy_variants or {},
    }


def build_policy_variant_summaries(
    policy_rows: Sequence[Dict[str, Any]],
    *,
    raw_candidates: Sequence[Dict[str, Any]],
    args: argparse.Namespace,
) -> Dict[str, Dict[str, Any]]:
    """Replay fixed trigger-family variants with independent budgets."""
    out: Dict[str, Dict[str, Any]] = {}
    for variant_name, support_regimes in POLICY_VARIANT_SUPPORT_REGIMES.items():
        variant_rows = build_ledger_rows(
            policy_rows,
            raw_candidates=raw_candidates,
            allowed_support_regimes=support_regimes,
            stake_usdc=args.stake,
            daily_budget_usdc=args.daily_budget,
            per_game_budget_fraction=args.per_game_budget_fraction,
            max_orders_per_game=args.max_orders_per_game,
            max_orders_per_game_line=args.max_orders_per_game_line,
            price_policy=args.price_policy,
            fill_assumption=args.fill_assumption,
            price_offset_cents=args.price_offset_cents,
            touch_window_seconds=args.touch_window_seconds,
            include_unsettled=args.include_unsettled,
        )
        out[variant_name] = {
            "description": (
                "Independent paper-ledger replay for this no-score drift trigger family. "
                "Budgets and per-game caps are reset per variant."
            ),
            "support_regimes": list(support_regimes),
            "overall": summarize_ledger(variant_rows),
            "by_support_regime": _group_summary(variant_rows, "support_regime"),
            "by_session_date": _group_summary(variant_rows, "session_date"),
            "by_decision": _group_summary(variant_rows, "decision"),
            "by_skip_reason": _group_summary(variant_rows, "skip_reason"),
            "by_poisson_empirical_ask_drawdown": _group_summary(
                variant_rows,
                "shadow_no_score_poisson_empirical_ask_drawdown_bucket",
            ),
        }
    return out


def write_outputs(
    output_root: Path,
    ledger_rows: Sequence[Dict[str, Any]],
    summary: Dict[str, Any],
) -> Dict[str, str]:
    output_root.mkdir(parents=True, exist_ok=True)
    summary_path = output_root / "no_score_drift_paper_ledger_summary.json"
    rows_jsonl_path = output_root / "no_score_drift_paper_ledger_rows.jsonl"
    rows_csv_path = output_root / "no_score_drift_paper_ledger_rows.csv"

    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    with rows_jsonl_path.open("w", encoding="utf-8") as f:
        for row in ledger_rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    with rows_csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LEDGER_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in ledger_rows:
            writer.writerow(row)
    return {
        "summary": str(summary_path),
        "rows_jsonl": str(rows_jsonl_path),
        "rows_csv": str(rows_csv_path),
    }


def _validate_args(args: argparse.Namespace) -> None:
    if args.min_date:
        datetime.strptime(args.min_date, "%Y-%m-%d")
    if args.max_date:
        datetime.strptime(args.max_date, "%Y-%m-%d")
    if args.min_date and args.max_date and args.min_date > args.max_date:
        raise SystemExit("--min-date must be <= --max-date")
    if args.stake <= 0:
        raise SystemExit("--stake must be > 0")
    if args.daily_budget <= 0:
        raise SystemExit("--daily-budget must be > 0")
    if args.per_game_budget_fraction <= 0:
        raise SystemExit("--per-game-budget-fraction must be > 0")
    if args.max_orders_per_game < 0:
        raise SystemExit("--max-orders-per-game must be >= 0")
    if args.max_orders_per_game_line < 0:
        raise SystemExit("--max-orders-per-game-line must be >= 0")
    if args.price_offset_cents < 0:
        raise SystemExit("--price-offset-cents must be >= 0")
    if args.touch_window_seconds < 0:
        raise SystemExit("--touch-window-seconds must be >= 0")
    if args.min_poisson_edge < 0:
        raise SystemExit("--min-poisson-edge must be >= 0")
    if args.min_empirical_edge < 0:
        raise SystemExit("--min-empirical-edge must be >= 0")


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    _validate_args(args)
    support_regimes = _allowed_support_regimes(args.support_regimes)

    candidates, outcomes = nsd.load_rows(args.mode, args.min_date, args.max_date)
    policy_rows, policy_counts = nsd.build_policy_rows(
        candidates,
        outcomes,
        min_poisson_edge=args.min_poisson_edge,
        min_empirical_edge=args.min_empirical_edge,
    )
    ledger_rows = build_ledger_rows(
        policy_rows,
        raw_candidates=candidates,
        allowed_support_regimes=support_regimes,
        stake_usdc=args.stake,
        daily_budget_usdc=args.daily_budget,
        per_game_budget_fraction=args.per_game_budget_fraction,
        max_orders_per_game=args.max_orders_per_game,
        max_orders_per_game_line=args.max_orders_per_game_line,
        price_policy=args.price_policy,
        fill_assumption=args.fill_assumption,
        price_offset_cents=args.price_offset_cents,
        touch_window_seconds=args.touch_window_seconds,
        include_unsettled=args.include_unsettled,
    )
    if args.strict and not ledger_rows:
        raise SystemExit("No no-score drift paper ledger rows produced")

    policy_variants = build_policy_variant_summaries(
        policy_rows,
        raw_candidates=candidates,
        args=args,
    )
    summary = build_summary(
        ledger_rows,
        policy_counts=policy_counts,
        args=args,
        support_regimes=support_regimes,
        policy_variants=policy_variants,
    )
    paths = write_outputs(args.output_root, ledger_rows, summary)
    overall = summary["overall"]

    print(f"Wrote {paths['summary']}")
    print(f"Wrote {paths['rows_jsonl']}")
    print(f"Wrote {paths['rows_csv']}")
    print(
        "No-score drift paper ledger: "
        f"rows={overall['rows']} submitted={overall['submitted_orders']} "
        f"filled={overall['filled_orders']} skipped={overall['skipped']} "
        f"profit=${overall['profit_usdc']:+.2f} roi={overall['roi']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
