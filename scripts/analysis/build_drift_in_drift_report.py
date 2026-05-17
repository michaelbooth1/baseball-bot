#!/usr/bin/env python3
"""build_drift_in_drift_report.py -- slow-creep drift detection.

The companion to `build_concept_drift_report.py`. concept_drift_health
fires on day-vs-baseline PSI (>= 0.25). This script catches the failure
mode it can't see:

    A feature that drifts ~0.005/day for 8 weeks -- never crossing the
    daily threshold but ending up wildly outside the original
    distribution.

Method
------
For each tracked feature, read trailing 30 days of PSI observations
from `psi_history.jsonl`, fit an OLS slope on (day_index, psi_value),
and project the PSI value `projection_horizon_days` (default 30) into
the future:

    projected_psi = intercept + slope * (last_day_index + horizon_days)

If `projected_psi >= 0.25` (the existing major-shift threshold) -> alert.
If `projected_psi >= 0.10` -> informational note. Otherwise stable.

The intercept-based projection (vs `current_psi + slope * horizon`)
avoids double-counting noise on the most recent observation. OLS is
intentionally simple -- alternatives (Mann-Kendall, EWMA, CUSUM) are
better-suited for different problems and add hyperparameters without
clear value at our ~30-point sample sizes.

Outputs
-------
  data/analysis_output/concept_drift/drift_in_drift_report.json
  data/analysis_output/concept_drift/drift_in_drift_report.md

The JSON is read by `build_daily_human_review_report.py`'s
`drift_in_drift_health` block; alerts mirror into the top-level Notes
alongside the existing `concept_drift_health` alerts.

Why two scripts vs extending concept_drift_report
-------------------------------------------------
Different question, different timescale. concept_drift answers "is
today different from last month?"; drift-in-drift answers "is each
day's drift trending upward?". Keeping the artifacts separate lets
either be inspected on its own and makes the meta-trend file useful
standalone for future analyses.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_HISTORY_PATH = (
    PROJECT_DIR / "data" / "analysis_output" / "concept_drift" / "psi_history.jsonl"
)
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "concept_drift"

# Mirror the cutoffs from concept_drift_report so an alert here on a
# projection has the same severity meaning as an alert there on a
# current PSI value.
DEFAULT_PROJECTED_PSI_MINOR = 0.10
DEFAULT_PROJECTED_PSI_MAJOR = 0.25
# How far back to read history, and how far forward to project. Both
# default to 30 days -- "based on the last month, where will this
# feature be next month?".
DEFAULT_HISTORY_WINDOW_DAYS = 30
DEFAULT_PROJECTION_HORIZON_DAYS = 30
# Below this many usable (non-null) points, the slope is too noisy to
# trust; verdict is `insufficient_history` and no alert is raised.
DEFAULT_MIN_POINTS_FOR_TREND = 7


# ---------------------------------------------------------------------------
# OLS slope + projection math
# ---------------------------------------------------------------------------


def ols_slope_intercept(
    xs: List[float], ys: List[float]
) -> Tuple[float, float, float]:
    """Plain ordinary-least-squares fit: y = slope * x + intercept.

    Returns (slope, intercept, r_squared). When `xs` has zero variance
    (all-same x values, which shouldn't happen for distinct days but
    is handled defensively), returns (0.0, mean(ys), 0.0).
    """
    n = len(xs)
    if n != len(ys):
        raise ValueError(f"length mismatch: xs={n}, ys={len(ys)}")
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0), 0.0
    x_bar = sum(xs) / n
    y_bar = sum(ys) / n
    ss_xx = sum((x - x_bar) ** 2 for x in xs)
    if ss_xx == 0:
        return 0.0, y_bar, 0.0
    ss_xy = sum((x - x_bar) * (y - y_bar) for x, y in zip(xs, ys))
    slope = ss_xy / ss_xx
    intercept = y_bar - slope * x_bar
    # R-squared: 1 - SSE/SST. Bounded to [0, 1] except in degenerate
    # cases (constant y where SST=0); return 0.0 there.
    ss_yy = sum((y - y_bar) ** 2 for y in ys)
    if ss_yy == 0:
        return slope, intercept, 0.0
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r_squared = max(0.0, min(1.0, 1.0 - ss_res / ss_yy))
    return slope, intercept, r_squared


def project_psi(
    *, slope: float, intercept: float, last_x: float, horizon_days: int
) -> float:
    """Project the PSI value `horizon_days` past the most recent
    observation. Uses the OLS line: intercept + slope * (last_x + horizon).
    Clamped to >= 0 since PSI is non-negative."""
    raw = intercept + slope * (last_x + horizon_days)
    return max(0.0, raw)


# ---------------------------------------------------------------------------
# History loading + windowing
# ---------------------------------------------------------------------------


def _parse_date(date_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def load_psi_history(path: Path) -> List[Dict[str, Any]]:
    """Read append-only psi_history.jsonl. Skip malformed lines silently."""
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rows.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def trailing_window(
    rows: List[Dict[str, Any]],
    *,
    active_date: str,
    window_days: int,
) -> List[Dict[str, Any]]:
    """Filter to history rows whose active_date falls in
    [active_date - window_days + 1, active_date]. Active_date is
    inclusive so today's row counts."""
    cutoff_dt = _parse_date(active_date)
    if cutoff_dt is None:
        return []
    start_dt = cutoff_dt - _timedelta(days=window_days - 1)
    out: List[Dict[str, Any]] = []
    for r in rows:
        dt = _parse_date(r.get("active_date") or "")
        if dt is None:
            continue
        if start_dt <= dt <= cutoff_dt:
            out.append(r)
    return out


# Importing timedelta lazily because the math primitives above are
# date-free; only the windowing helper needs it.
from datetime import timedelta as _timedelta  # noqa: E402


def per_feature_series(
    rows: List[Dict[str, Any]],
    *,
    active_date: str,
) -> Dict[str, List[Tuple[float, float]]]:
    """Group rows by feature, dedupe same-date rows (latest wins),
    convert to (day_index, psi_value) tuples where day_index is days
    since the most recent date in the window (so today = 0, yesterday
    = -1, ... -29).

    Rows with null `value` (insufficient_data verdicts on that day)
    are EXCLUDED -- those days don't have a real PSI to fit on.
    """
    cutoff_dt = _parse_date(active_date)
    if cutoff_dt is None:
        return {}
    by_feature_date: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for r in rows:
        feature = str(r.get("feature") or "")
        date = str(r.get("active_date") or "")[:10]
        if not feature or not date:
            continue
        val = r.get("value")
        if val is None:
            continue
        try:
            psi = float(val)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(psi):
            continue
        # Same-date dedup: keep the most recent generated_at_utc.
        existing = by_feature_date.setdefault(feature, {}).get(date)
        if existing:
            if (str(r.get("generated_at_utc") or "")
                    > str(existing.get("generated_at_utc") or "")):
                by_feature_date[feature][date] = {"psi": psi, **r}
        else:
            by_feature_date.setdefault(feature, {})[date] = {"psi": psi, **r}

    out: Dict[str, List[Tuple[float, float]]] = {}
    for feature, date_to_row in by_feature_date.items():
        points: List[Tuple[float, float]] = []
        for date, row in sorted(date_to_row.items()):
            dt = _parse_date(date)
            if dt is None:
                continue
            day_index = float((dt - cutoff_dt).days)  # negative for past
            points.append((day_index, float(row["psi"])))
        out[feature] = points
    return out


# ---------------------------------------------------------------------------
# Per-feature verdict
# ---------------------------------------------------------------------------


def _verdict_for_projection(
    projected: float, *, minor: float, major: float
) -> str:
    if projected >= major:
        return "major"
    if projected >= minor:
        return "minor"
    return "stable"


def evaluate_feature(
    feature: str,
    points: List[Tuple[float, float]],
    *,
    min_points: int = DEFAULT_MIN_POINTS_FOR_TREND,
    horizon_days: int = DEFAULT_PROJECTION_HORIZON_DAYS,
    psi_minor: float = DEFAULT_PROJECTED_PSI_MINOR,
    psi_major: float = DEFAULT_PROJECTED_PSI_MAJOR,
) -> Dict[str, Any]:
    n_points = len(points)
    if n_points < min_points:
        return {
            "n_points": n_points,
            "min_points_required": min_points,
            "verdict": "insufficient_history",
            "current_psi": (points[-1][1] if points else None),
        }
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    slope, intercept, r_squared = ols_slope_intercept(xs, ys)
    last_x = xs[-1]
    current_psi = ys[-1]
    projected = project_psi(
        slope=slope, intercept=intercept,
        last_x=last_x, horizon_days=horizon_days,
    )
    verdict = _verdict_for_projection(projected, minor=psi_minor, major=psi_major)
    return {
        "n_points": n_points,
        "first_day_index": xs[0],
        "last_day_index": last_x,
        "current_psi": round(current_psi, 4),
        "slope_per_day": round(slope, 6),
        "intercept": round(intercept, 4),
        "r_squared": round(r_squared, 4),
        "projected_psi": round(projected, 4),
        "projection_horizon_days": horizon_days,
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _alert_text(feature: str, info: Dict[str, Any]) -> str:
    return (
        f"{feature} projected PSI {info['projected_psi']:.2f} in "
        f"{info['projection_horizon_days']}d (slope {info['slope_per_day']:+.4f}/day, "
        f"R^2={info['r_squared']:.2f} over {info['n_points']} points; "
        f"current PSI {info['current_psi']:.2f}). Slow-creep major drift."
    )


def build_report(
    *,
    history_path: Path,
    active_date: str,
    history_window_days: int = DEFAULT_HISTORY_WINDOW_DAYS,
    projection_horizon_days: int = DEFAULT_PROJECTION_HORIZON_DAYS,
    min_points_for_trend: int = DEFAULT_MIN_POINTS_FOR_TREND,
    psi_minor: float = DEFAULT_PROJECTED_PSI_MINOR,
    psi_major: float = DEFAULT_PROJECTED_PSI_MAJOR,
) -> Dict[str, Any]:
    rows = load_psi_history(history_path)
    window_rows = trailing_window(
        rows, active_date=active_date, window_days=history_window_days,
    )
    feature_series = per_feature_series(window_rows, active_date=active_date)

    features: Dict[str, Dict[str, Any]] = {}
    for feature, points in sorted(feature_series.items()):
        features[feature] = evaluate_feature(
            feature, points,
            min_points=min_points_for_trend,
            horizon_days=projection_horizon_days,
            psi_minor=psi_minor, psi_major=psi_major,
        )

    alerts: List[str] = []
    for feature, info in features.items():
        if info.get("verdict") == "major":
            alerts.append(_alert_text(feature, info))

    return {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "active_date": active_date,
        "history_path": str(history_path),
        "history_window_days": history_window_days,
        "projection_horizon_days": projection_horizon_days,
        "min_points_for_trend": min_points_for_trend,
        "n_history_rows_in_window": len(window_rows),
        "n_features_evaluated": len(features),
        "thresholds": {
            "projected_psi_minor": psi_minor,
            "projected_psi_major": psi_major,
        },
        "features": features,
        "alerts": alerts,
    }


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _render_markdown(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"# Drift-in-drift report -- {payload['active_date']}")
    lines.append("")
    lines.append(
        f"- History window: trailing {payload['history_window_days']}d  "
        f"({payload['n_history_rows_in_window']} rows)"
    )
    lines.append(
        f"- Projection horizon: {payload['projection_horizon_days']}d forward"
    )
    lines.append(
        f"- Minimum points for trend: {payload['min_points_for_trend']}"
    )
    lines.append("")
    alerts = payload.get("alerts") or []
    lines.append(f"## Alerts ({len(alerts)})")
    if not alerts:
        lines.append("- No slow-creep major drift detected.")
    for a in alerts:
        lines.append(f"- {a}")
    lines.append("")
    lines.append("## Per-feature trends")
    lines.append("")
    lines.append(
        "| Feature | N points | Current PSI | Slope/day | R^2 | Projected PSI | Verdict |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for fname, info in (payload.get("features") or {}).items():
        slope = info.get("slope_per_day")
        slope_str = f"{slope:+.4f}" if isinstance(slope, (int, float)) else "-"
        proj = info.get("projected_psi")
        proj_str = f"{proj:.3f}" if isinstance(proj, (int, float)) else "-"
        cur = info.get("current_psi")
        cur_str = f"{cur:.3f}" if isinstance(cur, (int, float)) else "-"
        r2 = info.get("r_squared")
        r2_str = f"{r2:.2f}" if isinstance(r2, (int, float)) else "-"
        lines.append(
            f"| {fname} | {info.get('n_points')} | {cur_str} | "
            f"{slope_str} | {r2_str} | {proj_str} | {info.get('verdict')} |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drift-in-drift analyzer: linear-trend PSI projection over time.",
    )
    p.add_argument("--history-path", type=Path, default=DEFAULT_HISTORY_PATH)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--active-date", type=str, default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--history-window-days", type=int, default=DEFAULT_HISTORY_WINDOW_DAYS)
    p.add_argument(
        "--projection-horizon-days", type=int, default=DEFAULT_PROJECTION_HORIZON_DAYS,
    )
    p.add_argument(
        "--min-points-for-trend", type=int, default=DEFAULT_MIN_POINTS_FOR_TREND,
    )
    p.add_argument("--psi-minor", type=float, default=DEFAULT_PROJECTED_PSI_MINOR)
    p.add_argument("--psi-major", type=float, default=DEFAULT_PROJECTED_PSI_MAJOR)
    p.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero when any feature has a 'major' projected verdict.",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    payload = build_report(
        history_path=args.history_path,
        active_date=args.active_date,
        history_window_days=args.history_window_days,
        projection_horizon_days=args.projection_horizon_days,
        min_points_for_trend=args.min_points_for_trend,
        psi_minor=args.psi_minor,
        psi_major=args.psi_major,
    )
    json_path = args.output_root / "drift_in_drift_report.json"
    md_path = args.output_root / "drift_in_drift_report.md"
    _write_json(json_path, payload)
    md_path.write_text(_render_markdown(payload), encoding="utf-8")
    print(
        f"Wrote {json_path}\n"
        f"Wrote {md_path}\n"
        f"Alerts: {len(payload.get('alerts') or [])}, "
        f"features evaluated: {payload.get('n_features_evaluated', 0)}"
    )
    if args.strict and payload.get("alerts"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
