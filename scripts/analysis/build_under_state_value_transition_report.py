#!/usr/bin/env python3
"""
Build UNDER-side state-value transition diagnostics.

Sibling to `build_state_value_transition_report.py` (which is OVER-side).
Phase A3 of the Bidirectional-trading roadmap (2026-05-16) foundation.
Pure offline analysis: this report does NOT change live trading
behavior; the live engine remains Over-only until Phase C wires
two-sided quoting.

Why a separate report (not a side-flipped reuse of the Over one):
  - UNDER outcomes have an asymmetric tail behavior the Over view
    cannot expose. A game ending 0-0 in regulation is a massive
    UNDER win that the Over-side report only sees as a normal loss.
  - The state-value REGIME classifiers are inverted on UNDER:
    a NEGATIVE current_state_value_edge (over is overvalued
    relative to current-state expectation) is a POSITIVE signal
    for Under, not a negative one.
  - Under-side ROI math uses `under_best_ask`, not `decision_ask`
    (which is the over ask). The under ask sits in the candidate
    row payload when under_pair_available=True (~50% in
    production today).

Inputs:
  data/live_trading/candidate_universe/*_candidates.jsonl
  data/live_trading/candidate_universe/*_outcomes.jsonl
  data/paper_trading/candidate_universe/*_candidates.jsonl
  data/paper_trading/candidate_universe/*_outcomes.jsonl

Output:
  data/analysis_output/under_state_value_transition/
    under_state_value_transition_report.json
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
LIVE_CANDIDATES_ROOT = PROJECT_DIR / "data" / "live_trading" / "candidate_universe"
PAPER_CANDIDATES_ROOT = PROJECT_DIR / "data" / "paper_trading" / "candidate_universe"
DEFAULT_OUTPUT_ROOT = (
    PROJECT_DIR / "data" / "analysis_output" / "under_state_value_transition"
)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build UNDER-side state-value transition diagnostics."
    )
    p.add_argument("--mode", choices=["live", "paper", "both"], default="both")
    p.add_argument("--min-date", type=str, default="", help="Inclusive YYYY-MM-DD.")
    p.add_argument("--max-date", type=str, default="", help="Inclusive YYYY-MM-DD.")
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument(
        "--top-n",
        type=int,
        default=50,
        help="Top rows to include per ranked list.",
    )
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


def _date_in_range(date_str: str, min_date: str, max_date: str) -> bool:
    if min_date and date_str < min_date:
        return False
    if max_date and date_str > max_date:
        return False
    return True


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
    mode: str, root: Path, min_date: str, max_date: str
) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, str, int, str], Dict[str, Any]]]:
    candidates: List[Dict[str, Any]] = []
    outcomes: Dict[Tuple[str, str, int, str], Dict[str, Any]] = {}
    if not root.exists():
        return candidates, outcomes

    for path in sorted(root.glob("*_outcomes.jsonl")):
        date_str = path.name[:10]
        if not _date_in_range(date_str, min_date, max_date):
            continue
        for row in _iter_jsonl(path):
            game_pk = row.get("game_pk")
            line = row.get("line")
            if game_pk is None or line is None:
                continue
            outcomes[(date_str, mode, int(game_pk), str(line))] = row

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
    mode: str, min_date: str, max_date: str
) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, str, int, str], Dict[str, Any]]]:
    rows: List[Dict[str, Any]] = []
    outcomes: Dict[Tuple[str, str, int, str], Dict[str, Any]] = {}
    if mode in ("live", "both"):
        live_rows, live_outcomes = _load_mode_rows(
            "live", LIVE_CANDIDATES_ROOT, min_date, max_date
        )
        rows.extend(live_rows)
        outcomes.update(live_outcomes)
    if mode in ("paper", "both"):
        paper_rows, paper_outcomes = _load_mode_rows(
            "paper", PAPER_CANDIDATES_ROOT, min_date, max_date
        )
        rows.extend(paper_rows)
        outcomes.update(paper_outcomes)
    return rows, outcomes


def _outcome_for(
    row: Dict[str, Any],
    outcomes: Dict[Tuple[str, str, int, str], Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    game_pk = row.get("game_pk")
    line = row.get("line")
    if game_pk is None or line is None:
        return None
    key = (
        str(row.get("session_date") or ""),
        str(row.get("mode") or ""),
        int(game_pk),
        str(line),
    )
    return outcomes.get(key)


def _under_hit(over_hit: Optional[bool]) -> Optional[bool]:
    """Symmetric flip. Outcome semantics: over_hit=True means total
    EXCEEDED the line; under_hit is the complement. The line itself
    is exclusive of `line.5`-style middles (Polymarket only lists
    `.5` lines today), so over/under never both win on the same game."""
    if over_hit is None:
        return None
    return not bool(over_hit)


def _under_ask_for(row: Dict[str, Any]) -> Optional[float]:
    """Under-side ask from the candidate row's book payload.

    When under_pair_available=False (~50% of rows in production), no
    under-side book was captured in that poll cycle and the under ask
    is None. The returned None routes the row to ROI=None so it does
    not pollute the aggregate. Callers should check `under_metrics`
    rows count vs total rows to see the coverage gap.
    """
    if not bool(row.get("under_pair_available")):
        return None
    return _safe_float(row.get("under_best_ask"))


def _metrics(
    rows: List[Dict[str, Any]],
    outcomes: Dict[Tuple[str, str, int, str], Dict[str, Any]],
) -> Dict[str, Any]:
    labels: List[bool] = []
    profits: List[float] = []
    under_ask_present = 0
    numeric_fields = {
        "under_best_ask": [],
        "under_best_bid": [],
        "decision_ask": [],
        "fair_value": [],
        "edge": [],
        "current_state_value_edge": [],
        "current_state_value_empirical_edge": [],
        "shadow_fv_inferred_lift": [],
        "shadow_phantom_risk_score": [],
        "score_segment_drawdown": [],
    }

    for row in rows:
        outcome = _outcome_for(row, outcomes)
        under_ask = _under_ask_for(row)
        if under_ask is not None:
            under_ask_present += 1
        if outcome is not None:
            won = _under_hit(outcome.get("over_hit"))
            if won is not None:
                labels.append(won)
                # Taker ROI per unit cost: buy under at under_ask;
                # win -> payout 1 (shares); lose -> payout 0.
                # ROI = (1/under_ask - 1) if won else -1.
                if under_ask is not None and under_ask > 0:
                    profits.append((1.0 / under_ask - 1.0) if won else -1.0)
        for field, values in numeric_fields.items():
            value = _safe_float(row.get(field))
            if value is not None:
                values.append(value)

    out: Dict[str, Any] = {
        "rows": len(rows),
        "rows_with_under_ask": under_ask_present,
        "under_ask_coverage": (
            round(under_ask_present / len(rows), 4) if rows else None
        ),
        "label_rows": len(labels),
        "under_win_rate": (
            round(sum(labels) / len(labels), 4) if labels else None
        ),
        "taker_roi_per_cost": round(mean(profits), 4) if profits else None,
        "taker_profit_units": round(sum(profits), 4) if profits else None,
    }
    for field, values in numeric_fields.items():
        if values:
            out[f"{field}_avg"] = round(mean(values), 4)
            out[f"{field}_min"] = round(min(values), 4)
            out[f"{field}_max"] = round(max(values), 4)
    return out


def _bucket_summary(
    rows: List[Dict[str, Any]],
    outcomes: Dict[Tuple[str, str, int, str], Dict[str, Any]],
    field: str,
) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get(field) or "missing"), []).append(row)
    return {
        key: _metrics(group, outcomes)
        for key, group in sorted(grouped.items(), key=lambda item: item[0])
    }


def _under_score_event_regime(row: Dict[str, Any]) -> str:
    """UNDER-side regime classifier.

    Inversion vs `_score_event_regime` in the Over report:
      - high_phantom + POSITIVE current_state_edge -> bad for Over,
        but for UNDER we are betting on the *opposite* outcome,
        so phantom-high + positive_over_edge means under is
        candidate-poor.
      - low/medium phantom + NEGATIVE current_state_edge means
        market over-values the Over (under is undervalued) -> good
        UNDER candidate.

    Phantom-risk band itself is OVER-side overheating; for UNDER we
    invert by reading its complement: high phantom + negative current
    edge is the UNDER analog of low phantom + positive current edge.
    """
    current_edge = _safe_float(row.get("current_state_value_edge"))
    band = str(row.get("shadow_phantom_risk_band") or "missing")
    if band in {"low", "medium"} and current_edge is not None and current_edge < 0:
        # Under is undervalued by current-state; over is overheated low
        return "low_medium_phantom_negative_current_edge"
    if band == "high" and current_edge is not None and current_edge >= 0:
        # Over phantom is high and current edge agrees -> under is the
        # contrarian side; flip of the Over-side high_phantom case.
        return "high_phantom_positive_current_edge"
    if current_edge is not None and current_edge < 0:
        return "negative_current_edge_other"
    if current_edge is not None and current_edge >= 0:
        return "positive_current_edge_other"
    return "missing_current_edge"


def _under_no_score_regime(row: Dict[str, Any]) -> str:
    """UNDER-side no-score-drift regime.

    On UNDER, a row with strong NEGATIVE empirical+poisson edge means
    over is overvalued in the no-score drift cohort: the Under side
    has support. Otherwise weak / mixed.
    """
    empirical_edge = _safe_float(row.get("current_state_value_empirical_edge"))
    poisson_edge = _safe_float(row.get("current_state_value_edge"))
    if (
        empirical_edge is not None and empirical_edge <= -0.08
        and poisson_edge is not None and poisson_edge <= -0.10
    ):
        return "poisson_and_empirical_under_support"
    if empirical_edge is not None and empirical_edge <= -0.08:
        return "empirical_under_support"
    if poisson_edge is not None and poisson_edge <= -0.10:
        return "poisson_only_under_support"
    return "weak_or_missing_under_support"


def _compact_row(
    row: Dict[str, Any],
    outcomes: Dict[Tuple[str, str, int, str], Dict[str, Any]],
) -> Dict[str, Any]:
    outcome = _outcome_for(row, outcomes) or {}
    fields = [
        "candidate_id",
        "ts",
        "mode",
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
        "decision",
        "decision_reason",
        "decision_ask",
        "fair_value",
        "edge",
        "current_state_value_edge",
        "current_state_value_empirical_edge",
        "shadow_phantom_risk_band",
        "shadow_phantom_risk_score",
        "shadow_fv_inferred_lift",
        "score_segment_drawdown",
        "shadow_no_score_drift_trigger",
        "under_pair_available",
        "under_best_bid",
        "under_best_ask",
    ]
    out = {field: row.get(field) for field in fields if row.get(field) is not None}
    if outcome:
        over_hit = outcome.get("over_hit")
        out["over_hit"] = bool(over_hit) if over_hit is not None else None
        under_hit = _under_hit(over_hit)
        out["under_hit"] = None if under_hit is None else bool(under_hit)
        out["final_total"] = outcome.get("final_total")
    return out


def build_report(
    rows: List[Dict[str, Any]],
    outcomes: Dict[Tuple[str, str, int, str], Dict[str, Any]],
    *,
    top_n: int = 50,
) -> Dict[str, Any]:
    """Build the UNDER state-value report.

    Mirrors the Over report's two-domain structure:
      - score_event_transition rows: bucketed by phantom band +
        Under-side regime classifier; ranked by highest UNDER
        opportunity (most negative current-state edge).
      - shadow_no_score_drift rows: bucketed by trigger + inning +
        Under-side regime; ranked by strongest empirical UNDER
        support (most negative empirical edge).
    """
    score_rows = [
        r for r in rows if r.get("state_value_strategy") == "score_event_transition"
    ]
    no_score_rows = [r for r in rows if r.get("decision") == "shadow_no_score_drift"]

    score_by_regime: Dict[str, List[Dict[str, Any]]] = {}
    for row in score_rows:
        score_by_regime.setdefault(_under_score_event_regime(row), []).append(row)

    drift_by_regime: Dict[str, List[Dict[str, Any]]] = {}
    for row in no_score_rows:
        drift_by_regime.setdefault(_under_no_score_regime(row), []).append(row)

    # Top UNDER candidates: most negative current_state_value_edge
    # (Over is over-priced relative to current state) -- inverse of
    # Over report which sorts by max positive current-state edge.
    ranked_drift = sorted(
        no_score_rows,
        key=lambda r: (
            _safe_float(r.get("current_state_value_empirical_edge")) is not None,
            -(_safe_float(r.get("current_state_value_empirical_edge")) or 999.0),
            -(_safe_float(r.get("current_state_value_edge")) or 999.0),
        ),
        reverse=True,
    )
    # For score_event rows, top UNDER candidates are those with the
    # most negative current-state edge AND lowest phantom score (low
    # over-overheating means under has room to run).
    ranked_phantom = sorted(
        score_rows,
        key=lambda r: (
            -(_safe_float(r.get("current_state_value_edge")) or -999.0),
            _safe_float(r.get("shadow_phantom_risk_score")) or 999.0,
        ),
        reverse=True,
    )

    return {
        "generated_at_utc": _now_iso(),
        "side": "under",
        "counts": {
            "candidate_rows": len(rows),
            "outcome_keys": len(outcomes),
            "decisions": dict(
                Counter(str(r.get("decision") or "missing") for r in rows)
            ),
            "score_event_transition_rows": len(score_rows),
            "shadow_no_score_drift_rows": len(no_score_rows),
        },
        "score_event_transition": {
            "overall": _metrics(score_rows, outcomes),
            "by_decision": _bucket_summary(score_rows, outcomes, "decision"),
            "by_phantom_band": _bucket_summary(
                score_rows, outcomes, "shadow_phantom_risk_band"
            ),
            "by_regime": {
                key: _metrics(group, outcomes)
                for key, group in sorted(score_by_regime.items())
            },
            "highest_under_opportunity_rows": [
                _compact_row(row, outcomes)
                for row in ranked_phantom[: max(0, top_n)]
            ],
        },
        "no_score_drift": {
            "overall": _metrics(no_score_rows, outcomes),
            "by_trigger": _bucket_summary(
                no_score_rows, outcomes, "shadow_no_score_drift_trigger"
            ),
            "by_inning": _bucket_summary(no_score_rows, outcomes, "inning"),
            "by_regime": {
                key: _metrics(group, outcomes)
                for key, group in sorted(drift_by_regime.items())
            },
            "ranked_under_support_rows": [
                _compact_row(row, outcomes)
                for row in ranked_drift[: max(0, top_n)]
            ],
        },
    }


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    if args.min_date:
        datetime.strptime(args.min_date, "%Y-%m-%d")
    if args.max_date:
        datetime.strptime(args.max_date, "%Y-%m-%d")
    if args.min_date and args.max_date and args.min_date > args.max_date:
        raise SystemExit("--min-date must be <= --max-date")

    rows, outcomes = load_rows(args.mode, args.min_date, args.max_date)
    report = build_report(rows, outcomes, top_n=args.top_n)
    report["config"] = {
        "mode": args.mode,
        "min_date": args.min_date or None,
        "max_date": args.max_date or None,
        "top_n": args.top_n,
    }

    args.output_root.mkdir(parents=True, exist_ok=True)
    out_path = args.output_root / "under_state_value_transition_report.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
