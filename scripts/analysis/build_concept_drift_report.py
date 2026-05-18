#!/usr/bin/env python3
"""build_concept_drift_report.py -- leading-indicator drift detection on
the model's *input* features.

The existing five drift dimensions on the daily review (calibration,
fill rate, signal quality, regime mix, cohort ROI) are all *outcome*
indicators -- they fire after the model has already started being wrong
on real money. This script adds a leading indicator: when the input
distribution the live model sees today diverges from the distribution
it saw recently, alert *before* the calibration error / cohort losses
materialize.

Method
------
For each tracked input feature, compute Population Stability Index (PSI)
between a trailing 7-day window (current) and the 30 days immediately
prior (baseline). PSI is the textbook metric for "has this distribution
shifted":

    PSI = sum_i (current_pct_i - baseline_pct_i) * ln(current_pct_i / baseline_pct_i)

Standard interpretation:
    < 0.10 stable        no alert
    0.10-0.25 minor      informational note only
    >= 0.25 major        alert

For categorical features (e.g. stadium_id) we use Total Variation Distance
instead, matching the existing regime_mix_health helper.

Why this baseline shape (recent-prior, not training-corpus)
----------------------------------------------------------
Two viable framings: (a) snapshot the model's training distribution
once and compare against it forever; (b) compare current trailing
window against an immediately-prior trailing window. v1 ships (b)
because it doesn't require modifying Stage-2/Stage-3 build scripts to
dump baselines, naturally rolls forward as time passes, and matches
the shape of the existing drift family. Trade-off: doesn't catch
whole-season drift from prior years; v2 could add that as a second
comparison.

Outputs
-------
  data/analysis_output/concept_drift/concept_drift_report.json
  data/analysis_output/concept_drift/concept_drift_report.md
  data/analysis_output/concept_drift/psi_history.jsonl   (append-only)

The JSON is read by `build_daily_human_review_report.py`'s
`concept_drift_health` block; alerts mirror into the top-level Notes.
The history file lets future analysis detect "drift in the drift" --
features that gradually shift over weeks even if no single day's PSI
crossed the alert threshold.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_INPUT_PATH = (
    PROJECT_DIR / "data" / "analysis_output" / "unified_signals" / "signals_master.jsonl"
)
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "concept_drift"

# Feature catalogue. Continuous features are scored by PSI; categorical by TVD.
# Names match the schema in `unified_signal_table/row_builder.py` (verified
# against signals_master.jsonl on 2026-05-15).
CONTINUOUS_FEATURES: Tuple[str, ...] = (
    "weather_temp_f",
    "weather_wind_out_component_mph",
    "weather_air_density_index",
    "stage2_run_env_delta",
    "team_offense_delta",
    "base_fair_value",
)
CATEGORICAL_FEATURES: Tuple[str, ...] = (
    "stadium_id",
)

# Thresholds. PSI cutoffs are the textbook values; TVD matches
# regime_mix_health for consistency.
DEFAULT_PSI_MINOR = 0.10
DEFAULT_PSI_MAJOR = 0.25
DEFAULT_TVD_MAJOR = 0.30
# Rows-per-window sample-size guard. At ~10-20 model-bearing signals/day,
# 7-day current window has ~70-140 rows; per-feature non-null subset
# can be smaller (weather_* is null for indoor games). 30 is the floor
# below which PSI starts behaving badly on 10-bucket histograms.
DEFAULT_MIN_ROWS_PER_FEATURE = 30
DEFAULT_CURRENT_WINDOW_DAYS = 7
DEFAULT_BASELINE_WINDOW_DAYS = 30
DEFAULT_PSI_BINS = 10
# Smoothing constant added to current_pct to avoid log(0) when a current
# bin is empty. Standard PSI implementation detail.
PSI_SMOOTHING = 1e-6


# ---------------------------------------------------------------------------
# Math primitives
# ---------------------------------------------------------------------------


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def equal_frequency_bin_edges(values: Sequence[float], n_bins: int) -> List[float]:
    """Return n_bins+1 edges defining equal-frequency bins on `values`.

    Uses statistics.quantiles for the interior cut points and stretches
    the outer edges to +-inf so the bin set is exhaustive (any current
    observation lands in some bin).

    When `values` has fewer unique points than n_bins (e.g. a categorical
    masquerading as continuous), the returned edges may collapse to
    fewer effective bins -- duplicate edges are deduped. PSI handles
    this gracefully because empty bins (after dedup) just contribute 0.
    """
    if not values:
        raise ValueError("equal_frequency_bin_edges: empty values")
    if n_bins < 2:
        raise ValueError(f"equal_frequency_bin_edges: n_bins must be >= 2, got {n_bins}")
    sorted_vals = sorted(values)
    if len(sorted_vals) < n_bins:
        # Not enough samples for the requested bin count; fall back to
        # min-max with as many bins as we have unique values.
        unique = sorted(set(sorted_vals))
        if len(unique) == 1:
            return [-math.inf, math.inf]
        return [-math.inf] + unique[1:-1] + [math.inf]
    try:
        cuts = statistics.quantiles(sorted_vals, n=n_bins, method="inclusive")
    except statistics.StatisticsError:
        return [-math.inf, math.inf]
    edges = [-math.inf] + list(cuts) + [math.inf]
    # Deduplicate edges (e.g. when many baseline values are identical).
    deduped: List[float] = [edges[0]]
    for e in edges[1:]:
        if e > deduped[-1]:
            deduped.append(e)
    if len(deduped) < 2:
        return [-math.inf, math.inf]
    return deduped


def bucket_counts(values: Iterable[float], edges: Sequence[float]) -> List[int]:
    """Histogram count per (edges[i], edges[i+1]] bucket. The first
    bucket is left-inclusive, all others left-exclusive right-inclusive.
    """
    counts = [0] * (len(edges) - 1)
    for v in values:
        # Find the rightmost edge that v exceeds; that's the bucket index.
        # Loop is short (n_bins ~ 10) so a linear scan is fine.
        placed = False
        for i in range(len(edges) - 1):
            if edges[i] <= v <= edges[i + 1] if i == 0 else edges[i] < v <= edges[i + 1]:
                counts[i] += 1
                placed = True
                break
        if not placed:
            # v outside [-inf, inf] is impossible, but be defensive.
            counts[-1] += 1
    return counts


def _to_pct(counts: Sequence[int]) -> List[float]:
    total = sum(counts)
    if total <= 0:
        return [0.0 for _ in counts]
    return [c / total for c in counts]


def population_stability_index(
    current_values: Sequence[float],
    baseline_values: Sequence[float],
    *,
    n_bins: int = DEFAULT_PSI_BINS,
    smoothing: float = PSI_SMOOTHING,
) -> Tuple[float, List[float], List[float], List[float]]:
    """Return (psi, edges, baseline_pct, current_pct).

    Bin edges are computed on `baseline_values` (equal-frequency).
    `current_values` are then placed into those edges. Smoothing
    constant is added to current_pct only -- baseline can never be
    zero in a bin defined on it.
    """
    if not baseline_values:
        raise ValueError("population_stability_index: empty baseline_values")
    edges = equal_frequency_bin_edges(baseline_values, n_bins)
    baseline_counts = bucket_counts(baseline_values, edges)
    current_counts = bucket_counts(current_values, edges)
    baseline_pct = _to_pct(baseline_counts)
    current_pct = _to_pct(current_counts)
    psi = 0.0
    for b, c in zip(baseline_pct, current_pct):
        # Skip empty baseline bins (shouldn't happen with equal-frequency
        # binning, but defensive). Smoothing on current side handles
        # the "all current obs missed this bin" case.
        if b <= 0:
            continue
        c_smoothed = max(c, smoothing)
        psi += (c_smoothed - b) * math.log(c_smoothed / b)
    return psi, edges, baseline_pct, current_pct


def total_variation_distance(
    current_counts: Dict[str, int], baseline_counts: Dict[str, int]
) -> Optional[float]:
    """TVD on categorical bucket counts. Returns None if either side
    has zero observations. Matches the helper in build_daily_human_review_report
    but reproduced here so this script has no cross-import dependency
    on the report builder."""
    cur_total = sum(current_counts.values())
    base_total = sum(baseline_counts.values())
    if cur_total <= 0 or base_total <= 0:
        return None
    keys = set(current_counts) | set(baseline_counts)
    diff = 0.0
    for k in keys:
        diff += abs(
            current_counts.get(k, 0) / cur_total
            - baseline_counts.get(k, 0) / base_total
        )
    return diff / 2.0


# ---------------------------------------------------------------------------
# Window construction
# ---------------------------------------------------------------------------


def _shift_date(date_str: str, days: int) -> str:
    base = datetime.strptime(date_str, "%Y-%m-%d")
    return (base + timedelta(days=days)).strftime("%Y-%m-%d")


def compute_window_bounds(
    *,
    active_date: str,
    current_window_days: int,
    baseline_window_days: int,
) -> Dict[str, Dict[str, str]]:
    """Return start/end (inclusive) for the current and baseline windows.

    Current window is the last `current_window_days` days *strictly
    before* `active_date` (so an in-progress active session can't pull
    its own partial data into the comparison). Baseline is the
    `baseline_window_days` days immediately prior to current.
    """
    current_end = _shift_date(active_date, -1)
    current_start = _shift_date(current_end, -(current_window_days - 1))
    baseline_end = _shift_date(current_start, -1)
    baseline_start = _shift_date(baseline_end, -(baseline_window_days - 1))
    return {
        "current": {"start": current_start, "end": current_end},
        "baseline": {"start": baseline_start, "end": baseline_end},
    }


def _row_in_window(row: Dict[str, Any], start: str, end: str) -> bool:
    sd = str(row.get("session_date") or "")
    if len(sd) < 10:
        return False
    return start <= sd[:10] <= end


def split_rows_by_window(
    rows: Iterable[Dict[str, Any]], windows: Dict[str, Dict[str, str]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    cur, base = [], []
    cw = windows["current"]
    bw = windows["baseline"]
    for r in rows:
        if _row_in_window(r, cw["start"], cw["end"]):
            cur.append(r)
        elif _row_in_window(r, bw["start"], bw["end"]):
            base.append(r)
    return cur, base


# ---------------------------------------------------------------------------
# Per-feature evaluation
# ---------------------------------------------------------------------------


def _verdict_from_psi(psi: float, *, minor: float, major: float) -> str:
    if psi >= major:
        return "major"
    if psi >= minor:
        return "minor"
    return "stable"


def _summarize_continuous(values: Sequence[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"mean": None, "std": None, "min": None, "max": None}
    return {
        "mean": round(statistics.fmean(values), 4),
        "std": round(statistics.pstdev(values), 4) if len(values) > 1 else 0.0,
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "p10": round(_quantile(values, 0.10), 4),
        "p50": round(_quantile(values, 0.50), 4),
        "p90": round(_quantile(values, 0.90), 4),
    }


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[int(pos)]
    frac = pos - lo
    return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])


def evaluate_continuous_feature(
    *,
    feature_name: str,
    current_rows: Sequence[Dict[str, Any]],
    baseline_rows: Sequence[Dict[str, Any]],
    psi_minor: float = DEFAULT_PSI_MINOR,
    psi_major: float = DEFAULT_PSI_MAJOR,
    min_rows: int = DEFAULT_MIN_ROWS_PER_FEATURE,
    n_bins: int = DEFAULT_PSI_BINS,
) -> Dict[str, Any]:
    cur_vals = [v for v in (_safe_float(r.get(feature_name)) for r in current_rows) if v is not None]
    base_vals = [v for v in (_safe_float(r.get(feature_name)) for r in baseline_rows) if v is not None]

    if len(cur_vals) < min_rows or len(base_vals) < min_rows:
        return {
            "kind": "continuous",
            "metric": "psi",
            "value": None,
            "verdict": "insufficient_data",
            "current_n": len(cur_vals),
            "baseline_n": len(base_vals),
            "current_summary": _summarize_continuous(cur_vals),
            "baseline_summary": _summarize_continuous(base_vals),
            "min_rows_required": min_rows,
        }

    psi, edges, baseline_pct, current_pct = population_stability_index(
        cur_vals, base_vals, n_bins=n_bins,
    )
    return {
        "kind": "continuous",
        "metric": "psi",
        "value": round(psi, 4),
        "verdict": _verdict_from_psi(psi, minor=psi_minor, major=psi_major),
        "current_n": len(cur_vals),
        "baseline_n": len(base_vals),
        "current_summary": _summarize_continuous(cur_vals),
        "baseline_summary": _summarize_continuous(base_vals),
        # Edges + percentage vectors are useful for forensic review
        # but bulky; keep them in the JSON for now (the daily review
        # block only loads top-level alert strings).
        "bin_edges": [
            None if math.isinf(e) else round(e, 4) for e in edges
        ],
        "baseline_pct": [round(p, 4) for p in baseline_pct],
        "current_pct": [round(p, 4) for p in current_pct],
    }


def evaluate_categorical_feature(
    *,
    feature_name: str,
    current_rows: Sequence[Dict[str, Any]],
    baseline_rows: Sequence[Dict[str, Any]],
    tvd_major: float = DEFAULT_TVD_MAJOR,
    min_rows: int = DEFAULT_MIN_ROWS_PER_FEATURE,
) -> Dict[str, Any]:
    def _counts(rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for r in rows:
            v = r.get(feature_name)
            if v is None:
                continue
            key = str(v)
            out[key] = out.get(key, 0) + 1
        return out

    cur_counts = _counts(current_rows)
    base_counts = _counts(baseline_rows)
    cur_n = sum(cur_counts.values())
    base_n = sum(base_counts.values())

    if cur_n < min_rows or base_n < min_rows:
        return {
            "kind": "categorical",
            "metric": "tvd",
            "value": None,
            "verdict": "insufficient_data",
            "current_n": cur_n,
            "baseline_n": base_n,
            "current_counts": cur_counts,
            "baseline_counts": base_counts,
            "min_rows_required": min_rows,
        }

    tvd = total_variation_distance(cur_counts, base_counts)
    verdict = "major" if (tvd is not None and tvd >= tvd_major) else "stable"
    return {
        "kind": "categorical",
        "metric": "tvd",
        "value": round(tvd, 4) if tvd is not None else None,
        "verdict": verdict,
        "current_n": cur_n,
        "baseline_n": base_n,
        "current_counts": cur_counts,
        "baseline_counts": base_counts,
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _continuous_alert_text(name: str, info: Dict[str, Any]) -> str:
    cs = info.get("current_summary") or {}
    bs = info.get("baseline_summary") or {}
    return (
        f"{name} PSI={info['value']:.2f} (major shift): "
        f"current mean={cs.get('mean')} (n={info['current_n']}) "
        f"vs baseline mean={bs.get('mean')} (n={info['baseline_n']})."
    )


def _categorical_alert_text(name: str, info: Dict[str, Any]) -> str:
    return (
        f"{name} TVD={info['value']:.2f} (major shift): "
        f"current_n={info['current_n']} vs baseline_n={info['baseline_n']} "
        f"({len(info.get('current_counts') or {})} distinct values today)."
    )


def build_report(
    *,
    input_path: Path,
    active_date: str,
    current_window_days: int = DEFAULT_CURRENT_WINDOW_DAYS,
    baseline_window_days: int = DEFAULT_BASELINE_WINDOW_DAYS,
    min_rows_per_feature: int = DEFAULT_MIN_ROWS_PER_FEATURE,
    psi_minor: float = DEFAULT_PSI_MINOR,
    psi_major: float = DEFAULT_PSI_MAJOR,
    tvd_major: float = DEFAULT_TVD_MAJOR,
    n_bins: int = DEFAULT_PSI_BINS,
    continuous_features: Sequence[str] = CONTINUOUS_FEATURES,
    categorical_features: Sequence[str] = CATEGORICAL_FEATURES,
) -> Dict[str, Any]:
    """Build the concept-drift report. Pure: takes paths + config,
    returns the JSON-serializable payload."""
    rows: List[Dict[str, Any]] = []
    if input_path.exists():
        with open(input_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    windows = compute_window_bounds(
        active_date=active_date,
        current_window_days=current_window_days,
        baseline_window_days=baseline_window_days,
    )
    current_rows, baseline_rows = split_rows_by_window(rows, windows)

    features: Dict[str, Dict[str, Any]] = {}
    for fname in continuous_features:
        features[fname] = evaluate_continuous_feature(
            feature_name=fname,
            current_rows=current_rows,
            baseline_rows=baseline_rows,
            psi_minor=psi_minor,
            psi_major=psi_major,
            min_rows=min_rows_per_feature,
            n_bins=n_bins,
        )
    for fname in categorical_features:
        features[fname] = evaluate_categorical_feature(
            feature_name=fname,
            current_rows=current_rows,
            baseline_rows=baseline_rows,
            tvd_major=tvd_major,
            min_rows=min_rows_per_feature,
        )

    alerts: List[str] = []
    for fname, info in features.items():
        if info.get("verdict") != "major":
            continue
        if info["kind"] == "continuous":
            alerts.append(_continuous_alert_text(fname, info))
        else:
            alerts.append(_categorical_alert_text(fname, info))

    return {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "active_date": active_date,
        "input_path": str(input_path),
        "current_window": {**windows["current"], "n_rows": len(current_rows)},
        "baseline_window": {**windows["baseline"], "n_rows": len(baseline_rows)},
        "thresholds": {
            "psi_minor": psi_minor,
            "psi_major": psi_major,
            "tvd_major": tvd_major,
            "min_rows_per_feature": min_rows_per_feature,
            "n_bins": n_bins,
            "current_window_days": current_window_days,
            "baseline_window_days": baseline_window_days,
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


def _write_history_rows(path: Path, payload: Dict[str, Any]) -> None:
    """Append one row per feature per day to the history file. Same-day
    re-runs append additional rows; analysis can dedupe by
    `(active_date, feature)` if needed.

    Active #14 (2026-05-17): after appending, trim rows older than
    PSI_HISTORY_RETENTION_DAYS (default 365). The drift-in-drift
    analyzer (and every other consumer) only looks at the trailing
    30d, so older rows are pure storage cost. Trim is cheap (~7
    features/day x 365 days = ~2.5k rows max).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    active_date = payload["active_date"]
    generated_at_utc = payload["generated_at_utc"]
    with open(path, "a", encoding="utf-8") as f:
        for fname, info in (payload.get("features") or {}).items():
            row = {
                "generated_at_utc": generated_at_utc,
                "active_date": active_date,
                "feature": fname,
                "kind": info.get("kind"),
                "metric": info.get("metric"),
                "value": info.get("value"),
                "verdict": info.get("verdict"),
                "current_n": info.get("current_n"),
                "baseline_n": info.get("baseline_n"),
            }
            f.write(json.dumps(row) + "\n")
    _trim_psi_history(path, retention_days=PSI_HISTORY_RETENTION_DAYS)


# Active #14 (2026-05-17): retention policy on psi_history.jsonl.
# Drift-in-drift only looks at the trailing 30 days; everything else
# (concept_drift, daily_review) reads only the latest row per feature.
# 365 days is generous enough to debug a "this drift started last
# year" investigation if the operator wants while bounding the file
# at ~2.5k rows.
PSI_HISTORY_RETENTION_DAYS = 365


def _trim_psi_history(path: Path, *, retention_days: int) -> None:
    """Rewrite `path` keeping only rows whose `active_date` is within
    `retention_days` of the LATEST date in the file. Atomic rewrite via
    tmp file. Best-effort: ANY error is silently swallowed so the
    refresh pipeline never blocks on GC.
    """
    if not path.exists() or retention_days <= 0:
        return
    try:
        rows: List[Dict[str, Any]] = []
        n_input_lines = 0
        n_skipped_corrupt = 0
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                n_input_lines += 1
                try:
                    rows.append(json.loads(raw))
                except json.JSONDecodeError:
                    # Skip corrupted lines but DO rewrite (their
                    # presence is itself a reason to clean up).
                    n_skipped_corrupt += 1
                    continue
        if not rows:
            return
        # Cutoff anchored on the LATEST active_date in the file, not
        # today's date -- ensures the trim is stable even if the
        # operator runs the refresh on a stale corpus.
        from datetime import datetime as _dt, timedelta as _td
        latest_date: Optional[_dt] = None
        for r in rows:
            d_str = str(r.get("active_date") or "")
            try:
                d = _dt.strptime(d_str, "%Y-%m-%d")
            except ValueError:
                continue
            if latest_date is None or d > latest_date:
                latest_date = d
        if latest_date is None:
            return
        cutoff = latest_date - _td(days=retention_days - 1)

        def _within(row: Dict[str, Any]) -> bool:
            d_str = str(row.get("active_date") or "")
            try:
                d = _dt.strptime(d_str, "%Y-%m-%d")
            except ValueError:
                # Rows without a parseable active_date are kept --
                # we don't drop data on parse errors.
                return True
            return d >= cutoff

        kept = [r for r in rows if _within(r)]
        # Rewrite when EITHER (a) we dropped retention rows, OR (b)
        # we skipped corrupted lines (which would otherwise stay on
        # disk forever).
        if len(kept) == len(rows) and n_skipped_corrupt == 0:
            return  # nothing to trim, file is clean
        # Atomic rewrite via tmp file so a crash mid-trim doesn't
        # corrupt the history.
        tmp = path.with_suffix(path.suffix + ".trim_tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for r in kept:
                f.write(json.dumps(r) + "\n")
        import os as _os
        _os.replace(tmp, path)
    except OSError:
        return


def _render_markdown(payload: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"# Concept drift report -- {payload['active_date']}")
    lines.append("")
    cw = payload["current_window"]
    bw = payload["baseline_window"]
    lines.append(
        f"- **Current window**: {cw['start']} -> {cw['end']} ({cw['n_rows']} rows)"
    )
    lines.append(
        f"- **Baseline window**: {bw['start']} -> {bw['end']} ({bw['n_rows']} rows)"
    )
    lines.append("")
    alerts = payload.get("alerts") or []
    lines.append(f"## Alerts ({len(alerts)})")
    if not alerts:
        lines.append("- No major drift detected.")
    else:
        for a in alerts:
            lines.append(f"- {a}")
    lines.append("")
    lines.append("## Per-feature scores")
    lines.append("")
    lines.append("| Feature | Kind | Metric | Value | Verdict | Current N | Baseline N |")
    lines.append("|---|---|---|---|---|---|---|")
    for fname, info in (payload.get("features") or {}).items():
        val = info.get("value")
        val_str = f"{val:.3f}" if isinstance(val, (int, float)) else "-"
        lines.append(
            f"| {fname} | {info.get('kind')} | {info.get('metric')} | {val_str} | "
            f"{info.get('verdict')} | {info.get('current_n')} | {info.get('baseline_n')} |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build concept-drift report on model input features.",
    )
    p.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--history-path", type=Path, default=None,
                   help="Override history JSONL path (default: <output-root>/psi_history.jsonl)")
    p.add_argument("--active-date", type=str, default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--current-window-days", type=int, default=DEFAULT_CURRENT_WINDOW_DAYS)
    p.add_argument("--baseline-window-days", type=int, default=DEFAULT_BASELINE_WINDOW_DAYS)
    p.add_argument("--min-rows-per-feature", type=int, default=DEFAULT_MIN_ROWS_PER_FEATURE)
    p.add_argument("--psi-minor", type=float, default=DEFAULT_PSI_MINOR)
    p.add_argument("--psi-major", type=float, default=DEFAULT_PSI_MAJOR)
    p.add_argument("--tvd-major", type=float, default=DEFAULT_TVD_MAJOR)
    p.add_argument("--n-bins", type=int, default=DEFAULT_PSI_BINS)
    p.add_argument(
        "--no-history",
        action="store_true",
        help="Skip appending to the psi_history.jsonl file (used by tests)",
    )
    p.add_argument("--strict", action="store_true",
                   help="Exit non-zero if the report has any 'major' alerts")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    payload = build_report(
        input_path=args.input_path,
        active_date=args.active_date,
        current_window_days=args.current_window_days,
        baseline_window_days=args.baseline_window_days,
        min_rows_per_feature=args.min_rows_per_feature,
        psi_minor=args.psi_minor,
        psi_major=args.psi_major,
        tvd_major=args.tvd_major,
        n_bins=args.n_bins,
    )
    json_path = args.output_root / "concept_drift_report.json"
    md_path = args.output_root / "concept_drift_report.md"
    _write_json(json_path, payload)
    md_path.write_text(_render_markdown(payload), encoding="utf-8")
    if not args.no_history:
        history_path = args.history_path or (args.output_root / "psi_history.jsonl")
        _write_history_rows(history_path, payload)
    print(
        f"Wrote {json_path}\n"
        f"Wrote {md_path}\n"
        f"Alerts: {len(payload.get('alerts') or [])}"
    )
    if args.strict and payload.get("alerts"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
