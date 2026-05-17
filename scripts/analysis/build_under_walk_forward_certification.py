#!/usr/bin/env python3
"""build_under_walk_forward_certification.py -- UNDER cert report.

Phase A4 sibling (2026-05-16) to `build_walk_forward_certification.py`.
Reads the same `signal_training_table.jsonl` + the UNDER walk-forward
per-window output, flips outcome to UNDER terms, and emits:

  1. Sample-size readiness verdict (READY / PRELIMINARY / INSUFFICIENT)
     against UNDER label rows (same thresholds as OVER; the data
     bottleneck is the same).
  2. Cohort breakdowns -- same dimensions as OVER cert (edge / ask /
     inning / runs-needed / current-state-edge / phantom-risk / family)
     but with UNDER outcome and UNDER ROI math.
  3. Calibration drift over weeks -- per-week Brier from the UNDER
     walk-forward windows when available.

Intentionally narrower than the OVER cert:
  - **No per-gate scorecard**. The OVER cert sweeps thresholds for the
    enforced OVER gates (gate_extreme_edge, gate_min_edge, ...). UNDER
    has no enforced gates today; sweeping nothing is misleading. The
    per-gate piece lands in Phase C when the first UNDER gate ships.
  - **No fill-rate cohort metrics**. UNDER fill behavior is unknown
    (no live UNDER orders ever posted). Cohort rows report
    UNDER-signal-win-rate and UNDER taker ROI only.

Outputs:
  data/analysis_output/under_walk_forward_certification/
    under_walk_forward_certification.json
    under_walk_forward_certification.md

Read-only: never writes under live ledgers or game corpora.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_TRAINING_TABLE = (
    PROJECT_DIR / "data" / "analysis_output" / "training_tables"
    / "signal_training_table.jsonl"
)
DEFAULT_PER_WINDOW_PATH = (
    PROJECT_DIR / "data" / "analysis_output" / "under_walk_forward"
    / "per_window_results.jsonl"
)
DEFAULT_OUTPUT_DIR = (
    PROJECT_DIR / "data" / "analysis_output" / "under_walk_forward_certification"
)

# Same sample-readiness thresholds as the OVER cert -- the data
# bottleneck is the same row source, so flipping the label doesn't
# change the sample-size story.
READY_MIN_LABELS = 150
READY_MIN_DATES = 30
PRELIMINARY_MIN_LABELS = 75
PRELIMINARY_MIN_DATES = 14
COHORT_MIN_FOR_VERDICT = 10


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _safe_int(v: Any) -> Optional[int]:
    f = _safe_float(v)
    return None if f is None else int(f)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class UnderBetRow:
    """Projection of one training-table row for UNDER cohort/cert math.

    Carries the OVER outcome (target_win) AND the under-ask. The UNDER
    label is derived as `1 - target_win` at aggregation time.
    """
    session_date: str
    family: str
    line: Optional[float]
    inning: int
    runs_needed: Optional[float]
    decision_ask: float
    edge_at_ask: Optional[float]
    current_state_edge: Optional[float]
    phantom_risk_band: str
    target_over_win: Optional[int]
    under_best_ask: Optional[float]
    under_pair_available: bool


def load_training_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _to_under_bet_row(r: Dict[str, Any]) -> Optional[UnderBetRow]:
    """Project a training-table row to the UNDER cohort view.

    Skips rows without target_win (unsettled) or without decision_ask
    (no usable inference). under_best_ask can be None (the ~50%
    coverage gap) -- such rows participate in cohort N counts but
    not in UNDER ROI math (the ROI metric reports None for them).
    """
    decision_ask = _safe_float(r.get("decision_ask"))
    if decision_ask is None:
        return None
    target_over_win = _safe_int(r.get("target_win"))
    if target_over_win is None:
        return None

    inning = _safe_int(r.get("inning"))
    if inning is None:
        inning = -1
    return UnderBetRow(
        session_date=str(r.get("session_date") or ""),
        family=str(r.get("signal_model_family") or "unknown"),
        line=_safe_float(r.get("line")),
        inning=inning,
        runs_needed=_safe_float(r.get("runs_needed")),
        decision_ask=decision_ask,
        edge_at_ask=_safe_float(r.get("edge_at_ask")),
        current_state_edge=_safe_float(r.get("current_state_value_edge")),
        phantom_risk_band=str(r.get("shadow_phantom_risk_band") or "missing"),
        target_over_win=target_over_win,
        under_best_ask=_safe_float(r.get("under_best_ask")),
        under_pair_available=bool(r.get("under_pair_available")),
    )


def load_under_bet_rows(path: Path) -> List[UnderBetRow]:
    raw_rows = load_training_rows(path)
    out: List[UnderBetRow] = []
    for r in raw_rows:
        row = _to_under_bet_row(r)
        if row is not None:
            out.append(row)
    return out


def _band(
    value: Optional[float], cuts: Sequence[float], labels: Sequence[str]
) -> str:
    if value is None:
        return "missing"
    assert len(labels) == len(cuts) + 1
    for cut, label in zip(cuts, labels):
        if value <= cut:
            return label
    return labels[-1]


COHORT_DEFS: List[Tuple[str, Callable[[UnderBetRow], str]]] = [
    ("edge_band", lambda b: _band(
        b.edge_at_ask, [0.10, 0.15, 0.22],
        ["<=0.10", "0.10-0.15", "0.15-0.22", ">0.22"],
    )),
    ("ask_band", lambda b: _band(
        b.decision_ask, [0.65, 0.80],
        ["<0.65", "0.65-0.80", ">=0.80"],
    )),
    ("inning_band", lambda b: _band(
        float(b.inning) if b.inning >= 0 else None, [5.0, 7.0],
        ["inn_4-5", "inn_6-7", "inn_8-9"],
    )),
    ("runs_needed_band", lambda b: _band(
        b.runs_needed, [1.5, 3.0],
        ["rn_<=1.5", "rn_2.0-3.0", "rn_>3.0"],
    )),
    ("current_state_edge_band", lambda b: _band(
        b.current_state_edge, [0.03, 0.08],
        ["cse_<0.03", "cse_0.03-0.08", "cse_>=0.08"],
    )),
    ("phantom_risk_band", lambda b: b.phantom_risk_band or "missing"),
    ("family", lambda b: b.family or "missing"),
]


@dataclass
class UnderCohortStats:
    """Per-cohort UNDER rollup.

    UNDER signal win = 1 - over_win.
    UNDER taker ROI: when bet wins, payout = 1/under_ask - 1; when
    bet loses, payout = -1. Requires under_pair_available row.
    """
    n_bets: int = 0
    n_outcomes: int = 0
    n_under_wins: int = 0
    n_with_under_ask: int = 0
    profits: List[float] = field(default_factory=list)

    def add(self, b: UnderBetRow) -> None:
        self.n_bets += 1
        under_win = (1 - b.target_over_win) if b.target_over_win is not None else None
        if under_win is not None:
            self.n_outcomes += 1
            if under_win:
                self.n_under_wins += 1
        if b.under_pair_available and b.under_best_ask is not None and b.under_best_ask > 0:
            self.n_with_under_ask += 1
            if under_win is not None:
                profit = (1.0 / b.under_best_ask - 1.0) if under_win else -1.0
                self.profits.append(profit)

    @property
    def under_signal_win_rate(self) -> Optional[float]:
        return (self.n_under_wins / self.n_outcomes) if self.n_outcomes else None

    @property
    def under_taker_roi(self) -> Optional[float]:
        if not self.profits:
            return None
        return sum(self.profits) / len(self.profits)

    @property
    def under_ask_coverage(self) -> Optional[float]:
        return (self.n_with_under_ask / self.n_bets) if self.n_bets else None

    @property
    def max_drawdown(self) -> Optional[float]:
        if not self.profits:
            return None
        peak = 0.0
        cum = 0.0
        worst = 0.0
        for p in self.profits:
            cum += p
            if cum > peak:
                peak = cum
            worst = min(worst, cum - peak)
        return round(worst, 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_bets": self.n_bets,
            "n_outcomes": self.n_outcomes,
            "n_under_wins": self.n_under_wins,
            "n_with_under_ask": self.n_with_under_ask,
            "under_signal_win_rate": (
                round(self.under_signal_win_rate, 4)
                if self.under_signal_win_rate is not None else None
            ),
            "under_ask_coverage": (
                round(self.under_ask_coverage, 4)
                if self.under_ask_coverage is not None else None
            ),
            "under_taker_roi": (
                round(self.under_taker_roi, 4)
                if self.under_taker_roi is not None else None
            ),
            "max_drawdown": self.max_drawdown,
        }


def aggregate_overall(rows: Iterable[UnderBetRow]) -> UnderCohortStats:
    s = UnderCohortStats()
    for b in rows:
        s.add(b)
    return s


def aggregate_by_cohort(
    rows: Sequence[UnderBetRow],
    keyer: Callable[[UnderBetRow], str],
) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, UnderCohortStats] = {}
    for b in rows:
        key = keyer(b)
        grouped.setdefault(key, UnderCohortStats()).add(b)
    return {k: grouped[k].to_dict() for k in sorted(grouped.keys())}


def readiness_verdict(n_outcomes: int, n_dates: int) -> Dict[str, Any]:
    if n_outcomes >= READY_MIN_LABELS and n_dates >= READY_MIN_DATES:
        verdict = "READY"
    elif n_outcomes >= PRELIMINARY_MIN_LABELS and n_dates >= PRELIMINARY_MIN_DATES:
        verdict = "PRELIMINARY"
    else:
        verdict = "INSUFFICIENT"
    return {
        "verdict": verdict,
        "n_under_outcomes": n_outcomes,
        "n_distinct_dates": n_dates,
        "ready_min_outcomes": READY_MIN_LABELS,
        "ready_min_dates": READY_MIN_DATES,
        "preliminary_min_outcomes": PRELIMINARY_MIN_LABELS,
        "preliminary_min_dates": PRELIMINARY_MIN_DATES,
    }


def _iso_week(d: str) -> str:
    try:
        parsed = date.fromisoformat(d)
    except (ValueError, TypeError):
        return "unknown"
    iso = parsed.isocalendar()
    return f"{iso[0]:04d}-W{iso[1]:02d}"


def aggregate_drift_by_week(
    rows: Sequence[UnderBetRow],
) -> "OrderedDict[str, Dict[str, Any]]":
    grouped: Dict[str, UnderCohortStats] = {}
    for b in rows:
        wk = _iso_week(b.session_date)
        grouped.setdefault(wk, UnderCohortStats()).add(b)
    out = OrderedDict()
    for wk in sorted(grouped.keys()):
        out[wk] = grouped[wk].to_dict()
    return out


def load_under_per_window(path: Path) -> List[Dict[str, Any]]:
    """Load UNDER walk-forward per_window_results.jsonl if present.

    Phase A4 ships the runner + cert simultaneously, but the runner
    can fail to find enough training history yet; cert should still
    render with an empty `per_window` block in that case.
    """
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _per_window_summary(per_window_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    completed = [w for w in per_window_rows if w.get("completed")]
    briers = [
        (w.get("task_metrics", {}).get("signal_win", {}) or {}).get("test", {}).get("brier")
        for w in completed
    ]
    briers = [b for b in briers if isinstance(b, (int, float))]
    return {
        "n_windows_total": len(per_window_rows),
        "n_windows_completed": len(completed),
        "test_brier_mean": (
            round(sum(briers) / len(briers), 6) if briers else None
        ),
        "test_brier_n_windows": len(briers),
    }


def build_certification_payload(
    rows: Sequence[UnderBetRow],
    per_window_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    overall = aggregate_overall(rows)
    distinct_dates = len({b.session_date for b in rows if b.session_date})

    by_cohort: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for cohort_name, keyer in COHORT_DEFS:
        by_cohort[cohort_name] = aggregate_by_cohort(rows, keyer)

    return {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "side": "under",
        "phase": "A4",
        "scope": "offline_only_until_phase_c",
        "readiness": readiness_verdict(
            n_outcomes=overall.n_outcomes,
            n_dates=distinct_dates,
        ),
        "overall": overall.to_dict(),
        "cohort_breakdowns": by_cohort,
        "drift_by_week": aggregate_drift_by_week(rows),
        "walk_forward": _per_window_summary(per_window_rows),
        "notes": {
            "under_outcome": "under_win = 1 - target_over_win",
            "under_roi": (
                "Taker ROI: payout = 1/under_best_ask - 1 on win, -1 on "
                "loss. Requires under_pair_available; rows without it "
                "count toward N but not toward ROI/profits."
            ),
            "excluded_features": (
                "Per-gate scorecard intentionally absent: no UNDER gates "
                "are enforced today. Fill metrics absent: no UNDER orders "
                "have ever been posted."
            ),
        },
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build UNDER walk-forward certification report."
    )
    p.add_argument("--training-table", type=Path, default=DEFAULT_TRAINING_TABLE)
    p.add_argument("--per-window-path", type=Path, default=DEFAULT_PER_WINDOW_PATH)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return p.parse_args(argv)


def _fmt_pct(v: Optional[float], digits: int = 1) -> str:
    return "n/a" if v is None else f"{v * 100:.{digits}f}%"


def _fmt_signed_pct(v: Optional[float], digits: int = 1) -> str:
    if v is None:
        return "n/a"
    return f"{v * 100:+.{digits}f}%"


def render_markdown(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# UNDER Walk-Forward Certification (Phase A4)")
    lines.append("")
    lines.append(f"Generated: {payload['generated_at_utc']}")
    lines.append(f"Side: **{payload['side']}**  ")
    lines.append(f"Scope: {payload['scope']}")
    lines.append("")
    r = payload["readiness"]
    lines.append(f"## Readiness verdict: **{r['verdict']}**")
    lines.append(
        f"- UNDER outcomes: {r['n_under_outcomes']} "
        f"(ready: >= {r['ready_min_outcomes']})"
    )
    lines.append(
        f"- Distinct dates: {r['n_distinct_dates']} "
        f"(ready: >= {r['ready_min_dates']})"
    )
    lines.append("")
    o = payload["overall"]
    lines.append("## Overall metrics")
    lines.append(
        f"- N bets: {o['n_bets']}, outcomes: {o['n_outcomes']}, "
        f"UNDER wins: {o['n_under_wins']}"
    )
    lines.append(
        f"- UNDER signal win rate: {_fmt_pct(o['under_signal_win_rate'])}"
    )
    lines.append(
        f"- UNDER ask coverage: {_fmt_pct(o['under_ask_coverage'])}"
    )
    lines.append(
        f"- UNDER taker ROI: {_fmt_signed_pct(o['under_taker_roi'])}, "
        f"max drawdown: {o['max_drawdown']}"
    )
    lines.append("")
    lines.append("## Cohort breakdowns")
    for cohort_name, buckets in payload["cohort_breakdowns"].items():
        lines.append(f"### {cohort_name}")
        for label, stats in buckets.items():
            lines.append(
                f"- `{label}`: n={stats['n_bets']}, "
                f"UNDER WR={_fmt_pct(stats['under_signal_win_rate'])}, "
                f"ROI={_fmt_signed_pct(stats['under_taker_roi'])}, "
                f"coverage={_fmt_pct(stats['under_ask_coverage'])}"
            )
        lines.append("")
    if payload.get("drift_by_week"):
        lines.append("## Per-week drift")
        for wk, stats in payload["drift_by_week"].items():
            lines.append(
                f"- {wk}: n={stats['n_bets']}, "
                f"UNDER WR={_fmt_pct(stats['under_signal_win_rate'])}, "
                f"ROI={_fmt_signed_pct(stats['under_taker_roi'])}"
            )
        lines.append("")
    wf = payload.get("walk_forward") or {}
    lines.append("## UNDER walk-forward summary")
    lines.append(
        f"- Windows planned: {wf.get('n_windows_total')}, "
        f"completed: {wf.get('n_windows_completed')}"
    )
    lines.append(f"- Mean test Brier (under signal_win): {wf.get('test_brier_mean')}")
    lines.append("")
    lines.append("## Notes")
    for k, v in (payload.get("notes") or {}).items():
        lines.append(f"- **{k}**: {v}")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    rows = load_under_bet_rows(args.training_table)
    per_window = load_under_per_window(args.per_window_path)
    payload = build_certification_payload(rows, per_window)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "under_walk_forward_certification.json"
    md_path = args.output_dir / "under_walk_forward_certification.md"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(payload))
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
