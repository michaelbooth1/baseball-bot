#!/usr/bin/env python3
"""build_gate_counterfactual_report.py -- Active #11.

Counterfactual gate-change logger.

The walk-forward certification report (build_walk_forward_certification.py)
emits a single KEEP/RETUNE/RETIRE verdict per gate computed against the full
training-table window. That tells the operator "is this gate sound on average,"
but it cannot answer the operator's other question: "if I had moved THIS gate's
threshold by ONE click in the tightening direction over the last 14 / 7 days,
how much money would I have saved (or lost) in realized P&L?"

This builder takes the same gate library (GATE_DEFS) and replays each gate's
sweep thresholds against three time windows:
  - all       : every filled+settled bet in the training table
  - trailing_30d : bets from session_dates within 30 days of latest
  - trailing_7d  : bets from session_dates within 7 days of latest

For each (gate, alt_threshold, window) we compute:
  - kept cohort (bets that would still have been placed)
  - blocked cohort (bets that would have been removed by the alt threshold)
  - counterfactual_profit_delta_vs_current = -blocked.total_profit
      i.e. P&L the operator would have saved (positive) or missed (negative)
      by ENFORCING the alt threshold instead of the current one
  - kept_roi_delta_vs_current = kept.roi - current_kept.roi

A `top_recommendations` list ranks the highest-impact tightening
counterfactuals across all gates / windows, with a confidence label that
auto-degrades when the blocked cohort is thin.

Why the cert report's per-gate verdict is not enough:
  - The cert collapses all data into one verdict, which is correct for
    making a structural keep/retune/retire decision but obscures recent
    trends.
  - Composite gates (e.g. gate_inn6_rn_max applies only inning==6) show
    different signals over time as the bet mix shifts; per-window
    breakdowns surface that.
  - The cert never says "you would have saved $X by tightening THIS gate
    last week" in dollars; this report does.

Output:
  data/analysis_output/gate_counterfactual/gate_counterfactual_report.json
  data/analysis_output/gate_counterfactual/gate_counterfactual_report.md

Pure offline analysis. Reads signal_training_table.jsonl. Never writes
under live ledgers, game corpora, or cache files.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

import build_walk_forward_certification as cert  # noqa: E402


DEFAULT_TRAINING_TABLE = (
    PROJECT_DIR / "data" / "analysis_output" / "training_tables"
    / "signal_training_table.jsonl"
)
DEFAULT_OUTPUT_DIR = (
    PROJECT_DIR / "data" / "analysis_output" / "gate_counterfactual"
)

# Window definitions (in days). Match the trailing windows the other
# drift-alert dimensions use so cross-comparison is straightforward.
TRAILING_30D_DAYS = 30
TRAILING_7D_DAYS = 7

# Minimum blocked-N for a confidence label; mirrors
# GATE_RETUNE_MIN_BLOCKED_N in the cert builder so the two reports
# agree on what "enough data" means.
COUNTERFACTUAL_MIN_BLOCKED_N = cert.GATE_RETUNE_MIN_BLOCKED_N  # 5

# Counterfactual P&L delta threshold for a recommendation to appear in
# top_recommendations: a tightening must save at least this much in the
# 30d window AND have at least the min blocked-N. $25 is "roughly two
# average stakes" -- below that, signal-vs-noise is poor.
RECOMMENDATION_MIN_DELTA_USD = 25.0

# Confidence boundaries on blocked-N
CONFIDENCE_HIGH_MIN_BLOCKED = 20
CONFIDENCE_MEDIUM_MIN_BLOCKED = 10
# below CONFIDENCE_MEDIUM_MIN_BLOCKED -> low


# ---------------------------------------------------------------------------
# Window slicing
# ---------------------------------------------------------------------------

def _parse_date(d: str) -> Optional[datetime]:
    try:
        return datetime.strptime(d, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _latest_date(rows: Sequence[cert.BetRow]) -> Optional[datetime]:
    latest: Optional[datetime] = None
    for r in rows:
        d = _parse_date(r.session_date)
        if d is None:
            continue
        if latest is None or d > latest:
            latest = d
    return latest


@dataclass(frozen=True)
class WindowSlice:
    name: str
    rows: List[cert.BetRow]
    date_min: Optional[str]
    date_max: Optional[str]


def slice_windows(rows: Sequence[cert.BetRow]) -> "OrderedDict[str, WindowSlice]":
    """Slice the bet rows into all / trailing_30d / trailing_7d windows.

    All windows are anchored on the LATEST session_date in the table
    (not today's date), so the report stays meaningful even if the daily
    refresh runs on a day with no new training data.
    """
    out: "OrderedDict[str, WindowSlice]" = OrderedDict()
    out["all"] = _window_from_rows("all", rows)
    latest = _latest_date(rows)
    if latest is None:
        out["trailing_30d"] = _window_from_rows("trailing_30d", [])
        out["trailing_7d"] = _window_from_rows("trailing_7d", [])
        return out
    cutoff_30 = latest - timedelta(days=TRAILING_30D_DAYS - 1)  # inclusive
    cutoff_7 = latest - timedelta(days=TRAILING_7D_DAYS - 1)
    out["trailing_30d"] = _window_from_rows(
        "trailing_30d",
        [r for r in rows if _in_window(r, cutoff_30, latest)],
    )
    out["trailing_7d"] = _window_from_rows(
        "trailing_7d",
        [r for r in rows if _in_window(r, cutoff_7, latest)],
    )
    return out


def _in_window(
    row: cert.BetRow, lo: datetime, hi: datetime,
) -> bool:
    d = _parse_date(row.session_date)
    if d is None:
        return False
    return lo <= d <= hi


def _window_from_rows(name: str, rows: Sequence[cert.BetRow]) -> WindowSlice:
    # Only parseable session_date strings count toward date_min / date_max.
    # Unparseable rows are still kept in `rows` (the gate sweep treats them
    # like any other bet) but they shouldn't pollute the displayed range.
    parseable = {
        r.session_date for r in rows
        if r.session_date and _parse_date(r.session_date) is not None
    }
    dates = sorted(parseable)
    return WindowSlice(
        name=name,
        rows=list(rows),
        date_min=dates[0] if dates else None,
        date_max=dates[-1] if dates else None,
    )


# ---------------------------------------------------------------------------
# Counterfactual math
# ---------------------------------------------------------------------------

@dataclass
class SweepCounterfactual:
    """One (gate, alt_threshold, window) counterfactual evaluation."""
    threshold: float
    is_current: bool
    is_tightening: Optional[bool]   # None when threshold == current
    kept: Dict[str, Any]
    blocked: Dict[str, Any]
    counterfactual_profit_delta_vs_current: Optional[float]
    kept_roi_delta_vs_current: Optional[float]
    n_applicable: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "threshold": self.threshold,
            "is_current": self.is_current,
            "is_tightening": self.is_tightening,
            "n_applicable": self.n_applicable,
            "kept": self.kept,
            "blocked": self.blocked,
            "counterfactual_profit_delta_vs_current": _round_or_none(
                self.counterfactual_profit_delta_vs_current, 2,
            ),
            "kept_roi_delta_vs_current": _round_or_none(
                self.kept_roi_delta_vs_current, 4,
            ),
        }


def _round_or_none(v: Optional[float], digits: int) -> Optional[float]:
    return None if v is None else round(v, digits)


def _is_tightening(
    gate: cert.GateDef, threshold: float,
) -> Optional[bool]:
    """Tightening direction relative to the current threshold.

    For max-direction gates (block above), LOWERING the threshold
    tightens. For min-direction gates (block below), RAISING tightens.
    Returns None when there is no current threshold (shadow-only gates).
    """
    if gate.current_threshold is None:
        return None
    if threshold == gate.current_threshold:
        return None
    if gate.direction == "max":
        return threshold < gate.current_threshold
    return threshold > gate.current_threshold


def _count_applicable(
    rows: Sequence[cert.BetRow], gate: cert.GateDef,
) -> int:
    if gate.applicability is None:
        return len(rows)
    return sum(1 for r in rows if gate.applicability(r))


def evaluate_sweep_for_window(
    rows: Sequence[cert.BetRow], gate: cert.GateDef,
) -> List[SweepCounterfactual]:
    """Build the sweep counterfactual list for one window."""
    n_applicable = _count_applicable(rows, gate)
    # Reference: kept cohort under the CURRENT threshold (None for shadow).
    current_kept_roi: Optional[float] = None
    current_kept_profit: Optional[float] = None
    if gate.current_threshold is not None:
        cur_kept, cur_blocked = cert._sweep_one(
            rows, gate, gate.current_threshold,
        )
        current_kept_roi = cur_kept.roi
        current_kept_profit = cur_kept.total_profit

    results: List[SweepCounterfactual] = []
    # Include the current threshold in the sweep set if it's not already
    # there, so the operator always sees a "current" anchor row.
    sweep_thresholds = list(gate.sweep_thresholds)
    if (
        gate.current_threshold is not None
        and gate.current_threshold not in sweep_thresholds
    ):
        sweep_thresholds.append(float(gate.current_threshold))
        sweep_thresholds.sort(reverse=(gate.direction == "max"))

    for thr in sweep_thresholds:
        kept, blocked = cert._sweep_one(rows, gate, thr)
        is_cur = (
            gate.current_threshold is not None
            and thr == gate.current_threshold
        )
        cf_delta: Optional[float] = None
        roi_delta: Optional[float] = None
        if gate.current_threshold is not None and not is_cur:
            # Counterfactual P&L delta vs CURRENT threshold.
            # Two cases:
            #  - TIGHTENING (alt would block bets the current does not):
            #      blocked@alt strictly contains blocked@current. The
            #      money we'd have saved = -profit of the bets newly
            #      blocked = -(blocked@alt.profit - blocked@current.profit).
            #  - LOOSENING (alt would un-block bets the current blocks):
            #      blocked@current strictly contains blocked@alt. The
            #      money we'd have gained = profit of the bets newly
            #      kept = -(blocked@alt.profit - blocked@current.profit).
            # In both cases the formula is the same (sign carries the
            # interpretation): positive = improvement vs status quo.
            cf_delta = float(cur_blocked.total_profit) - float(
                blocked.total_profit
            )
            if kept.roi is not None and current_kept_roi is not None:
                roi_delta = kept.roi - current_kept_roi
        results.append(SweepCounterfactual(
            threshold=thr,
            is_current=is_cur,
            is_tightening=_is_tightening(gate, thr),
            n_applicable=n_applicable,
            kept=kept.to_dict(),
            blocked=blocked.to_dict(),
            counterfactual_profit_delta_vs_current=cf_delta,
            kept_roi_delta_vs_current=roi_delta,
        ))
    return results


def evaluate_gate_counterfactual(
    windows: "OrderedDict[str, WindowSlice]", gate: cert.GateDef,
) -> Dict[str, Any]:
    """Evaluate one gate across all windows."""
    per_window: Dict[str, Any] = OrderedDict()
    for name, w in windows.items():
        sweep = evaluate_sweep_for_window(w.rows, gate)
        per_window[name] = {
            "date_range": (
                [w.date_min, w.date_max]
                if (w.date_min and w.date_max) else None
            ),
            "n_rows": len(w.rows),
            "sweep": [s.to_dict() for s in sweep],
        }
    return {
        "name": gate.name,
        "description": gate.description,
        "direction": gate.direction,
        "current_threshold": gate.current_threshold,
        "shadow_only": gate.shadow_only,
        "applicability_label": _applicability_label(gate),
        "windows": per_window,
    }


def _applicability_label(gate: cert.GateDef) -> Optional[str]:
    """Human-readable applicability descriptor, or None for universal."""
    if gate.applicability is None:
        return None
    # The cert library expresses applicability via small helper
    # closures; their docstrings describe the domain. We surface them
    # by name when possible.
    pred = gate.applicability
    doc = (pred.__doc__ or "").strip().splitlines()
    if doc:
        return doc[0]
    name = getattr(pred, "__qualname__", None) or getattr(pred, "__name__", "")
    return name or "composite"


# ---------------------------------------------------------------------------
# Top recommendations
# ---------------------------------------------------------------------------

def _confidence_label(n_blocked: int) -> str:
    if n_blocked >= CONFIDENCE_HIGH_MIN_BLOCKED:
        return "high"
    if n_blocked >= CONFIDENCE_MEDIUM_MIN_BLOCKED:
        return "medium"
    return "low"


def build_top_recommendations(
    gates_payload: Sequence[Dict[str, Any]],
    window_name: str = "trailing_30d",
    *,
    min_blocked_n: int = COUNTERFACTUAL_MIN_BLOCKED_N,
    min_delta_usd: float = RECOMMENDATION_MIN_DELTA_USD,
    max_results: int = 10,
) -> List[Dict[str, Any]]:
    """Rank the highest-impact tightening counterfactuals in `window_name`.

    Sort key: counterfactual_profit_delta_vs_current DESC (most $ saved
    first). Only entries that:
      - have a non-None counterfactual delta
      - are tightenings (current threshold remains a candidate for
        loosening but those need a separate confidence story)
      - have blocked.n_filled >= min_blocked_n in the chosen window
      - have counterfactual_profit_delta_vs_current >= min_delta_usd

    qualify. The cap (max_results) keeps the operator's eye on the top
    of the list rather than 50+ candidate flips.
    """
    candidates: List[Dict[str, Any]] = []
    for g in gates_payload:
        if g.get("shadow_only"):
            # Shadow gates have no current threshold to tighten FROM.
            continue
        current_threshold = g.get("current_threshold")
        if current_threshold is None:
            continue
        windows = g.get("windows") or {}
        window = windows.get(window_name) or {}
        sweep = window.get("sweep") or []
        for s in sweep:
            if not s.get("is_tightening"):
                continue
            cf_delta = s.get("counterfactual_profit_delta_vs_current")
            if cf_delta is None or cf_delta < min_delta_usd:
                continue
            blocked = s.get("blocked") or {}
            n_blocked = int(blocked.get("n_filled") or 0)
            if n_blocked < min_blocked_n:
                continue
            kept_roi = (s.get("kept") or {}).get("roi")
            kept_roi_delta = s.get("kept_roi_delta_vs_current")
            blocked_roi = blocked.get("roi")
            candidates.append({
                "gate": g.get("name"),
                "from_threshold": current_threshold,
                "to_threshold": s.get("threshold"),
                "direction": g.get("direction"),
                "applicability_label": g.get("applicability_label"),
                "window": window_name,
                "window_date_range": window.get("date_range"),
                "counterfactual_profit_delta_usd": cf_delta,
                "kept_roi_after": kept_roi,
                "kept_roi_delta_vs_current": kept_roi_delta,
                "blocked_n_filled": n_blocked,
                "blocked_roi": blocked_roi,
                "confidence": _confidence_label(n_blocked),
                "rationale": _rationale_text(
                    gate_name=str(g.get("name")),
                    from_threshold=current_threshold,
                    to_threshold=s.get("threshold"),
                    cf_delta=cf_delta,
                    blocked=blocked,
                    kept=s.get("kept") or {},
                    kept_roi_delta=kept_roi_delta,
                    window_name=window_name,
                ),
            })
    candidates.sort(
        key=lambda r: r.get("counterfactual_profit_delta_usd") or 0.0,
        reverse=True,
    )
    return candidates[:max_results]


def _rationale_text(
    *,
    gate_name: str,
    from_threshold: Any,
    to_threshold: Any,
    cf_delta: float,
    blocked: Dict[str, Any],
    kept: Dict[str, Any],
    kept_roi_delta: Optional[float],
    window_name: str,
) -> str:
    n_blocked = int(blocked.get("n_filled") or 0)
    blocked_roi = blocked.get("roi")
    kept_n = int(kept.get("n_filled") or 0)
    kept_roi = kept.get("roi")
    parts = [
        (
            f"Tightening `{gate_name}` from {from_threshold} to "
            f"{to_threshold} over {window_name.replace('_', ' ')} "
            f"would have removed {n_blocked} filled bet(s) "
        )
    ]
    if blocked_roi is not None:
        parts.append(f"at ROI {blocked_roi * 100:+.1f}%")
    parts.append(f", saving ${cf_delta:+,.2f} in realized P&L. ")
    if kept_n and kept_roi is not None:
        kept_part = (
            f"Remaining {kept_n} kept bet(s) lift to ROI "
            f"{kept_roi * 100:+.1f}%"
        )
        if kept_roi_delta is not None:
            kept_part += (
                f" ({kept_roi_delta * 100:+.1f}pp vs current)"
            )
        kept_part += "."
        parts.append(kept_part)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Top-level payload
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )


def build_counterfactual_payload(
    rows: Sequence[cert.BetRow],
    *,
    training_table_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Top-level builder: windows × gates × sweep, plus top_recommendations."""
    windows = slice_windows(rows)

    gates_payload: List[Dict[str, Any]] = []
    for g in cert.GATE_DEFS:
        gates_payload.append(evaluate_gate_counterfactual(windows, g))

    overall_dates = sorted({r.session_date for r in rows if r.session_date})
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "active_priority": "Active #11 (counterfactual gate-change logger)",
        "training_table_path": (
            str(training_table_path) if training_table_path else None
        ),
        "n_rows": len(rows),
        "date_span": (
            {"first": overall_dates[0], "last": overall_dates[-1]}
            if overall_dates else None
        ),
        "windows": OrderedDict(
            (
                name,
                {
                    "date_range": (
                        [w.date_min, w.date_max]
                        if (w.date_min and w.date_max) else None
                    ),
                    "n_rows": len(w.rows),
                },
            )
            for name, w in windows.items()
        ),
        "config": {
            "trailing_30d_days": TRAILING_30D_DAYS,
            "trailing_7d_days": TRAILING_7D_DAYS,
            "min_blocked_n": COUNTERFACTUAL_MIN_BLOCKED_N,
            "recommendation_min_delta_usd": RECOMMENDATION_MIN_DELTA_USD,
        },
        "gates": gates_payload,
        "top_recommendations": build_top_recommendations(gates_payload),
        # Also surface the trailing-7d top list for the operator who
        # wants the freshest signal (lower sample, lower confidence,
        # but most responsive).
        "top_recommendations_trailing_7d": build_top_recommendations(
            gates_payload, window_name="trailing_7d",
        ),
    }
    return payload


# ---------------------------------------------------------------------------
# Markdown render
# ---------------------------------------------------------------------------

def _fmt_money(v: Optional[float]) -> str:
    return "—" if v is None else f"${v:+,.2f}"


def _fmt_pct(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _fmt_signed_pct(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def _recommendation_table_md(
    title: str, recs: Sequence[Dict[str, Any]],
) -> str:
    rows = [
        f"### {title}",
        "",
    ]
    if not recs:
        rows.append("_No counterfactual recommendations clear the threshold._")
        rows.append("")
        return "\n".join(rows)
    rows.extend([
        "| Gate | From | To | $ Δ (saved) | Blocked N | Blocked ROI "
        "| Kept ROI (after) | Δ ROI | Confidence |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ])
    for r in recs:
        rows.append(
            f"| `{r.get('gate')}` | {r.get('from_threshold')} | "
            f"{r.get('to_threshold')} | "
            f"{_fmt_money(r.get('counterfactual_profit_delta_usd'))} | "
            f"{r.get('blocked_n_filled')} | "
            f"{_fmt_pct(r.get('blocked_roi'))} | "
            f"{_fmt_pct(r.get('kept_roi_after'))} | "
            f"{_fmt_signed_pct(r.get('kept_roi_delta_vs_current'))} | "
            f"{r.get('confidence')} |"
        )
    rows.append("")
    return "\n".join(rows)


def _gate_block_md(g: Dict[str, Any]) -> str:
    title = f"### `{g['name']}`"
    if g.get("shadow_only"):
        title += " _(shadow-only)_"
    parts = [title, "", f"_{g['description']}_", ""]
    cur = g.get("current_threshold")
    direction = g.get("direction")
    direction_str = "block above" if direction == "max" else "block below"
    parts.append(
        f"**Current threshold:** `{cur}` ({direction_str})"
        + (
            f" — applies to: _{g['applicability_label']}_"
            if g.get("applicability_label") else ""
        )
    )
    parts.append("")
    for window_name, window in g.get("windows", {}).items():
        n_rows = window.get("n_rows", 0)
        date_range = window.get("date_range")
        date_str = (
            f"{date_range[0]} → {date_range[1]}"
            if date_range else "no data"
        )
        parts.append(
            f"**{window_name}** (n_rows_in_window={n_rows}, {date_str}):"
        )
        sweep = window.get("sweep") or []
        if not sweep:
            parts.append("_No sweep results — empty window._")
            parts.append("")
            continue
        rows = [
            "| Threshold | n_app | Kept N | Kept Filled | Kept ROI "
            "| Blocked N | Blocked ROI | $ Δ vs current | Δ ROI |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for s in sweep:
            marker = "**" if s.get("is_current") else ""
            cf = s.get("counterfactual_profit_delta_vs_current")
            roi_d = s.get("kept_roi_delta_vs_current")
            rows.append(
                f"| {marker}{s.get('threshold')}{marker} | "
                f"{s.get('n_applicable', 0)} | "
                f"{(s.get('kept') or {}).get('n_bets')} | "
                f"{(s.get('kept') or {}).get('n_filled')} | "
                f"{_fmt_pct((s.get('kept') or {}).get('roi'))} | "
                f"{(s.get('blocked') or {}).get('n_filled')} | "
                f"{_fmt_pct((s.get('blocked') or {}).get('roi'))} | "
                f"{_fmt_money(cf)} | {_fmt_signed_pct(roi_d)} |"
            )
        parts.append("\n".join(rows))
        parts.append("")
    return "\n".join(parts)


def render_markdown(payload: Dict[str, Any]) -> str:
    parts: List[str] = []
    parts.append("# Gate counterfactual report (Active #11)\n")
    parts.append(f"_Generated {payload.get('generated_at_utc')}._\n")
    span = payload.get("date_span") or {}
    span_str = (
        f"{span.get('first', '?')} → {span.get('last', '?')}"
        if span else "no data"
    )
    parts.append(
        f"**Inputs:** {payload.get('n_rows', 0)} filled+settled bet rows; "
        f"window {span_str}.\n"
    )
    cfg = payload.get("config") or {}
    parts.append(
        f"**Confidence floor:** blocked_n_filled >= "
        f"{cfg.get('min_blocked_n')}; "
        f"recommendation $ Δ floor: "
        f"${cfg.get('recommendation_min_delta_usd'):.2f}.\n"
    )
    parts.append("## How to read this\n")
    parts.append(
        "Every gate is replayed at its sweep thresholds against three "
        "time windows. The **$ Δ vs current** column shows the realized "
        "P&L that would have been preserved (positive) or sacrificed "
        "(negative) by enforcing the alt threshold instead of the "
        "current one. The top section ranks the highest-impact "
        "tightenings -- the changes most likely to be worth the "
        "operator's review.\n"
    )
    parts.append("## Top counterfactual recommendations\n")
    parts.append(
        "_Tightening direction only; loosening counterfactuals require "
        "fill-rate modeling on never-placed bets and are deferred to "
        "Active #11 v2._\n"
    )
    parts.append(_recommendation_table_md(
        "Trailing 30d (primary)",
        payload.get("top_recommendations") or [],
    ))
    parts.append(_recommendation_table_md(
        "Trailing 7d (freshest signal, lower confidence)",
        payload.get("top_recommendations_trailing_7d") or [],
    ))
    parts.append("## Per-gate sweep panels\n")
    for g in payload.get("gates") or []:
        parts.append(_gate_block_md(g))
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
    rows = cert.load_bet_rows(Path(args.training_table))
    payload = build_counterfactual_payload(
        rows, training_table_path=Path(args.training_table),
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "gate_counterfactual_report.json"
    md_path = output_dir / "gate_counterfactual_report.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    n_recs = len(payload.get("top_recommendations") or [])
    n_recs_7 = len(payload.get("top_recommendations_trailing_7d") or [])
    print(
        f"Counterfactual: {payload.get('n_rows', 0)} rows across "
        f"{len(payload.get('windows') or {})} windows; "
        f"top recommendations 30d={n_recs} / 7d={n_recs_7}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
