#!/usr/bin/env python3
"""
Build an UNDER-only paper ledger from the side-neutral opportunity table.

This is a shadow research ledger. It never places live orders. The row unit is
the first eligible UNDER opportunity per game-line/score segment so repeated
ticks do not masquerade as independent evidence.

Outputs:
  data/analysis_output/under_paper_ledger/
    under_paper_ledger_summary.json
    under_paper_ledger_rows.jsonl
    under_paper_ledger_rows.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))


LOGGER = logging.getLogger("build_under_paper_ledger")

DEFAULT_INPUT_PATH = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "side_neutral_opportunities"
    / "side_neutral_opportunities.jsonl"
)
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "under_paper_ledger"

DEFAULT_STAKE_USDC = 10.0
DEFAULT_DAILY_BUDGET_USDC = 80.0
DEFAULT_PER_GAME_BUDGET_FRACTION = 0.40
DEFAULT_MAX_ORDERS_PER_GAME = 2
DEFAULT_MAX_ORDERS_PER_GAME_LINE = 1
DEFAULT_MIN_UNDER_EDGE = 0.05
UNDER_POLICY_VARIANT_THRESHOLDS = (0.10, 0.15)

LEDGER_COLUMNS = [
    "ledger_id",
    "paper_order_id",
    "source_row_id",
    "dedup_key",
    "duplicate_rows_collapsed",
    "session_date",
    "ts",
    "decision",
    "skip_reason",
    "game_pk",
    "away_abbrev",
    "home_abbrev",
    "line",
    "inning",
    "inning_state",
    "outs",
    "runners_on",
    "away_score",
    "home_score",
    "current_total",
    "expected_remaining_half_innings",
    "expected_remaining_pa_bucket",
    "home_skip_bottom9_risk",
    "fair_under",
    "under_bid",
    "under_ask",
    "under_mid",
    "under_edge_to_ask",
    "under_market_logit_residual",
    "over_bid",
    "over_ask",
    "fair_over",
    "over_edge_to_ask",
    "over_under_ask_sum",
    "price_policy",
    "fill_assumption",
    "limit_price",
    "fill_price",
    "filled",
    "settled",
    "under_hit",
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
    "daily_committed_before",
    "daily_committed_after_submit",
    "game_budget_usdc",
    "game_committed_before",
    "game_committed_after_submit",
    "game_open_or_filled_before",
    "game_line_open_or_filled_before",
]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build UNDER-only paper ledger from side-neutral opportunities.")
    p.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    p.add_argument("--min-date", type=str, default="", help="Inclusive YYYY-MM-DD.")
    p.add_argument("--max-date", type=str, default="", help="Inclusive YYYY-MM-DD.")
    p.add_argument("--stake", type=float, default=DEFAULT_STAKE_USDC)
    p.add_argument("--daily-budget", type=float, default=DEFAULT_DAILY_BUDGET_USDC)
    p.add_argument("--per-game-budget-fraction", type=float, default=DEFAULT_PER_GAME_BUDGET_FRACTION)
    p.add_argument("--max-orders-per-game", type=int, default=DEFAULT_MAX_ORDERS_PER_GAME)
    p.add_argument("--max-orders-per-game-line", type=int, default=DEFAULT_MAX_ORDERS_PER_GAME_LINE)
    p.add_argument("--min-under-edge", type=float, default=DEFAULT_MIN_UNDER_EDGE)
    p.add_argument(
        "--price-policy",
        choices=["taker", "bid_plus_cents", "ask_minus_cents"],
        default="taker",
    )
    p.add_argument("--price-offset-cents", type=float, default=1.0)
    p.add_argument(
        "--fill-assumption",
        choices=["immediate", "touch_same_tick"],
        default="immediate",
        help="Paper fill assumption. touch_same_tick fills if limit >= current under ask.",
    )
    p.add_argument("--include-unsettled", action="store_true")
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--strict", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        if not math.isfinite(out):
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


def _bool_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if int(value) == 1 else 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return 1
    if text in {"0", "false", "no", "n"}:
        return 0
    return None


def _date_in_range(date_str: str, min_date: str, max_date: str) -> bool:
    if min_date and date_str < min_date:
        return False
    if max_date and date_str > max_date:
        return False
    return True


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            rows.append(json.loads(raw))
    return rows


def _round_price(price: float) -> float:
    return round(max(0.01, min(0.99, float(price))), 2)


def _round_money(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 2)


def _dedup_key(row: Dict[str, Any]) -> str:
    return "|".join(
        str(row.get(k) if row.get(k) is not None else "")
        for k in (
            "session_date",
            "game_pk",
            "line",
            "away_score",
            "home_score",
        )
    )


def _sort_key(row: Dict[str, Any]) -> Tuple[str, str, str]:
    return (str(row.get("session_date") or ""), str(row.get("ts") or ""), str(row.get("row_id") or ""))


def load_source_rows(path: Path, min_date: str = "", max_date: str = "") -> List[Dict[str, Any]]:
    rows = [
        r
        for r in _read_jsonl(path)
        if _date_in_range(str(r.get("session_date") or ""), min_date, max_date)
    ]
    rows.sort(key=_sort_key)
    return rows


def dedupe_score_segments(rows: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    first: Dict[str, Dict[str, Any]] = {}
    counts: Dict[str, int] = defaultdict(int)
    for row in sorted(rows, key=_sort_key):
        key = _dedup_key(row)
        counts[key] += 1
        if key not in first:
            out = dict(row)
            out["dedup_key"] = key
            first[key] = out
    out_rows = list(first.values())
    for row in out_rows:
        row["duplicate_rows_collapsed"] = counts.get(str(row.get("dedup_key")), 1)
    out_rows.sort(key=_sort_key)
    return out_rows, dict(counts)


def _limit_price(row: Dict[str, Any], price_policy: str, offset_cents: float) -> Optional[float]:
    bid = _safe_float(row.get("under_bid"))
    ask = _safe_float(row.get("under_ask"))
    offset = offset_cents / 100.0
    if ask is None:
        return None
    if price_policy == "taker":
        return _round_price(ask)
    if price_policy == "bid_plus_cents":
        if bid is None:
            return None
        return _round_price(min(ask, bid + offset))
    if price_policy == "ask_minus_cents":
        return _round_price(max(0.01, ask - offset))
    return None


def _filled(row: Dict[str, Any], *, limit_price: Optional[float], fill_assumption: str) -> bool:
    if limit_price is None:
        return False
    ask = _safe_float(row.get("under_ask"))
    if fill_assumption == "immediate":
        return ask is not None and limit_price >= ask - 1e-9
    if fill_assumption == "touch_same_tick":
        return ask is not None and limit_price >= ask - 1e-9
    return False


def _ledger_row(
    *,
    idx: int,
    row: Dict[str, Any],
    decision: str,
    skip_reason: str,
    price_policy: str,
    fill_assumption: str,
    stake_usdc: float,
    daily_budget_usdc: float,
    daily_committed_before: float,
    daily_committed_after: float,
    game_budget_usdc: float,
    game_committed_before: float,
    game_committed_after: float,
    game_open_before: int,
    game_line_open_before: int,
    limit_price: Optional[float],
    fill_price: Optional[float],
    filled: bool,
) -> Dict[str, Any]:
    under_hit = _bool_int(row.get("target_under_win"))
    settled = under_hit is not None
    filled_shares = (stake_usdc / fill_price) if filled and fill_price and fill_price > 0 else None
    fill_cost_usdc = stake_usdc if filled else None
    payout_usdc = filled_shares if filled and under_hit == 1 else (0.0 if filled and settled else None)
    profit_usdc = (payout_usdc - fill_cost_usdc) if payout_usdc is not None and fill_cost_usdc is not None else None
    ledger_id = f"under_paper_{idx:06d}"
    out = {
        "ledger_id": ledger_id,
        "paper_order_id": f"{ledger_id}_order" if decision == "submitted" else None,
        "source_row_id": row.get("row_id"),
        "dedup_key": row.get("dedup_key") or _dedup_key(row),
        "duplicate_rows_collapsed": row.get("duplicate_rows_collapsed"),
        "session_date": row.get("session_date"),
        "ts": row.get("ts"),
        "decision": decision,
        "skip_reason": skip_reason,
        "game_pk": row.get("game_pk"),
        "away_abbrev": row.get("away_abbrev"),
        "home_abbrev": row.get("home_abbrev"),
        "line": row.get("line"),
        "inning": row.get("inning"),
        "inning_state": row.get("inning_state"),
        "outs": row.get("outs"),
        "runners_on": row.get("runners_on"),
        "away_score": row.get("away_score"),
        "home_score": row.get("home_score"),
        "current_total": row.get("current_total"),
        "expected_remaining_half_innings": row.get("expected_remaining_half_innings"),
        "expected_remaining_pa_bucket": row.get("expected_remaining_pa_bucket"),
        "home_skip_bottom9_risk": row.get("home_skip_bottom9_risk"),
        "fair_under": row.get("fair_under"),
        "under_bid": row.get("under_bid"),
        "under_ask": row.get("under_ask"),
        "under_mid": row.get("under_mid"),
        "under_edge_to_ask": row.get("under_edge_to_ask"),
        "under_market_logit_residual": row.get("under_market_logit_residual"),
        "over_bid": row.get("over_bid"),
        "over_ask": row.get("over_ask"),
        "fair_over": row.get("fair_over"),
        "over_edge_to_ask": row.get("over_edge_to_ask"),
        "over_under_ask_sum": row.get("over_under_ask_sum"),
        "price_policy": price_policy,
        "fill_assumption": fill_assumption,
        "limit_price": limit_price,
        "fill_price": fill_price,
        "filled": filled,
        "settled": settled,
        "under_hit": under_hit,
        "final_total": row.get("final_total"),
        "final_away": row.get("final_away"),
        "final_home": row.get("final_home"),
        "stake_usdc": stake_usdc if decision == "submitted" else None,
        "filled_shares": filled_shares,
        "fill_cost_usdc": fill_cost_usdc,
        "payout_usdc": payout_usdc,
        "profit_usdc": profit_usdc,
        "roi": (profit_usdc / stake_usdc) if profit_usdc is not None and stake_usdc else None,
        "daily_budget_usdc": daily_budget_usdc,
        "daily_committed_before": daily_committed_before,
        "daily_committed_after_submit": daily_committed_after,
        "game_budget_usdc": game_budget_usdc,
        "game_committed_before": game_committed_before,
        "game_committed_after_submit": game_committed_after,
        "game_open_or_filled_before": game_open_before,
        "game_line_open_or_filled_before": game_line_open_before,
    }
    return out


def build_ledger_rows(
    rows: Sequence[Dict[str, Any]],
    *,
    stake_usdc: float = DEFAULT_STAKE_USDC,
    daily_budget_usdc: float = DEFAULT_DAILY_BUDGET_USDC,
    per_game_budget_fraction: float = DEFAULT_PER_GAME_BUDGET_FRACTION,
    max_orders_per_game: int = DEFAULT_MAX_ORDERS_PER_GAME,
    max_orders_per_game_line: int = DEFAULT_MAX_ORDERS_PER_GAME_LINE,
    min_under_edge: float = DEFAULT_MIN_UNDER_EDGE,
    price_policy: str = "taker",
    fill_assumption: str = "immediate",
    price_offset_cents: float = 1.0,
    include_unsettled: bool = False,
) -> List[Dict[str, Any]]:
    deduped, _dupe_counts = dedupe_score_segments(rows)
    ledger_rows: List[Dict[str, Any]] = []
    daily_committed: Dict[str, float] = defaultdict(float)
    game_committed: Dict[Tuple[str, str], float] = defaultdict(float)
    game_counts: Counter = Counter()
    game_line_counts: Counter = Counter()
    game_budget = daily_budget_usdc * per_game_budget_fraction

    for idx, row in enumerate(deduped, start=1):
        date = str(row.get("session_date") or "")
        game_key = (date, str(row.get("game_pk") or ""))
        game_line_key = (date, str(row.get("game_pk") or ""), str(row.get("line") or ""))
        daily_before = daily_committed[date]
        game_before = game_committed[game_key]
        game_open_before = game_counts[game_key]
        game_line_open_before = game_line_counts[game_line_key]

        decision = "submitted"
        skip_reason = ""
        edge = _safe_float(row.get("under_edge_to_ask"))
        ask = _safe_float(row.get("under_ask"))
        target = _bool_int(row.get("target_under_win"))
        limit = _limit_price(row, price_policy, price_offset_cents)
        filled = False
        fill_price: Optional[float] = None

        if edge is None or edge < min_under_edge:
            decision, skip_reason = "skipped", "min_under_edge"
        elif ask is None:
            decision, skip_reason = "skipped", "missing_under_ask"
        elif target is None and not include_unsettled:
            decision, skip_reason = "skipped", "unsettled"
        elif daily_before + stake_usdc > daily_budget_usdc + 1e-9:
            decision, skip_reason = "skipped", "daily_budget"
        elif game_before + stake_usdc > game_budget + 1e-9:
            decision, skip_reason = "skipped", "game_budget"
        elif game_counts[game_key] >= max_orders_per_game:
            decision, skip_reason = "skipped", "max_orders_per_game"
        elif game_line_counts[game_line_key] >= max_orders_per_game_line:
            decision, skip_reason = "skipped", "max_orders_per_game_line"
        elif limit is None:
            decision, skip_reason = "skipped", "missing_limit_price"

        if decision == "submitted":
            filled = _filled(row, limit_price=limit, fill_assumption=fill_assumption)
            fill_price = limit if filled else None
            daily_committed[date] += stake_usdc
            game_committed[game_key] += stake_usdc
            game_counts[game_key] += 1
            game_line_counts[game_line_key] += 1

        ledger_rows.append(
            _ledger_row(
                idx=idx,
                row=row,
                decision=decision,
                skip_reason=skip_reason,
                price_policy=price_policy,
                fill_assumption=fill_assumption,
                stake_usdc=stake_usdc,
                daily_budget_usdc=daily_budget_usdc,
                daily_committed_before=daily_before,
                daily_committed_after=daily_committed[date],
                game_budget_usdc=game_budget,
                game_committed_before=game_before,
                game_committed_after=game_committed[game_key],
                game_open_before=game_open_before,
                game_line_open_before=game_line_open_before,
                limit_price=limit,
                fill_price=fill_price,
                filled=filled,
            )
        )
    return ledger_rows


def _edge_bucket(value: Any) -> str:
    edge = _safe_float(value)
    if edge is None:
        return "missing"
    if edge < 0:
        return "<0"
    if edge < 0.02:
        return "0-0.02"
    if edge < 0.05:
        return "0.02-0.05"
    if edge < 0.10:
        return "0.05-0.10"
    if edge < 0.15:
        return "0.10-0.15"
    return ">=0.15"


def _ask_bucket(value: Any) -> str:
    ask = _safe_float(value)
    if ask is None:
        return "missing"
    if ask < 0.40:
        return "<0.40"
    if ask < 0.55:
        return "0.40-0.55"
    if ask < 0.70:
        return "0.55-0.70"
    if ask < 0.85:
        return "0.70-0.85"
    return ">=0.85"


def _summarize_subset(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    filled = [r for r in rows if r.get("filled")]
    settled = [r for r in filled if r.get("under_hit") in (0, 1)]
    wins = sum(1 for r in settled if r.get("under_hit") == 1)
    stake = sum(_safe_float(r.get("fill_cost_usdc")) or 0.0 for r in filled)
    profit = sum(_safe_float(r.get("profit_usdc")) or 0.0 for r in settled)
    return {
        "rows": len(rows),
        "submitted_orders": sum(1 for r in rows if r.get("decision") == "submitted"),
        "skipped": sum(1 for r in rows if r.get("decision") == "skipped"),
        "filled_orders": len(filled),
        "settled_filled_orders": len(settled),
        "wins": wins,
        "losses": len(settled) - wins,
        "fill_rate": len(filled) / sum(1 for r in rows if r.get("decision") == "submitted")
        if any(r.get("decision") == "submitted" for r in rows)
        else None,
        "win_rate": wins / len(settled) if settled else None,
        "stake_filled_usdc": _round_money(stake),
        "profit_usdc": _round_money(profit),
        "roi": profit / stake if stake else None,
    }


def _group_under_summary(rows: Sequence[Dict[str, Any]], field: str) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if field == "under_edge_to_ask":
            key = _edge_bucket(row.get(field))
        elif field == "under_ask":
            key = _ask_bucket(row.get(field))
        else:
            key = str(row.get(field) or "missing")
        groups[key].append(row)
    return {key: _summarize_subset(group) for key, group in sorted(groups.items())}


def build_threshold_variant_summaries(
    source_rows: Sequence[Dict[str, Any]],
    args: argparse.Namespace,
) -> Dict[str, Dict[str, Any]]:
    variants: Dict[str, Dict[str, Any]] = {}
    for threshold in UNDER_POLICY_VARIANT_THRESHOLDS:
        rows = build_ledger_rows(
            source_rows,
            stake_usdc=args.stake,
            daily_budget_usdc=args.daily_budget,
            per_game_budget_fraction=args.per_game_budget_fraction,
            max_orders_per_game=args.max_orders_per_game,
            max_orders_per_game_line=args.max_orders_per_game_line,
            min_under_edge=threshold,
            price_policy=args.price_policy,
            fill_assumption=args.fill_assumption,
            price_offset_cents=args.price_offset_cents,
            include_unsettled=args.include_unsettled,
        )
        variants[f"min_under_edge_{threshold:.2f}"] = {
            "description": "Independent UNDER paper replay with this edge threshold.",
            "min_under_edge": threshold,
            "overall": _summarize_subset(rows),
            "by_under_edge_bucket": _group_under_summary(rows, "under_edge_to_ask"),
            "by_under_ask_bucket": _group_under_summary(rows, "under_ask"),
            "by_session_date": _group_under_summary(rows, "session_date"),
        }
    return variants


def build_summary(
    rows: Sequence[Dict[str, Any]],
    source_count: int,
    args: argparse.Namespace,
    *,
    policy_variants: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    by_edge: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_ask: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_skip: Dict[str, int] = defaultdict(int)
    for row in rows:
        by_edge[_edge_bucket(row.get("under_edge_to_ask"))].append(row)
        by_ask[_ask_bucket(row.get("under_ask"))].append(row)
        by_date[str(row.get("session_date") or "missing")].append(row)
        if row.get("decision") == "skipped":
            by_skip[str(row.get("skip_reason") or "missing")] += 1
    return {
        "generated_at_utc": _now_iso(),
        "description": "UNDER-only paper ledger from side-neutral raw tick opportunities.",
        "config": {
            "input_path": str(args.input_path),
            "min_date": args.min_date or None,
            "max_date": args.max_date or None,
            "stake": args.stake,
            "daily_budget": args.daily_budget,
            "per_game_budget_fraction": args.per_game_budget_fraction,
            "max_orders_per_game": args.max_orders_per_game,
            "max_orders_per_game_line": args.max_orders_per_game_line,
            "min_under_edge": args.min_under_edge,
            "price_policy": args.price_policy,
            "fill_assumption": args.fill_assumption,
            "price_offset_cents": args.price_offset_cents,
            "include_unsettled": args.include_unsettled,
        },
        "source_rows": source_count,
        "ledger_rows": len(rows),
        "overall": _summarize_subset(rows),
        "by_under_edge_bucket": {k: _summarize_subset(v) for k, v in sorted(by_edge.items())},
        "by_under_ask_bucket": {k: _summarize_subset(v) for k, v in sorted(by_ask.items())},
        "by_session_date": {k: _summarize_subset(v) for k, v in sorted(by_date.items())},
        "skips_by_reason": dict(sorted(by_skip.items())),
        "policy_variants": policy_variants or {},
        "warnings": [
            "Paper-only research output. Do not use as live evidence until side-aware walk-forward is mature.",
            "Rows are first eligible tick per game-line/score segment; labels are still correlated within game.",
        ],
    }


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]], columns: Sequence[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps({c: row.get(c) for c in columns}) + "\n")


def _write_csv(path: Path, rows: Sequence[Dict[str, Any]], columns: Sequence[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c) for c in columns})


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    if not args.input_path.exists():
        raise SystemExit(f"Missing input path: {args.input_path}")
    source_rows = load_source_rows(args.input_path, min_date=args.min_date, max_date=args.max_date)
    ledger_rows = build_ledger_rows(
        source_rows,
        stake_usdc=args.stake,
        daily_budget_usdc=args.daily_budget,
        per_game_budget_fraction=args.per_game_budget_fraction,
        max_orders_per_game=args.max_orders_per_game,
        max_orders_per_game_line=args.max_orders_per_game_line,
        min_under_edge=args.min_under_edge,
        price_policy=args.price_policy,
        fill_assumption=args.fill_assumption,
        price_offset_cents=args.price_offset_cents,
        include_unsettled=args.include_unsettled,
    )
    if args.strict and not ledger_rows:
        raise SystemExit("Strict mode failed: no UNDER paper ledger rows produced.")

    args.output_root.mkdir(parents=True, exist_ok=True)
    rows_path = args.output_root / "under_paper_ledger_rows.jsonl"
    csv_path = args.output_root / "under_paper_ledger_rows.csv"
    summary_path = args.output_root / "under_paper_ledger_summary.json"
    _write_jsonl(rows_path, ledger_rows, LEDGER_COLUMNS)
    _write_csv(csv_path, ledger_rows, LEDGER_COLUMNS)
    policy_variants = build_threshold_variant_summaries(source_rows, args)
    _write_json(
        summary_path,
        build_summary(ledger_rows, len(source_rows), args, policy_variants=policy_variants),
    )
    LOGGER.info("Wrote %s", rows_path)
    LOGGER.info("Wrote %s", csv_path)
    LOGGER.info("Wrote %s", summary_path)


if __name__ == "__main__":
    main()
