#!/usr/bin/env python3
"""
Evaluate no-score drift as a paper/shadow policy.

This is an offline research tool for the state-value objective:
"State-value Over trading around market overreaction to score and no-score
transitions." It does not place or recommend live orders.

Policy shape:
  1. Read shadow no-score drift candidates from candidate_universe logs.
  2. Collapse to the first eligible row per game-line/same-score segment.
  3. Join final game-line outcomes.
  4. Report Poisson + empirical support separately from Poisson-only support.

Inputs:
  data/live_trading/candidate_universe/*_candidates.jsonl
  data/live_trading/candidate_universe/*_outcomes.jsonl
  data/paper_trading/candidate_universe/*_candidates.jsonl
  data/paper_trading/candidate_universe/*_outcomes.jsonl

Outputs:
  data/analysis_output/no_score_drift_policy/no_score_drift_policy_summary.json
  data/analysis_output/no_score_drift_policy/no_score_drift_policy_rows.jsonl
  data/analysis_output/no_score_drift_policy/no_score_drift_policy_rows.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
LIVE_CANDIDATES_ROOT = PROJECT_DIR / "data" / "live_trading" / "candidate_universe"
PAPER_CANDIDATES_ROOT = PROJECT_DIR / "data" / "paper_trading" / "candidate_universe"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "no_score_drift_policy"

DEFAULT_MIN_POISSON_EDGE = 0.10
DEFAULT_MIN_EMPIRICAL_EDGE = 0.08

from scripts.trading.remaining_opportunity import compute_remaining_opportunity_fields  # noqa: E402
from scripts.trading.scoring_path_features import SCORING_PATH_FIELD_KEYS  # noqa: E402
from scripts.trading.shadow_diagnostic_features import compute_shadow_diagnostic_fields  # noqa: E402
from scripts.trading.weather_client import WEATHER_FEATURE_FIELD_KEYS  # noqa: E402

OutcomeKey = Tuple[str, str, int, str]
SegmentKey = Tuple[str, str, int, str, str]


POLICY_ROW_COLUMNS = [
    "policy_row_id",
    "dedup_key",
    "duplicate_candidate_rows",
    "support_regime",
    "poisson_edge_pass",
    "empirical_edge_pass",
    "signal_model_family",
    "session_date",
    "mode",
    "ts",
    "candidate_id",
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
    *SCORING_PATH_FIELD_KEYS,
    *WEATHER_FEATURE_FIELD_KEYS,
    "score_segment_key",
    "score_segment_age_secs",
    "score_segment_ticks",
    "score_segment_high_ask",
    "score_segment_drawdown",
    "decision_ask",
    "best_bid",
    "spread",
    "fair_value",
    "edge",
    "current_state_value_base_poisson",
    "current_state_value_base_empirical",
    "current_state_value_edge",
    "current_state_value_empirical_edge",
    "current_state_value_fv_raw",
    "current_state_value_used_fallback",
    "current_state_value_state_fallback_level",
    "current_state_value_state_fallback_label",
    "current_state_value_line_fallback_mode",
    "shadow_low_ask_high_edge",
    "shadow_runs_needed_exact_3p5",
    "shadow_score_event_current_edge_strong_ask_lt_085",
    "shadow_current_state_edge_bucket",
    "shadow_phantom_risk_bucket",
    "shadow_current_phantom_combo_bucket",
    "shadow_inning_bucket",
    "shadow_inning_runs_needed_bucket",
    "shadow_bottom9_home_lead_context",
    "shadow_home_skip_bottom9_risk_bucket",
    "shadow_no_score_poisson_edge_bucket",
    "shadow_no_score_empirical_edge_bucket",
    "shadow_no_score_ask_bucket",
    "shadow_no_score_drawdown_bucket",
    "shadow_no_score_poisson_empirical_ask_drawdown_bucket",
    "shadow_no_score_drift_trigger",
    "shadow_no_score_drift_min_po_edge",
    "shadow_no_score_drift_min_emp_edge",
    "baseline_ask",
    "ask_jump",
    "lookback_ticks",
    "outcome_available",
    "over_hit",
    "final_total",
    "final_away",
    "final_home",
    "taker_profit_units",
]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate no-score drift paper/shadow policy.")
    p.add_argument("--mode", choices=["live", "paper", "both"], default="both")
    p.add_argument("--min-date", type=str, default="", help="Inclusive YYYY-MM-DD.")
    p.add_argument("--max-date", type=str, default="", help="Inclusive YYYY-MM-DD.")
    p.add_argument(
        "--min-poisson-edge",
        type=float,
        default=DEFAULT_MIN_POISSON_EDGE,
        help=f"Poisson/current-state support threshold (default: {DEFAULT_MIN_POISSON_EDGE}).",
    )
    p.add_argument(
        "--min-empirical-edge",
        type=float,
        default=DEFAULT_MIN_EMPIRICAL_EDGE,
        help=f"Empirical/current-state support threshold (default: {DEFAULT_MIN_EMPIRICAL_EDGE}).",
    )
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
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
        return int(value)
    except Exception:
        return None


def _date_in_range(date_str: str, min_date: str, max_date: str) -> bool:
    if min_date and date_str < min_date:
        return False
    if max_date and date_str > max_date:
        return False
    return True


def _line_key(value: Any) -> str:
    num = _safe_float(value)
    if num is None:
        return str(value)
    return f"{num:.1f}"


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if isinstance(row, dict):
                yield row


def _load_mode_rows(
    mode: str,
    root: Path,
    min_date: str,
    max_date: str,
) -> Tuple[List[Dict[str, Any]], Dict[OutcomeKey, Dict[str, Any]]]:
    candidates: List[Dict[str, Any]] = []
    outcomes: Dict[OutcomeKey, Dict[str, Any]] = {}
    if not root.exists():
        return candidates, outcomes

    for path in sorted(root.glob("*_outcomes.jsonl")):
        date_str = path.name[:10]
        if not _date_in_range(date_str, min_date, max_date):
            continue
        for row in _iter_jsonl(path):
            game_pk = _safe_int(row.get("game_pk"))
            line = row.get("line")
            if game_pk is None or line is None:
                continue
            outcomes[(date_str, mode, game_pk, _line_key(line))] = row

    for path in sorted(root.glob("*_candidates.jsonl")):
        date_str = path.name[:10]
        if not _date_in_range(date_str, min_date, max_date):
            continue
        for row in _iter_jsonl(path):
            row = dict(row)
            row.setdefault("session_date", date_str)
            row.setdefault("mode", mode)
            row["source_path"] = str(path)
            candidates.append(row)

    return candidates, outcomes


def load_rows(
    mode: str,
    min_date: str,
    max_date: str,
) -> Tuple[List[Dict[str, Any]], Dict[OutcomeKey, Dict[str, Any]]]:
    candidates: List[Dict[str, Any]] = []
    outcomes: Dict[OutcomeKey, Dict[str, Any]] = {}
    if mode in ("live", "both"):
        live_candidates, live_outcomes = _load_mode_rows(
            "live",
            LIVE_CANDIDATES_ROOT,
            min_date,
            max_date,
        )
        candidates.extend(live_candidates)
        outcomes.update(live_outcomes)
    if mode in ("paper", "both"):
        paper_candidates, paper_outcomes = _load_mode_rows(
            "paper",
            PAPER_CANDIDATES_ROOT,
            min_date,
            max_date,
        )
        candidates.extend(paper_candidates)
        outcomes.update(paper_outcomes)
    return candidates, outcomes


def _outcome_for(
    row: Dict[str, Any],
    outcomes: Dict[OutcomeKey, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    game_pk = _safe_int(row.get("game_pk"))
    line = row.get("line")
    if game_pk is None or line is None:
        return None
    return outcomes.get((
        str(row.get("session_date") or ""),
        str(row.get("mode") or ""),
        game_pk,
        _line_key(line),
    ))


def is_no_score_drift_candidate(row: Dict[str, Any]) -> bool:
    return (
        row.get("decision") == "shadow_no_score_drift"
        or row.get("decision_reason") == "state_value_no_score_drift"
        or row.get("state_value_strategy") == "no_score_drift"
    )


def classify_support_regime(
    row: Dict[str, Any],
    *,
    min_poisson_edge: float,
    min_empirical_edge: float,
) -> Tuple[str, bool, bool]:
    poisson_edge = _safe_float(row.get("current_state_value_edge"))
    if poisson_edge is None:
        poisson_edge = _safe_float(row.get("edge"))
    empirical_edge = _safe_float(row.get("current_state_value_empirical_edge"))

    poisson_pass = poisson_edge is not None and poisson_edge >= min_poisson_edge
    empirical_pass = empirical_edge is not None and empirical_edge >= min_empirical_edge

    if poisson_pass and empirical_pass:
        return "poisson_and_empirical_support", True, True
    if poisson_pass:
        return "poisson_only_support", True, False
    if empirical_pass:
        return "empirical_only_support", False, True
    return "weak_or_missing_support", False, False


def segment_key(row: Dict[str, Any]) -> SegmentKey:
    game_pk = _safe_int(row.get("game_pk"))
    score_segment = row.get("score_segment_key")
    if score_segment in (None, ""):
        away_score = row.get("away_score_before")
        home_score = row.get("home_score_before")
        score_segment = f"{away_score}-{home_score}"
    return (
        str(row.get("mode") or ""),
        str(row.get("session_date") or ""),
        int(game_pk) if game_pk is not None else -1,
        _line_key(row.get("line")),
        str(score_segment),
    )


def _sort_key(row: Dict[str, Any]) -> Tuple[str, str]:
    return (
        str(row.get("ts") or row.get("recorded_at") or ""),
        str(row.get("candidate_id") or ""),
    )


def _compact_value(row: Dict[str, Any], field: str) -> Any:
    value = row.get(field)
    if isinstance(value, float):
        return round(value, 6)
    return value


def build_policy_rows(
    candidates: List[Dict[str, Any]],
    outcomes: Dict[OutcomeKey, Dict[str, Any]],
    *,
    min_poisson_edge: float = DEFAULT_MIN_POISSON_EDGE,
    min_empirical_edge: float = DEFAULT_MIN_EMPIRICAL_EDGE,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    raw_rows = [row for row in candidates if is_no_score_drift_candidate(row)]
    duplicate_counts: Counter[SegmentKey] = Counter()
    first_by_segment: Dict[SegmentKey, Dict[str, Any]] = {}

    for row in sorted(raw_rows, key=_sort_key):
        key = segment_key(row)
        duplicate_counts[key] += 1
        if key not in first_by_segment:
            first_by_segment[key] = row

    policy_rows: List[Dict[str, Any]] = []
    for idx, (key, row) in enumerate(
        sorted(first_by_segment.items(), key=lambda item: _sort_key(item[1])),
        start=1,
    ):
        support_regime, poisson_pass, empirical_pass = classify_support_regime(
            row,
            min_poisson_edge=min_poisson_edge,
            min_empirical_edge=min_empirical_edge,
        )
        outcome = _outcome_for(row, outcomes)
        ask = _safe_float(row.get("decision_ask"))
        taker_profit = None
        over_hit = None
        if outcome is not None:
            over_hit = bool(outcome.get("over_hit"))
            if ask is not None and ask > 0:
                taker_profit = (1.0 / ask - 1.0) if over_hit else -1.0

        out: Dict[str, Any] = {
            "policy_row_id": f"nsd_{idx:06d}",
            "dedup_key": "|".join(str(part) for part in key),
            "duplicate_candidate_rows": duplicate_counts[key],
            "support_regime": support_regime,
            "poisson_edge_pass": poisson_pass,
            "empirical_edge_pass": empirical_pass,
            "signal_model_family": row.get("signal_model_family") or "no_score_drift",
            "outcome_available": outcome is not None,
            "over_hit": over_hit,
            "final_total": outcome.get("final_total") if outcome else None,
            "final_away": outcome.get("final_away") if outcome else None,
            "final_home": outcome.get("final_home") if outcome else None,
            "taker_profit_units": round(taker_profit, 6) if taker_profit is not None else None,
        }
        for field in POLICY_ROW_COLUMNS:
            if field in out:
                continue
            if field in row:
                out[field] = _compact_value(row, field)
        remaining_fields = compute_remaining_opportunity_fields(
            away_score=out.get("away_score_before"),
            home_score=out.get("home_score_before"),
            inning=out.get("inning"),
            inning_state=out.get("inning_state"),
        )
        for field, value in remaining_fields.items():
            if out.get(field) in (None, ""):
                out[field] = value
        for field, value in compute_shadow_diagnostic_fields(out).items():
            if out.get(field) in (None, ""):
                out[field] = value
        policy_rows.append(out)

    counts = {
        "raw_shadow_no_score_drift_rows": len(raw_rows),
        "policy_rows": len(policy_rows),
        "duplicate_rows_collapsed": len(raw_rows) - len(policy_rows),
        "outcome_keys": len(outcomes),
    }
    return policy_rows, counts


def _numeric_values(rows: List[Dict[str, Any]], field: str) -> List[float]:
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


def summarize_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    labeled = [row for row in rows if row.get("outcome_available")]
    wins = [row for row in labeled if row.get("over_hit") is True]
    losses = [row for row in labeled if row.get("over_hit") is False]
    profits = _numeric_values(labeled, "taker_profit_units")
    candidate_rows_represented = int(
        sum(_safe_int(row.get("duplicate_candidate_rows")) or 0 for row in rows)
    )
    unique_games = {
        (row.get("mode"), row.get("session_date"), row.get("game_pk"))
        for row in rows
    }
    unique_game_lines = {
        (row.get("mode"), row.get("session_date"), row.get("game_pk"), _line_key(row.get("line")))
        for row in rows
    }

    out: Dict[str, Any] = {
        "rows": len(rows),
        "labeled_rows": len(labeled),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(labeled), 6) if labeled else None,
        "taker_profit_units": round(sum(profits), 6) if profits else None,
        "taker_roi_per_cost": _mean(profits),
        "unique_games": len(unique_games),
        "unique_game_lines": len(unique_game_lines),
        "candidate_rows_represented": candidate_rows_represented,
        "duplicate_rows_collapsed": candidate_rows_represented - len(rows),
    }

    for field in [
        "decision_ask",
        "fair_value",
        "edge",
        "current_state_value_edge",
        "current_state_value_empirical_edge",
        "score_segment_age_secs",
        "score_segment_ticks",
        "score_segment_drawdown",
        "spread",
    ]:
        values = _numeric_values(rows, field)
        out[f"{field}_avg"] = _mean(values)
        out[f"{field}_min"] = round(min(values), 6) if values else None
        out[f"{field}_max"] = round(max(values), 6) if values else None

    return out


def _group_summary(rows: List[Dict[str, Any]], field: str) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get(field) or "missing"), []).append(row)
    return {
        key: summarize_rows(group)
        for key, group in sorted(grouped.items(), key=lambda item: item[0])
    }


def build_report(
    policy_rows: List[Dict[str, Any]],
    counts: Dict[str, Any],
    *,
    mode: str,
    min_date: str,
    max_date: str,
    min_poisson_edge: float,
    min_empirical_edge: float,
) -> Dict[str, Any]:
    return {
        "generated_at_utc": _now_iso(),
        "description": (
            "Offline no-score drift paper/shadow evaluator. Rows are first eligible "
            "shadow candidates per game-line/same-score segment; no live trading."
        ),
        "config": {
            "mode": mode,
            "min_date": min_date or None,
            "max_date": max_date or None,
            "min_poisson_edge": min_poisson_edge,
            "min_empirical_edge": min_empirical_edge,
            "dedup_rule": "first_eligible_row_per_mode_date_game_line_score_segment",
            "profit_model": "one_cost_unit_at_decision_ask",
        },
        "counts": counts,
        "overall": summarize_rows(policy_rows),
        "by_support_regime": _group_summary(policy_rows, "support_regime"),
        "by_trigger": _group_summary(policy_rows, "shadow_no_score_drift_trigger"),
        "by_inning": _group_summary(policy_rows, "inning"),
        "by_poisson_empirical_ask_drawdown": _group_summary(
            policy_rows,
            "shadow_no_score_poisson_empirical_ask_drawdown_bucket",
        ),
        "by_poisson_edge_bucket": _group_summary(policy_rows, "shadow_no_score_poisson_edge_bucket"),
        "by_empirical_edge_bucket": _group_summary(policy_rows, "shadow_no_score_empirical_edge_bucket"),
        "by_ask_bucket": _group_summary(policy_rows, "shadow_no_score_ask_bucket"),
        "by_drawdown_bucket": _group_summary(policy_rows, "shadow_no_score_drawdown_bucket"),
    }


def write_outputs(
    output_root: Path,
    policy_rows: List[Dict[str, Any]],
    report: Dict[str, Any],
) -> Dict[str, str]:
    output_root.mkdir(parents=True, exist_ok=True)
    summary_path = output_root / "no_score_drift_policy_summary.json"
    rows_jsonl_path = output_root / "no_score_drift_policy_rows.jsonl"
    rows_csv_path = output_root / "no_score_drift_policy_rows.csv"

    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    with open(rows_jsonl_path, "w", encoding="utf-8") as f:
        for row in policy_rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    with open(rows_csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=POLICY_ROW_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in policy_rows:
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
    if args.min_poisson_edge < 0:
        raise SystemExit("--min-poisson-edge must be >= 0")
    if args.min_empirical_edge < 0:
        raise SystemExit("--min-empirical-edge must be >= 0")


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    _validate_args(args)

    candidates, outcomes = load_rows(args.mode, args.min_date, args.max_date)
    policy_rows, counts = build_policy_rows(
        candidates,
        outcomes,
        min_poisson_edge=args.min_poisson_edge,
        min_empirical_edge=args.min_empirical_edge,
    )
    report = build_report(
        policy_rows,
        counts,
        mode=args.mode,
        min_date=args.min_date,
        max_date=args.max_date,
        min_poisson_edge=args.min_poisson_edge,
        min_empirical_edge=args.min_empirical_edge,
    )
    paths = write_outputs(args.output_root, policy_rows, report)

    print(f"Wrote {paths['summary']}")
    print(f"Wrote {paths['rows_jsonl']}")
    print(f"Wrote {paths['rows_csv']}")
    print(
        "No-score drift policy rows: "
        f"{counts['policy_rows']} "
        f"(collapsed {counts['duplicate_rows_collapsed']} duplicate candidate rows)"
    )
    for regime, metrics in report["by_support_regime"].items():
        print(
            f"  {regime}: rows={metrics['rows']} "
            f"win_rate={metrics['win_rate']} "
            f"profit_units={metrics['taker_profit_units']}"
        )


if __name__ == "__main__":
    main()
