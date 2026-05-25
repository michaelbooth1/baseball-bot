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
    config_label: str = "default"
    # Fields added 2026-05-17 for the expanded per-gate scorecard so
    # composite + applicability-gated gates (e.g. gate_fv_ask_gap_max
    # applies only inning>=7; gate_min_current_total looks at the
    # game-state total) can be evaluated without schema gymnastics.
    current_total: Optional[int] = None
    lead_abs: Optional[int] = None
    base_fair_value: Optional[float] = None
    stage2_run_env_delta: Optional[float] = None


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
        config_label=str(r.get("config_label") or "default"),
        current_total=_safe_int(r.get("current_total")),
        lead_abs=_safe_int(r.get("lead_abs")),
        base_fair_value=_safe_float(r.get("base_fair_value")),
        stage2_run_env_delta=_safe_float(r.get("stage2_run_env_delta")),
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
    current_threshold: Optional[float]    # None for shadow-only (no enforcement today)
    sweep_thresholds: Sequence[float]
    # Optional predicate that filters the bet population BEFORE
    # threshold evaluation. Used for composite gates like
    # gate_fv_ask_gap_max (only applies inning>=7) or
    # gate_close_game_rn (only applies lead_abs<2). When None, the
    # gate sees every bet. Bets that fail applicability are
    # EXCLUDED from both kept and blocked cohorts so the verdict
    # comparison stays apples-to-apples within the gate's domain.
    # Added 2026-05-17 (expanded per-gate scorecard).
    applicability: Optional[Callable[[BetRow], bool]] = None
    # Shadow gates carry no enforced threshold; the cert report
    # surfaces the sweep so the operator can pick a value to
    # promote later. Indicated by current_threshold=None.
    shadow_only: bool = False


def _bet_field_edge(b: BetRow) -> float:           return b.edge_at_ask
def _bet_field_decision_ask(b: BetRow) -> float:   return b.decision_ask
def _bet_field_inning(b: BetRow) -> float:         return float(b.inning)
def _bet_field_runs_needed(b: BetRow) -> float:    return b.runs_needed


# ---- 2026-05-17 expanded scorecard helpers ----

def _bet_field_base_fair_value(b: BetRow) -> Optional[float]:
    return b.base_fair_value


def _bet_field_fv_ask_gap(b: BetRow) -> Optional[float]:
    """fair_value - decision_ask; the phantom-score detection gap."""
    if b.fair_value is None or b.decision_ask is None:
        return None
    return b.fair_value - b.decision_ask


def _bet_field_current_total(b: BetRow) -> Optional[float]:
    return float(b.current_total) if b.current_total is not None else None


def _bet_field_s2_delta(b: BetRow) -> Optional[float]:
    return b.stage2_run_env_delta


def _bet_field_current_state_edge(b: BetRow) -> Optional[float]:
    return b.current_state_edge


def _applies_inning_gte(threshold: int) -> Callable[[BetRow], bool]:
    """Composite-gate applicability: only rows in inning >= threshold."""
    def _pred(b: BetRow) -> bool:
        return b.inning is not None and b.inning >= threshold
    return _pred


def _applies_inning_eq(value: int) -> Callable[[BetRow], bool]:
    def _pred(b: BetRow) -> bool:
        return b.inning is not None and b.inning == value
    return _pred


def _applies_close_game(b: BetRow) -> bool:
    """Close-game gate domain: lead < 2."""
    return b.lead_abs is not None and b.lead_abs < 2


def _applies_high_line(b: BetRow) -> bool:
    """High-line gate domain: line >= 8.5 (matches DEFAULT_HIGH_LINE_CUTOFF)."""
    return b.line is not None and b.line >= 8.5


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
    # ---- 2026-05-17: expanded gate scorecard (9 new gates) ----
    # The original 5 gates above are all SINGLE-CONDITION + UNIVERSAL.
    # The 9 below split into:
    #   - 1 universal: gate_max_base_fv
    #   - 5 composite (applicability-gated): gate_fv_ask_gap_max,
    #     gate_min_current_total, gate_inn5_rn_max, gate_inn6_rn_max,
    #     gate_close_game_rn, gate_s2_suppress_max
    #   - 2 high-line-only: gate_high_line_min_edge,
    #     gate_high_line_min_inning
    #   - 1 shadow-only (no production threshold today):
    #     shadow_gate_current_state_edge_min
    # All thresholds are pulled from scripts/trading/signal_config.py
    # DEFAULT_* constants as of 2026-05-17.
    GateDef(
        name="gate_max_base_fv",
        description=(
            "Block if base_fair_value is at saturation (>0.99) -- "
            "Stage-1 prior alone exceeds 99% Over probability, "
            "which is a phantom-score fingerprint."
        ),
        bet_field=_bet_field_base_fair_value,
        direction="max",
        current_threshold=0.99,
        sweep_thresholds=[0.95, 0.97, 0.98, 0.99, 1.00],
    ),
    GateDef(
        name="gate_fv_ask_gap_max",
        description=(
            "Late-game phantom-score detection: in inning >= 7, "
            "block when (fair_value - decision_ask) exceeds the gap "
            "threshold. Default lowered 2026-05-17 to 0.26."
        ),
        bet_field=_bet_field_fv_ask_gap,
        direction="max",
        current_threshold=0.26,
        sweep_thresholds=[0.20, 0.24, 0.26, 0.28, 0.30, 0.35],
        applicability=_applies_inning_gte(7),
    ),
    GateDef(
        name="gate_min_current_total",
        description=(
            "Block when game is too low-scoring (away+home < N runs) "
            "-- low-total games don't generate the Over pressure the "
            "model expects."
        ),
        bet_field=_bet_field_current_total,
        direction="min",
        current_threshold=4,
        sweep_thresholds=[2, 3, 4, 5, 6],
    ),
    GateDef(
        name="gate_inn5_rn_max",
        description=(
            "Inning 5 reliever transition: in inning == 5, block "
            "when runs_needed >= 2.5 (sample shows 2W/5L 29% WR "
            "-$375 at the boundary; TR10 hardened the threshold)."
        ),
        bet_field=_bet_field_runs_needed,
        direction="max",
        current_threshold=2.5,
        sweep_thresholds=[2.0, 2.5, 3.0, 3.5],
        applicability=_applies_inning_eq(5),
    ),
    GateDef(
        name="gate_inn6_rn_max",
        description=(
            "Inning 6 setup-reliever dead zone: in inning == 6, "
            "block when runs_needed >= 2.5 (TR9)."
        ),
        bet_field=_bet_field_runs_needed,
        direction="max",
        current_threshold=2.5,
        sweep_thresholds=[2.0, 2.5, 3.0, 3.5],
        applicability=_applies_inning_eq(6),
    ),
    GateDef(
        name="gate_close_game_rn",
        description=(
            "Close-game runs-needed cap: when lead_abs < 2, block "
            "if runs_needed >= 4.0 (TR6 -- close games rarely break "
            "out into the high-RN over)."
        ),
        bet_field=_bet_field_runs_needed,
        direction="max",
        current_threshold=4.0,
        sweep_thresholds=[3.0, 3.5, 4.0, 4.5, 5.0],
        applicability=_applies_close_game,
    ),
    GateDef(
        name="gate_s2_suppress_max",
        description=(
            "Stage-2 suppression: in inning >= 6, block when "
            "stage2_run_env_delta <= -0.20 (logit; park/weather "
            "model says scoring environment is materially worse "
            "than average). TR13 default."
        ),
        bet_field=_bet_field_s2_delta,
        direction="min",   # block when DELTA is below threshold (more negative = block)
        current_threshold=-0.20,
        sweep_thresholds=[-0.40, -0.30, -0.20, -0.10, 0.0],
        applicability=_applies_inning_gte(6),
    ),
    GateDef(
        name="gate_high_line_min_edge",
        description=(
            "High-line edge floor: when line >= 8.5, require edge "
            ">= 0.16 (vs the universal 0.10). High-line markets "
            "have wider FV uncertainty so a higher edge floor."
        ),
        bet_field=_bet_field_edge,
        direction="min",
        current_threshold=0.16,
        sweep_thresholds=[0.10, 0.13, 0.16, 0.18, 0.22],
        applicability=_applies_high_line,
    ),
    GateDef(
        name="gate_high_line_min_inning",
        description=(
            "High-line inning floor: when line >= 8.5, require "
            "inning >= 5 (vs universal 4). High-line resolution "
            "needs more game played for Stage-1 stability."
        ),
        bet_field=_bet_field_inning,
        direction="min",
        current_threshold=5,
        sweep_thresholds=[3, 4, 5, 6],
        applicability=_applies_high_line,
    ),
    GateDef(
        name="shadow_gate_current_state_edge_min",
        description=(
            "Shadow-only: would blocking on current_state_value_edge "
            "below a threshold improve ROI? Surfaced for operator "
            "review because the 2026-05-17 cohort breakdown shows "
            "cse<0.03 = +10.5% ROI vs cse>=0.08 = -11.4%. The "
            "counterintuitive direction (lower cse is BETTER) means "
            "Active #3's proposed gate_current_state_edge_min >= 0.05 "
            "should be RE-EVALUATED. This shadow gate runs the sweep "
            "without enforcing anything."
        ),
        bet_field=_bet_field_current_state_edge,
        direction="min",
        current_threshold=None,    # shadow_only
        sweep_thresholds=[0.0, 0.02, 0.03, 0.05, 0.08],
        shadow_only=True,
    ),
]


def _sweep_one(
    rows: Sequence[BetRow], gate: GateDef, threshold: float,
) -> Tuple[CohortStats, CohortStats]:
    """Return (kept_stats, blocked_stats) for one threshold value.

    For composite gates with an `applicability` predicate, rows that
    fail the predicate are EXCLUDED from both cohorts. This keeps the
    verdict comparison apples-to-apples within the gate's domain
    (e.g., gate_fv_ask_gap_max applies only inning>=7, so inning<7
    bets don't dilute the kept-vs-blocked comparison).
    """
    kept = CohortStats()
    blocked = CohortStats()
    for b in rows:
        if gate.applicability is not None and not gate.applicability(b):
            continue
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
    """Evaluate one gate against the bet population.

    For shadow-only gates (current_threshold is None), the verdict
    becomes an EXPLORE recommendation pointing at the best sweep
    threshold by blocked-vs-kept ROI delta, but never a KEEP/RETUNE
    /RETIRE action label -- a shadow gate has nothing to retire.
    """
    sweep: List[Dict[str, Any]] = []
    for thr in gate.sweep_thresholds:
        kept, blocked = _sweep_one(rows, gate, thr)
        sweep.append({
            "threshold": thr,
            "kept": kept.to_dict(),
            "blocked": blocked.to_dict(),
        })

    if gate.shadow_only or gate.current_threshold is None:
        # No production threshold -- emit an EXPLORE verdict that
        # surfaces the best sweep threshold by ROI delta.
        best_thr = None
        best_delta = None
        for s in sweep:
            kept_roi = s["kept"].get("roi")
            blocked_roi = s["blocked"].get("roi")
            n_blocked = s["blocked"].get("n_filled", 0)
            if (
                kept_roi is None or blocked_roi is None
                or n_blocked < GATE_RETUNE_MIN_BLOCKED_N
            ):
                continue
            delta = kept_roi - blocked_roi  # positive = blocking is good
            if best_delta is None or delta > best_delta:
                best_delta = delta
                best_thr = s["threshold"]
        verdict = {
            "verdict": "EXPLORE",
            "confidence": "low",
            "recommended_threshold": best_thr,
            "reason": (
                f"Shadow-only gate (no production threshold today). "
                + (
                    f"Best sweep threshold by ROI delta: {best_thr} "
                    f"(kept-blocked = {best_delta * 100:+.1f}pp)."
                    if best_thr is not None
                    else "No sweep threshold meets the minimum "
                         f"blocked-N {GATE_RETUNE_MIN_BLOCKED_N} for "
                         "a directional recommendation."
                )
            ),
        }
        return {
            "name": gate.name,
            "description": gate.description,
            "direction": gate.direction,
            "current_threshold": None,
            "applicability": gate.applicability.__name__ if gate.applicability else None,
            "shadow_only": True,
            "current_kept": None,
            "current_blocked": None,
            "sweep": sweep,
            "verdict": verdict,
        }

    current_kept, current_blocked = _sweep_one(rows, gate, gate.current_threshold)
    verdict = _gate_verdict(current_kept, current_blocked, sweep)
    return {
        "name": gate.name,
        "description": gate.description,
        "direction": gate.direction,
        "current_threshold": gate.current_threshold,
        "applicability": gate.applicability.__name__ if gate.applicability else None,
        "shadow_only": False,
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


def build_certification_payload(
    rows: Sequence[BetRow],
    *,
    config_label_filter: Optional[str] = None,
) -> Dict[str, Any]:
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
        "config_label_filter": config_label_filter,
        "overall": overall.to_dict(),
        "cohorts": cohorts,
        "gates": gates,
        "weekly_drift": drift_payload,
    }


# ---------------------------------------------------------------------------
# Markdown render
# ---------------------------------------------------------------------------


# Render functions moved to walk_forward_certification_render_md.py
# on 2026-05-25 (Tier 2). Re-exported for back-compat with any caller.
from scripts.analysis.walk_forward_certification_render_md import (  # noqa: F401
    _fmt_pct, _fmt_signed_pct, _fmt_money,
    _cohort_row_md, _cohort_table_md, _gate_block_md,
    _drift_table_md, _verdict_summary_md, render_markdown,
)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--training-table", type=Path, default=DEFAULT_TRAINING_TABLE)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--config-label-filter", type=str, default="",
                   help="Optional config_label filter for parallel engine certification.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    rows = load_bet_rows(Path(args.training_table))
    if args.config_label_filter:
        rows = [
            r for r in rows
            if str(getattr(r, "config_label", "default") or "default") == str(args.config_label_filter)
        ]
    payload = build_certification_payload(
        rows,
        config_label_filter=args.config_label_filter or None,
    )

    # Active #16 v2 (2026-05-17): stamp lineage on the certification
    # report. When the operator acts on a per-gate verdict (RETUNE,
    # RETIRE) the audit row should be able to point at WHICH cert
    # produced the recommendation -- this lineage block answers that
    # by capturing the training-table hash + git_sha at build time.
    try:
        from scripts.analysis.artifact_lineage import compute_lineage as _compute_lineage
    except ImportError:
        try:
            from artifact_lineage import compute_lineage as _compute_lineage  # type: ignore[no-redef]
        except ImportError:
            _compute_lineage = None  # type: ignore[assignment]
    if _compute_lineage is not None:
        try:
            payload["lineage"] = _compute_lineage(
                builder_path=__file__,
                input_paths=[args.training_table],
                project_root=PROJECT_DIR,
                extra={
                    "cli_args_summary": {
                        "training_table": str(args.training_table),
                        "output_dir": str(args.output_dir),
                        "config_label_filter": args.config_label_filter or None,
                        "readiness_label": (
                            payload.get("readiness") or {}
                        ).get("label"),
                        "n_filled": (
                            payload.get("overall") or {}
                        ).get("n_filled"),
                    },
                },
            )
        except Exception as _lineage_exc:  # noqa: BLE001
            print(f"[lineage] warning: stamp failed: {_lineage_exc!r}")

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
