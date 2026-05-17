#!/usr/bin/env python3
"""
Build the canonical analysis-safe trade table for live MLB Polymarket runs.

This table is the measurement layer for trade/order outcomes. It deliberately
does less than the raw session + ledger artifacts:

  - session JSONs are the primary one-row-per-bet source
  - order_status="error" rows are excluded by default because they are failed
    placement attempts, not executed or resting trades
  - ledger lifecycle rows are deduped by (bet_id, order_id, event)
  - execution mode is explicit: live, paper_fallback, dry_run, paper, unknown
  - share/cost/payout fields carry source labels so pre-fix historical rows do
    not look as precise as modern rows

Outputs:
  data/analysis_output/analysis_safe_trades/analysis_safe_trades.jsonl
  data/analysis_output/analysis_safe_trades/analysis_safe_trades.csv
  data/analysis_output/analysis_safe_trades/analysis_safe_trades_summary.json

Read-only over data/live_trading. It does not change gate, sizing, or runtime
execution behavior.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.trading.scoring_path_features import SCORING_PATH_FIELD_KEYS  # noqa: E402

DEFAULT_LIVE_ROOT = PROJECT_DIR / "data" / "live_trading"
DEFAULT_SESSIONS_DIR = DEFAULT_LIVE_ROOT / "sessions"
DEFAULT_LIVE_ORDERS_LEDGER = DEFAULT_LIVE_ROOT / "live_orders_ledger.jsonl"
DEFAULT_MASTER_LEDGER = DEFAULT_LIVE_ROOT / "master_ledger.jsonl"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "analysis_safe_trades"

ERROR_STATUSES = {"error"}
FILLED_STATUSES = {"filled"}
OPEN_STATUSES = {"live", "pending", "delayed", "matched"}
CLOSED_NONFILLED_STATUSES = {"cancelled", "expired", "missed", "dry_run"}

OUTPUT_COLUMNS = [
    "schema_version",
    "mode",
    "session_date",
    "source_session_path",
    "bet_id",
    "order_id",
    "game_pk",
    "away_abbrev",
    "home_abbrev",
    "side",
    "line",
    "inning",
    "inning_state",
    "outs",
    "runners_on",
    "away_score_before",
    "home_score_before",
    "current_total",
    "inferred_runs",
    "inferred_away_after",
    "inferred_home_after",
    "order_status_final",
    "execution_mode",
    "is_live_money",
    "is_paper_fallback",
    "is_dry_run",
    "is_filled",
    "is_open",
    "is_cancelled",
    "is_settled",
    "won",
    "result",
    "final_away",
    "final_home",
    "final_total",
    "placed_at",
    "order_placed_at",
    "filled_at",
    "cancelled_at",
    "settled_at",
    "cancel_reason",
    "entry_ask",
    "decision_ask",
    "execution_bid",
    "execution_ask",
    "posted_limit",
    "limit_price",
    "actual_fill_price",
    "fill_price",
    "stake_usdc",
    "order_size_shares",
    "filled_shares",
    "filled_shares_source",
    "fill_cost_usdc",
    "fill_cost_source",
    "payout_usdc",
    "payout_source",
    "realized_profit_usdc",
    "profit_source",
    "roi_on_cost",
    "fair_value",
    "base_fair_value",
    "edge",
    "stage2_run_env_delta",
    "team_offense_delta",
    "state_value_strategy",
    "current_state_value_edge",
    "current_state_value_fv_raw",
    "current_state_value_empirical_edge",
    "shadow_phantom_risk_score",
    "shadow_phantom_risk_band",
    "shadow_fv_inferred_lift",
    "inferred_state_base_poisson",
    "inferred_state_base_empirical",
    "inferred_state_poisson_minus_empirical",
    "inferred_state_n",
    "inferred_state_fallback_level",
    "home_leading_late",
    "batting_team_is_home",
    "bottom9_available_if_needed",
    "expected_remaining_half_innings",
    "expected_remaining_pa_bucket",
    "home_skip_bottom9_risk",
    *SCORING_PATH_FIELD_KEYS,
    "placement_mode_raw",
    "clob_accept_status",
    "paper_fallback_reason",
    "over_token_id",
    "ledger_event_count_raw",
    "ledger_event_count_deduped",
    "ledger_duplicate_event_count",
    "ledger_event_types",
    "ledger_sources",
    "ledger_latest_event_at",
    "excluded_from_real_money_pnl_reason",
]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build canonical analysis-safe trade table.")
    parser.add_argument("--sessions-dir", type=Path, default=DEFAULT_SESSIONS_DIR)
    parser.add_argument("--live-orders-ledger", type=Path, default=DEFAULT_LIVE_ORDERS_LEDGER)
    parser.add_argument("--master-ledger", type=Path, default=DEFAULT_MASTER_LEDGER)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--min-date", type=str, default="", help="Inclusive session date lower bound.")
    parser.add_argument("--max-date", type=str, default="", help="Inclusive session date upper bound.")
    parser.add_argument(
        "--include-errors",
        action="store_true",
        help="Include order_status=error rows with is_analysis_safe=false semantics.",
    )
    parser.add_argument("--strict", action="store_true", help="Fail if hard integrity warnings are found.")
    return parser.parse_args(argv)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"true", "1", "yes", "y", "win", "won"}:
        return True
    if s in {"false", "0", "no", "n", "loss", "lost"}:
        return False
    return None


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    if value is None:
        return None
    return round(value, digits)


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _date_in_range(date_str: str, min_date: str = "", max_date: str = "") -> bool:
    if min_date and date_str < min_date:
        return False
    if max_date and date_str > max_date:
        return False
    return True


def _session_date_from_path(path: Path) -> str:
    name = path.name
    suffix = "_session.json"
    return name[: -len(suffix)] if name.endswith(suffix) else ""


def _timestamp_sort_key(row: Dict[str, Any]) -> str:
    return str(
        _coalesce(
            row.get("settled_at"),
            row.get("filled_at"),
            row.get("cancelled_at"),
            row.get("order_placed_at"),
            row.get("placed_at"),
            row.get("ts"),
            row.get("timestamp"),
            "",
        )
    )


def _event_type(row: Dict[str, Any]) -> str:
    return str(
        _coalesce(
            row.get("_event"),
            row.get("event_type"),
            row.get("order_status"),
            row.get("clob_accept_status"),
            "unknown",
        )
    )


def _ledger_key(row: Dict[str, Any]) -> Tuple[str, str, str]:
    bet_id = str(row.get("bet_id") or "")
    order_id = str(row.get("order_id") or "")
    return bet_id, order_id, _event_type(row)


def _nonnull_count(row: Dict[str, Any]) -> int:
    return sum(1 for value in row.values() if value is not None and value != "")


def _merge_prefer_latest(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Merge duplicate ledger events without dropping late enrichment fields."""

    merged = dict(existing)
    existing_ts = _timestamp_sort_key(existing)
    incoming_ts = _timestamp_sort_key(incoming)
    incoming_is_later = incoming_ts >= existing_ts
    for key, value in incoming.items():
        if value is None or value == "":
            continue
        if key not in merged or merged.get(key) in (None, "") or incoming_is_later:
            merged[key] = value
    return merged


def _iter_jsonl(path: Path, warnings: List[str]) -> Iterator[Dict[str, Any]]:
    if not path.exists():
        warnings.append(f"ledger path missing: {path}")
        return
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_no, raw in enumerate(handle, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                warnings.append(f"bad JSON in {path}:{line_no}: {exc}")
                continue
            if not isinstance(row, dict):
                warnings.append(f"non-object JSON row in {path}:{line_no}")
                continue
            row["_ledger_source"] = path.name
            row["_ledger_line_no"] = line_no
            yield row


def load_sessions(
    sessions_dir: Path,
    *,
    min_date: str = "",
    max_date: str = "",
    warnings: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    warnings = warnings if warnings is not None else []
    out: Dict[str, Dict[str, Any]] = {}
    if not sessions_dir.exists():
        warnings.append(f"sessions dir missing: {sessions_dir}")
        return out

    for path in sorted(sessions_dir.glob("*_session.json")):
        session_date = _session_date_from_path(path)
        if not session_date or not _date_in_range(session_date, min_date, max_date):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            warnings.append(f"failed to read session file {path}: {exc}")
            continue
        if not isinstance(payload, dict):
            warnings.append(f"session file is not an object: {path}")
            continue
        bets = payload.get("bets") or []
        if not isinstance(bets, list):
            warnings.append(f"session file has non-list bets: {path}")
            continue
        mode = str(payload.get("mode") or "live")
        params = payload.get("params") or {}
        for index, bet in enumerate(bets):
            if not isinstance(bet, dict):
                warnings.append(f"non-object bet in {path}:{index}")
                continue
            bet_id = str(bet.get("bet_id") or "")
            if not bet_id:
                warnings.append(f"bet without bet_id in {path}:{index}")
                continue
            if bet_id in out:
                warnings.append(f"duplicate bet_id across sessions: {bet_id}")
                continue
            out[bet_id] = {
                "mode": mode,
                "session_date": session_date,
                "session_path": str(path),
                "session_params": params if isinstance(params, dict) else {},
                "bet": bet,
            }
    return out


def load_deduped_ledgers(
    ledger_paths: Iterable[Path],
    *,
    min_date: str = "",
    max_date: str = "",
    warnings: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    warnings = warnings if warnings is not None else []
    raw_by_bet: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    dedup_by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    duplicate_count_by_bet: Counter[str] = Counter()

    for path in ledger_paths:
        for row in _iter_jsonl(path, warnings):
            bet_id = str(row.get("bet_id") or "")
            if not bet_id:
                warnings.append(f"ledger row missing bet_id in {path}:{row.get('_ledger_line_no')}")
                continue
            session_date = _infer_session_date(row)
            if session_date and not _date_in_range(session_date, min_date, max_date):
                continue
            raw_by_bet[bet_id].append(row)
            key = _ledger_key(row)
            if key in dedup_by_key:
                duplicate_count_by_bet[bet_id] += 1
                dedup_by_key[key] = _merge_prefer_latest(dedup_by_key[key], row)
            else:
                dedup_by_key[key] = dict(row)

    dedup_by_bet: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in dedup_by_key.values():
        bet_id = str(row.get("bet_id") or "")
        if bet_id:
            dedup_by_bet[bet_id].append(row)

    out: Dict[str, Dict[str, Any]] = {}
    all_bet_ids = set(raw_by_bet) | set(dedup_by_bet)
    for bet_id in all_bet_ids:
        raw_events = raw_by_bet.get(bet_id, [])
        dedup_events = sorted(
            dedup_by_bet.get(bet_id, []),
            key=lambda row: (_timestamp_sort_key(row), _nonnull_count(row)),
        )
        event_types = sorted({_event_type(row) for row in dedup_events})
        sources = sorted({str(row.get("_ledger_source") or "") for row in raw_events if row.get("_ledger_source")})
        latest = dedup_events[-1] if dedup_events else {}
        out[bet_id] = {
            "raw_events": raw_events,
            "dedup_events": dedup_events,
            "raw_count": len(raw_events),
            "dedup_count": len(dedup_events),
            "duplicate_count": int(duplicate_count_by_bet.get(bet_id, 0)),
            "event_types": event_types,
            "sources": sources,
            "latest_event_at": _timestamp_sort_key(latest) if latest else "",
            "latest_event": latest,
        }
    return out


def _infer_session_date(row: Dict[str, Any]) -> str:
    session_date = str(row.get("session_date") or "")
    if len(session_date) == 10:
        return session_date
    bet_id = str(row.get("bet_id") or "")
    if len(bet_id) >= 10 and bet_id[4] == "-" and bet_id[7] == "-":
        return bet_id[:10]
    for key in ("placed_at", "order_placed_at", "settled_at", "filled_at", "cancelled_at"):
        value = str(row.get(key) or "")
        if len(value) >= 10 and value[4] == "-" and value[7] == "-":
            return value[:10]
    return ""


def _best_ledger_value(ledger_events: List[Dict[str, Any]], key: str) -> Any:
    for row in sorted(
        ledger_events,
        key=lambda item: (_timestamp_sort_key(item), _nonnull_count(item)),
        reverse=True,
    ):
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _infer_execution_mode(bet: Dict[str, Any], mode: str, ledger_events: List[Dict[str, Any]]) -> str:
    raw_mode = str(_coalesce(bet.get("placement_mode"), _best_ledger_value(ledger_events, "placement_mode"), ""))
    if raw_mode in {"live", "paper_fallback", "dry_run", "paper"}:
        return raw_mode

    clob_status = str(_coalesce(bet.get("clob_accept_status"), _best_ledger_value(ledger_events, "clob_accept_status"), ""))
    order_id = str(_coalesce(bet.get("order_id"), _best_ledger_value(ledger_events, "order_id"), ""))
    if clob_status == "paper_fallback" or order_id.startswith("paper_fallback_"):
        return "paper_fallback"
    if clob_status == "dry_run" or order_id.startswith("dry_run_"):
        return "dry_run"
    if mode == "paper":
        return "paper"
    if order_id.startswith("0x"):
        return "live"
    return "unknown"


def _derive_filled_shares(bet: Dict[str, Any], ledger_events: List[Dict[str, Any]]) -> Tuple[Optional[float], str]:
    explicit = _safe_float(_coalesce(bet.get("filled_shares"), _best_ledger_value(ledger_events, "filled_shares")))
    if explicit is not None:
        return explicit, "filled_shares"

    order_size = _safe_float(_coalesce(bet.get("order_size_shares"), _best_ledger_value(ledger_events, "order_size_shares")))
    status = str(_coalesce(bet.get("order_status"), _best_ledger_value(ledger_events, "order_status"), ""))
    if order_size is not None and status == "filled":
        return order_size, "order_size_shares_filled"

    fill_size = _safe_float(_coalesce(bet.get("fill_size"), _best_ledger_value(ledger_events, "fill_size")))
    fill_cost = _safe_float(_coalesce(bet.get("fill_cost_usdc"), bet.get("fill_cost"), _best_ledger_value(ledger_events, "fill_cost_usdc")))
    fill_price = _safe_float(_coalesce(bet.get("fill_price"), bet.get("actual_fill_price"), _best_ledger_value(ledger_events, "fill_price")))
    if fill_size is not None and fill_cost is not None and fill_price is not None:
        # Modern rows have fill_size as shares. Historical rows before the
        # share-size fix are ambiguous, so only mark as inferred when the
        # cost-price arithmetic is coherent.
        if abs((fill_size * fill_price) - fill_cost) <= max(0.03, fill_cost * 0.01):
            return fill_size, "fill_size_verified"
    return None, "missing"


def _derive_fill_cost(
    bet: Dict[str, Any],
    ledger_events: List[Dict[str, Any]],
    *,
    filled_shares: Optional[float],
) -> Tuple[Optional[float], str]:
    explicit = _safe_float(_coalesce(bet.get("fill_cost_usdc"), _best_ledger_value(ledger_events, "fill_cost_usdc")))
    if explicit is not None:
        return explicit, "fill_cost_usdc"

    legacy = _safe_float(_coalesce(bet.get("fill_cost"), _best_ledger_value(ledger_events, "fill_cost")))
    if legacy is not None:
        return legacy, "fill_cost"

    fill_price = _safe_float(_coalesce(bet.get("fill_price"), bet.get("actual_fill_price"), _best_ledger_value(ledger_events, "fill_price")))
    if filled_shares is not None and fill_price is not None:
        return filled_shares * fill_price, "filled_shares_x_fill_price"

    status = str(_coalesce(bet.get("order_status"), _best_ledger_value(ledger_events, "order_status"), ""))
    stake = _safe_float(bet.get("stake"))
    if status == "filled" and stake is not None:
        return stake, "stake_fallback"
    return None, "missing"


def _derive_payout(
    bet: Dict[str, Any],
    ledger_events: List[Dict[str, Any]],
    *,
    filled_shares: Optional[float],
    fill_cost: Optional[float],
) -> Tuple[Optional[float], str]:
    explicit = _safe_float(_coalesce(bet.get("payout_usdc"), _best_ledger_value(ledger_events, "payout_usdc")))
    if explicit is not None:
        return explicit, "payout_usdc"

    won = _safe_bool(_coalesce(bet.get("won"), _best_ledger_value(ledger_events, "won")))
    if won is False:
        return 0.0, "loss_zero"
    if won is True and filled_shares is not None:
        return filled_shares, "filled_shares_if_won"

    profit = _safe_float(_coalesce(bet.get("profit"), _best_ledger_value(ledger_events, "profit")))
    if profit is not None and fill_cost is not None:
        return profit + fill_cost, "profit_plus_cost"
    return None, "missing"


def _derive_profit(
    bet: Dict[str, Any],
    ledger_events: List[Dict[str, Any]],
    *,
    fill_cost: Optional[float],
    payout: Optional[float],
) -> Tuple[Optional[float], str]:
    explicit = _safe_float(_coalesce(bet.get("profit"), _best_ledger_value(ledger_events, "profit")))
    if explicit is not None:
        return explicit, "profit"
    if fill_cost is not None and payout is not None:
        return payout - fill_cost, "payout_minus_cost"
    return None, "missing"


def _final_status(bet: Dict[str, Any], ledger_events: List[Dict[str, Any]]) -> str:
    status = str(_coalesce(bet.get("order_status"), "")).strip()
    if status:
        return status
    for event_name in ("filled", "cancelled", "expired", "live", "pending", "dry_run", "settled"):
        if any(_event_type(row) == event_name for row in ledger_events):
            return event_name
    return "unknown"


def _final_result(bet: Dict[str, Any], ledger_events: List[Dict[str, Any]]) -> str:
    value = str(_coalesce(bet.get("result"), _best_ledger_value(ledger_events, "result"), "")).strip()
    if value:
        return value.upper()
    won = _safe_bool(_coalesce(bet.get("won"), _best_ledger_value(ledger_events, "won")))
    if won is True:
        return "WIN"
    if won is False:
        return "LOSS"
    return ""


def build_trade_row(
    bet_id: str,
    session_entry: Dict[str, Any],
    ledger_info: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    bet = session_entry["bet"]
    mode = str(session_entry.get("mode") or "live")
    ledger_info = ledger_info or {
        "raw_events": [],
        "dedup_events": [],
        "raw_count": 0,
        "dedup_count": 0,
        "duplicate_count": 0,
        "event_types": [],
        "sources": [],
        "latest_event_at": "",
    }
    ledger_events = list(ledger_info.get("dedup_events") or [])

    status = _final_status(bet, ledger_events)
    execution_mode = _infer_execution_mode(bet, mode, ledger_events)
    filled_shares, filled_shares_source = _derive_filled_shares(bet, ledger_events)
    fill_cost, fill_cost_source = _derive_fill_cost(bet, ledger_events, filled_shares=filled_shares)
    payout, payout_source = _derive_payout(
        bet,
        ledger_events,
        filled_shares=filled_shares,
        fill_cost=fill_cost,
    )
    profit, profit_source = _derive_profit(
        bet,
        ledger_events,
        fill_cost=fill_cost,
        payout=payout,
    )
    roi = profit / fill_cost if profit is not None and fill_cost and fill_cost > 0 else None

    is_filled = status in FILLED_STATUSES or filled_shares is not None or _coalesce(bet.get("filled_at"), _best_ledger_value(ledger_events, "filled_at")) is not None
    is_open = status in OPEN_STATUSES
    is_cancelled = status in CLOSED_NONFILLED_STATUSES or bool(_coalesce(bet.get("cancelled_at"), _best_ledger_value(ledger_events, "cancelled_at")))
    is_settled = bool(
        _coalesce(
            bet.get("settled"),
            _best_ledger_value(ledger_events, "settled"),
            bet.get("settled_at"),
            _best_ledger_value(ledger_events, "settled_at"),
        )
    )
    is_live_money = execution_mode == "live"
    is_paper_fallback = execution_mode == "paper_fallback"
    is_dry_run = execution_mode == "dry_run"
    excluded_reason = ""
    if not is_live_money:
        excluded_reason = f"execution_mode={execution_mode}"
    if not is_filled:
        excluded_reason = "not_filled" if not excluded_reason else f"{excluded_reason};not_filled"

    row: Dict[str, Any] = {
        "schema_version": 1,
        "mode": mode,
        "session_date": session_entry.get("session_date"),
        "source_session_path": session_entry.get("session_path"),
        "bet_id": bet_id,
        "order_id": _coalesce(bet.get("order_id"), _best_ledger_value(ledger_events, "order_id")),
        "game_pk": _safe_int(bet.get("game_pk")),
        "away_abbrev": bet.get("away_abbrev"),
        "home_abbrev": bet.get("home_abbrev"),
        "side": bet.get("side") or "over",
        "line": bet.get("line"),
        "inning": _safe_int(bet.get("inning")),
        "inning_state": bet.get("inning_state"),
        "outs": _safe_int(bet.get("outs")),
        "runners_on": _safe_int(bet.get("runners_on")),
        "away_score_before": _safe_int(bet.get("away_score_before")),
        "home_score_before": _safe_int(bet.get("home_score_before")),
        "current_total": _safe_int(bet.get("current_total")),
        "inferred_runs": _safe_int(bet.get("inferred_runs")),
        "inferred_away_after": _safe_int(bet.get("inferred_away_after")),
        "inferred_home_after": _safe_int(bet.get("inferred_home_after")),
        "order_status_final": status,
        "execution_mode": execution_mode,
        "is_live_money": is_live_money,
        "is_paper_fallback": is_paper_fallback,
        "is_dry_run": is_dry_run,
        "is_filled": bool(is_filled),
        "is_open": bool(is_open),
        "is_cancelled": bool(is_cancelled),
        "is_settled": bool(is_settled),
        "won": _safe_bool(_coalesce(bet.get("won"), _best_ledger_value(ledger_events, "won"))),
        "result": _final_result(bet, ledger_events),
        "final_away": _safe_int(_coalesce(bet.get("final_away"), _best_ledger_value(ledger_events, "final_away"))),
        "final_home": _safe_int(_coalesce(bet.get("final_home"), _best_ledger_value(ledger_events, "final_home"))),
        "final_total": _safe_int(_coalesce(bet.get("final_total"), _best_ledger_value(ledger_events, "final_total"))),
        "placed_at": _coalesce(bet.get("placed_at"), _best_ledger_value(ledger_events, "placed_at")),
        "order_placed_at": _coalesce(bet.get("order_placed_at"), _best_ledger_value(ledger_events, "order_placed_at")),
        "filled_at": _coalesce(bet.get("filled_at"), _best_ledger_value(ledger_events, "filled_at")),
        "cancelled_at": _coalesce(bet.get("cancelled_at"), _best_ledger_value(ledger_events, "cancelled_at")),
        "settled_at": _coalesce(bet.get("settled_at"), _best_ledger_value(ledger_events, "settled_at")),
        "cancel_reason": _coalesce(bet.get("cancel_reason"), _best_ledger_value(ledger_events, "cancel_reason")),
        "entry_ask": _round(_safe_float(bet.get("entry_ask"))),
        "decision_ask": _round(_safe_float(_coalesce(bet.get("decision_ask"), bet.get("entry_ask")))),
        "execution_bid": _round(_safe_float(bet.get("execution_bid"))),
        "execution_ask": _round(_safe_float(bet.get("execution_ask"))),
        "posted_limit": _round(_safe_float(_coalesce(bet.get("posted_limit"), bet.get("limit_price")))),
        "limit_price": _round(_safe_float(bet.get("limit_price"))),
        "actual_fill_price": _round(_safe_float(bet.get("actual_fill_price"))),
        "fill_price": _round(_safe_float(bet.get("fill_price"))),
        "stake_usdc": _round(_safe_float(bet.get("stake")), 2),
        "order_size_shares": _round(_safe_float(_coalesce(bet.get("order_size_shares"), _best_ledger_value(ledger_events, "order_size_shares")))),
        "filled_shares": _round(filled_shares),
        "filled_shares_source": filled_shares_source,
        "fill_cost_usdc": _round(fill_cost, 2),
        "fill_cost_source": fill_cost_source,
        "payout_usdc": _round(payout, 2),
        "payout_source": payout_source,
        "realized_profit_usdc": _round(profit, 2),
        "profit_source": profit_source,
        "roi_on_cost": _round(roi, 6),
        "fair_value": _round(_safe_float(bet.get("fair_value"))),
        "base_fair_value": _round(_safe_float(bet.get("base_fair_value"))),
        "edge": _round(_safe_float(bet.get("edge"))),
        "stage2_run_env_delta": _round(_safe_float(bet.get("stage2_run_env_delta"))),
        "team_offense_delta": _round(_safe_float(bet.get("team_offense_delta"))),
        "state_value_strategy": bet.get("state_value_strategy"),
        "current_state_value_edge": _round(_safe_float(bet.get("current_state_value_edge"))),
        "current_state_value_fv_raw": _round(_safe_float(bet.get("current_state_value_fv_raw"))),
        "current_state_value_empirical_edge": _round(_safe_float(bet.get("current_state_value_empirical_edge"))),
        "shadow_phantom_risk_score": _round(_safe_float(bet.get("shadow_phantom_risk_score"))),
        "shadow_phantom_risk_band": bet.get("shadow_phantom_risk_band"),
        "shadow_fv_inferred_lift": _round(_safe_float(bet.get("shadow_fv_inferred_lift"))),
        "inferred_state_base_poisson": _round(_safe_float(bet.get("inferred_state_base_poisson"))),
        "inferred_state_base_empirical": _round(_safe_float(bet.get("inferred_state_base_empirical"))),
        "inferred_state_poisson_minus_empirical": _round(_safe_float(bet.get("inferred_state_poisson_minus_empirical"))),
        "inferred_state_n": _round(_safe_float(bet.get("inferred_state_n"))),
        "inferred_state_fallback_level": _safe_int(bet.get("inferred_state_fallback_level")),
        "home_leading_late": _safe_bool(bet.get("home_leading_late")),
        "batting_team_is_home": _safe_bool(bet.get("batting_team_is_home")),
        "bottom9_available_if_needed": _safe_bool(bet.get("bottom9_available_if_needed")),
        "expected_remaining_half_innings": _round(_safe_float(bet.get("expected_remaining_half_innings"))),
        "expected_remaining_pa_bucket": bet.get("expected_remaining_pa_bucket"),
        "home_skip_bottom9_risk": _round(_safe_float(bet.get("home_skip_bottom9_risk"))),
        "scoring_path_available": _safe_bool(bet.get("scoring_path_available")),
        "scoring_path_innings_observed": _safe_int(bet.get("scoring_path_innings_observed")),
        "scoring_path_runs_observed": _safe_int(bet.get("scoring_path_runs_observed")),
        "scoring_path_inning_runs": bet.get("scoring_path_inning_runs"),
        "scoring_inning_rate": _round(_safe_float(bet.get("scoring_inning_rate"))),
        "scoring_half_rate": _round(_safe_float(bet.get("scoring_half_rate"))),
        "burst_share": _round(_safe_float(bet.get("burst_share"))),
        "scoreless_streak": _safe_int(bet.get("scoreless_streak")),
        "recent2_run_share": _round(_safe_float(bet.get("recent2_run_share"))),
        "weighted_run_inning_norm": _round(_safe_float(bet.get("weighted_run_inning_norm"))),
        "inning_run_slope": _round(_safe_float(bet.get("inning_run_slope"))),
        "placement_mode_raw": _coalesce(bet.get("placement_mode"), _best_ledger_value(ledger_events, "placement_mode")),
        "clob_accept_status": _coalesce(bet.get("clob_accept_status"), _best_ledger_value(ledger_events, "clob_accept_status")),
        "paper_fallback_reason": _coalesce(bet.get("paper_fallback_reason"), _best_ledger_value(ledger_events, "paper_fallback_reason")),
        "over_token_id": bet.get("over_token_id"),
        "ledger_event_count_raw": int(ledger_info.get("raw_count") or 0),
        "ledger_event_count_deduped": int(ledger_info.get("dedup_count") or 0),
        "ledger_duplicate_event_count": int(ledger_info.get("duplicate_count") or 0),
        "ledger_event_types": ",".join(str(v) for v in ledger_info.get("event_types") or []),
        "ledger_sources": ",".join(str(v) for v in ledger_info.get("sources") or []),
        "ledger_latest_event_at": ledger_info.get("latest_event_at") or "",
        "excluded_from_real_money_pnl_reason": excluded_reason,
    }
    return row


def build_rows(
    sessions: Dict[str, Dict[str, Any]],
    ledger_by_bet: Dict[str, Dict[str, Any]],
    *,
    include_errors: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    excluded = Counter()
    warnings: List[str] = []

    for bet_id, session_entry in sorted(
        sessions.items(),
        key=lambda item: (str(item[1].get("session_date") or ""), item[0]),
    ):
        bet = session_entry["bet"]
        status = str(bet.get("order_status") or "")
        if status in ERROR_STATUSES and not include_errors:
            excluded["order_status_error"] += 1
            continue
        row = build_trade_row(bet_id, session_entry, ledger_by_bet.get(bet_id))
        rows.append(row)

    session_bet_ids = set(sessions)
    ledger_only_bet_ids = set(ledger_by_bet) - session_bet_ids
    for bet_id in sorted(ledger_only_bet_ids):
        # Legacy ledgers contain old dry-run/settled spam. Keep the count in
        # summary, but do not synthesize rows unless a session record exists.
        excluded["ledger_only_bet_id"] += 1

    if any(row["order_status_final"] == "error" for row in rows) and not include_errors:
        warnings.append("error row leaked into analysis-safe output")

    return rows, {
        "excluded": dict(excluded),
        "warnings": warnings,
        "ledger_only_bet_ids": len(ledger_only_bet_ids),
    }


def _sum_profit(rows: Iterable[Dict[str, Any]], predicate) -> float:
    total = 0.0
    for row in rows:
        if predicate(row):
            value = _safe_float(row.get("realized_profit_usdc"))
            if value is not None:
                total += value
    return round(total, 2)


def _sum_cost(rows: Iterable[Dict[str, Any]], predicate) -> float:
    total = 0.0
    for row in rows:
        if predicate(row):
            value = _safe_float(row.get("fill_cost_usdc"))
            if value is not None:
                total += value
    return round(total, 2)


def _summarize_rows(
    rows: List[Dict[str, Any]],
    *,
    sessions: Dict[str, Dict[str, Any]],
    ledger_by_bet: Dict[str, Dict[str, Any]],
    load_warnings: List[str],
    build_notes: Dict[str, Any],
) -> Dict[str, Any]:
    status_counts = Counter(str(row.get("order_status_final") or "missing") for row in rows)
    mode_counts = Counter(str(row.get("execution_mode") or "missing") for row in rows)
    date_counts = Counter(str(row.get("session_date") or "missing") for row in rows)
    filled_rows = [row for row in rows if row.get("is_filled")]
    live_filled_rows = [row for row in filled_rows if row.get("is_live_money")]
    paper_fallback_rows = [row for row in filled_rows if row.get("is_paper_fallback")]
    dry_run_rows = [row for row in rows if row.get("is_dry_run")]

    live_cost = _sum_cost(live_filled_rows, lambda row: True)
    live_profit = _sum_profit(live_filled_rows, lambda row: True)
    paper_cost = _sum_cost(paper_fallback_rows, lambda row: True)
    paper_profit = _sum_profit(paper_fallback_rows, lambda row: True)
    all_cost = _sum_cost(filled_rows, lambda row: True)
    all_profit = _sum_profit(filled_rows, lambda row: True)

    duplicate_ledger_events = sum(int(info.get("duplicate_count") or 0) for info in ledger_by_bet.values())
    rows_missing_cost = sum(1 for row in filled_rows if row.get("fill_cost_usdc") in (None, ""))
    rows_missing_shares = sum(1 for row in filled_rows if row.get("filled_shares") in (None, ""))

    return {
        "generated_at": _now_iso(),
        "schema_version": 1,
        "source": {
            "session_rows_loaded": len(sessions),
            "ledger_bet_ids_loaded": len(ledger_by_bet),
            "ledger_duplicate_events_deduped": duplicate_ledger_events,
            "load_warnings": load_warnings,
        },
        "row_counts": {
            "analysis_safe_rows": len(rows),
            "filled_rows": len(filled_rows),
            "live_money_filled_rows": len(live_filled_rows),
            "paper_fallback_filled_rows": len(paper_fallback_rows),
            "dry_run_rows": len(dry_run_rows),
            "excluded": build_notes.get("excluded", {}),
            "ledger_only_bet_ids": build_notes.get("ledger_only_bet_ids", 0),
        },
        "status_counts": dict(status_counts.most_common()),
        "execution_mode_counts": dict(mode_counts.most_common()),
        "session_date_counts": dict(date_counts.most_common()),
        "pnl": {
            "all_filled_cost_usdc": all_cost,
            "all_filled_profit_usdc": all_profit,
            "all_filled_roi": round(all_profit / all_cost, 6) if all_cost else None,
            "live_money_cost_usdc": live_cost,
            "live_money_profit_usdc": live_profit,
            "live_money_roi": round(live_profit / live_cost, 6) if live_cost else None,
            "paper_fallback_cost_usdc": paper_cost,
            "paper_fallback_profit_usdc": paper_profit,
            "paper_fallback_roi": round(paper_profit / paper_cost, 6) if paper_cost else None,
        },
        "audit_quality": {
            "filled_rows_missing_fill_cost_usdc": rows_missing_cost,
            "filled_rows_missing_filled_shares": rows_missing_shares,
            "build_warnings": build_notes.get("warnings", []),
        },
    }


def write_outputs(
    rows: List[Dict[str, Any]],
    summary: Dict[str, Any],
    output_root: Path,
) -> Tuple[Path, Path, Path]:
    output_root.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_root / "analysis_safe_trades.jsonl"
    csv_path = output_root / "analysis_safe_trades.csv"
    summary_path = output_root / "analysis_safe_trades_summary.json"

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col) for col in OUTPUT_COLUMNS})

    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return jsonl_path, csv_path, summary_path


def build_analysis_safe_trade_table(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    warnings: List[str] = []
    sessions = load_sessions(
        args.sessions_dir,
        min_date=args.min_date,
        max_date=args.max_date,
        warnings=warnings,
    )
    ledger_by_bet = load_deduped_ledgers(
        [args.live_orders_ledger, args.master_ledger],
        min_date=args.min_date,
        max_date=args.max_date,
        warnings=warnings,
    )
    rows, build_notes = build_rows(
        sessions,
        ledger_by_bet,
        include_errors=bool(args.include_errors),
    )
    summary = _summarize_rows(
        rows,
        sessions=sessions,
        ledger_by_bet=ledger_by_bet,
        load_warnings=warnings,
        build_notes=build_notes,
    )
    return rows, summary


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    rows, summary = build_analysis_safe_trade_table(args)
    jsonl_path, csv_path, summary_path = write_outputs(rows, summary, args.output_root)

    hard_warnings = list(summary.get("audit_quality", {}).get("build_warnings", []))
    hard_warnings.extend(summary.get("source", {}).get("load_warnings", []))
    if args.strict and hard_warnings:
        print(f"ERROR: strict mode found warnings: {hard_warnings[:5]}")
        return 1

    pnl = summary["pnl"]
    counts = summary["row_counts"]
    print(
        "analysis-safe trades: "
        f"rows={counts['analysis_safe_rows']} filled={counts['filled_rows']} "
        f"live_filled={counts['live_money_filled_rows']} paper_fallback_filled={counts['paper_fallback_filled_rows']} "
        f"excluded={counts['excluded']}"
    )
    print(
        "P&L: "
        f"live=${pnl['live_money_profit_usdc']:+.2f} "
        f"paper_fallback=${pnl['paper_fallback_profit_usdc']:+.2f} "
        f"all_filled=${pnl['all_filled_profit_usdc']:+.2f}"
    )
    print(f"wrote {jsonl_path}")
    print(f"wrote {csv_path}")
    print(f"wrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
