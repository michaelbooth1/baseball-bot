#!/usr/bin/env python3
"""
Prototype a learned execution-policy selector from the queue-aware replay.

Inputs
------
data/analysis_output/execution_replay/queue_aware_execution_replay_rows.jsonl

Each replay row records what one of four candidate posting policies
(current_limit, limit_p1c, limit_p2c, taker_like) would have yielded for a
single placed bet, under one of two fill models (queue_adjusted, touch).
The realized ROI per row treats no-fill as ``None``; for per-bet aggregation
we coerce no-fill to ``0`` (no trade -> no P&L).

What this script answers
------------------------
1. Baseline: what is mean realized ROI per bet if we always post
   ``current_limit`` (today's behavior)?
2. Oracle:   what is mean ROI if we picked the best policy *per bet* with
   perfect hindsight? (In-sample upper bound.)
3. Simple rules: does "always taker_like" or "always limit_p2c" capture
   most of the oracle headroom?
4. Cohort lookup: split bets by decision-time features and pick the best
   policy per cohort. Cross-validated via leave-one-out so we don't
   overfit on n ~= 71.

Outputs
-------
data/analysis_output/execution_policy_prototype/
  learned_execution_policy_report.json
  learned_execution_policy_report.md

The artifact is research-only -- it documents headroom and rule strength;
nothing here changes live execution. Promotion to live needs walk-forward
evidence on a larger sample.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_INPUT_PATH = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "execution_replay"
    / "queue_aware_execution_replay_rows.jsonl"
)
DEFAULT_OUTPUT_ROOT = (
    PROJECT_DIR / "data" / "analysis_output" / "execution_policy_prototype"
)
DEFAULT_OUTPUT_STEM = "learned_execution_policy_report"

POLICIES: Tuple[str, ...] = ("current_limit", "limit_p1c", "limit_p2c", "taker_like")
BASELINE_POLICY = "current_limit"
DEFAULT_FILL_MODEL = "queue_adjusted"  # realistic; "touch" is optimistic
MIN_COHORT_SAMPLE = 5  # below this, fall back to global best instead of cohort best


# --------------------------------------------------------------------------
# Loading / pivoting
# --------------------------------------------------------------------------


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerced_roi(value: Any) -> float:
    """For per-bet aggregation: treat no-fill (None) as 0 ROI."""
    v = _safe_float(value)
    return v if v is not None else 0.0


def _pivot_bets(
    rows: Sequence[Dict[str, Any]], *, fill_model: str
) -> List[Dict[str, Any]]:
    """One record per bet for the given fill_model.

    Each record carries the decision-time features plus a ``policy_roi``
    dict keyed by policy name.
    """
    by_bet: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if str(row.get("fill_model") or "") != fill_model:
            continue
        bet_id = row.get("bet_id")
        if not bet_id:
            continue
        rec = by_bet.setdefault(
            bet_id,
            {
                "bet_id": bet_id,
                "session_date": row.get("session_date"),
                "mode": row.get("mode"),
                "decision_ask": _safe_float(row.get("decision_ask")),
                "entry_spread": _safe_float(row.get("entry_spread")),
                "fair_value": _safe_float(row.get("fair_value")),
                "current_state_value_edge": _safe_float(
                    row.get("current_state_value_edge")
                ),
                "inning": row.get("inning"),
                "line": row.get("line"),
                "marketable_at_entry": bool(row.get("marketable_at_entry")),
                "shadow_phantom_risk_band": row.get("shadow_phantom_risk_band"),
                "state_value_strategy": row.get("state_value_strategy"),
                "policy_roi": {},
                "policy_filled": {},
                "policy_profit": {},
            },
        )
        policy = str(row.get("policy") or "")
        if policy in POLICIES:
            rec["policy_roi"][policy] = _safe_float(row.get("realized_roi"))
            rec["policy_filled"][policy] = bool(row.get("filled"))
            rec["policy_profit"][policy] = _safe_float(row.get("realized_profit")) or 0.0
    # Keep only bets that have all four policies represented.
    return [
        rec for rec in by_bet.values()
        if set(rec["policy_roi"]) == set(POLICIES)
    ]


# --------------------------------------------------------------------------
# Feature cohorts (decision-time only)
# --------------------------------------------------------------------------


def _ask_bucket(ask: Optional[float]) -> str:
    if ask is None:
        return "missing"
    if ask < 0.55:
        return "<0.55"
    if ask < 0.70:
        return "0.55-0.70"
    if ask < 0.85:
        return "0.70-0.85"
    return ">=0.85"


def _spread_bucket(spread: Optional[float]) -> str:
    if spread is None:
        return "missing"
    if spread < 0.02:
        return "tight(<0.02)"
    if spread < 0.04:
        return "medium(0.02-0.04)"
    return "wide(>=0.04)"


def _inning_bucket(inning: Any) -> str:
    try:
        i = int(inning)
    except (TypeError, ValueError):
        return "missing"
    if i <= 3:
        return "early(<=3)"
    if i <= 6:
        return "middle(4-6)"
    return "late(>=7)"


def _marketable_bucket(rec: Dict[str, Any]) -> str:
    return "marketable" if rec.get("marketable_at_entry") else "passive"


FEATURE_FUNCS: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "ask_bucket": lambda r: _ask_bucket(r.get("decision_ask")),
    "spread_bucket": lambda r: _spread_bucket(r.get("entry_spread")),
    "inning_bucket": lambda r: _inning_bucket(r.get("inning")),
    "marketable": _marketable_bucket,
}


# --------------------------------------------------------------------------
# Per-policy aggregates
# --------------------------------------------------------------------------


def _policy_aggregates(bets: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for policy in POLICIES:
        rois_coerced = [_coerced_roi(b["policy_roi"].get(policy)) for b in bets]
        rois_filled_only = [
            r for r in (_safe_float(b["policy_roi"].get(policy)) for b in bets)
            if r is not None
        ]
        filled = [bool(b["policy_filled"].get(policy)) for b in bets]
        profits = [_safe_float(b["policy_profit"].get(policy)) or 0.0 for b in bets]
        n = len(bets)
        out[policy] = {
            "bets": n,
            "filled": sum(filled),
            "fill_rate": round(sum(filled) / n, 4) if n else None,
            "mean_roi_per_bet": round(sum(rois_coerced) / n, 6) if n else None,
            "mean_roi_when_filled": round(
                sum(rois_filled_only) / len(rois_filled_only), 6
            ) if rois_filled_only else None,
            "total_realized_profit": round(sum(profits), 2),
        }
    return out


def _oracle_roi_per_bet(bets: Sequence[Dict[str, Any]]) -> float:
    """Best (highest) realized ROI per bet, no-fill coerced to 0."""
    if not bets:
        return 0.0
    total = 0.0
    for bet in bets:
        best = max(_coerced_roi(bet["policy_roi"].get(p)) for p in POLICIES)
        total += best
    return total / len(bets)


def _oracle_choice_distribution(
    bets: Sequence[Dict[str, Any]],
) -> Dict[str, int]:
    """Which policy hindsight would have picked per bet (with ties broken
    by the POLICIES tuple order so cheaper-fill policies win ties)."""
    counter: Counter = Counter()
    for bet in bets:
        best_roi = max(_coerced_roi(bet["policy_roi"].get(p)) for p in POLICIES)
        for p in POLICIES:
            if _coerced_roi(bet["policy_roi"].get(p)) == best_roi:
                counter[p] += 1
                break
    return dict(counter)


# --------------------------------------------------------------------------
# Cohort breakdown
# --------------------------------------------------------------------------


def _cohort_breakdown(
    bets: Sequence[Dict[str, Any]],
    *,
    feature_name: str,
) -> Dict[str, Dict[str, Any]]:
    bucket_fn = FEATURE_FUNCS[feature_name]
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for bet in bets:
        grouped[bucket_fn(bet)].append(bet)

    out: Dict[str, Dict[str, Any]] = {}
    for bucket, group in sorted(grouped.items()):
        per_policy = {
            policy: round(
                sum(_coerced_roi(b["policy_roi"].get(policy)) for b in group)
                / len(group),
                6,
            )
            for policy in POLICIES
        }
        best_policy = max(per_policy.items(), key=lambda kv: kv[1])[0]
        out[bucket] = {
            "n": len(group),
            "mean_roi_by_policy": per_policy,
            "best_policy": best_policy,
            "best_policy_roi": per_policy[best_policy],
            "baseline_roi": per_policy[BASELINE_POLICY],
            "headroom_vs_baseline": round(
                per_policy[best_policy] - per_policy[BASELINE_POLICY], 6
            ),
        }
    return out


# --------------------------------------------------------------------------
# Candidate rules
# --------------------------------------------------------------------------


def _rule_always(policy: str) -> Callable[[Dict[str, Any], Sequence[Dict[str, Any]]], str]:
    def _choose(_rec: Dict[str, Any], _training: Sequence[Dict[str, Any]]) -> str:
        return policy
    return _choose


def _rule_marketable_takes(rec: Dict[str, Any], _training: Sequence[Dict[str, Any]]) -> str:
    """If the book was already at-or-below our limit at decision time, the
    edge is shrinking fast -- take the touch with taker_like; otherwise
    sit passively at the current ask."""
    return "taker_like" if rec.get("marketable_at_entry") else "current_limit"


def _learned_lookup_rule(
    feature_name: str,
) -> Callable[[Dict[str, Any], Sequence[Dict[str, Any]]], str]:
    """Pick the best policy for the bet's cohort, fitted on the training
    set. Falls back to the training-set global best when a cohort has
    fewer than MIN_COHORT_SAMPLE rows.
    """
    bucket_fn = FEATURE_FUNCS[feature_name]

    def _choose(rec: Dict[str, Any], training: Sequence[Dict[str, Any]]) -> str:
        bucket = bucket_fn(rec)
        per_bucket: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for b in training:
            per_bucket[bucket_fn(b)].append(b)
        group = per_bucket.get(bucket, [])
        if len(group) >= MIN_COHORT_SAMPLE:
            target = group
        else:
            target = list(training)
        best_policy = BASELINE_POLICY
        best_roi = float("-inf")
        for policy in POLICIES:
            roi = (
                sum(_coerced_roi(b["policy_roi"].get(policy)) for b in target)
                / len(target)
            ) if target else 0.0
            if roi > best_roi:
                best_roi = roi
                best_policy = policy
        return best_policy

    return _choose


CANDIDATE_RULES: Dict[
    str, Callable[[Dict[str, Any], Sequence[Dict[str, Any]]], str]
] = {
    "baseline_current_limit": _rule_always("current_limit"),
    "always_taker_like": _rule_always("taker_like"),
    "always_limit_p2c": _rule_always("limit_p2c"),
    "marketable_takes_else_current": _rule_marketable_takes,
    "learned_by_ask_bucket": _learned_lookup_rule("ask_bucket"),
    "learned_by_spread_bucket": _learned_lookup_rule("spread_bucket"),
    "learned_by_marketable": _learned_lookup_rule("marketable"),
}


def _evaluate_rule_in_sample(
    bets: Sequence[Dict[str, Any]],
    rule: Callable[[Dict[str, Any], Sequence[Dict[str, Any]]], str],
) -> Dict[str, Any]:
    choices = [rule(bet, bets) for bet in bets]
    rois = [_coerced_roi(bet["policy_roi"].get(choice)) for bet, choice in zip(bets, choices)]
    return {
        "n": len(bets),
        "mean_roi": round(sum(rois) / len(rois), 6) if rois else None,
        "choice_distribution": dict(Counter(choices)),
    }


def _evaluate_rule_loocv(
    bets: Sequence[Dict[str, Any]],
    rule: Callable[[Dict[str, Any], Sequence[Dict[str, Any]]], str],
) -> Dict[str, Any]:
    """Leave-one-out CV. For each bet, fit the rule on the other n-1
    and apply to the held-out bet. Honest sample of how the rule
    generalizes when it cannot peek at the held-out bet's outcome.
    """
    choices: List[str] = []
    rois: List[float] = []
    for i, bet in enumerate(bets):
        training = list(bets[:i]) + list(bets[i + 1:])
        choice = rule(bet, training)
        choices.append(choice)
        rois.append(_coerced_roi(bet["policy_roi"].get(choice)))
    return {
        "n": len(bets),
        "mean_roi": round(sum(rois) / len(rois), 6) if rois else None,
        "choice_distribution": dict(Counter(choices)),
    }


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_report(
    *,
    input_path: Path = DEFAULT_INPUT_PATH,
    fill_model: str = DEFAULT_FILL_MODEL,
) -> Dict[str, Any]:
    raw_rows = _load_jsonl(input_path)
    bets = _pivot_bets(raw_rows, fill_model=fill_model)

    if not bets:
        return {
            "schema_version": 1,
            "generated_at_utc": _now_iso(),
            "input_path": str(input_path),
            "fill_model": fill_model,
            "bets": 0,
            "warning": "No eligible bets found in replay input.",
        }

    aggregates = _policy_aggregates(bets)
    oracle_mean = round(_oracle_roi_per_bet(bets), 6)
    oracle_dist = _oracle_choice_distribution(bets)
    baseline_mean = aggregates[BASELINE_POLICY]["mean_roi_per_bet"]
    headroom = round((oracle_mean or 0.0) - (baseline_mean or 0.0), 6)

    cohorts = {
        name: _cohort_breakdown(bets, feature_name=name)
        for name in FEATURE_FUNCS
    }

    rule_results: Dict[str, Dict[str, Any]] = {}
    for name, rule in CANDIDATE_RULES.items():
        in_sample = _evaluate_rule_in_sample(bets, rule)
        loocv = _evaluate_rule_loocv(bets, rule)
        rule_results[name] = {
            "in_sample": in_sample,
            "loocv": loocv,
            "loocv_roi_vs_baseline": (
                round(loocv["mean_roi"] - baseline_mean, 6)
                if loocv["mean_roi"] is not None and baseline_mean is not None
                else None
            ),
        }

    # Best learned rule by LOOCV mean ROI (excluding baseline).
    best_rule_name: Optional[str] = None
    best_rule_roi: Optional[float] = None
    for name, result in rule_results.items():
        if name == "baseline_current_limit":
            continue
        roi = result["loocv"]["mean_roi"]
        if roi is None:
            continue
        if best_rule_roi is None or roi > best_rule_roi:
            best_rule_roi = roi
            best_rule_name = name

    headroom_captured: Optional[float] = None
    if (
        baseline_mean is not None
        and best_rule_roi is not None
        and headroom > 1e-9
    ):
        headroom_captured = round(
            (best_rule_roi - baseline_mean) / headroom, 4
        )

    return {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "input_path": str(input_path),
        "fill_model": fill_model,
        "bets": len(bets),
        "policies": list(POLICIES),
        "policy_aggregates": aggregates,
        "baseline_policy": BASELINE_POLICY,
        "baseline_mean_roi_per_bet": baseline_mean,
        "oracle_mean_roi_per_bet": oracle_mean,
        "oracle_headroom_vs_baseline": headroom,
        "oracle_choice_distribution": oracle_dist,
        "cohort_breakdowns": cohorts,
        "rule_results": rule_results,
        "best_rule_by_loocv": best_rule_name,
        "best_rule_loocv_roi": best_rule_roi,
        "best_rule_headroom_captured": headroom_captured,
        "thresholds": {"min_cohort_sample": MIN_COHORT_SAMPLE},
        "interpretation_notes": [
            "Per-bet ROI coerces no-fill to 0 (no trade = no P&L).",
            "Cohort 'best policy' is the policy with highest mean per-bet ROI in that cohort.",
            "LOOCV provides an honest generalization estimate at n bets; treat as research-only at this sample size.",
            "Promotion to live execution must wait for walk-forward evidence with score-event and no-score-drift families kept separate.",
        ],
    }


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value) * 100:+.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _markdown_table(headers: List[str], rows: List[List[Any]]) -> List[str]:
    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        out.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return out


def render_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = [
        "# Learned Execution Policy Prototype",
        "",
        f"- Generated: {report.get('generated_at_utc')}",
        f"- Input: {report.get('input_path')}",
        f"- Fill model: {report.get('fill_model')} (realistic)",
        f"- Bets: {report.get('bets')}",
    ]
    if report.get("bets", 0) == 0:
        lines.append("")
        lines.append("**No eligible bets.**")
        return "\n".join(lines) + "\n"

    baseline = report["baseline_mean_roi_per_bet"]
    oracle = report["oracle_mean_roi_per_bet"]
    headroom = report["oracle_headroom_vs_baseline"]
    lines.extend([
        "",
        "## Headroom",
        f"- Baseline ({BASELINE_POLICY}) mean ROI/bet: **{_fmt_pct(baseline)}**",
        f"- Oracle (in-sample best per bet) mean ROI/bet: **{_fmt_pct(oracle)}**",
        f"- Oracle headroom vs baseline: **{_fmt_pct(headroom)}**",
    ])

    lines.extend(["", "## Per-policy aggregates"])
    rows = []
    for policy in POLICIES:
        agg = report["policy_aggregates"][policy]
        rows.append([
            policy,
            agg["bets"],
            f"{agg['fill_rate'] * 100:.0f}%" if agg["fill_rate"] is not None else "n/a",
            _fmt_pct(agg["mean_roi_per_bet"]),
            _fmt_pct(agg["mean_roi_when_filled"]),
            f"${agg['total_realized_profit']:+.2f}",
        ])
    lines.extend(_markdown_table(
        ["Policy", "Bets", "Fill Rate", "Mean ROI/bet", "Mean ROI when filled", "Total realized"],
        rows,
    ))

    lines.extend(["", "## Oracle policy choice distribution"])
    for policy in POLICIES:
        n = report["oracle_choice_distribution"].get(policy, 0)
        lines.append(f"- {policy}: {n} bets")

    lines.extend(["", "## Cohort breakdowns"])
    for name, buckets in report["cohort_breakdowns"].items():
        lines.append(f"### {name}")
        rows = []
        for bucket, payload in buckets.items():
            rows.append([
                bucket,
                payload["n"],
                payload["best_policy"],
                _fmt_pct(payload["best_policy_roi"]),
                _fmt_pct(payload["baseline_roi"]),
                _fmt_pct(payload["headroom_vs_baseline"]),
            ])
        lines.extend(_markdown_table(
            ["Bucket", "N", "Best policy", "Best ROI", "Baseline ROI", "Headroom"],
            rows,
        ))
        lines.append("")

    lines.extend(["## Candidate rules"])
    rows = []
    for name, result in report["rule_results"].items():
        in_sample = result["in_sample"]
        loocv = result["loocv"]
        rows.append([
            name,
            _fmt_pct(in_sample["mean_roi"]),
            _fmt_pct(loocv["mean_roi"]),
            _fmt_pct(result["loocv_roi_vs_baseline"]),
            ", ".join(f"{k}={v}" for k, v in sorted(loocv["choice_distribution"].items())),
        ])
    lines.extend(_markdown_table(
        ["Rule", "In-sample ROI", "LOOCV ROI", "vs Baseline", "LOOCV choice dist"],
        rows,
    ))

    best = report.get("best_rule_by_loocv")
    captured = report.get("best_rule_headroom_captured")
    lines.extend([
        "",
        "## Summary",
        f"- Best non-baseline rule by LOOCV: **{best}**",
        f"- LOOCV mean ROI: **{_fmt_pct(report.get('best_rule_loocv_roi'))}**",
    ])
    if captured is not None:
        lines.append(
            f"- Captures **{captured * 100:.1f}%** of the in-sample oracle headroom over baseline."
        )

    lines.extend(["", "## Interpretation"])
    for note in report.get("interpretation_notes") or []:
        lines.append(f"- {note}")

    return "\n".join(lines) + "\n"


def write_report(
    report: Dict[str, Any],
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    *,
    output_stem: str = DEFAULT_OUTPUT_STEM,
) -> Tuple[Path, Path]:
    output_root.mkdir(parents=True, exist_ok=True)
    json_path = output_root / f"{output_stem}.json"
    md_path = output_root / f"{output_stem}.md"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Prototype a learned posting-offset policy from replay data."
    )
    p.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    p.add_argument(
        "--fill-model",
        choices=["queue_adjusted", "touch"],
        default=DEFAULT_FILL_MODEL,
    )
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--output-stem", type=str, default=DEFAULT_OUTPUT_STEM)
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    report = build_report(input_path=args.input_path, fill_model=args.fill_model)
    json_path, md_path = write_report(report, args.output_root, output_stem=args.output_stem)
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
