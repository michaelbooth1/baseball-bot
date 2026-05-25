"""Markdown renderer for the Stage-1 shadow-override report.

Extracted from build_stage1_shadow_override_report.py on 2026-05-25
(Tier 2). Pure rendering — takes a fully-built payload dict and
returns a string. Public surface re-exported by the original module.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# Constant referenced inside the rendered text. Mirrors the value in
# build_stage1_shadow_override_report.py; keep in sync.
ALT_B_FALLBACK_LEVEL_THRESHOLD = 2


def _fmt_pct(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _fmt_signed_pct(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def _fmt_money(v: Optional[float]) -> str:
    return "—" if v is None else f"${v:+,.2f}"


def _window_md(window_name: str, window: Dict[str, Any]) -> str:
    n = window.get("n_bets", 0)
    date_range = window.get("date_range") or []
    date_str = (
        f"{date_range[0]} → {date_range[1]}"
        if len(date_range) == 2 else "no data"
    )
    if n == 0:
        return f"### {window_name} ({date_str})\n\n_No bets in window._\n"
    prod = window.get("production") or {}
    alt_a = window.get("alt_a_empirical_when_available") or {}
    alt_b = window.get("alt_b_block_fallback_level_2plus") or {}
    parts = [
        f"### {window_name} ({date_str}, n={n})",
        "",
        f"**Production:** mean_p3={_fmt_pct(prod.get('mean_p3'))} "
        f"vs mean_won={_fmt_pct(prod.get('mean_won'))}; "
        f"**bias={_fmt_signed_pct(prod.get('bias'))}** "
        f"(P&L={_fmt_money(prod.get('total_profit'))}).",
        "",
        f"**Alt A — empirical-when-available** "
        f"(applies to {alt_a.get('n_changed', 0)} / {n} bets "
        f"= {_fmt_pct(alt_a.get('n_coverage_rate'))} coverage):",
        f"- alt mean_p3={_fmt_pct(alt_a.get('mean_p3'))}, "
        f"alt bias={_fmt_signed_pct(alt_a.get('bias'))}",
        f"- **Bias improvement vs production: "
        f"{(alt_a.get('bias_delta_vs_prod_pp') or 0.0):+.2f}pp** "
        "(positive = bias moves toward 0)",
        "",
        f"**Alt B — block fallback_level >= "
        f"{ALT_B_FALLBACK_LEVEL_THRESHOLD}** "
        f"(blocks {alt_b.get('n_blocked', 0)} / {n} bets):",
        f"- Kept ({alt_b.get('n_kept', 0)} bets) mean_p3="
        f"{_fmt_pct(alt_b.get('kept_mean_p3'))}, "
        f"kept bias={_fmt_signed_pct(alt_b.get('kept_bias'))}, "
        f"kept P&L={_fmt_money(alt_b.get('kept_total_profit'))}",
        f"- Blocked split: "
        f"{alt_b.get('blocked_n_wins', 0)}W / "
        f"{alt_b.get('blocked_n_losses', 0)}L, "
        f"blocked P&L={_fmt_money(alt_b.get('blocked_total_profit'))}",
        f"- **Counterfactual $ delta from blocking: "
        f"{_fmt_money(alt_b.get('counterfactual_profit_delta_usd'))}** "
        "(positive = saved by blocking)",
        "",
    ]
    recs = window.get("recommendations") or []
    if recs:
        parts.append("**Recommendations:**")
        parts.append("")
        for r in recs:
            parts.append(f"- `{r['alt']}` -- {r['verdict']}: " + r["rationale"])
        parts.append("")
    return "\n".join(parts)


def render_markdown(payload: Dict[str, Any]) -> str:
    parts: List[str] = []
    parts.append("# Stage-1 shadow-override report (Active #8 prep)\n")
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
        "Today's Stage-1 cell-conditional drill surfaced two specific "
        "candidate fixes to the Stage-1 over-prediction bias:\n\n"
        "- **Alt A**: in the runtime cell lookup, prefer the cell's "
        "OWN empirical rate over the Poisson smoothing when both are "
        "available. The drill found Poisson inflates by +16pp vs "
        "empirical on average.\n"
        "- **Alt B**: block bets where the runtime cell lookup fell "
        "back to `fallback_level >= "
        f"{ALT_B_FALLBACK_LEVEL_THRESHOLD}`. The drill found "
        "level_2+ fallback cells have +40pp bias (1.44x aggregate).\n\n"
        "This report replays both alts against the actual training "
        "table outcomes so the operator can see the counterfactual "
        "impact BEFORE changing the live runtime. Recommendations "
        "fire only when the alt's evidence clears the floor "
        "(>= 1pp bias improvement at >= 25% coverage for Alt A; "
        ">= $20 counterfactual saved on >= 3 blocked bets for "
        "Alt B). After this report shows durable improvement, "
        "Active #8 promotes the change to live behind a runtime "
        "shadow flag.\n"
    )
    for window_name, window in (payload.get("windows") or {}).items():
        parts.append(_window_md(window_name, window))
    cohort = payload.get("cohort_breakdown_trailing_30d") or {}
    if cohort.get("n_bets_total"):
        parts.append(_cohort_md(cohort))
    return "\n".join(parts) + "\n"


def _cohort_md(cohort: Dict[str, Any]) -> str:
    """Render the trailing-30d cohort breakdown as readable markdown.

    Surfaces:
      - top_cohorts summary (most_improved / regressions /
        highest_coverage / largest_alt_b_savings)
      - per-dimension tables for the operator who wants to dig
    """
    n_total = cohort.get("n_bets_total", 0)
    min_n = cohort.get("min_n_per_cohort", 0)
    parts: List[str] = []
    parts.append("")
    parts.append("## Cohort breakdown (trailing 30d)")
    parts.append("")
    parts.append(
        f"_Slices the trailing-30d window ({n_total} bets) by 5 "
        "cohort dimensions (edge / inning / line / ask / current-"
        "state-edge) and shows per-cohort production vs Alt A bias. "
        f"Cohorts with n < {min_n} are shown in the per-dimension "
        "tables but excluded from the top_cohorts summary._"
    )
    parts.append("")

    top = cohort.get("top_cohorts") or {}

    def _fmt_top_entry(e: Dict[str, Any]) -> str:
        delta = e.get("bias_delta_vs_prod_pp") or 0.0
        cov = e.get("coverage_rate")
        return (
            f"`{e.get('dimension')}={e.get('bucket')}`  n={e.get('n_bets')}, "
            f"delta={delta:+.2f}pp, "
            f"coverage={_fmt_pct(cov)}"
        )

    most_improved = top.get("most_improved") or []
    if most_improved:
        parts.append("### Most-improved cohorts (Alt A reduces bias most)")
        for e in most_improved:
            parts.append(f"- {_fmt_top_entry(e)}")
        parts.append("")

    regressions = top.get("regressions") or []
    if regressions:
        parts.append(
            "### Regressions (cohorts where Alt A makes bias WORSE)"
        )
        for e in regressions:
            parts.append(f"- {_fmt_top_entry(e)}")
        parts.append("")

    highest_cov = top.get("highest_coverage") or []
    if highest_cov:
        parts.append(
            "### Highest-coverage cohorts (where empirical fix has most reach)"
        )
        for e in highest_cov:
            parts.append(f"- {_fmt_top_entry(e)}")
        parts.append("")

    alt_b_savings = top.get("largest_alt_b_savings") or []
    if alt_b_savings and any(
        (e.get("alt_b_counterfactual_usd") or 0.0) > 0
        for e in alt_b_savings
    ):
        parts.append("### Largest Alt B counterfactual savings")
        for e in alt_b_savings:
            cf = e.get("alt_b_counterfactual_usd") or 0.0
            if cf <= 0:
                continue
            parts.append(
                f"- `{e.get('dimension')}={e.get('bucket')}`  "
                f"n={e.get('n_bets')}, blocked={e.get('alt_b_n_blocked')}, "
                f"savings={_fmt_money(cf)}"
            )
        parts.append("")

    by_dim = cohort.get("by_dimension") or {}
    if by_dim:
        parts.append("### Per-dimension detail")
        for dim_name in (
            "edge_bucket", "inning_bucket", "line_bucket",
            "ask_bucket", "current_state_edge_bucket",
        ):
            buckets = by_dim.get(dim_name) or {}
            if not buckets:
                continue
            parts.append("")
            parts.append(f"#### {dim_name}")
            parts.append("")
            parts.append("| bucket | n | prod_bias | alt_a_bias | delta_pp | coverage | alt_b_blocked | alt_b_$ |")
            parts.append("|---|---:|---:|---:|---:|---:|---:|---:|")
            for bucket_key in sorted(buckets.keys()):
                c = buckets[bucket_key]
                if c.get("n_bets", 0) == 0:
                    continue
                prod_bias = c.get("production", {}).get("bias")
                alt_bias = c.get("alt_a", {}).get("bias")
                delta = c.get("alt_a", {}).get("bias_delta_vs_prod_pp")
                cov = c.get("alt_a", {}).get("coverage_rate")
                ab = c.get("alt_b", {}) or {}
                parts.append(
                    f"| {bucket_key} | {c['n_bets']} | "
                    f"{_fmt_signed_pct(prod_bias)} | "
                    f"{_fmt_signed_pct(alt_bias)} | "
                    f"{(delta or 0.0):+.2f}pp | "
                    f"{_fmt_pct(cov)} | "
                    f"{ab.get('n_blocked', 0)} | "
                    f"{_fmt_money(ab.get('counterfactual_profit_delta_usd'))} |"
                )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

