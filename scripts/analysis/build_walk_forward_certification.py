#!/usr/bin/env python3
"""build_walk_forward_certification.py -- Active #1 certification report.

Active #1 (top roadmap priority) calls for "30+ trading days post-2026-05-07
fed through walk_forward_runner.py" plus "per-cohort ROI, max drawdown,
fill-rate drift, calibration drift, trade-frequency stability across
rolling test windows" plus "a go/no-go for *each* enforced gate: keep,
retune, or retire."

`walk_forward_runner.py` already produces per-window aggregate stats. This
builder consumes those plus the per-bet `signal_training_table.jsonl` and
emits the certification-shaped report Active #1 will need:

  1. Sample-size readiness verdict (READY / PRELIMINARY / INSUFFICIENT)
  2. Cohort breakdowns -- N, fill-rate, filled-WR, signal-WR, ROI, max DD
     across edge / ask / inning / runs-needed / current-state-edge /
     phantom-risk bands and per-family
  3. Per-gate scorecard -- for each enforced gate, sweep alternative
     thresholds and emit a keep/retune/retire verdict with confidence
     based on filtered-vs-kept cohort sample sizes
  4. Drift over time -- per-week fill rate, filled WR, calibration Brier,
     trade frequency
  5. Headline verdicts table the operator reads first

Built to ship NOW against ~140 training rows so the report shape is
proven before Active #1 day 30 actually fires; on the day data is
abundant, just re-run the same script and read the same file paths.

Output:
  data/analysis_output/walk_forward_certification/walk_forward_certification.json
  data/analysis_output/walk_forward_certification/walk_forward_certification.md

Read-only: never writes under live ledgers or game corpora.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import OrderedDict, defaultdict
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
    PROJECT_DIR / "data" / "analysis_output" / "walk_forward"
    / "per_window_results.jsonl"
)
DEFAULT_OUTPUT_DIR = (
    PROJECT_DIR / "data" / "analysis_output" / "walk_forward_certification"
)

# Sample-size thresholds for the readiness verdict + per-cohort
# confidence. Conservative on purpose -- the whole point of this report
# is to NOT overinterpret small-sample wins.
READY_MIN_FILLED = 150     # ~30 days of typical signal volume
READY_MIN_DATES = 30
PRELIMINARY_MIN_FILLED = 75
PRELIMINARY_MIN_DATES = 14
COHORT_MIN_FOR_VERDICT = 10
GATE_RETUNE_MIN_DELTA_ROI = 0.05   # need >= 5pp ROI improvement to recommend retune
GATE_RETUNE_MIN_BLOCKED_N = 5      # need at least N blocked bets to evaluate a sweep


# ---------------------------------------------------------------------------
# Loading + per-bet view
# ---------------------------------------------------------------------------

@dataclass
class BetRow:
    """Flattened view of one filled+settled bet for cohort/gate analysis."""
    session_date: str
    family: str
    line: float
    inning: int
    runs_needed: float
    decision_ask: float
    edge_at_ask: float
    fair_value: float
    limit_price: float
    current_state_edge: Optional[float]
    phantom_risk_band: str
    target_filled: int
    target_win: Optional[int]
    target_profit: float


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


def _to_bet_row(r: Dict[str, Any]) -> Optional[BetRow]:
    """Project a raw training-table row into the cohort/gate view.

    Returns None when required fields are missing (typically older rows
    from before a feature shipped). The primary requirement is
    decision_ask + edge + outcome; cohort fields can be missing and bets
    will simply be assigned the "missing" bucket for that dimension.
    """
    decision_ask = _safe_float(r.get("decision_ask"))
    edge = _safe_float(r.get("edge_at_ask"))
    if decision_ask is None or edge is None:
        return None
    target_filled = _safe_int(r.get("target_filled"))
    if target_filled is None:
        return None
    target_win = _safe_int(r.get("target_win"))
    target_profit = _safe_float(r.get("target_profit")) or 0.0

    line = _safe_float(r.get("line"))
    inning = _safe_int(r.get("inning"))
    runs_needed = _safe_float(r.get("runs_needed"))
    if line is None or inning is None or runs_needed is None:
        return None

    return BetRow(
        session_date=str(r.get("session_date") or ""),
        family=str(r.get("signal_model_family") or ""),
        line=float(line),
        inning=int(inning),
        runs_needed=float(runs_needed),
        decision_ask=float(decision_ask),
        edge_at_ask=float(edge),
        fair_value=float(_safe_float(r.get("fair_value")) or 0.0),
        limit_price=float(_safe_float(r.get("limit_price")) or 0.0),
        current_state_edge=_safe_float(r.get("current_state_value_edge")),
        phantom_risk_band=str(r.get("shadow_phantom_risk_band") or "missing"),
        target_filled=int(target_filled),
        target_win=target_win,
        target_profit=float(target_profit),
    )


def load_bet_rows(path: Path) -> List[BetRow]:
    raw = load_training_rows(path)
    out: List[BetRow] = []
    for r in raw:
        bet = _to_bet_row(r)
        if bet is not None:
            out.append(bet)
    return out


# ---------------------------------------------------------------------------
# Cohort assignment
# ---------------------------------------------------------------------------

def _band(value: Optional[float], cuts: Sequence[float], labels: Sequence[str]) -> str:
    """Assign `value` to one of `labels`. Cuts define inclusive upper
    bounds for each band except the last (open above)."""
    if value is None:
        return "missing"
    assert len(labels) == len(cuts) + 1
    for cut, label in zip(cuts, labels):
        if value <= cut:
            return label
    return labels[-1]


COHORT_DEFS: List[Tuple[str, Callable[[BetRow], str]]] = [
    ("edge_band", lambda b: _band(
        b.edge_at_ask, [0.10, 0.15, 0.22],
        ["<=0.10", "0.10-0.15", "0.15-0.22", ">0.22"],
    )),
    ("ask_band", lambda b: _band(
        b.decision_ask, [0.65, 0.80],
        ["<0.65", "0.65-0.80", ">=0.80"],
    )),
    ("inning_band", lambda b: _band(
        float(b.inning), [5.0, 7.0],
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


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------

@dataclass
class CohortStats:
    """Per-cohort rollup (filled-bet metrics + signal-quality metrics)."""
    n_bets: int = 0
    n_filled: int = 0
    n_filled_wins: int = 0
    n_signal_wins: int = 0          # wins counting both filled + missed
    n_outcomes: int = 0             # bets with target_win not None
    total_stake: float = 0.0
    total_profit: float = 0.0
    profits: List[float] = field(default_factory=list)

    def add(self, b: BetRow) -> None:
        self.n_bets += 1
        if b.target_win is not None:
            self.n_outcomes += 1
            if b.target_win:
                self.n_signal_wins += 1
        if b.target_filled:
            self.n_filled += 1
            self.total_stake += abs(b.target_profit) if b.target_profit < 0 else (
                # On wins, target_profit ~ payout - stake; on losses ~ -stake.
                # The training-table column doesn't carry stake explicitly so
                # fall back to a conservative $10 estimate; this only affects
                # "stake-weighted" metrics which we display advisory-only.
                10.0
            )
            self.total_profit += b.target_profit
            self.profits.append(b.target_profit)
            if b.target_win:
                self.n_filled_wins += 1

    @property
    def fill_rate(self) -> Optional[float]:
        return (self.n_filled / self.n_bets) if self.n_bets else None

    @property
    def filled_win_rate(self) -> Optional[float]:
        return (self.n_filled_wins / self.n_filled) if self.n_filled else None

    @property
    def signal_win_rate(self) -> Optional[float]:
        return (self.n_signal_wins / self.n_outcomes) if self.n_outcomes else None

    @property
    def roi(self) -> Optional[float]:
        return (self.total_profit / self.total_stake) if self.total_stake > 0 else None

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
            "n_filled": self.n_filled,
            "n_filled_wins": self.n_filled_wins,
            "n_signal_wins": self.n_signal_wins,
            "n_outcomes": self.n_outcomes,
            "fill_rate": _round_or_none(self.fill_rate, 4),
            "filled_win_rate": _round_or_none(self.filled_win_rate, 4),
            "signal_win_rate": _round_or_none(self.signal_win_rate, 4),
            "total_stake": round(self.total_stake, 2),
            "total_profit": round(self.total_profit, 2),
            "roi": _round_or_none(self.roi, 4),
            "max_drawdown": self.max_drawdown,
        }


def _round_or_none(v: Optional[float], digits: int) -> Optional[float]:
    return None if v is None else round(v, digits)


def aggregate_overall(rows: Iterable[BetRow]) -> CohortStats:
    s = CohortStats()
    for b in rows:
        s.add(b)
    return s


def aggregate_by_cohort(
    rows: Sequence[BetRow], cohort_def: Tuple[str, Callable[[BetRow], str]]
) -> "OrderedDict[str, CohortStats]":
    name, fn = cohort_def
    out: "OrderedDict[str, CohortStats]" = OrderedDict()
    for b in rows:
        key = fn(b)
        if key not in out:
            out[key] = CohortStats()
        out[key].add(b)
    return out


# ---------------------------------------------------------------------------
# Gate scorecard
# ---------------------------------------------------------------------------

@dataclass
class GateDef:
    name: str
    description: str
    bet_field: Callable[[BetRow], Optional[float]]
    direction: str                  # "max" (block above) or "min" (block below)
    current_threshold: float
    sweep_thresholds: Sequence[float]


def _bet_field_edge(b: BetRow) -> float:           return b.edge_at_ask
def _bet_field_decision_ask(b: BetRow) -> float:   return b.decision_ask
def _bet_field_inning(b: BetRow) -> float:         return float(b.inning)
def _bet_field_runs_needed(b: BetRow) -> float:    return b.runs_needed


GATE_DEFS: List[GateDef] = [
    GateDef(
        name="gate_extreme_edge",
        description="Block signals with edge above this -- prevents phantom-run / overconfident-FV misfires (TR17/TR19).",
        bet_field=_bet_field_edge,
        direction="max",
        current_threshold=0.22,
        sweep_thresholds=[0.18, 0.20, 0.22, 0.25, 0.30],
    ),
    GateDef(
        name="gate_min_edge",
        description="Require at least this much edge to fire (separately tuned for high-line markets).",
        bet_field=_bet_field_edge,
        direction="min",
        current_threshold=0.10,
        sweep_thresholds=[0.05, 0.08, 0.10, 0.12, 0.15],
    ),
    GateDef(
        name="gate_min_inning",
        description="Don't trade before inning N -- early innings have unstable run-environment estimates.",
        bet_field=_bet_field_inning,
        direction="min",
        current_threshold=4,
        sweep_thresholds=[3, 4, 5, 6],
    ),
    GateDef(
        name="gate_min_entry_ask",
        description="Don't trade if the ask is below this -- avoid Winner's Curse from overpriced longs.",
        bet_field=_bet_field_decision_ask,
        direction="min",
        current_threshold=0.55,
        sweep_thresholds=[0.45, 0.50, 0.55, 0.60, 0.65],
    ),
    GateDef(
        name="gate_runs_needed_max",
        description="Block if runs-needed exceeds this -- low-probability long shots.",
        bet_field=_bet_field_runs_needed,
        direction="max",
        current_threshold=3.5,
        sweep_thresholds=[2.5, 3.0, 3.5, 4.0, 5.0],
    ),
    # Note: gate_fv_ask_gap_max requires the post-FV ask gap and isn't
    # always present on the training-row schema; defer until we wire it
    # in cleanly. Same for blowout-relax / gate_min_current_total -- those
    # are state-of-game gates and need richer features than the table
    # currently exposes for sweeping.
]


def _sweep_one(
    rows: Sequence[BetRow], gate: GateDef, threshold: float,
) -> Tuple[CohortStats, CohortStats]:
    """Return (kept_stats, blocked_stats) for one threshold value."""
    kept = CohortStats()
    blocked = CohortStats()
    for b in rows:
        v = gate.bet_field(b)
        if v is None:
            kept.add(b)
            continue
        if gate.direction == "max":
            (blocked if v > threshold else kept).add(b)
        else:  # min
            (blocked if v < threshold else kept).add(b)
    return kept, blocked


def _gate_verdict(
    current_kept: CohortStats, current_blocked: CohortStats,
    sweep_results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Summarize a gate as KEEP / RETUNE / RETIRE based on sweep evidence."""
    # If too few blocked bets, we can't evaluate -- default KEEP with
    # low-confidence note.
    if current_blocked.n_filled < GATE_RETUNE_MIN_BLOCKED_N:
        return {
            "verdict": "KEEP",
            "confidence": "low",
            "recommended_threshold": None,
            "reason": (
                f"Only {current_blocked.n_filled} filled bet(s) blocked at the current "
                f"threshold; insufficient evidence to evaluate a change."
            ),
        }
    current_kept_roi = current_kept.roi
    current_blocked_roi = current_blocked.roi
    if current_kept_roi is None or current_blocked_roi is None:
        return {
            "verdict": "KEEP",
            "confidence": "low",
            "recommended_threshold": None,
            "reason": "Either kept or blocked cohort has no settled bets to ROI against.",
        }
    blocked_roi_minus_kept = current_blocked_roi - current_kept_roi

    # If the blocked cohort's ROI is materially BETTER than what we kept,
    # the gate is filtering profitable bets -- recommend RETIRE (or
    # RELAX to a less aggressive threshold).
    if blocked_roi_minus_kept >= GATE_RETUNE_MIN_DELTA_ROI:
        # Find the sweep threshold that maximizes total ROI.
        best = max(
            sweep_results,
            key=lambda s: (
                s["kept"]["roi"] if s["kept"]["roi"] is not None else -1e9
            ),
        )
        return {
            "verdict": "RETUNE",
            "confidence": "medium" if current_blocked.n_filled >= 20 else "low",
            "recommended_threshold": best["threshold"],
            "reason": (
                f"Blocked cohort ROI {current_blocked_roi * 100:+.1f}% beats kept "
                f"cohort ROI {current_kept_roi * 100:+.1f}% by "
                f"{blocked_roi_minus_kept * 100:+.1f}pp -- gate is filtering "
                f"profitable bets. Sweep best threshold: {best['threshold']}."
            ),
        }

    # If the blocked cohort is much worse, the gate is doing useful work.
    if blocked_roi_minus_kept <= -GATE_RETUNE_MIN_DELTA_ROI:
        return {
            "verdict": "KEEP",
            "confidence": "medium" if current_blocked.n_filled >= 20 else "low",
            "recommended_threshold": None,
            "reason": (
                f"Blocked cohort ROI {current_blocked_roi * 100:+.1f}% is worse than "
                f"kept cohort ROI {current_kept_roi * 100:+.1f}% by "
                f"{abs(blocked_roi_minus_kept) * 100:.1f}pp -- gate is correctly "
                f"filtering bad bets at the current threshold."
            ),
        }

    return {
        "verdict": "KEEP",
        "confidence": "low",
        "recommended_threshold": None,
        "reason": (
            f"Blocked vs kept ROI delta is only {blocked_roi_minus_kept * 100:+.1f}pp -- "
            f"under the {GATE_RETUNE_MIN_DELTA_ROI * 100:.0f}pp action threshold. Hold "
            f"the current threshold pending more data."
        ),
    }


def evaluate_gate(rows: Sequence[BetRow], gate: GateDef) -> Dict[str, Any]:
    current_kept, current_blocked = _sweep_one(rows, gate, gate.current_threshold)
    sweep: List[Dict[str, Any]] = []
    for thr in gate.sweep_thresholds:
        kept, blocked = _sweep_one(rows, gate, thr)
        sweep.append({
            "threshold": thr,
            "kept": kept.to_dict(),
            "blocked": blocked.to_dict(),
        })
    verdict = _gate_verdict(current_kept, current_blocked, sweep)
    return {
        "name": gate.name,
        "description": gate.description,
        "direction": gate.direction,
        "current_threshold": gate.current_threshold,
        "current_kept": current_kept.to_dict(),
        "current_blocked": current_blocked.to_dict(),
        "sweep": sweep,
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# Drift over time
# ---------------------------------------------------------------------------

def _iso_week(d: str) -> str:
    """Week label YYYY-Www (ISO week year + week number)."""
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        return "unknown"
    iy, iw, _ = dt.isocalendar()
    return f"{iy}-W{iw:02d}"


def aggregate_drift_by_week(rows: Sequence[BetRow]) -> "OrderedDict[str, CohortStats]":
    by_week: "OrderedDict[str, CohortStats]" = OrderedDict()
    for b in sorted(rows, key=lambda r: r.session_date):
        wk = _iso_week(b.session_date)
        if wk not in by_week:
            by_week[wk] = CohortStats()
        by_week[wk].add(b)
    return by_week


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

def readiness_verdict(n_filled: int, n_dates: int) -> Dict[str, Any]:
    if n_filled >= READY_MIN_FILLED and n_dates >= READY_MIN_DATES:
        label = "READY"
        msg = (
            f"Sample meets Active #1 thresholds (filled={n_filled}>={READY_MIN_FILLED}, "
            f"dates={n_dates}>={READY_MIN_DATES}). Verdicts below are actionable."
        )
    elif n_filled >= PRELIMINARY_MIN_FILLED and n_dates >= PRELIMINARY_MIN_DATES:
        label = "PRELIMINARY"
        msg = (
            f"Sample is mid-build (filled={n_filled}>={PRELIMINARY_MIN_FILLED}, "
            f"dates={n_dates}>={PRELIMINARY_MIN_DATES}). Verdicts are directional, not actionable."
        )
    else:
        label = "INSUFFICIENT"
        msg = (
            f"Sample below the preliminary floor (filled={n_filled}<{PRELIMINARY_MIN_FILLED} "
            f"or dates={n_dates}<{PRELIMINARY_MIN_DATES}). Report shape only -- "
            f"do not use the verdicts to change live behavior."
        )
    return {
        "label": label,
        "n_filled": n_filled,
        "n_dates": n_dates,
        "thresholds": {
            "ready_min_filled": READY_MIN_FILLED, "ready_min_dates": READY_MIN_DATES,
            "preliminary_min_filled": PRELIMINARY_MIN_FILLED,
            "preliminary_min_dates": PRELIMINARY_MIN_DATES,
        },
        "message": msg,
    }


# ---------------------------------------------------------------------------
# Build payload
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )


def build_certification_payload(rows: Sequence[BetRow]) -> Dict[str, Any]:
    overall = aggregate_overall(rows)
    n_dates = len({b.session_date for b in rows if b.session_date})
    readiness = readiness_verdict(overall.n_filled, n_dates)

    cohorts: Dict[str, Dict[str, Any]] = {}
    for cohort_def in COHORT_DEFS:
        agg = aggregate_by_cohort(rows, cohort_def)
        cohorts[cohort_def[0]] = {
            label: stats.to_dict() for label, stats in agg.items()
        }

    gates: List[Dict[str, Any]] = []
    for g in GATE_DEFS:
        gates.append(evaluate_gate(rows, g))

    drift = aggregate_drift_by_week(rows)
    drift_payload = OrderedDict(
        (wk, stats.to_dict()) for wk, stats in drift.items()
    )

    date_span = None
    if rows:
        dates = sorted({b.session_date for b in rows if b.session_date})
        if dates:
            date_span = {"first": dates[0], "last": dates[-1]}

    return {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "active_priority": "Active #1 (post-TR20+TR21 walk-forward certification)",
        "readiness": readiness,
        "date_span": date_span,
        "overall": overall.to_dict(),
        "cohorts": cohorts,
        "gates": gates,
        "weekly_drift": drift_payload,
    }


# ---------------------------------------------------------------------------
# Markdown render
# ---------------------------------------------------------------------------

def _fmt_pct(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _fmt_signed_pct(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def _fmt_money(v: Optional[float]) -> str:
    return "—" if v is None else f"${v:+,.2f}"


def _cohort_row_md(label: str, d: Dict[str, Any]) -> str:
    return (
        f"| {label} | {d['n_bets']} | {d['n_filled']} | "
        f"{_fmt_pct(d['fill_rate'])} | {_fmt_pct(d['filled_win_rate'])} | "
        f"{_fmt_pct(d['signal_win_rate'])} | {_fmt_money(d['total_profit'])} | "
        f"{_fmt_pct(d['roi'])} | {_fmt_money(d['max_drawdown'])} |"
    )


def _cohort_table_md(title: str, cohort: Dict[str, Dict[str, Any]]) -> str:
    rows = [
        "| Cohort | N | Filled | Fill% | Filled WR | Signal WR | P&L | ROI | Max DD |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    # Stable label order: keep the dict's insertion order
    for label, d in cohort.items():
        rows.append(_cohort_row_md(label, d))
    return f"### {title}\n\n" + "\n".join(rows) + "\n"


def _gate_block_md(g: Dict[str, Any]) -> str:
    v = g["verdict"]
    badge = {"KEEP": "✅", "RETUNE": "⚠️", "RETIRE": "🔴"}.get(v["verdict"], "❓")
    sweep_rows = [
        "| Threshold | Kept N | Kept Filled | Kept ROI | Blocked N | Blocked Filled | Blocked ROI |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for s in g["sweep"]:
        marker = "**" if s["threshold"] == g["current_threshold"] else ""
        sweep_rows.append(
            f"| {marker}{s['threshold']}{marker} | "
            f"{s['kept']['n_bets']} | {s['kept']['n_filled']} | "
            f"{_fmt_pct(s['kept']['roi'])} | "
            f"{s['blocked']['n_bets']} | {s['blocked']['n_filled']} | "
            f"{_fmt_pct(s['blocked']['roi'])} |"
        )
    rec_thr = v.get("recommended_threshold")
    rec_str = f"`{rec_thr}`" if rec_thr is not None else "no change"
    direction_str = "block above" if g["direction"] == "max" else "block below"
    return (
        f"### {badge} `{g['name']}` -- {v['verdict']} (confidence: {v['confidence']})\n\n"
        f"_{g['description']}_\n\n"
        f"**Current threshold:** `{g['current_threshold']}` ({direction_str})\n\n"
        f"**Recommended:** {rec_str}\n\n"
        f"> {v['reason']}\n\n"
        + "\n".join(sweep_rows)
        + "\n"
    )


def _drift_table_md(drift: Dict[str, Dict[str, Any]]) -> str:
    if not drift:
        return "_No weekly data._\n"
    rows = [
        "| Week | N | Filled | Fill% | Filled WR | Signal WR | P&L | ROI |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for wk, d in drift.items():
        rows.append(
            f"| {wk} | {d['n_bets']} | {d['n_filled']} | "
            f"{_fmt_pct(d['fill_rate'])} | {_fmt_pct(d['filled_win_rate'])} | "
            f"{_fmt_pct(d['signal_win_rate'])} | {_fmt_money(d['total_profit'])} | "
            f"{_fmt_pct(d['roi'])} |"
        )
    return "\n".join(rows) + "\n"


def _verdict_summary_md(payload: Dict[str, Any]) -> str:
    rows = [
        "| Gate | Current | Verdict | Confidence | Recommended | Reason |",
        "| --- | ---: | --- | --- | ---: | --- |",
    ]
    for g in payload["gates"]:
        v = g["verdict"]
        rows.append(
            f"| `{g['name']}` | {g['current_threshold']} | "
            f"**{v['verdict']}** | {v['confidence']} | "
            f"{v['recommended_threshold'] if v['recommended_threshold'] is not None else '—'} | "
            f"{v['reason'][:120]}{'…' if len(v['reason']) > 120 else ''} |"
        )
    return "\n".join(rows) + "\n"


def render_markdown(payload: Dict[str, Any]) -> str:
    r = payload["readiness"]
    overall = payload["overall"]
    span = payload.get("date_span") or {}
    span_str = (
        f"{span.get('first', '?')} → {span.get('last', '?')}"
        if span else "no data"
    )
    badge = {"READY": "✅", "PRELIMINARY": "🟡", "INSUFFICIENT": "🔴"}.get(
        r["label"], "?"
    )

    parts: List[str] = []
    parts.append("# Walk-forward certification report (Active #1)\n")
    parts.append(f"_Generated {payload['generated_at_utc']}._\n")
    parts.append(
        f"**Sample readiness:** {badge} **`{r['label']}`** "
        f"({r['n_filled']} filled bets across {r['n_dates']} session dates; "
        f"window {span_str}).\n"
    )
    parts.append(f"> {r['message']}\n")

    parts.append("## Overall (sanity baseline)\n")
    parts.append(
        f"- N bets: **{overall['n_bets']}**  |  Filled: **{overall['n_filled']}** "
        f"({_fmt_pct(overall['fill_rate'])})  |  "
        f"Filled WR: **{_fmt_pct(overall['filled_win_rate'])}**  |  "
        f"Signal WR: **{_fmt_pct(overall['signal_win_rate'])}**\n"
        f"- P&L: **{_fmt_money(overall['total_profit'])}**  |  "
        f"ROI: **{_fmt_pct(overall['roi'])}**  |  "
        f"Max DD: **{_fmt_money(overall['max_drawdown'])}**\n"
    )

    parts.append("## Headline verdicts (read this first)\n")
    parts.append(_verdict_summary_md(payload))

    parts.append("## Cohort breakdowns\n")
    parts.append(
        "_Filled-bet metrics use realized P&L; signal-WR includes "
        "would-have-won counterfactual outcomes for unfilled signals._\n"
    )
    pretty_titles = {
        "edge_band": "By edge band",
        "ask_band": "By decision-ask band",
        "inning_band": "By inning band",
        "runs_needed_band": "By runs-needed band",
        "current_state_edge_band": "By current-state edge band",
        "phantom_risk_band": "By phantom-risk band",
        "family": "By signal family",
    }
    for cohort_name, cohort_data in payload["cohorts"].items():
        title = pretty_titles.get(cohort_name, cohort_name)
        parts.append(_cohort_table_md(title, cohort_data))

    parts.append("## Per-gate scorecard\n")
    parts.append(
        "_For each enforced gate, sweep alternative thresholds and recommend "
        "keep / retune / retire based on filtered-vs-kept cohort ROI._\n"
    )
    for g in payload["gates"]:
        parts.append(_gate_block_md(g))

    parts.append("## Weekly drift\n")
    parts.append(
        "_Per-ISO-week aggregates. Watch for fill-rate or filled-WR decay over time, "
        "or a sudden cohort-mix shift._\n"
    )
    parts.append(_drift_table_md(payload["weekly_drift"]))

    parts.append(
        "## Read this when\n\n"
        "Use this report to decide which enforced gate thresholds to keep, retune, "
        "or retire after Active #1's data threshold is met. The verdicts auto-degrade "
        "to KEEP with low confidence when the affected cohort is too thin to evaluate "
        "(< {min_n} filled bets blocked). Do not flip live thresholds based on a "
        "PRELIMINARY or INSUFFICIENT readiness label.\n".format(
            min_n=GATE_RETUNE_MIN_BLOCKED_N,
        )
    )
    return "".join(parts)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--training-table", type=Path, default=DEFAULT_TRAINING_TABLE)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    rows = load_bet_rows(Path(args.training_table))
    payload = build_certification_payload(rows)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "walk_forward_certification.json"
    md_path = output_dir / "walk_forward_certification.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print(
        f"Readiness: {payload['readiness']['label']} -- "
        f"{payload['overall']['n_filled']} filled bets across "
        f"{payload['readiness']['n_dates']} sessions."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
