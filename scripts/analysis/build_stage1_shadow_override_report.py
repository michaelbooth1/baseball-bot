#!/usr/bin/env python3
"""build_stage1_shadow_override_report.py -- Active #8 prep.

Today's Stage-1 cell-conditional drill (`build_stage1_cell_loss_attribution.py`)
identified two specific candidate fixes for the Stage-1 over-prediction
bias:

  - **Alt A**: prefer the cell's empirical rate over the Poisson smoothing
    whenever empirical is available. The drill found Poisson inflates by
    +16pp vs empirical when both are available.
  - **Alt B**: fail closed on `inferred_state_fallback_level >= 2` --
    skip bets where the runtime lookup fell back to a deep bucket.

Before changing the live FV computation, the operator needs evidence
of what each alt WOULD have done on the actual bet sample. This builder
replays both alts against `signal_training_table.jsonl` and shows the
counterfactual outcomes side-by-side:

  - For each filled+settled bet:
    - production p3 = `fair_value` (calibrated, current behavior)
    - alt_A_p3 = sigmoid(logit(empirical) + s2 + s3) when empirical
      is available; else production p3 (no change)
    - alt_B_kept = NOT (fallback_level >= 2); when False, the bet
      would not have been placed at all
    - bias_prod = p3 - won, bias_alt_A = alt_A_p3 - won
  - Aggregate:
    - mean_bias_prod vs mean_bias_alt_A (the "improvement" metric)
    - n_alt_A_applies (where empirical changed FV) vs n_total
    - n_alt_B_blocks (bets that would have been skipped)
    - Alt B aggregate over the kept subset (= what production would
      have done on the bets we'd still have placed)
    - $-impact: counterfactual profit delta on the kept subset for
      Alt B (= sum(profit) over blocked bets, signed) -- positive
      when we'd have saved money by blocking, negative when we'd
      have missed winners

Output:
  data/analysis_output/stage1_shadow_override/
    stage1_shadow_override_report.{json,md}

This is the "shadow first, then promote" pattern shipped with every
other risky live-runtime change in the codebase (no-score drift, EV
policy, stake scaling, quote engine). After this report shows durable
improvement over ~30 days, Active #8 can promote alt A and/or alt B
to live via a runtime flag.

Pure offline analysis. Reads signal_training_table.jsonl. No runtime
changes.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))


DEFAULT_TRAINING_TABLE = (
    PROJECT_DIR / "data" / "analysis_output" / "training_tables"
    / "signal_training_table.jsonl"
)
DEFAULT_OUTPUT_DIR = (
    PROJECT_DIR / "data" / "analysis_output" / "stage1_shadow_override"
)


TRAILING_30D_DAYS = 30
TRAILING_7D_DAYS = 7

# Alt B fallback-level threshold. Cells deeper than this would be
# blocked under Alt B. 2 chosen because the cell-loss drill showed
# level_2plus_fallback at +40pp bias vs +28pp aggregate (1.44x
# amplification).
ALT_B_FALLBACK_LEVEL_THRESHOLD = 2

# Minimum sample to render a recommendation verdict.
MIN_N_FOR_RECOMMENDATION = 30


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

_LOGIT_EPS = 1e-6


def _logit(p: float) -> float:
    p = max(_LOGIT_EPS, min(1.0 - _LOGIT_EPS, p))
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


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


# ---------------------------------------------------------------------------
# Per-bet projection + counterfactual computation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShadowBet:
    """Per-bet record with production FV + both alt counterfactuals.

    The alt fields are pre-computed at projection time so the
    aggregator stays trivial (sums + counts).

    `alt_a_source` distinguishes how p3_alt_a was obtained: 'runtime'
    means the live engine logged it via the
    --stage1-shadow-empirical-override shadow flag (preferred --
    matches the production calibrator exactly), 'offline' means this
    report computed it from base_fair_value + s2 + s3 (used as a
    fallback when the runtime didn't log), or 'no_change' when no
    empirical was available.
    """
    session_date: str
    won: int
    target_profit: float
    p0_poisson: float
    p0_empirical: Optional[float]   # the alt-A input
    p3_prod: float
    p3_alt_a: float                 # when no empirical, equals p3_prod
    alt_a_changed: bool             # whether alt A differs from prod
    alt_a_source: str               # 'runtime' / 'offline' / 'no_change'
    fallback_level: Optional[int]
    alt_b_kept: bool                # False = would have been blocked
    inning: Optional[int]
    line: Optional[float]
    # Cohort breakdown fields (2026-05-19 follow-up). Carried through
    # so the cohort-level aggregate can answer "WHERE does Alt A
    # help most?" rather than just the aggregate "by how much?"
    # number. All Optional -- rows that lack a field land in the
    # `missing` bucket of the corresponding cohort dimension.
    edge_at_ask: Optional[float]
    decision_ask: Optional[float]
    current_state_value_edge: Optional[float]


def project_bet(row: Dict[str, Any]) -> Optional[ShadowBet]:
    """Project a training-table row into a `ShadowBet` with both alt
    counterfactuals pre-computed.

    Filter requirements: target_filled=1, target_win in (0,1),
    base_fair_value + fair_value + stage2_run_env_delta +
    team_offense_delta present (the FV chain). Rows without
    empirical OR without fallback metadata are still included -- the
    alt counterfactuals fall back to the production FV (alt_a_changed
    = False) / alt_b_kept = True for those rows.
    """
    target_filled = _safe_int(row.get("target_filled"))
    if target_filled != 1:
        return None
    target_win = _safe_int(row.get("target_win"))
    if target_win not in (0, 1):
        return None
    p0 = _safe_float(row.get("base_fair_value"))
    p3 = _safe_float(row.get("fair_value"))
    s2 = _safe_float(row.get("stage2_run_env_delta"))
    s3 = _safe_float(row.get("team_offense_delta"))
    if None in (p0, p3, s2, s3):
        return None
    if not (0.0 < p0 < 1.0 and 0.0 < p3 < 1.0):
        return None

    empirical = _safe_float(row.get("inferred_state_base_empirical"))

    # Active #8 prep (2026-05-17): prefer the runtime-logged alt FV
    # when present. The live engine writes
    # `fair_value_alt_empirical` per candidate when
    # --stage1-shadow-empirical-override=shadow is set; that value
    # already ran through the same calibrator production uses, so it
    # matches the eventual ENFORCE-mode behavior exactly. Fall back
    # to offline-computed (logit-additive chain on the raw deltas)
    # for older rows or when the flag was off.
    runtime_alt = _safe_float(row.get("fair_value_alt_empirical"))
    runtime_used_empirical = bool(
        row.get("fair_value_alt_empirical_used_empirical")
    )
    if runtime_alt is not None and runtime_used_empirical:
        p3_alt_a = runtime_alt
        alt_a_changed = True
        alt_a_source = "runtime"
    elif empirical is not None and 0.0 < empirical < 1.0:
        # Alt A: empirical-when-available (offline fallback)
        p3_alt_a = _sigmoid(_logit(empirical) + s2 + s3)
        alt_a_changed = True
        alt_a_source = "offline"
    else:
        p3_alt_a = p3
        alt_a_changed = False
        alt_a_source = "no_change"

    fallback_level = _safe_int(row.get("inferred_state_fallback_level"))
    # Alt B: fail closed on fallback_level >= threshold
    if fallback_level is not None and fallback_level >= ALT_B_FALLBACK_LEVEL_THRESHOLD:
        alt_b_kept = False
    else:
        alt_b_kept = True

    return ShadowBet(
        session_date=str(row.get("session_date") or ""),
        won=int(target_win),
        target_profit=_safe_float(row.get("target_profit")) or 0.0,
        p0_poisson=p0,
        p0_empirical=empirical,
        p3_prod=p3,
        p3_alt_a=p3_alt_a,
        alt_a_changed=alt_a_changed,
        alt_a_source=alt_a_source,
        fallback_level=fallback_level,
        alt_b_kept=alt_b_kept,
        inning=_safe_int(row.get("inning")),
        line=_safe_float(row.get("line")),
        # Prefer edge_at_ask (the canonical training-table column);
        # fall back to plain `edge` for forward-compat with older rows
        # that only carry the unsuffixed alias.
        edge_at_ask=_safe_float(
            row.get("edge_at_ask") if row.get("edge_at_ask") is not None
            else row.get("edge")
        ),
        decision_ask=_safe_float(
            row.get("decision_ask") if row.get("decision_ask") is not None
            else row.get("entry_ask")
        ),
        current_state_value_edge=_safe_float(
            row.get("current_state_value_edge")
        ),
    )


def load_bets(path: Path) -> List[ShadowBet]:
    out: List[ShadowBet] = []
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
# Window slicing (same shape as other reports)
# ---------------------------------------------------------------------------

def _parse_date(d: str) -> Optional[datetime]:
    try:
        return datetime.strptime(d, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _latest_date(bets: Sequence[ShadowBet]) -> Optional[datetime]:
    latest: Optional[datetime] = None
    for b in bets:
        d = _parse_date(b.session_date)
        if d is None:
            continue
        if latest is None or d > latest:
            latest = d
    return latest


def slice_windows(
    bets: Sequence[ShadowBet],
) -> "OrderedDict[str, List[ShadowBet]]":
    out: "OrderedDict[str, List[ShadowBet]]" = OrderedDict()
    out["all"] = list(bets)
    latest = _latest_date(bets)
    if latest is None:
        out["trailing_30d"] = []
        out["trailing_7d"] = []
        return out
    cut_30 = latest - timedelta(days=TRAILING_30D_DAYS - 1)
    cut_7 = latest - timedelta(days=TRAILING_7D_DAYS - 1)

    def _in(b: ShadowBet, lo: datetime, hi: datetime) -> bool:
        d = _parse_date(b.session_date)
        return d is not None and lo <= d <= hi

    out["trailing_30d"] = [b for b in bets if _in(b, cut_30, latest)]
    out["trailing_7d"] = [b for b in bets if _in(b, cut_7, latest)]
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _mean(values: Sequence[float]) -> Optional[float]:
    return (sum(values) / len(values)) if values else None


def _round_or_none(v: Optional[float], digits: int) -> Optional[float]:
    return None if v is None else round(v, digits)


def aggregate_window(
    bets: Sequence[ShadowBet],
) -> Dict[str, Any]:
    """Compute production vs Alt A vs Alt B aggregates for one window."""
    n = len(bets)
    if n == 0:
        return {
            "n_bets": 0,
            "production": {
                "mean_p3": None, "mean_won": None, "bias": None,
                "total_profit": None,
            },
            "alt_a_empirical_when_available": {
                "mean_p3": None, "mean_won": None, "bias": None,
                "n_changed": 0,
                "bias_delta_vs_prod_pp": None,
                "alt_source_breakdown": {
                    "runtime": 0, "offline": 0, "no_change": 0,
                },
            },
            "alt_b_block_fallback_level_2plus": {
                "n_blocked": 0,
                "n_kept": 0,
                "kept_mean_p3": None, "kept_mean_won": None, "kept_bias": None,
                "kept_total_profit": None,
                "counterfactual_profit_delta_usd": None,
                "blocked_total_profit": None,
                "blocked_n_wins": 0, "blocked_n_losses": 0,
            },
            "recommendations": [],
        }
    mean_won = _mean([float(b.won) for b in bets])
    mean_p3_prod = _mean([b.p3_prod for b in bets])
    bias_prod = mean_p3_prod - mean_won

    # Alt A
    mean_p3_alt_a = _mean([b.p3_alt_a for b in bets])
    bias_alt_a = mean_p3_alt_a - mean_won
    n_changed = sum(1 for b in bets if b.alt_a_changed)
    # Source breakdown: how many alt FVs came from the live runtime
    # (preferred, matches eventual ENFORCE behavior exactly) vs from
    # the offline logit-additive fallback (used for older rows / rows
    # logged before the runtime flag flipped to shadow).
    n_runtime = sum(1 for b in bets if b.alt_a_source == "runtime")
    n_offline = sum(1 for b in bets if b.alt_a_source == "offline")
    # The "improvement" (positive = bias moves toward 0)
    if bias_prod > 0:
        bias_delta_pp = (bias_prod - bias_alt_a) * 100
    else:
        bias_delta_pp = (bias_alt_a - bias_prod) * 100

    # Alt B: split into kept / blocked
    kept = [b for b in bets if b.alt_b_kept]
    blocked = [b for b in bets if not b.alt_b_kept]
    n_blocked = len(blocked)
    n_kept = len(kept)
    if n_kept:
        kept_mean_p3 = _mean([b.p3_prod for b in kept])
        kept_mean_won = _mean([float(b.won) for b in kept])
        kept_bias = kept_mean_p3 - kept_mean_won
        kept_total_profit = sum(b.target_profit for b in kept)
    else:
        kept_mean_p3 = None
        kept_mean_won = None
        kept_bias = None
        kept_total_profit = None
    blocked_total_profit = sum(b.target_profit for b in blocked)
    blocked_n_wins = sum(1 for b in blocked if b.won == 1)
    blocked_n_losses = sum(1 for b in blocked if b.won == 0)
    # Counterfactual: enforcing Alt B SKIPS the blocked bets.
    # Positive value = we'd have saved money by blocking
    # (= negate the blocked-bets' realized profit).
    cf_delta = -blocked_total_profit if blocked else 0.0

    payload = {
        "n_bets": n,
        "production": {
            "mean_p3": _round_or_none(mean_p3_prod, 4),
            "mean_won": _round_or_none(mean_won, 4),
            "bias": _round_or_none(bias_prod, 4),
            "total_profit": round(sum(b.target_profit for b in bets), 2),
        },
        "alt_a_empirical_when_available": {
            "mean_p3": _round_or_none(mean_p3_alt_a, 4),
            "mean_won": _round_or_none(mean_won, 4),
            "bias": _round_or_none(bias_alt_a, 4),
            "n_changed": n_changed,
            "n_coverage_rate": _round_or_none(
                n_changed / n if n else None, 4,
            ),
            "bias_delta_vs_prod_pp": _round_or_none(bias_delta_pp, 2),
            "alt_source_breakdown": {
                "runtime": n_runtime,
                "offline": n_offline,
                "no_change": n - n_runtime - n_offline,
            },
        },
        "alt_b_block_fallback_level_2plus": {
            "n_blocked": n_blocked,
            "n_kept": n_kept,
            "kept_mean_p3": _round_or_none(kept_mean_p3, 4),
            "kept_mean_won": _round_or_none(kept_mean_won, 4),
            "kept_bias": _round_or_none(kept_bias, 4),
            "kept_total_profit": (
                round(kept_total_profit, 2)
                if kept_total_profit is not None else None
            ),
            "blocked_total_profit": round(blocked_total_profit, 2),
            "blocked_n_wins": blocked_n_wins,
            "blocked_n_losses": blocked_n_losses,
            "counterfactual_profit_delta_usd": round(cf_delta, 2),
        },
    }
    payload["recommendations"] = _build_recommendations(payload, n)
    return payload


# ---------------------------------------------------------------------------
# Cohort breakdown (2026-05-19 follow-up)
#
# Aggregate Alt A's bias-reduction by cohort so the operator can see
# WHERE Alt A helps most. Lets the eventual Active #8 ENFORCE flip be
# scoped to specific cohorts (e.g. "enforce Alt A only on edge>=0.22
# bets") instead of all-or-nothing global.
# ---------------------------------------------------------------------------

# Match the cohort buckets used by `cohort_roi_health` /
# `cohort_calibration_health` in build_daily_human_review_report.py
# so cross-block comparison stays consistent.

def _edge_bucket(value: Optional[float]) -> str:
    if value is None:
        return "missing"
    if value < 0.15:
        return "<0.15"
    if value < 0.18:
        return "0.15-0.18"
    if value < 0.22:
        return "0.18-0.22"
    return ">=0.22"


def _inning_bucket(value: Optional[int]) -> str:
    if value is None:
        return "missing"
    if value <= 5:
        return "<=5"
    if value == 6:
        return "6"
    if value == 7:
        return "7"
    return ">=8"


def _line_bucket(value: Optional[float]) -> str:
    if value is None:
        return "missing"
    if value <= 7.5:
        return "<=7.5"
    if value <= 8.5:
        return "8.5"
    if value <= 9.5:
        return "9.5"
    return ">=10.5"


def _ask_bucket(value: Optional[float]) -> str:
    if value is None:
        return "missing"
    if value < 0.55:
        return "<0.55"
    if value < 0.70:
        return "0.55-0.70"
    if value < 0.85:
        return "0.70-0.85"
    return ">=0.85"


def _current_state_edge_bucket(value: Optional[float]) -> str:
    if value is None:
        return "missing"
    if value < 0.00:
        return "<0.00"
    if value < 0.03:
        return "0.00-0.03"
    if value < 0.06:
        return "0.03-0.06"
    return ">=0.06"


# Each (label, getter, bucketer). `getter` extracts the cohort value
# from a ShadowBet; `bucketer` maps it to a string bucket key.
COHORT_DIMENSIONS: List[Tuple[str, Any, Any]] = [
    ("edge_bucket",
     lambda b: b.edge_at_ask, _edge_bucket),
    ("inning_bucket",
     lambda b: b.inning, _inning_bucket),
    ("line_bucket",
     lambda b: b.line, _line_bucket),
    ("ask_bucket",
     lambda b: b.decision_ask, _ask_bucket),
    ("current_state_edge_bucket",
     lambda b: b.current_state_value_edge, _current_state_edge_bucket),
]

# Below this sample size the per-cohort bias-delta is dominated by
# noise. 5 chosen as the minimum where mean(won) starts to be
# meaningful at all; the daily-review surface raises this to 10 for
# "promote on cohort" recommendations.
COHORT_MIN_N_FOR_AGGREGATE = 5


def _aggregate_cohort(bets: Sequence[ShadowBet]) -> Dict[str, Any]:
    """Per-cohort aggregate: production vs Alt A bias, plus Alt B
    kept/blocked split. Smaller surface than `aggregate_window`
    because cohorts are sliced thinly -- skip the source breakdown
    and the recommendations subtree to keep the JSON readable.
    """
    n = len(bets)
    if n == 0:
        return {
            "n_bets": 0,
            "production": {"mean_p3": None, "mean_won": None, "bias": None},
            "alt_a": {
                "mean_p3": None, "bias": None,
                "n_changed": 0, "coverage_rate": None,
                "bias_delta_vs_prod_pp": None,
            },
            "alt_b": {
                "n_blocked": 0, "n_kept": 0,
                "kept_bias": None,
                "counterfactual_profit_delta_usd": 0.0,
            },
        }
    mean_won = _mean([float(b.won) for b in bets])
    mean_p3_prod = _mean([b.p3_prod for b in bets])
    mean_p3_alt_a = _mean([b.p3_alt_a for b in bets])
    bias_prod = mean_p3_prod - mean_won
    bias_alt_a = mean_p3_alt_a - mean_won
    n_changed = sum(1 for b in bets if b.alt_a_changed)
    # Signed delta: positive = bias moves toward 0 (Alt A helps).
    # Mirrors the aggregate formula in `aggregate_window`.
    if bias_prod > 0:
        bias_delta_pp = (bias_prod - bias_alt_a) * 100
    else:
        bias_delta_pp = (bias_alt_a - bias_prod) * 100

    blocked = [b for b in bets if not b.alt_b_kept]
    kept = [b for b in bets if b.alt_b_kept]
    n_blocked = len(blocked)
    n_kept = len(kept)
    if kept:
        kept_mean_p3 = _mean([b.p3_prod for b in kept])
        kept_mean_won = _mean([float(b.won) for b in kept])
        kept_bias = kept_mean_p3 - kept_mean_won
    else:
        kept_bias = None
    blocked_total_profit = sum(b.target_profit for b in blocked)
    cf_delta = -blocked_total_profit if blocked else 0.0
    total_profit = sum(b.target_profit for b in bets)

    return {
        "n_bets": n,
        "production": {
            "mean_p3": _round_or_none(mean_p3_prod, 4),
            "mean_won": _round_or_none(mean_won, 4),
            "bias": _round_or_none(bias_prod, 4),
            "total_profit": round(total_profit, 2),
        },
        "alt_a": {
            "mean_p3": _round_or_none(mean_p3_alt_a, 4),
            "bias": _round_or_none(bias_alt_a, 4),
            "n_changed": n_changed,
            "coverage_rate": _round_or_none(
                n_changed / n if n else None, 4,
            ),
            "bias_delta_vs_prod_pp": _round_or_none(bias_delta_pp, 2),
        },
        "alt_b": {
            "n_blocked": n_blocked,
            "n_kept": n_kept,
            "kept_bias": _round_or_none(kept_bias, 4),
            "counterfactual_profit_delta_usd": round(cf_delta, 2),
        },
    }


def build_cohort_breakdown(
    bets: Sequence[ShadowBet],
    *,
    min_n_per_cohort: int = COHORT_MIN_N_FOR_AGGREGATE,
) -> Dict[str, Any]:
    """Slice `bets` by each cohort dimension and aggregate per bucket.

    Returns a dict keyed by dimension name; each dimension maps to a
    dict of buckets -> per-cohort aggregate. Plus a `top_cohorts`
    summary surfacing the cohorts where Alt A helps most, where it
    REGRESSES (Alt A makes bias worse), and where its coverage is
    highest (= where the empirical fix has the most reach).
    """
    by_dim: Dict[str, Dict[str, Any]] = {}
    flat_entries: List[Dict[str, Any]] = []
    for dim_name, getter, bucketer in COHORT_DIMENSIONS:
        buckets: Dict[str, List[ShadowBet]] = {}
        for b in bets:
            key = bucketer(getter(b))
            buckets.setdefault(key, []).append(b)
        per_bucket: Dict[str, Any] = {}
        # Ordered iteration: lexicographic by bucket key. Stable + reproducible.
        for bucket_key in sorted(buckets.keys()):
            cohort = _aggregate_cohort(buckets[bucket_key])
            per_bucket[bucket_key] = cohort
            n_b = cohort["n_bets"]
            if n_b >= min_n_per_cohort:
                delta_pp = cohort["alt_a"].get("bias_delta_vs_prod_pp")
                coverage = cohort["alt_a"].get("coverage_rate")
                flat_entries.append({
                    "dimension": dim_name,
                    "bucket": bucket_key,
                    "n_bets": n_b,
                    "bias_delta_vs_prod_pp": delta_pp,
                    "coverage_rate": coverage,
                    "production_bias": cohort["production"].get("bias"),
                    "alt_a_bias": cohort["alt_a"].get("bias"),
                    "alt_b_n_blocked": cohort["alt_b"].get("n_blocked"),
                    "alt_b_counterfactual_usd": cohort["alt_b"].get(
                        "counterfactual_profit_delta_usd"
                    ),
                })
        by_dim[dim_name] = per_bucket

    # Top cohorts summary: sorted views of the same flat entries.
    def _by_key(key: str, *, reverse: bool, limit: int = 5):
        valid = [e for e in flat_entries if e.get(key) is not None]
        return sorted(valid, key=lambda e: e[key], reverse=reverse)[:limit]

    top_cohorts = {
        "most_improved": _by_key(
            "bias_delta_vs_prod_pp", reverse=True,
        ),
        "regressions": [
            # Negative bias_delta = Alt A makes bias WORSE
            e for e in sorted(
                flat_entries,
                key=lambda e: e.get("bias_delta_vs_prod_pp") or 0.0,
            )
            if (e.get("bias_delta_vs_prod_pp") or 0.0) < 0.0
        ][:5],
        "highest_coverage": _by_key(
            "coverage_rate", reverse=True,
        ),
        "largest_alt_b_savings": _by_key(
            "alt_b_counterfactual_usd", reverse=True,
        ),
    }

    return {
        "min_n_per_cohort": min_n_per_cohort,
        "n_bets_total": len(bets),
        "by_dimension": by_dim,
        "top_cohorts": top_cohorts,
    }


def _build_recommendations(
    agg: Dict[str, Any], n_total: int,
) -> List[Dict[str, Any]]:
    """Synthesize human-readable recommendations from the alt aggregates.

    A recommendation surfaces when:
      - Alt A: bias improves by >= 1pp AND coverage_rate >= 25%
        (some empirical-data coverage, otherwise not meaningful)
      - Alt B: counterfactual_profit_delta >= $20 AND n_blocked >= 3
    Below those thresholds the alts haven't accumulated enough
    evidence; the operator should let the report run for more days
    before making a runtime change.
    """
    out: List[Dict[str, Any]] = []
    if n_total < MIN_N_FOR_RECOMMENDATION:
        return out

    alt_a = agg["alt_a_empirical_when_available"]
    delta_pp = alt_a.get("bias_delta_vs_prod_pp") or 0.0
    coverage = alt_a.get("n_coverage_rate") or 0.0
    if delta_pp >= 1.0 and coverage >= 0.25:
        prod_bias_pp = (agg["production"].get("bias") or 0.0) * 100
        alt_bias_pp = (alt_a.get("bias") or 0.0) * 100
        out.append({
            "alt": "alt_a_empirical_when_available",
            "verdict": "promote_to_runtime_shadow",
            "rationale": (
                f"Alt A reduces aggregate bias by {delta_pp:.1f}pp "
                f"({prod_bias_pp:+.1f}pp -> {alt_bias_pp:+.1f}pp) on "
                f"{alt_a.get('n_changed')} of {n_total} bets "
                f"({coverage * 100:.0f}% coverage). The reduction is "
                "concentrated where empirical data exists; full-coverage "
                "extrapolation would close additional bias. Next step: "
                "promote to a runtime shadow flag (compute alt p0 on "
                "every tick, log alongside production p3, then promote "
                "to live after a clean 30d shadow window)."
            ),
        })

    alt_b = agg["alt_b_block_fallback_level_2plus"]
    cf_delta = alt_b.get("counterfactual_profit_delta_usd") or 0.0
    n_blocked = alt_b.get("n_blocked") or 0
    if cf_delta >= 20.0 and n_blocked >= 3:
        out.append({
            "alt": "alt_b_block_fallback_level_2plus",
            "verdict": "promote_to_runtime_shadow",
            "rationale": (
                f"Alt B blocks {n_blocked} bets where fallback_level "
                f">= {ALT_B_FALLBACK_LEVEL_THRESHOLD}. Counterfactual "
                f"P&L: ${cf_delta:+.2f} saved by enforcing the block "
                f"({alt_b.get('blocked_n_wins')}W / "
                f"{alt_b.get('blocked_n_losses')}L blocked). Next "
                "step: ship a runtime gate at "
                "`inferred_state_fallback_level >= "
                f"{ALT_B_FALLBACK_LEVEL_THRESHOLD}` and promote "
                "after a clean 30d shadow window."
            ),
        })
    return out


# ---------------------------------------------------------------------------
# Top-level payload
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )


def build_payload(
    bets: Sequence[ShadowBet],
    *,
    training_table_path: Optional[Path] = None,
) -> Dict[str, Any]:
    windows = slice_windows(bets)
    dates = sorted({b.session_date for b in bets if b.session_date})
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "related_active_priority": "Active #8 (Stage-1 retrain)",
        "training_table_path": (
            str(training_table_path) if training_table_path else None
        ),
        "n_bets": len(bets),
        "date_span": (
            {"first": dates[0], "last": dates[-1]} if dates else None
        ),
        "config": {
            "alt_b_fallback_level_threshold":
                ALT_B_FALLBACK_LEVEL_THRESHOLD,
            "trailing_30d_days": TRAILING_30D_DAYS,
            "trailing_7d_days": TRAILING_7D_DAYS,
            "min_n_for_recommendation": MIN_N_FOR_RECOMMENDATION,
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
            **aggregate_window(window_bets),
        }
    # 2026-05-19 follow-up: cohort breakdown on trailing_30d so the
    # operator sees WHERE Alt A helps most -- not just the aggregate
    # delta. Enables a scoped ENFORCE-only-on-specific-cohorts
    # promotion path instead of all-or-nothing.
    trailing_30d_bets = list(windows.get("trailing_30d") or [])
    payload["cohort_breakdown_trailing_30d"] = build_cohort_breakdown(
        trailing_30d_bets,
    )
    return payload


# ---------------------------------------------------------------------------
# Markdown render
# ---------------------------------------------------------------------------


# Render functions moved to stage1_shadow_override_render_md.py on
# 2026-05-25 (Tier 2). Re-exported for back-compat.
from scripts.analysis.stage1_shadow_override_render_md import (  # noqa: F401
    _fmt_pct, _fmt_signed_pct, _fmt_money,
    _window_md, render_markdown, _cohort_md,
)


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

    # Lineage stamp (Active #16 v2)
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
                        "alt_b_fallback_level_threshold":
                            ALT_B_FALLBACK_LEVEL_THRESHOLD,
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[lineage] warning: stamp failed: {exc!r}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "stage1_shadow_override_report.json"
    md_path = output_dir / "stage1_shadow_override_report.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    w30 = (payload.get("windows") or {}).get("trailing_30d") or {}
    if w30.get("n_bets"):
        prod = w30.get("production", {})
        alt_a = w30.get("alt_a_empirical_when_available", {})
        alt_b = w30.get("alt_b_block_fallback_level_2plus", {})
        print(
            f"Trailing-30d: prod_bias={(prod.get('bias') or 0) * 100:+.1f}pp -> "
            f"alt_A_bias={(alt_a.get('bias') or 0) * 100:+.1f}pp "
            f"(delta {(alt_a.get('bias_delta_vs_prod_pp') or 0):+.1f}pp on "
            f"{alt_a.get('n_changed')}/{w30.get('n_bets')} bets)"
        )
        print(
            f"Alt B: blocks {alt_b.get('n_blocked')}/{w30.get('n_bets')} bets; "
            f"counterfactual delta=${alt_b.get('counterfactual_profit_delta_usd', 0):+.2f}"
        )
        recs = w30.get("recommendations") or []
        if recs:
            print(f"Recommendations ({len(recs)}):")
            for r in recs:
                print(f"  - {r['alt']}: {r['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
