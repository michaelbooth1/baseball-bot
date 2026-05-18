#!/usr/bin/env python3
"""build_loss_attribution_report.py -- Active #10.

Bet-level loss attribution.

Today's `cohort_calibration_health` block (Active #9, shipped earlier today)
surfaced a 22pp aggregate over-prediction in the production model. It tells
the operator "the model is wrong"; it does not tell the operator "WHICH
PART of the model is wrong." This builder answers that.

The FV pipeline composes logit-additively:

    fair_value = sigmoid(
        logit(base_fair_value)
        + stage2_run_env_delta          # Stage-2 park / weather
        + team_offense_delta            # Stage-3 team offense
        + calibration_delta             # final calibration shift
    )

Empirically verified on all 87 filled+settled production bets: the LHS
matches the RHS to within 0.001 in every case (calibration is in shadow
mode in production, so calibration_delta ~= 0). The chain identity gives
us a clean per-stage decomposition of the FV that the operator can use to
ask "which stage is responsible for over-shooting the realized win rate?"

For each filled+settled bet:

    p0 = base_fair_value                # Stage-1 alone
    p1 = sigmoid(logit(p0) + s2_delta)  # Stage-1 + Stage-2
    p2 = sigmoid(logit(p1) + s3_delta)  # Stage-1 + Stage-2 + Stage-3
    p3 = fair_value                     # final (post-calibration)

    stage_shift_stage1 = p0 - 0.5       # shift from neutral baseline
    stage_shift_stage2 = p1 - p0
    stage_shift_stage3 = p2 - p1
    stage_shift_calibration = p3 - p2

    bet_bias = p3 - won                 # signed; positive = over-predicted

Aggregate: each stage's signed mean shift, plus its `attribution_share`
(= fraction of the aggregate bias it owns IN THE BIAS DIRECTION). The
operator reads the top_culprits ranking to decide which stage to retune /
retrain first.

Pure offline analysis. Reads signal_training_table.jsonl. Never writes
under live ledgers / corpora / caches.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))


DEFAULT_TRAINING_TABLE = (
    PROJECT_DIR / "data" / "analysis_output" / "training_tables"
    / "signal_training_table.jsonl"
)
DEFAULT_OUTPUT_DIR = (
    PROJECT_DIR / "data" / "analysis_output" / "loss_attribution"
)


# Trailing windows match the rest of the drift-alert family for
# cross-comparison.
TRAILING_30D_DAYS = 30
TRAILING_7D_DAYS = 7

# A stage is flagged as a "culprit" when it owns >= this share of the
# aggregate bias in the bias direction. Below this share, the stage's
# contribution is structural-baseline noise and not actionable for the
# operator. 25% is one-quarter of the bias -- material enough that
# focusing the retrain on this stage moves the needle.
TOP_CULPRIT_MIN_SHARE = 0.25

# Logit clip floor to avoid sigmoid blowups on probabilities reported at
# exactly 0.0 or 1.0 in older candidate rows.
_LOGIT_EPS = 1e-6


# ---------------------------------------------------------------------------
# Per-bet decomposition (the load-bearing math)
# ---------------------------------------------------------------------------

def _logit(p: float) -> float:
    p = max(_LOGIT_EPS, min(1.0 - _LOGIT_EPS, p))
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


@dataclass(frozen=True)
class BetDecomposition:
    """Per-bet probability decomposition + outcome.

    p0..p3 are the cumulative-stage probabilities; stage_shift_* are the
    per-stage probability deltas (they sum to p3 - 0.5 when the chain is
    consistent). `won` is 0 or 1. `bias` = p3 - won (positive when the
    model over-predicted).
    """
    session_date: str
    p0: float
    p1: float
    p2: float
    p3: float
    stage_shift_stage1: float
    stage_shift_stage2: float
    stage_shift_stage3: float
    stage_shift_calibration: float
    won: int
    bias: float
    target_profit: float
    decision_ask: Optional[float]
    inning: Optional[int]
    line: Optional[float]
    edge_at_ask: Optional[float]
    current_state_value_edge: Optional[float]


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


def decompose_bet(row: Dict[str, Any]) -> Optional[BetDecomposition]:
    """Project a training-table row into the per-stage decomposition.

    Requires `target_filled == 1`, `target_win` in (0, 1), and the four
    pipeline fields (base_fair_value, stage2_run_env_delta,
    team_offense_delta, fair_value). Returns None for rows missing any of
    these, including rows from prior schema versions that don't carry the
    Stage-2 / Stage-3 deltas.

    Math: logit-additive composition. Empirically verified on all 87
    filled+settled production bets as of 2026-05-17 (max chain-vs-fair_value
    deviation < 0.001).
    """
    target_filled = _safe_int(row.get("target_filled"))
    if target_filled != 1:
        return None
    target_win = _safe_int(row.get("target_win"))
    if target_win not in (0, 1):
        return None
    p0 = _safe_float(row.get("base_fair_value"))
    p3 = _safe_float(row.get("fair_value"))
    s2_delta = _safe_float(row.get("stage2_run_env_delta"))
    s3_delta = _safe_float(row.get("team_offense_delta"))
    if None in (p0, p3, s2_delta, s3_delta):
        return None
    if not (0.0 < p0 < 1.0 and 0.0 < p3 < 1.0):
        return None

    p1 = _sigmoid(_logit(p0) + s2_delta)
    p2 = _sigmoid(_logit(p1) + s3_delta)

    return BetDecomposition(
        session_date=str(row.get("session_date") or ""),
        p0=p0,
        p1=p1,
        p2=p2,
        p3=p3,
        stage_shift_stage1=p0 - 0.5,
        stage_shift_stage2=p1 - p0,
        stage_shift_stage3=p2 - p1,
        stage_shift_calibration=p3 - p2,
        won=int(target_win),
        bias=p3 - float(target_win),
        target_profit=_safe_float(row.get("target_profit")) or 0.0,
        decision_ask=_safe_float(row.get("decision_ask")),
        inning=_safe_int(row.get("inning")),
        line=_safe_float(row.get("line")),
        edge_at_ask=_safe_float(row.get("edge_at_ask")),
        current_state_value_edge=_safe_float(row.get("current_state_value_edge")),
    )


def load_decompositions(path: Path) -> List[BetDecomposition]:
    out: List[BetDecomposition] = []
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            bd = decompose_bet(row)
            if bd is not None:
                out.append(bd)
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

STAGE_NAMES: Tuple[str, ...] = (
    "stage1_baseline",
    "stage2_run_env",
    "stage3_team_offense",
    "calibration",
)

STAGE_SHIFT_ATTRS: Dict[str, str] = {
    "stage1_baseline": "stage_shift_stage1",
    "stage2_run_env": "stage_shift_stage2",
    "stage3_team_offense": "stage_shift_stage3",
    "calibration": "stage_shift_calibration",
}


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _round_or_none(v: Optional[float], digits: int) -> Optional[float]:
    return None if v is None else round(v, digits)


def aggregate_decompositions(
    bets: Sequence[BetDecomposition],
) -> Dict[str, Any]:
    """Aggregate per-stage shifts + bias attribution across a window."""
    if not bets:
        return {
            "n": 0,
            "mean_p0": None, "mean_p1": None, "mean_p2": None, "mean_p3": None,
            "mean_won": None, "bias": None, "abs_bias": None,
            "stage_contributions": {
                name: {
                    "mean_shift": None,
                    "mean_shift_in_bias_direction": None,
                    "attribution_share": None,
                }
                for name in STAGE_NAMES
            },
            "top_culprits": [],
        }
    n = len(bets)
    mean_p0 = _mean([b.p0 for b in bets])
    mean_p1 = _mean([b.p1 for b in bets])
    mean_p2 = _mean([b.p2 for b in bets])
    mean_p3 = _mean([b.p3 for b in bets])
    mean_won = _mean([float(b.won) for b in bets])
    bias = mean_p3 - mean_won  # signed; positive = aggregate over-prediction
    bias_sign = 1.0 if bias >= 0 else -1.0

    # Per-stage signed shift toward the realized outcome.
    # Each stage's contribution to the aggregate FV is the mean of its
    # per-bet shift. The contribution "in the bias direction" is that
    # mean projected onto the sign of the aggregate bias: positive
    # means the stage pushed FV further into the over/under-prediction
    # error; negative means the stage helped correct it.
    stage_means: Dict[str, float] = {}
    stage_in_bias_dir: Dict[str, float] = {}
    for name in STAGE_NAMES:
        attr = STAGE_SHIFT_ATTRS[name]
        shifts = [getattr(b, attr) for b in bets]
        m = _mean(shifts)
        stage_means[name] = m if m is not None else 0.0
        stage_in_bias_dir[name] = stage_means[name] * bias_sign

    # Attribution share = each stage's signed contribution divided by
    # the sum of POSITIVE contributions (the "bad" stages). Stages that
    # helped (negative contribution in bias direction) receive a 0
    # share -- they aren't a culprit even if their absolute shift is
    # large. This matches how an operator thinks: "of the bad stages,
    # which one owns the biggest piece?"
    positive_sum = sum(v for v in stage_in_bias_dir.values() if v > 0)
    stage_contributions: Dict[str, Dict[str, Optional[float]]] = {}
    for name in STAGE_NAMES:
        v = stage_in_bias_dir[name]
        share = (v / positive_sum) if (positive_sum > 0 and v > 0) else (
            0.0 if v <= 0 else None
        )
        stage_contributions[name] = {
            "mean_shift": _round_or_none(stage_means[name], 4),
            "mean_shift_in_bias_direction": _round_or_none(v, 4),
            "attribution_share": _round_or_none(share, 4),
        }

    # Top culprits: stages whose share >= TOP_CULPRIT_MIN_SHARE, sorted
    # by share DESC.
    culprits = [
        {
            "stage": name,
            "attribution_share": stage_contributions[name]["attribution_share"],
            "mean_shift_in_bias_direction": stage_contributions[name][
                "mean_shift_in_bias_direction"
            ],
        }
        for name in STAGE_NAMES
        if (stage_contributions[name]["attribution_share"] or 0.0)
        >= TOP_CULPRIT_MIN_SHARE
    ]
    culprits.sort(
        key=lambda r: r.get("attribution_share") or 0.0, reverse=True,
    )

    return {
        "n": n,
        "mean_p0": _round_or_none(mean_p0, 4),
        "mean_p1": _round_or_none(mean_p1, 4),
        "mean_p2": _round_or_none(mean_p2, 4),
        "mean_p3": _round_or_none(mean_p3, 4),
        "mean_won": _round_or_none(mean_won, 4),
        "bias": _round_or_none(bias, 4),
        "abs_bias": _round_or_none(abs(bias), 4),
        "bias_direction": "over_predicting" if bias > 0 else (
            "under_predicting" if bias < 0 else "neutral"
        ),
        "stage_contributions": stage_contributions,
        "top_culprits": culprits,
    }


# ---------------------------------------------------------------------------
# Window slicing
# ---------------------------------------------------------------------------

def _parse_date(d: str) -> Optional[datetime]:
    try:
        return datetime.strptime(d, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _latest_date(bets: Sequence[BetDecomposition]) -> Optional[datetime]:
    latest: Optional[datetime] = None
    for b in bets:
        d = _parse_date(b.session_date)
        if d is None:
            continue
        if latest is None or d > latest:
            latest = d
    return latest


def slice_windows(
    bets: Sequence[BetDecomposition],
) -> "OrderedDict[str, List[BetDecomposition]]":
    """Return all / trailing_30d / trailing_7d slices anchored on latest."""
    out: "OrderedDict[str, List[BetDecomposition]]" = OrderedDict()
    out["all"] = list(bets)
    latest = _latest_date(bets)
    if latest is None:
        out["trailing_30d"] = []
        out["trailing_7d"] = []
        return out
    cut_30 = latest - timedelta(days=TRAILING_30D_DAYS - 1)
    cut_7 = latest - timedelta(days=TRAILING_7D_DAYS - 1)

    def _in(b: BetDecomposition, lo: datetime, hi: datetime) -> bool:
        d = _parse_date(b.session_date)
        return d is not None and lo <= d <= hi

    out["trailing_30d"] = [b for b in bets if _in(b, cut_30, latest)]
    out["trailing_7d"] = [b for b in bets if _in(b, cut_7, latest)]
    return out


# ---------------------------------------------------------------------------
# Per-cohort breakdown
# ---------------------------------------------------------------------------

def _cohort_edge_bucket(b: BetDecomposition) -> str:
    e = b.edge_at_ask
    if e is None:
        return "missing"
    if e < 0.15:
        return "<0.15"
    if e < 0.18:
        return "0.15-0.18"
    if e < 0.22:
        return "0.18-0.22"
    return ">=0.22"


def _cohort_ask_bucket(b: BetDecomposition) -> str:
    a = b.decision_ask
    if a is None:
        return "missing"
    if a < 0.55:
        return "<0.55"
    if a < 0.65:
        return "0.55-0.65"
    if a < 0.75:
        return "0.65-0.75"
    if a < 0.85:
        return "0.75-0.85"
    return ">=0.85"


def _cohort_inning_bucket(b: BetDecomposition) -> str:
    i = b.inning
    if i is None:
        return "missing"
    if i <= 5:
        return "<=5"
    if i == 6:
        return "6"
    if i == 7:
        return "7"
    return ">=8"


def _cohort_line_bucket(b: BetDecomposition) -> str:
    ln = b.line
    if ln is None:
        return "missing"
    if ln <= 7.5:
        return "<=7.5"
    if ln <= 8.5:
        return "8.5"
    if ln <= 9.5:
        return "9.5"
    return ">=10.5"


def _cohort_cse_bucket(b: BetDecomposition) -> str:
    cse = b.current_state_value_edge
    if cse is None:
        return "missing"
    if cse < 0.03:
        return "<0.03"
    if cse < 0.08:
        return "0.03-0.08"
    return ">=0.08"


COHORT_DIMENSIONS: Tuple[
    Tuple[str, Callable[[BetDecomposition], str]], ...
] = (
    ("edge_bucket", _cohort_edge_bucket),
    ("ask_bucket", _cohort_ask_bucket),
    ("inning_bucket", _cohort_inning_bucket),
    ("line_bucket", _cohort_line_bucket),
    ("current_state_edge_bucket", _cohort_cse_bucket),
)


def aggregate_by_cohort(
    bets: Sequence[BetDecomposition],
) -> Dict[str, Dict[str, Any]]:
    """Per-(dimension, bucket) aggregate of the decomposition."""
    out: Dict[str, Dict[str, Any]] = {}
    for dim_name, bucket_fn in COHORT_DIMENSIONS:
        per_bucket: Dict[str, List[BetDecomposition]] = {}
        for b in bets:
            per_bucket.setdefault(bucket_fn(b), []).append(b)
        out[dim_name] = {
            label: aggregate_decompositions(rows)
            for label, rows in per_bucket.items()
        }
    return out


# ---------------------------------------------------------------------------
# Top-level payload
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )


def build_attribution_payload(
    bets: Sequence[BetDecomposition],
    *,
    training_table_path: Optional[Path] = None,
) -> Dict[str, Any]:
    windows = slice_windows(bets)
    dates = sorted({b.session_date for b in bets if b.session_date})
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "active_priority": "Active #10 (bet-level loss attribution)",
        "training_table_path": (
            str(training_table_path) if training_table_path else None
        ),
        "n_bets": len(bets),
        "date_span": (
            {"first": dates[0], "last": dates[-1]} if dates else None
        ),
        "config": {
            "trailing_30d_days": TRAILING_30D_DAYS,
            "trailing_7d_days": TRAILING_7D_DAYS,
            "top_culprit_min_share": TOP_CULPRIT_MIN_SHARE,
        },
        "windows": OrderedDict(),
    }
    for window_name, window_bets in windows.items():
        window_dates = sorted(
            {b.session_date for b in window_bets if b.session_date}
        )
        payload["windows"][window_name] = {
            "date_range": (
                [window_dates[0], window_dates[-1]]
                if window_dates else None
            ),
            "n_bets": len(window_bets),
            "aggregate": aggregate_decompositions(window_bets),
            "by_cohort": aggregate_by_cohort(window_bets),
        }
    return payload


# ---------------------------------------------------------------------------
# Markdown render
# ---------------------------------------------------------------------------

def _fmt_pct(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _fmt_signed_pct(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def _aggregate_md(agg: Dict[str, Any]) -> str:
    if agg.get("n", 0) == 0:
        return "_No bets in window._\n"
    rows = [
        f"- **N bets:** {agg['n']}  |  **bias:** "
        f"{_fmt_signed_pct(agg['bias'])} ({agg['bias_direction']})",
        f"- mean_p0={_fmt_pct(agg['mean_p0'])} → "
        f"mean_p1={_fmt_pct(agg['mean_p1'])} → "
        f"mean_p2={_fmt_pct(agg['mean_p2'])} → "
        f"mean_p3={_fmt_pct(agg['mean_p3'])}  vs  "
        f"mean_won={_fmt_pct(agg['mean_won'])}",
        "",
        "| Stage | Mean shift | In bias dir | Attribution share |",
        "| --- | ---: | ---: | ---: |",
    ]
    for stage in STAGE_NAMES:
        c = (agg.get("stage_contributions") or {}).get(stage, {})
        rows.append(
            f"| `{stage}` | {_fmt_signed_pct(c.get('mean_shift'))} | "
            f"{_fmt_signed_pct(c.get('mean_shift_in_bias_direction'))} | "
            f"{_fmt_pct(c.get('attribution_share'))} |"
        )
    rows.append("")
    culprits = agg.get("top_culprits") or []
    if culprits:
        rows.append("**Top culprits:**")
        for c in culprits:
            rows.append(
                f"- `{c['stage']}` -- "
                f"{_fmt_pct(c['attribution_share'])} of bias "
                f"(shift {_fmt_signed_pct(c['mean_shift_in_bias_direction'])})"
            )
    else:
        rows.append("_No stage owns >= "
                    f"{int(TOP_CULPRIT_MIN_SHARE * 100)}% of bias._")
    return "\n".join(rows) + "\n"


def _cohort_md(by_cohort: Dict[str, Dict[str, Any]]) -> str:
    parts: List[str] = []
    for dim_name, buckets in by_cohort.items():
        parts.append(f"#### {dim_name}")
        parts.append("")
        parts.append(
            "| Bucket | N | bias | Top culprit | Share |"
        )
        parts.append(
            "| --- | ---: | ---: | --- | ---: |"
        )
        for label, agg in buckets.items():
            culprits = agg.get("top_culprits") or []
            top = culprits[0] if culprits else None
            top_name = top["stage"] if top else "—"
            top_share = (
                _fmt_pct(top["attribution_share"]) if top else "—"
            )
            parts.append(
                f"| {label} | {agg.get('n', 0)} | "
                f"{_fmt_signed_pct(agg.get('bias'))} | "
                f"{top_name} | {top_share} |"
            )
        parts.append("")
    return "\n".join(parts)


def render_markdown(payload: Dict[str, Any]) -> str:
    parts: List[str] = []
    parts.append("# Loss attribution report (Active #10)\n")
    parts.append(f"_Generated {payload.get('generated_at_utc')}._\n")
    span = payload.get("date_span") or {}
    span_str = (
        f"{span.get('first', '?')} → {span.get('last', '?')}"
        if span else "no data"
    )
    parts.append(
        f"**Inputs:** {payload.get('n_bets', 0)} filled+settled bets; "
        f"window {span_str}.\n"
    )
    parts.append("## How to read this\n")
    parts.append(
        "Each bet's calibrated fair value `p3` is decomposed via the "
        "logit-additive chain:\n"
        "```\n"
        "p0 = base_fair_value\n"
        "p1 = sigmoid(logit(p0) + stage2_run_env_delta)\n"
        "p2 = sigmoid(logit(p1) + team_offense_delta)\n"
        "p3 = fair_value\n"
        "```\n"
        "Per-stage shifts: s1=p0-0.5, s2=p1-p0, s3=p2-p1, "
        "sc=p3-p2.\n\n"
        "The **bias** is `mean_p3 - mean_won` -- positive when the "
        "model over-predicts on average. Each stage's "
        "**attribution_share** is its mean shift in the bias direction "
        "divided by the sum of all positive stage contributions. "
        "Stages that PUSHED against the bias receive a 0 share -- "
        "they helped, not hurt.\n"
    )
    for window_name, window in (payload.get("windows") or {}).items():
        date_range = window.get("date_range") or []
        date_str = (
            f"{date_range[0]} → {date_range[1]}"
            if len(date_range) == 2 else "no data"
        )
        parts.append(
            f"## {window_name} ({date_str}, n={window.get('n_bets', 0)})"
        )
        parts.append("")
        parts.append("### Aggregate")
        parts.append("")
        parts.append(_aggregate_md(window.get("aggregate") or {}))
        parts.append("### By cohort")
        parts.append("")
        parts.append(_cohort_md(window.get("by_cohort") or {}))
    return "\n".join(parts) + "\n"


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
    bets = load_decompositions(Path(args.training_table))
    payload = build_attribution_payload(
        bets, training_table_path=Path(args.training_table),
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "loss_attribution_report.json"
    md_path = output_dir / "loss_attribution_report.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print(
        f"Loss attribution: {payload.get('n_bets', 0)} bets across "
        f"{len(payload.get('windows') or {})} windows."
    )
    agg = ((payload.get("windows") or {}).get("trailing_30d") or {}).get("aggregate") or {}
    culprits = agg.get("top_culprits") or []
    if culprits:
        names = ", ".join(
            f"{c['stage']}({c['attribution_share'] * 100:.0f}%)"
            for c in culprits
        )
        print(
            f"Trailing-30d bias {(agg.get('bias') or 0) * 100:+.1f}pp; "
            f"top culprits: {names}."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
