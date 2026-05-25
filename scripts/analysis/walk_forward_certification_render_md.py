"""Markdown renderer for the walk-forward certification report.

Extracted from build_walk_forward_certification.py on 2026-05-25
as part of Tier 2 — pure rendering, no I/O. Public surface
re-exported by the original module for back-compat.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# Constant referenced inside render_markdown's "Read this when" footer.
# Mirrors the value in build_walk_forward_certification.py; they must
# stay in sync. Keeping a small local copy avoids a circular import.
GATE_RETUNE_MIN_BLOCKED_N = 5


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
    if payload.get("config_label_filter"):
        parts.append(f"**Config label filter:** `{payload['config_label_filter']}`\n")
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

