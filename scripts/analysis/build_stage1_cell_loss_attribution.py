#!/usr/bin/env python3
"""build_stage1_cell_loss_attribution.py -- Active #10 follow-up.

Today's Active #10 shipment surfaced that Stage-1 owns ~100% of the
27pp aggregate over-prediction bias. That's a stage-level diagnosis;
it does not yet tell the operator WHICH KIND OF Stage-1 cell is most
miscalibrated. This builder answers that.

Drills the Stage-1 contribution across Stage-1-internal cohort
dimensions the standard loss attribution doesn't expose:

  - `stage1_fallback_level_bucket` -- did the runtime cache lookup
    hit an exact cell (level 0) or fall back to a broader bucket
    (level 1+)? Tells the operator whether the Stage-1 cell values
    themselves are wrong (level 0 dominant) vs the fallback
    aggregation is wrong (level 1+ dominant).
  - `stage1_line_fallback_mode_bucket` -- exact / extrapolate /
    interpolate / missing. Tells the operator whether the line key
    mapping (the per-line probability lookup) is contributing.
  - `stage1_used_fallback_bucket` -- True / False. The headline cut:
    did we bet in cells where we KNEW we were on shaky ground?
  - `stage1_n_bucket` -- <50 / 50-200 / 200-1000 / >=1000. Tells the
    operator whether thin-support cells dominate the bias.
  - `stage1_poisson_empirical_gap_bucket` -- absolute |poisson -
    empirical| in 4 bands. The smoking gun: when the Poisson
    smoothing differs from the empirical rate that the SAME cell
    observed, by how much, and does that gap drive the loss?

For each cohort:
  - n_bets
  - mean_p0 (Stage-1 base FV) vs mean_won
  - stage1_bias = mean_p0 - mean_won (signed; positive = Stage-1
    over-predicts in this cohort)
  - mean_poisson_minus_empirical (when present) -- direct evidence
    of how much Poisson smoothing inflates above empirical for the
    cohort
  - mean_inferred_state_n (sample-size sanity)

Top culprits ranking flags cohorts with |stage1_bias| >= threshold
AND n >= min_n_for_alert. The operator reads this to decide whether
to (a) rebuild the Stage-1 cache on fresh data (whole-cache fix),
(b) tighten the fallback gating in the runtime (cell-level fix),
(c) raise min-n thresholds in the cache builder so thin cells fail
closed (builder-side fix), or (d) prefer empirical-when-available
over Poisson in the runtime lookup (lookup-side fix).

Output:
  data/analysis_output/stage1_cell_loss_attribution/
    stage1_cell_loss_attribution.json
    stage1_cell_loss_attribution.md

Pure offline analysis. Reads signal_training_table.jsonl.
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
    PROJECT_DIR / "data" / "analysis_output" / "stage1_cell_loss_attribution"
)


# Cohort thresholds.
TRAILING_30D_DAYS = 30
TRAILING_7D_DAYS = 7
# Stage-1 cells need more bets than the standard cohort breakdown to
# de-noise: per-cell sample sizes are themselves small. 5 is the
# absolute floor; below that, single-bet outcomes drive everything.
MIN_N_FOR_COHORT_VERDICT = 5
# A cohort's stage1_bias must exceed this share of the aggregate
# stage1_bias to be flagged as a top culprit. Mirrors #10's 25%
# threshold for stage-level ranking; applied here to cell-level
# cuts so the operator focuses on the largest cohorts first.
TOP_CULPRIT_MIN_SHARE = 0.25
# Absolute Stage-1 bias floor below which we don't bother surfacing
# cohort culprits. Below 5pp the model is approximately calibrated
# in that cohort and the attribution is noise.
TOP_CULPRIT_MIN_ABS_BIAS = 0.05


# ---------------------------------------------------------------------------
# Per-bet projection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Stage1Bet:
    """Per-bet projection focused on Stage-1 internals."""
    session_date: str
    p0: float                       # Stage-1 base FV (inferred-state cell lookup)
    won: int                        # 0 / 1
    bias: float                     # p0 - won (signed; positive = over-predicted)
    fallback_level: Optional[int]   # 0 = exact cell hit, 1+ = fallback to broader bucket
    line_fallback_mode: Optional[str]
    used_fallback: Optional[bool]
    inferred_state_n: Optional[int]
    poisson_minus_empirical: Optional[float]
    base_empirical: Optional[float]
    cell_key: Optional[str]


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


def _safe_bool(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.lower()
        if s in ("true", "1", "yes"):
            return True
        if s in ("false", "0", "no"):
            return False
    return None


def project_bet(row: Dict[str, Any]) -> Optional[Stage1Bet]:
    """Project a training-table row into the Stage-1 view.

    Requires `target_filled == 1`, `target_win` in (0, 1), and
    `base_fair_value` (Stage-1's output). All other Stage-1 metadata
    fields are best-effort -- older rows from before the inferred-state
    audit fields were added will surface as `missing` in the cohort
    cuts.
    """
    target_filled = _safe_int(row.get("target_filled"))
    if target_filled != 1:
        return None
    target_win = _safe_int(row.get("target_win"))
    if target_win not in (0, 1):
        return None
    p0 = _safe_float(row.get("base_fair_value"))
    if p0 is None or not (0.0 <= p0 <= 1.0):
        return None

    return Stage1Bet(
        session_date=str(row.get("session_date") or ""),
        p0=p0,
        won=int(target_win),
        bias=p0 - float(target_win),
        fallback_level=_safe_int(row.get("inferred_state_fallback_level")),
        line_fallback_mode=(
            str(row.get("inferred_state_line_fallback_mode"))
            if row.get("inferred_state_line_fallback_mode") is not None
            else None
        ),
        used_fallback=_safe_bool(row.get("inferred_state_used_fallback")),
        inferred_state_n=_safe_int(row.get("inferred_state_n")),
        poisson_minus_empirical=_safe_float(
            row.get("inferred_state_poisson_minus_empirical"),
        ),
        base_empirical=_safe_float(row.get("inferred_state_base_empirical")),
        cell_key=(
            str(row.get("inferred_state_cell_key"))
            if row.get("inferred_state_cell_key") is not None else None
        ),
    )


def load_bets(path: Path) -> List[Stage1Bet]:
    out: List[Stage1Bet] = []
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
            b = project_bet(row)
            if b is not None:
                out.append(b)
    return out


# ---------------------------------------------------------------------------
# Cohort bucketing (Stage-1 specific dimensions)
# ---------------------------------------------------------------------------

def _bucket_fallback_level(b: Stage1Bet) -> str:
    lvl = b.fallback_level
    if lvl is None:
        return "missing"
    if lvl == 0:
        return "level_0_exact"
    if lvl == 1:
        return "level_1_fallback"
    return "level_2plus_fallback"


def _bucket_line_fallback_mode(b: Stage1Bet) -> str:
    mode = b.line_fallback_mode
    if mode is None:
        return "missing"
    return str(mode)


def _bucket_used_fallback(b: Stage1Bet) -> str:
    if b.used_fallback is None:
        return "missing"
    return "fallback_used" if b.used_fallback else "exact_match"


def _bucket_n(b: Stage1Bet) -> str:
    n = b.inferred_state_n
    if n is None:
        return "missing"
    if n < 50:
        return "<50"
    if n < 200:
        return "50-200"
    if n < 1000:
        return "200-1000"
    return ">=1000"


def _bucket_poisson_empirical_gap(b: Stage1Bet) -> str:
    """Absolute |poisson - empirical| at the inferred Stage-1 cell."""
    gap = b.poisson_minus_empirical
    if gap is None:
        return "missing"
    a = abs(gap)
    if a < 0.05:
        return "<0.05"
    if a < 0.10:
        return "0.05-0.10"
    if a < 0.20:
        return "0.10-0.20"
    return ">=0.20"


COHORT_DIMENSIONS: Tuple[Tuple[str, Callable[[Stage1Bet], str]], ...] = (
    ("stage1_fallback_level_bucket", _bucket_fallback_level),
    ("stage1_line_fallback_mode_bucket", _bucket_line_fallback_mode),
    ("stage1_used_fallback_bucket", _bucket_used_fallback),
    ("stage1_n_bucket", _bucket_n),
    ("stage1_poisson_empirical_gap_bucket", _bucket_poisson_empirical_gap),
)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _mean(values: Sequence[float]) -> Optional[float]:
    return (sum(values) / len(values)) if values else None


def _round_or_none(v: Optional[float], digits: int) -> Optional[float]:
    return None if v is None else round(v, digits)


def aggregate(bets: Sequence[Stage1Bet]) -> Dict[str, Any]:
    """Aggregate a Stage-1 cohort: n, p0/won, bias, poisson-vs-empirical
    gap, mean cell sample size."""
    if not bets:
        return {
            "n": 0,
            "mean_p0": None, "mean_won": None,
            "stage1_bias": None, "abs_stage1_bias": None,
            "mean_poisson_minus_empirical": None,
            "n_with_empirical": 0,
            "mean_inferred_state_n": None,
            "fallback_rate": None,
        }
    n = len(bets)
    mean_p0 = _mean([b.p0 for b in bets])
    mean_won = _mean([float(b.won) for b in bets])
    bias = mean_p0 - mean_won

    empirical_gaps = [
        b.poisson_minus_empirical for b in bets
        if b.poisson_minus_empirical is not None
    ]
    cell_n_vals = [
        b.inferred_state_n for b in bets if b.inferred_state_n is not None
    ]
    fallback_known = [b.used_fallback for b in bets if b.used_fallback is not None]
    fallback_rate = (
        sum(1 for b in fallback_known if b) / len(fallback_known)
    ) if fallback_known else None

    return {
        "n": n,
        "mean_p0": _round_or_none(mean_p0, 4),
        "mean_won": _round_or_none(mean_won, 4),
        "stage1_bias": _round_or_none(bias, 4),
        "abs_stage1_bias": _round_or_none(abs(bias), 4),
        "mean_poisson_minus_empirical": _round_or_none(
            _mean(empirical_gaps), 4,
        ),
        "n_with_empirical": len(empirical_gaps),
        "mean_inferred_state_n": _round_or_none(
            _mean([float(x) for x in cell_n_vals]), 1,
        ),
        "fallback_rate": _round_or_none(fallback_rate, 4),
    }


def aggregate_by_cohort(
    bets: Sequence[Stage1Bet],
) -> Dict[str, Dict[str, Any]]:
    """Per-(dimension, bucket) aggregate."""
    out: Dict[str, Dict[str, Any]] = {}
    for dim_name, bucket_fn in COHORT_DIMENSIONS:
        per_bucket: Dict[str, List[Stage1Bet]] = {}
        for b in bets:
            per_bucket.setdefault(bucket_fn(b), []).append(b)
        out[dim_name] = {
            label: aggregate(rows) for label, rows in per_bucket.items()
        }
    return out


# ---------------------------------------------------------------------------
# Window slicing (same shape as Active #10 / #11)
# ---------------------------------------------------------------------------

def _parse_date(d: str) -> Optional[datetime]:
    try:
        return datetime.strptime(d, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _latest_date(bets: Sequence[Stage1Bet]) -> Optional[datetime]:
    latest: Optional[datetime] = None
    for b in bets:
        d = _parse_date(b.session_date)
        if d is None:
            continue
        if latest is None or d > latest:
            latest = d
    return latest


def slice_windows(
    bets: Sequence[Stage1Bet],
) -> "OrderedDict[str, List[Stage1Bet]]":
    out: "OrderedDict[str, List[Stage1Bet]]" = OrderedDict()
    out["all"] = list(bets)
    latest = _latest_date(bets)
    if latest is None:
        out["trailing_30d"] = []
        out["trailing_7d"] = []
        return out
    cut_30 = latest - timedelta(days=TRAILING_30D_DAYS - 1)
    cut_7 = latest - timedelta(days=TRAILING_7D_DAYS - 1)

    def _in(b: Stage1Bet, lo: datetime, hi: datetime) -> bool:
        d = _parse_date(b.session_date)
        return d is not None and lo <= d <= hi

    out["trailing_30d"] = [b for b in bets if _in(b, cut_30, latest)]
    out["trailing_7d"] = [b for b in bets if _in(b, cut_7, latest)]
    return out


# ---------------------------------------------------------------------------
# Top-culprit ranking
# ---------------------------------------------------------------------------

def build_top_culprits(
    by_cohort: Dict[str, Dict[str, Any]],
    *,
    aggregate_bias: Optional[float],
    min_n: int = MIN_N_FOR_COHORT_VERDICT,
    min_abs_bias: float = TOP_CULPRIT_MIN_ABS_BIAS,
    min_share: float = TOP_CULPRIT_MIN_SHARE,
    max_results: int = 10,
) -> List[Dict[str, Any]]:
    """Rank cohorts by |bias| descending, filtering by (a) min n,
    (b) min absolute bias, (c) min share of aggregate bias.

    The share filter requires the aggregate to itself be material
    (>= min_abs_bias) -- otherwise every cohort's share is meaningless
    division by ~zero.
    """
    out: List[Dict[str, Any]] = []
    if (
        aggregate_bias is None
        or abs(aggregate_bias) < min_abs_bias
    ):
        return out
    bias_sign = 1.0 if aggregate_bias >= 0 else -1.0
    for dim_name, buckets in by_cohort.items():
        for label, agg in buckets.items():
            if label == "missing":
                continue
            n = agg.get("n", 0)
            cohort_bias = agg.get("stage1_bias")
            if n < min_n or cohort_bias is None:
                continue
            cohort_bias_in_agg_dir = cohort_bias * bias_sign
            if cohort_bias_in_agg_dir < min_abs_bias:
                # Cohort is helping (negative in agg direction) OR is
                # noise. Either way, not a culprit.
                continue
            share = (
                abs(cohort_bias) / abs(aggregate_bias)
                if aggregate_bias else None
            )
            if share is None or share < min_share:
                continue
            out.append({
                "dimension": dim_name,
                "bucket": label,
                "n": n,
                "stage1_bias": cohort_bias,
                "stage1_bias_vs_aggregate_ratio": _round_or_none(share, 4),
                "mean_p0": agg.get("mean_p0"),
                "mean_won": agg.get("mean_won"),
                "mean_poisson_minus_empirical": agg.get(
                    "mean_poisson_minus_empirical",
                ),
                "n_with_empirical": agg.get("n_with_empirical"),
                "mean_inferred_state_n": agg.get("mean_inferred_state_n"),
                "fallback_rate": agg.get("fallback_rate"),
                "rationale": _culprit_rationale(
                    dim_name, label, agg, bias_sign,
                ),
            })
    out.sort(key=lambda r: r.get("stage1_bias") or 0.0, reverse=True)
    return out[:max_results]


def _culprit_rationale(
    dim_name: str, label: str, agg: Dict[str, Any], bias_sign: float,
) -> str:
    """Plain-English narrative for the operator's eye."""
    n = agg.get("n", 0)
    bias = agg.get("stage1_bias") or 0.0
    mean_p0 = agg.get("mean_p0")
    mean_won = agg.get("mean_won")
    gap = agg.get("mean_poisson_minus_empirical")
    n_with_emp = agg.get("n_with_empirical", 0)

    direction = "over-predicting" if bias > 0 else "under-predicting"
    parts = [
        f"{dim_name}={label} (n={n}): Stage-1 {direction} by "
        f"{bias * 100:+.1f}pp"
    ]
    if mean_p0 is not None and mean_won is not None:
        parts.append(
            f" (mean_p0={mean_p0 * 100:.1f}% vs mean_won={mean_won * 100:.1f}%)"
        )
    if gap is not None and n_with_emp:
        gap_pp = gap * 100
        if gap_pp > 5:
            parts.append(
                f". Poisson smoothing inflates by +{gap_pp:.1f}pp vs "
                f"the cell's own empirical rate (n_with_empirical={n_with_emp}) "
                "-- Poisson smoothing is the candidate fix."
            )
        elif gap_pp < -5:
            parts.append(
                f". Poisson smoothing UNDER-shoots by {gap_pp:.1f}pp vs the "
                f"cell's empirical rate (n_with_empirical={n_with_emp})."
            )
        else:
            parts.append(
                f". Poisson and empirical agree to within ±5pp "
                "-- the Stage-1 prior itself, not the smoothing, is "
                "the candidate fix."
            )
    return "".join(parts)


# ---------------------------------------------------------------------------
# Top-level payload
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )


def build_payload(
    bets: Sequence[Stage1Bet],
    *,
    training_table_path: Optional[Path] = None,
) -> Dict[str, Any]:
    windows = slice_windows(bets)
    dates = sorted({b.session_date for b in bets if b.session_date})
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "related_active_priority": "Active #10 (bet-level loss attribution)",
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
            "min_n_for_cohort_verdict": MIN_N_FOR_COHORT_VERDICT,
            "top_culprit_min_share": TOP_CULPRIT_MIN_SHARE,
            "top_culprit_min_abs_bias": TOP_CULPRIT_MIN_ABS_BIAS,
        },
        "windows": OrderedDict(),
    }
    for window_name, window_bets in windows.items():
        window_dates = sorted(
            {b.session_date for b in window_bets if b.session_date}
        )
        agg_all = aggregate(window_bets)
        by_cohort = aggregate_by_cohort(window_bets)
        culprits = build_top_culprits(
            by_cohort, aggregate_bias=agg_all.get("stage1_bias"),
        )
        payload["windows"][window_name] = {
            "date_range": (
                [window_dates[0], window_dates[-1]]
                if window_dates else None
            ),
            "n_bets": len(window_bets),
            "aggregate": agg_all,
            "by_cohort": by_cohort,
            "top_culprits": culprits,
        }
    return payload


# ---------------------------------------------------------------------------
# Markdown render
# ---------------------------------------------------------------------------

def _fmt_pct(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _fmt_signed_pct(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def _fmt_num(v: Optional[float]) -> str:
    return "—" if v is None else (
        f"{v:.1f}" if isinstance(v, float) else str(v)
    )


def _aggregate_md(agg: Dict[str, Any]) -> str:
    if agg.get("n", 0) == 0:
        return "_No Stage-1 bets in window._\n"
    rows = [
        f"- **N bets:** {agg['n']}  |  **Stage-1 bias:** "
        f"{_fmt_signed_pct(agg['stage1_bias'])}",
        f"- mean_p0={_fmt_pct(agg['mean_p0'])}  vs  "
        f"mean_won={_fmt_pct(agg['mean_won'])}",
    ]
    fb = agg.get("fallback_rate")
    if fb is not None:
        rows.append(f"- **fallback_rate:** {_fmt_pct(fb)}")
    gap = agg.get("mean_poisson_minus_empirical")
    if gap is not None:
        rows.append(
            f"- mean(poisson - empirical) = {_fmt_signed_pct(gap)} "
            f"(n_with_empirical={agg['n_with_empirical']})"
        )
    cell_n = agg.get("mean_inferred_state_n")
    if cell_n is not None:
        rows.append(f"- mean_inferred_state_n = {_fmt_num(cell_n)}")
    return "\n".join(rows) + "\n"


def _cohort_md(by_cohort: Dict[str, Dict[str, Any]]) -> str:
    parts: List[str] = []
    for dim_name, buckets in by_cohort.items():
        parts.append(f"#### {dim_name}")
        parts.append("")
        parts.append(
            "| Bucket | N | Stage-1 bias | mean_p0 | mean_won "
            "| poisson-empirical gap | fallback_rate |"
        )
        parts.append(
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"
        )
        for label, agg in buckets.items():
            parts.append(
                f"| {label} | {agg.get('n', 0)} | "
                f"{_fmt_signed_pct(agg.get('stage1_bias'))} | "
                f"{_fmt_pct(agg.get('mean_p0'))} | "
                f"{_fmt_pct(agg.get('mean_won'))} | "
                f"{_fmt_signed_pct(agg.get('mean_poisson_minus_empirical'))} | "
                f"{_fmt_pct(agg.get('fallback_rate'))} |"
            )
        parts.append("")
    return "\n".join(parts)


def _culprits_md(culprits: Sequence[Dict[str, Any]]) -> str:
    if not culprits:
        return "_No Stage-1 culprit cohorts meet the thresholds._\n"
    lines = ["**Top culprits** (cohorts owning the largest share of bias):", ""]
    for c in culprits:
        lines.append(f"- `{c['dimension']}={c['bucket']}` " + c["rationale"])
    lines.append("")
    return "\n".join(lines)


def render_markdown(payload: Dict[str, Any]) -> str:
    parts: List[str] = []
    parts.append("# Stage-1 cell loss attribution\n")
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
        "Active #10 surfaced that Stage-1 owns ~100% of the aggregate "
        "over-prediction bias. This report drills the Stage-1 contribution "
        "across Stage-1-internal cohort dimensions so the operator can see "
        "WHICH cells are the culprits. Headline cuts:\n\n"
        "- `stage1_used_fallback_bucket = fallback_used` -- did we bet "
        "in cells where the runtime fell back to a broader bucket?\n"
        "- `stage1_poisson_empirical_gap_bucket` -- when the cell carries "
        "both a Poisson and empirical estimate, how big is the gap?\n"
        "- `stage1_n_bucket` -- thin-support cells (<50 samples) tend "
        "to over-rely on the Poisson prior.\n"
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
        parts.append(_culprits_md(window.get("top_culprits") or []))
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
    bets = load_bets(Path(args.training_table))
    payload = build_payload(
        bets, training_table_path=Path(args.training_table),
    )

    # Active #16 v2 lineage stamping (shipped earlier today)
    try:
        from artifact_lineage import compute_lineage as _compute_lineage
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
                        "n_bets": payload["n_bets"],
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[lineage] warning: stamp failed: {exc!r}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "stage1_cell_loss_attribution.json"
    md_path = output_dir / "stage1_cell_loss_attribution.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    agg = ((payload.get("windows") or {}).get("trailing_30d") or {}).get("aggregate") or {}
    culprits = ((payload.get("windows") or {}).get("trailing_30d") or {}).get("top_culprits") or []
    if agg.get("n"):
        print(
            f"Trailing-30d Stage-1 bias {(agg.get('stage1_bias') or 0) * 100:+.1f}pp "
            f"(n={agg['n']}, fallback_rate={(agg.get('fallback_rate') or 0) * 100:.0f}%)"
        )
    if culprits:
        print(f"Top culprit cohorts: {len(culprits)}")
        for c in culprits[:3]:
            ratio = c.get('stage1_bias_vs_aggregate_ratio') or 0.0
            print(
                f"  - {c['dimension']}={c['bucket']} "
                f"bias={(c['stage1_bias'] or 0) * 100:+.1f}pp "
                f"(ratio_vs_agg={ratio:.2f}x, n={c['n']})"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
