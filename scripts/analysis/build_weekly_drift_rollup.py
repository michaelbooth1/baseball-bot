#!/usr/bin/env python3
"""build_weekly_drift_rollup.py -- One-page weekly health/drift HTML rollup.

Reads the per-date `*_human_review.json` files under
`data/analysis_output/daily_human_review/`, picks the trailing N days
ending at `--end-date`, and emits a single static HTML page with:

  - Headline KPIs (7-day P&L, ROI, fill rate, filled WR)
  - Active-alerts feed (chronologically newest first)
  - Sparkline trend panels (ROI, cumulative P&L, fill rate, filled WR,
    daily filled count, calibration alerts, regime-mix max TVD,
    reconciler recovered-share)
  - Per-day detail table

The page is fully self-contained: inline CSS + inline SVG sparklines, no
JS, no external assets. Open in any browser, no server required. The
intent is to answer "is anything trending bad over the last week" at a
glance, the longer-horizon companion to the per-day human-review
markdown.

This script is wired into `run_daily_refresh.py` as a step before
`refresh_health_rollup`, so the artifact stays fresh as new daily
reviews land. Output:
  - `data/analysis_output/weekly_rollup/<end_date>_weekly_rollup.html`
    (dated)
  - `data/analysis_output/weekly_rollup/weekly_rollup.html`
    (canonical "latest")

Read-only: never writes under `data/live_trading/` or `data/games/`.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_INPUT_DIR = PROJECT_DIR / "data" / "analysis_output" / "daily_human_review"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "data" / "analysis_output" / "weekly_rollup"
DEFAULT_STAKE_SCALING_PATH = (
    PROJECT_DIR / "data" / "analysis_output"
    / "stake_scaling_analysis" / "stake_scaling_analysis.json"
)

DEFAULT_DAYS = 7

# Sparkline dimensions (px). Small and uniform so the panels grid cleanly.
SPARK_W = 220
SPARK_H = 44
SPARK_PAD_X = 4
SPARK_PAD_Y = 4

# Drift thresholds for the trend coloring; mirrors the daily-review
# alert thresholds so the rollup does not invent its own.
THRESH_FILL_RATE_DROP_PP = 0.30   # a drop of 30pp from baseline
THRESH_REGIME_MIX_TVD = 0.30
THRESH_RECONCILED_SHARE = 0.10


# ---------------------------------------------------------------------------
# Per-day extract
# ---------------------------------------------------------------------------

@dataclass
class DailyMetrics:
    """One row in the rollup -- one session date."""
    session_date: str
    mode: str
    total_profit: float = 0.0
    roi: float = 0.0
    filled_win_rate: Optional[float] = None
    fill_rate: Optional[float] = None
    orders_placed: int = 0           # CLOB-success count from session_summary
    orders_filled: int = 0           # CLOB-fill count from session_summary
    placement_attempts: int = 0      # attempts (CLOB success + errored) from fill_rate_health
    filled_attempts: int = 0         # fills attributed to the attempt set
    signal_win_rate: Optional[float] = None
    calibration_alerts: int = 0
    fill_rate_alerts: int = 0
    signal_quality_alerts: int = 0
    regime_mix_alerts: int = 0
    reconciler_alerts: int = 0
    regime_mix_max_tvd: Optional[float] = None
    reconciled_share: Optional[float] = None
    raw_alerts: List[Tuple[str, str]] = field(default_factory=list)


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _alert_strings(block: Any) -> List[str]:
    """Coerce an alert list to a list of strings (entries may be str OR dict)."""
    if not isinstance(block, list):
        return []
    out: List[str] = []
    for entry in block:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict):
            # Daily review writes string alerts today; future-proof for dicts.
            text = entry.get("message") or entry.get("text") or json.dumps(entry, default=str)
            out.append(str(text))
    return out


def extract_daily_metrics(payload: Dict[str, Any]) -> DailyMetrics:
    """Pull the rollup-relevant numbers out of one daily-review JSON."""
    summary = payload.get("session_summary") or {}
    cal = payload.get("calibration_health") or {}
    fill = payload.get("fill_rate_health") or {}
    sig = payload.get("signal_quality_health") or {}
    regime = payload.get("regime_mix_health") or {}
    recon = payload.get("reconciler_summary") or {}

    placed = int(summary.get("orders_placed") or 0)
    filled = int(summary.get("orders_filled") or 0)
    fill_today = fill.get("today") or {}
    placement_attempts = int(fill_today.get("placed") or 0)
    filled_attempts = int(fill_today.get("filled") or 0)
    fill_rate_today = fill_today.get("fill_rate")
    fill_rate = _safe_float(fill_rate_today)
    if fill_rate is None and placement_attempts > 0:
        fill_rate = filled_attempts / placement_attempts

    tvds = (regime.get("tvd_by_dimension") or {}).values()
    tvd_floats = [_safe_float(v) for v in tvds]
    tvd_floats = [v for v in tvd_floats if v is not None]
    max_tvd = max(tvd_floats) if tvd_floats else None

    cal_alerts = _alert_strings(cal.get("alerts"))
    fill_alerts = _alert_strings(fill.get("alerts"))
    sig_alerts = _alert_strings(sig.get("alerts"))
    regime_alerts = _alert_strings(regime.get("alerts"))
    recon_alerts = _alert_strings(recon.get("alerts"))

    raw_alerts = (
        [("calibration", a) for a in cal_alerts]
        + [("fill_rate", a) for a in fill_alerts]
        + [("signal_quality", a) for a in sig_alerts]
        + [("regime_mix", a) for a in regime_alerts]
        + [("reconciler", a) for a in recon_alerts]
    )

    return DailyMetrics(
        session_date=str(payload.get("session_date") or ""),
        mode=str(payload.get("mode") or ""),
        total_profit=_safe_float(summary.get("total_profit")) or 0.0,
        roi=_safe_float(summary.get("roi")) or 0.0,
        filled_win_rate=_safe_float(summary.get("win_rate")),
        fill_rate=fill_rate,
        orders_placed=placed,
        orders_filled=filled,
        placement_attempts=placement_attempts,
        filled_attempts=filled_attempts,
        signal_win_rate=_safe_float(summary.get("signal_win_rate")),
        calibration_alerts=len(cal_alerts),
        fill_rate_alerts=len(fill_alerts),
        signal_quality_alerts=len(sig_alerts),
        regime_mix_alerts=len(regime_alerts),
        reconciler_alerts=len(recon_alerts),
        regime_mix_max_tvd=max_tvd,
        reconciled_share=_safe_float(recon.get("reconciled_share")),
        raw_alerts=raw_alerts,
    )


# ---------------------------------------------------------------------------
# Window selection
# ---------------------------------------------------------------------------

def _parse_iso_date(s: str) -> Optional[date]:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def discover_review_files(input_dir: Path) -> List[Tuple[date, Path]]:
    """Return [(date, path), ...] sorted by date asc."""
    out: List[Tuple[date, Path]] = []
    for p in input_dir.glob("*_human_review.json"):
        # filename: YYYY-MM-DD_human_review.json
        name = p.name
        if len(name) < 10:
            continue
        d = _parse_iso_date(name[:10])
        if d is None:
            continue
        out.append((d, p))
    out.sort(key=lambda t: t[0])
    return out


def select_window(
    files: Sequence[Tuple[date, Path]],
    *,
    end_date: date,
    days: int,
) -> List[Tuple[date, Path]]:
    """Trailing window of N days ending at end_date (inclusive)."""
    start = end_date - timedelta(days=days - 1)
    return [(d, p) for (d, p) in files if start <= d <= end_date]


def load_window_metrics(
    files: Sequence[Tuple[date, Path]],
) -> List[DailyMetrics]:
    rows: List[DailyMetrics] = []
    for _, p in files:
        try:
            with open(p, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"build_weekly_drift_rollup: skipping {p.name}: {exc}\n")
            continue
        rows.append(extract_daily_metrics(payload))
    return rows


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """
:root {
  --bg: #0f1115;
  --panel: #1a1d24;
  --panel-edge: #2a2e38;
  --fg: #e7e9ef;
  --muted: #8b93a3;
  --good: #4ec9b0;
  --warn: #d7a14e;
  --bad: #e0506b;
  --grid: #2a2e38;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px;
  background: var(--bg); color: var(--fg);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px; line-height: 1.45;
}
h1 { margin: 0 0 4px; font-size: 20px; font-weight: 600; }
h2 { font-size: 14px; font-weight: 600; margin: 24px 0 8px;
     color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
h3 { font-size: 12px; font-weight: 600; margin: 0 0 4px; color: var(--muted); }
.meta { color: var(--muted); margin-bottom: 16px; }
.kpi-row { display: flex; flex-wrap: wrap; gap: 12px; margin: 0 0 8px; }
.kpi { background: var(--panel); border: 1px solid var(--panel-edge);
       border-radius: 6px; padding: 10px 14px; min-width: 140px; }
.kpi .label { display: block; color: var(--muted); font-size: 11px;
              text-transform: uppercase; letter-spacing: 0.06em; }
.kpi .value { display: block; font-size: 18px; font-weight: 600; margin-top: 2px; }
.kpi.good .value { color: var(--good); }
.kpi.warn .value { color: var(--warn); }
.kpi.bad  .value { color: var(--bad); }

ul.alerts { list-style: none; padding: 0; margin: 0; }
ul.alerts li { background: var(--panel); border: 1px solid var(--panel-edge);
               border-left-width: 3px; border-radius: 4px;
               padding: 8px 10px; margin-bottom: 6px; }
ul.alerts li.calibration   { border-left-color: var(--warn); }
ul.alerts li.fill_rate     { border-left-color: var(--bad); }
ul.alerts li.signal_quality{ border-left-color: var(--bad); }
ul.alerts li.regime_mix    { border-left-color: var(--warn); }
ul.alerts li.reconciler    { border-left-color: var(--warn); }
ul.alerts .when { color: var(--muted); margin-right: 8px; }
ul.alerts .dim  { color: var(--muted); margin-right: 8px;
                  font-size: 11px; text-transform: uppercase; }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}
.panel {
  background: var(--panel); border: 1px solid var(--panel-edge);
  border-radius: 6px; padding: 10px 12px;
}
.panel .latest { color: var(--muted); margin-top: 4px; font-size: 11px; }
.panel .latest b { color: var(--fg); font-size: 13px; }
.panel .latest b.good { color: var(--good); }
.panel .latest b.warn { color: var(--warn); }
.panel .latest b.bad  { color: var(--bad); }
.spark { display: block; margin: 6px 0 4px; }

table.detail { border-collapse: collapse; width: 100%; font-size: 12px; }
table.detail th, table.detail td {
  text-align: right; padding: 4px 8px;
  border-bottom: 1px solid var(--grid);
}
table.detail th { color: var(--muted); font-weight: 600;
                  text-transform: uppercase; font-size: 11px;
                  letter-spacing: 0.06em; text-align: right; }
table.detail th:first-child, table.detail td:first-child { text-align: left; }
table.detail tr:hover td { background: var(--panel); }
table.detail td.good { color: var(--good); }
table.detail td.bad  { color: var(--bad); }

.empty { color: var(--muted); font-style: italic; padding: 12px 0; }
footer { color: var(--muted); margin-top: 24px; font-size: 11px; }
"""


def _classify(value: Optional[float], *, ok_band: Tuple[float, float],
              warn_band: Tuple[float, float]) -> str:
    """Return 'good' | 'warn' | 'bad' for a value against monotone bands.

    ok_band / warn_band are (lo, hi) inclusive ranges. Value below warn_band
    or above warn_band is 'bad'. Use sentinel infinities for one-sided.
    """
    if value is None:
        return ""
    if ok_band[0] <= value <= ok_band[1]:
        return "good"
    if warn_band[0] <= value <= warn_band[1]:
        return "warn"
    return "bad"


def _render_sparkline(
    values: Sequence[Optional[float]],
    *,
    kind: str = "line",
    zero_baseline: bool = False,
    color: str = "#4ec9b0",
) -> str:
    """Inline SVG sparkline. `kind` is 'line' or 'bar'. None values are gaps."""
    n = len(values)
    if n == 0:
        return f'<svg class="spark" width="{SPARK_W}" height="{SPARK_H}"></svg>'
    floats = [v for v in values if v is not None]
    if not floats:
        return f'<svg class="spark" width="{SPARK_W}" height="{SPARK_H}"></svg>'
    vmin, vmax = min(floats), max(floats)
    if zero_baseline:
        vmin = min(vmin, 0.0)
        vmax = max(vmax, 0.0)
    span = max(vmax - vmin, 1e-9)
    plot_w = SPARK_W - 2 * SPARK_PAD_X
    plot_h = SPARK_H - 2 * SPARK_PAD_Y

    def x_of(i: int) -> float:
        if n == 1:
            return SPARK_PAD_X + plot_w / 2.0
        return SPARK_PAD_X + (i / (n - 1)) * plot_w

    def y_of(v: float) -> float:
        return SPARK_PAD_Y + (1.0 - (v - vmin) / span) * plot_h

    parts: List[str] = [f'<svg class="spark" width="{SPARK_W}" height="{SPARK_H}" '
                        f'viewBox="0 0 {SPARK_W} {SPARK_H}">']

    # Zero reference line if the y-axis crosses zero.
    if vmin < 0 < vmax:
        y0 = y_of(0.0)
        parts.append(f'<line x1="{SPARK_PAD_X}" x2="{SPARK_W - SPARK_PAD_X}" '
                     f'y1="{y0:.1f}" y2="{y0:.1f}" stroke="#444" '
                     f'stroke-dasharray="2,2" stroke-width="1"/>')

    if kind == "bar":
        bar_w = plot_w / max(n, 1) * 0.8
        for i, v in enumerate(values):
            if v is None:
                continue
            bx = x_of(i) - bar_w / 2.0
            if zero_baseline and v >= 0:
                by = y_of(v)
                bh = y_of(0.0) - by
            elif zero_baseline:
                by = y_of(0.0)
                bh = y_of(v) - by
            else:
                by = y_of(v)
                bh = (SPARK_H - SPARK_PAD_Y) - by
            bh = max(bh, 1.0)
            parts.append(f'<rect x="{bx:.1f}" y="{by:.1f}" '
                         f'width="{bar_w:.1f}" height="{bh:.1f}" '
                         f'fill="{color}"/>')
    else:
        # Line: build a polyline; gaps split into multiple polylines.
        run: List[Tuple[float, float]] = []
        polylines: List[List[Tuple[float, float]]] = []
        for i, v in enumerate(values):
            if v is None:
                if run:
                    polylines.append(run)
                    run = []
                continue
            run.append((x_of(i), y_of(v)))
        if run:
            polylines.append(run)
        for poly in polylines:
            pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in poly)
            parts.append(f'<polyline fill="none" stroke="{color}" '
                         f'stroke-width="1.5" points="{pts}"/>')
        # Endpoint marker.
        last = next((v for v in reversed(values) if v is not None), None)
        if last is not None:
            last_idx = len(values) - 1 - next(
                i for i, v in enumerate(reversed(values)) if v is not None
            )
            parts.append(f'<circle cx="{x_of(last_idx):.1f}" '
                         f'cy="{y_of(last):.1f}" r="2.2" fill="{color}"/>')

    parts.append("</svg>")
    return "".join(parts)


def _fmt_pct(v: Optional[float], digits: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.{digits}f}%"


def _fmt_signed_pct(v: Optional[float], digits: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.{digits}f}%"


def _fmt_signed_money(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"${v:+,.2f}"


def _fmt_money(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"${v:,.2f}"


def _kpi_html(label: str, value: str, klass: str = "") -> str:
    cls = f" {klass}" if klass else ""
    return (f'<div class="kpi{cls}">'
            f'<span class="label">{escape(label)}</span>'
            f'<span class="value">{escape(value)}</span>'
            f'</div>')


def _panel_html(title: str, spark_svg: str, latest_html: str) -> str:
    return (f'<div class="panel"><h3>{escape(title)}</h3>'
            f'{spark_svg}<div class="latest">{latest_html}</div></div>')


def _value_class(value: Optional[float], *,
                 good_max: Optional[float] = None,
                 bad_min: Optional[float] = None,
                 good_min: Optional[float] = None,
                 bad_max: Optional[float] = None) -> str:
    """Pick a CSS class by comparing value against thresholds.

    Provide either (good_max, bad_min) for "lower is better" metrics,
    or (good_min, bad_max) for "higher is better" metrics.
    """
    if value is None:
        return ""
    if good_max is not None and bad_min is not None:
        if value <= good_max:
            return "good"
        if value >= bad_min:
            return "bad"
        return "warn"
    if good_min is not None and bad_max is not None:
        if value >= good_min:
            return "good"
        if value <= bad_max:
            return "bad"
        return "warn"
    return ""


def load_stake_scaling_payload(path: Path) -> Optional[Dict[str, Any]]:
    """Best-effort load of the Active #6 stake-scaling analyzer output."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def render_stake_scaling_section(payload: Optional[Dict[str, Any]]) -> str:
    """Render the Active #6 promotion analyzer block.

    Three states: no analyzer output -> note; need_more_data -> progress;
    hold/promote -> verdict + bucket table.
    """
    if payload is None:
        return (
            '<section class="stake-scaling-section">'
            '<h2>Active #6 stake-scaling promotion</h2>'
            '<div class="empty">No analyzer output yet -- run the daily refresh.</div>'
            '</section>'
        )
    verdict = str(payload.get("verdict") or "")
    reason = str(payload.get("verdict_reason") or "")
    n_sessions = payload.get("n_sessions") or 0
    n_bets = payload.get("n_filled_bets") or 0
    thresholds = payload.get("thresholds") or {}
    min_sessions = thresholds.get("min_sessions") or 0
    buckets = payload.get("buckets") or {}
    comp = payload.get("comparison_high_vs_low") or {}

    verdict_class = {
        "promote": "good",
        "hold": "warn",
        "need_more_data": "",
    }.get(verdict, "")

    rows_html: List[str] = []
    rows_html.append(
        "<tr><th>Bucket</th><th>N</th><th>Wins</th><th>WR</th>"
        "<th>Stake</th><th>P&L</th><th>ROI</th>"
        "<th>Avg mult</th><th>Avg edge</th></tr>"
    )
    for name in ("low", "mid", "high"):
        b = buckets.get(name) or {}
        wr = b.get("win_rate")
        roi = b.get("roi")
        am = b.get("avg_multiplier")
        ae = b.get("avg_edge_used")
        rows_html.append(
            "<tr>"
            f"<td>{escape(name)}</td>"
            f"<td>{b.get('n', 0)}</td>"
            f"<td>{b.get('wins', 0)}</td>"
            f"<td>{_fmt_pct(wr)}</td>"
            f"<td>${b.get('total_stake', 0):.2f}</td>"
            f'<td>{_fmt_signed_money(b.get("total_profit"))}</td>'
            f"<td>{_fmt_pct(roi)}</td>"
            f'<td>{ "—" if am is None else f"{am:.2f}x"}</td>'
            f'<td>{ "—" if ae is None else f"{ae:+.3f}"}</td>'
            "</tr>"
        )
    table = '<table class="detail">' + "".join(rows_html) + "</table>"

    progress_pct = min(100.0, (n_sessions / min_sessions * 100.0)
                       if min_sessions else 0.0)
    progress_bar = (
        '<div style="background:var(--grid);border-radius:3px;'
        'overflow:hidden;height:8px;margin:6px 0;">'
        f'<div style="width:{progress_pct:.1f}%;height:8px;'
        'background:var(--good);"></div></div>'
    )

    wr_delta = comp.get("wr_delta")
    roi_delta = comp.get("roi_delta")
    delta_html = (
        f'<div class="meta">High - Low cohort: '
        f'WR delta <b>{_fmt_signed_pct(wr_delta)}</b>, '
        f'ROI delta <b>{_fmt_signed_pct(roi_delta)}</b>'
        f' <span style="color:var(--muted)">'
        f'(promote thresholds WR ≥ {thresholds.get("promote_min_wr_delta", 0) * 100:.0f}pp '
        f'AND ROI ≥ {thresholds.get("promote_min_roi_delta", 0) * 100:.0f}pp)</span>'
        f'</div>'
    )

    return (
        '<section class="stake-scaling-section">'
        '<h2>Active #6 stake-scaling promotion</h2>'
        '<div class="kpi-row">'
        + _kpi_html("Verdict", verdict, verdict_class)
        + _kpi_html(
            "Sessions",
            f"{n_sessions} / {min_sessions}",
            "good" if n_sessions >= min_sessions else "",
        )
        + _kpi_html("Filled bets", str(n_bets))
        + '</div>'
        + progress_bar
        + f'<div class="meta">{escape(reason)}</div>'
        + delta_html
        + table
        + '</section>'
    )


def render_html(
    rows: List[DailyMetrics],
    *,
    end_date: date,
    days: int,
    generated_at_utc: str,
    stake_scaling_payload: Optional[Dict[str, Any]] = None,
) -> str:
    if not rows:
        body = ('<div class="empty">No daily-review files found in window '
                f'{end_date - timedelta(days=days - 1)} → {end_date}.</div>')
        return _wrap_html(body, end_date=end_date, generated_at_utc=generated_at_utc)

    # KPI roll-ups across the window.
    total_profit = sum(r.total_profit for r in rows)
    total_staked = sum(
        ((r.total_profit / r.roi) if (r.roi and r.roi != 0) else 0.0) for r in rows
    )
    rollup_roi = (total_profit / total_staked) if total_staked > 0 else None
    # Fill rate uses the ATTEMPT denominator (matches the daily-review alert):
    # "of every signal we tried to place, what fraction reached the book and filled?"
    # session_summary.orders_placed is CLOB-success count and undercounts attempts
    # on days when the wallet ran out of free USDC (62 errors on 2026-05-12).
    attempts = sum(r.placement_attempts for r in rows)
    fills_attempts = sum(r.filled_attempts for r in rows)
    rollup_fill_rate = (fills_attempts / attempts) if attempts > 0 else None
    fills_clob = sum(r.orders_filled for r in rows)
    wins_total = sum(int(round((r.filled_win_rate or 0.0) * r.orders_filled))
                     for r in rows if r.filled_win_rate is not None and r.orders_filled)
    rollup_filled_wr = (wins_total / fills_clob) if fills_clob > 0 else None
    total_alerts = sum(
        r.calibration_alerts + r.fill_rate_alerts + r.signal_quality_alerts
        + r.regime_mix_alerts + r.reconciler_alerts
        for r in rows
    )

    kpi_html = "".join([
        _kpi_html(
            f"{days}-day P&L",
            _fmt_signed_money(total_profit),
            "good" if total_profit > 0 else ("bad" if total_profit < 0 else ""),
        ),
        _kpi_html(
            f"{days}-day ROI",
            _fmt_signed_pct(rollup_roi),
            "good" if (rollup_roi or 0) > 0 else ("bad" if (rollup_roi or 0) < 0 else ""),
        ),
        _kpi_html(
            "Fill rate",
            _fmt_pct(rollup_fill_rate),
            _value_class(rollup_fill_rate, good_min=0.50, bad_max=0.20),
        ),
        _kpi_html(
            "Filled WR",
            _fmt_pct(rollup_filled_wr),
            _value_class(rollup_filled_wr, good_min=0.55, bad_max=0.45),
        ),
        _kpi_html(
            "Filled bets",
            f"{fills_clob} filled / {attempts} attempts",
        ),
        _kpi_html(
            "Active alerts",
            str(total_alerts),
            "good" if total_alerts == 0 else ("warn" if total_alerts <= 5 else "bad"),
        ),
    ])

    # Active alerts feed (most recent first).
    alert_items: List[str] = []
    for r in reversed(rows):
        for dim, message in r.raw_alerts:
            alert_items.append(
                f'<li class="{escape(dim)}">'
                f'<span class="when">{escape(r.session_date)}</span>'
                f'<span class="dim">{escape(dim)}</span>'
                f'{escape(message)}</li>'
            )
    if alert_items:
        alerts_html = '<ul class="alerts">' + "".join(alert_items) + "</ul>"
    else:
        alerts_html = '<div class="empty">No active alerts in window.</div>'

    # Sparkline panels.
    rois = [r.roi for r in rows]
    profits = [r.total_profit for r in rows]
    cum_profits: List[Optional[float]] = []
    running = 0.0
    for v in profits:
        running += v
        cum_profits.append(running)
    fill_rates = [r.fill_rate for r in rows]
    filled_wrs = [r.filled_win_rate for r in rows]
    filled_counts = [float(r.orders_filled) for r in rows]
    cal_counts = [float(r.calibration_alerts) for r in rows]
    tvds = [r.regime_mix_max_tvd for r in rows]
    recon_shares = [r.reconciled_share for r in rows]

    last = rows[-1]
    panels = [
        _panel_html(
            "Daily ROI",
            _render_sparkline(rois, kind="line", zero_baseline=True,
                              color="#4ec9b0"),
            f'Today: <b class="{_value_class(last.roi, good_min=0.0, bad_max=-0.05)}">'
            f'{_fmt_signed_pct(last.roi)}</b>',
        ),
        _panel_html(
            "Cumulative P&L (window)",
            _render_sparkline(cum_profits, kind="line", zero_baseline=True,
                              color="#4ec9b0" if (cum_profits[-1] or 0) >= 0 else "#e0506b"),
            f'Total: <b class="{ "good" if (cum_profits[-1] or 0) >= 0 else "bad"}">'
            f'{_fmt_signed_money(cum_profits[-1])}</b>',
        ),
        _panel_html(
            "Fill rate",
            _render_sparkline(fill_rates, kind="line", color="#4ec9b0"),
            f'Today: <b class="{_value_class(last.fill_rate, good_min=0.50, bad_max=0.20)}">'
            f'{_fmt_pct(last.fill_rate)}</b>',
        ),
        _panel_html(
            "Filled WR",
            _render_sparkline(filled_wrs, kind="line", color="#4ec9b0"),
            f'Today: <b class="{_value_class(last.filled_win_rate, good_min=0.55, bad_max=0.45)}">'
            f'{_fmt_pct(last.filled_win_rate)}</b>',
        ),
        _panel_html(
            "Filled bets per day",
            _render_sparkline(filled_counts, kind="bar", color="#4ec9b0"),
            f'Today: <b>{last.orders_filled}</b> filled '
            f'<span style="color:var(--muted)">({last.placement_attempts} attempts'
            f'{f", {last.placement_attempts - last.orders_placed} CLOB errors" if last.placement_attempts > last.orders_placed else ""})</span>',
        ),
        _panel_html(
            "Calibration alerts",
            _render_sparkline(cal_counts, kind="bar",
                              color="#d7a14e" if max(cal_counts) > 0 else "#4ec9b0"),
            f'Today: <b class="{ "good" if last.calibration_alerts == 0 else "warn"}">'
            f'{last.calibration_alerts}</b>',
        ),
        _panel_html(
            "Regime-mix max TVD",
            _render_sparkline(tvds, kind="line",
                              color="#e0506b" if (last.regime_mix_max_tvd or 0)
                              >= THRESH_REGIME_MIX_TVD else "#4ec9b0"),
            f'Today: <b class="{_value_class(last.regime_mix_max_tvd, good_max=0.20, bad_min=THRESH_REGIME_MIX_TVD)}">'
            f'{ "—" if last.regime_mix_max_tvd is None else f"{last.regime_mix_max_tvd:.2f}"}</b>'
            f' <span style="color:var(--muted)">(alert ≥ {THRESH_REGIME_MIX_TVD:.2f})</span>',
        ),
        _panel_html(
            "Reconciler recovered share",
            _render_sparkline(recon_shares, kind="line",
                              color="#e0506b" if (last.reconciled_share or 0)
                              >= THRESH_RECONCILED_SHARE else "#4ec9b0"),
            f'Today: <b class="{_value_class(last.reconciled_share, good_max=0.05, bad_min=THRESH_RECONCILED_SHARE)}">'
            f'{ "—" if last.reconciled_share is None else _fmt_pct(last.reconciled_share)}</b>'
            f' <span style="color:var(--muted)">(promote primary path ≥ {THRESH_RECONCILED_SHARE:.0%})</span>',
        ),
    ]
    panels_html = '<div class="grid">' + "".join(panels) + "</div>"

    # Per-day detail table.
    header = ("<tr><th>Date</th><th>Mode</th><th>Attempts</th><th>Placed</th>"
              "<th>Filled</th><th>Fill%</th><th>WR</th><th>P&L</th><th>ROI</th>"
              "<th>Cal alerts</th><th>Mix TVD</th><th>Recon%</th></tr>")
    body_rows: List[str] = []
    for r in rows:
        roi_cls = "good" if r.roi > 0 else ("bad" if r.roi < 0 else "")
        pnl_cls = "good" if r.total_profit > 0 else ("bad" if r.total_profit < 0 else "")
        attempts_cls = "bad" if r.placement_attempts > r.orders_placed else ""
        body_rows.append(
            "<tr>"
            f"<td>{escape(r.session_date)}</td>"
            f"<td>{escape(r.mode)}</td>"
            f'<td class="{attempts_cls}">{r.placement_attempts}</td>'
            f"<td>{r.orders_placed}</td>"
            f"<td>{r.orders_filled}</td>"
            f"<td>{_fmt_pct(r.fill_rate)}</td>"
            f"<td>{_fmt_pct(r.filled_win_rate)}</td>"
            f'<td class="{pnl_cls}">{_fmt_signed_money(r.total_profit)}</td>'
            f'<td class="{roi_cls}">{_fmt_signed_pct(r.roi)}</td>'
            f"<td>{r.calibration_alerts}</td>"
            f'<td>{ "—" if r.regime_mix_max_tvd is None else f"{r.regime_mix_max_tvd:.2f}"}</td>'
            f"<td>{_fmt_pct(r.reconciled_share)}</td>"
            "</tr>"
        )
    table_html = ('<table class="detail">'
                  + header + "".join(body_rows)
                  + "</table>")

    body = f"""
<header>
  <h1>Weekly Drift Rollup</h1>
  <div class="meta">
    {escape(rows[0].session_date)} → {escape(rows[-1].session_date)}
    ({len(rows)} session{'s' if len(rows) != 1 else ''})
    | window={days}d
    | generated {escape(generated_at_utc)}
  </div>
  <div class="kpi-row">{kpi_html}</div>
</header>
<section class="alerts-section">
  <h2>Active alerts (most recent first)</h2>
  {alerts_html}
</section>
<section class="sparklines-section">
  <h2>Trend panel</h2>
  {panels_html}
</section>
<section class="detail-section">
  <h2>Per-day detail</h2>
  {table_html}
</section>
{render_stake_scaling_section(stake_scaling_payload)}
"""
    return _wrap_html(body, end_date=end_date, generated_at_utc=generated_at_utc)


def _wrap_html(body: str, *, end_date: date, generated_at_utc: str) -> str:
    title = f"Weekly Drift Rollup — {end_date.isoformat()}"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(title)}</title>
<style>{CSS}</style>
</head>
<body>
{body}
<footer>
  Generated by scripts/analysis/build_weekly_drift_rollup.py at {escape(generated_at_utc)}.
  Read-only artifact; rerun via run_daily_refresh.py to regenerate.
</footer>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR,
                   help="Directory containing <date>_human_review.json files.")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                   help="Directory to write the HTML rollup into.")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"Trailing window size in days (default: {DEFAULT_DAYS}).")
    p.add_argument("--end-date", type=str, default=None,
                   help="End date YYYY-MM-DD (default: latest review file present).")
    p.add_argument("--no-canonical-copy", action="store_true",
                   help="Skip writing the unsuffixed weekly_rollup.html alias.")
    p.add_argument("--stake-scaling-path", type=Path,
                   default=DEFAULT_STAKE_SCALING_PATH,
                   help="Path to stake_scaling_analysis.json (Active #6).")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)

    files = discover_review_files(input_dir)
    if not files:
        sys.stderr.write(
            f"build_weekly_drift_rollup: no daily-review files in {input_dir}\n"
        )
        return 0  # not a hard failure -- nothing to render yet

    if args.end_date:
        end_date = _parse_iso_date(args.end_date)
        if end_date is None:
            sys.stderr.write(f"build_weekly_drift_rollup: bad --end-date '{args.end_date}'\n")
            return 2
    else:
        end_date = files[-1][0]

    window = select_window(files, end_date=end_date, days=args.days)
    rows = load_window_metrics(window)

    generated_at_utc = (datetime.now(timezone.utc)
                        .replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    stake_scaling_payload = load_stake_scaling_payload(Path(args.stake_scaling_path))
    html = render_html(rows, end_date=end_date, days=args.days,
                       generated_at_utc=generated_at_utc,
                       stake_scaling_payload=stake_scaling_payload)

    output_dir.mkdir(parents=True, exist_ok=True)
    dated_path = output_dir / f"{end_date.isoformat()}_weekly_rollup.html"
    dated_path.write_text(html, encoding="utf-8")
    print(f"Wrote {dated_path}")

    if not args.no_canonical_copy:
        canonical = output_dir / "weekly_rollup.html"
        canonical.write_text(html, encoding="utf-8")
        print(f"Wrote {canonical}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
