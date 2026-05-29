#!/usr/bin/env python3
"""Compare Edge Atlas bias across historical windows (RF1.a, 2026-05-27).

The 2026-05-27 Edge Atlas finding (RF1 in ROADMAP) says the
Polymarket market structurally OVERPRICES the Over by +2-5pp across
every cohort (inning band, line, score diff) when measured against
the 10y MLB Stage-1 cache (2016-2025). That finding is the strongest
pre-pivot evidence we have for bidirectional / market-maker work,
but the 10y baseline could be stale -- juiced-ball era, fence moves,
humidor changes, recent scoring environment shifts -- so the bias
might be a regime artifact rather than a real exploitable
inefficiency.

This script re-runs `build_atlas_payload` against multiple
historical windows and reports whether the bias pattern survives:

  - If recent-N windows show similar bias to 10y across every
    cohort: finding is robust, high confidence input for post-B4
    decisions.
  - If recent-N collapses (sign flip or > 3pp deviation):
    10y baseline is misleading; RF1 needs a caveat in the
    bidirectional pivot evidence section.

Outputs:
  data/analysis_output/edge_atlas/recent_n_comparison.json
  data/analysis_output/edge_atlas/recent_n_comparison.md

Reads from existing per-window Stage-1 cache files. Skips windows
whose cache file does not exist (printed in headline so operator
can build missing windows separately via build_mlb_ou_cache.py).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR / "scripts" / "analysis") not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR / "scripts" / "analysis"))

from build_edge_atlas import (  # noqa: E402
    DEFAULT_ROOTS,
    MIN_MLB_GAMES_FOR_CELL,
    MIN_MARKET_OBSERVATIONS,
    build_atlas_payload,
)


DEFAULT_OUTPUT_DIR = PROJECT_DIR / "data" / "analysis_output" / "edge_atlas"

# Canonical comparison ladder. Each entry maps a short window label
# to a Stage-1 cache file built by cache/build_mlb_ou_cache.py for
# a specific (--min-season .. --max-season) range. Missing caches
# are skipped (with a note) so the operator can run the comparison
# even with a subset built. The 10y entry is the BASELINE all
# other windows are compared against.
DEFAULT_WINDOW_LADDER: List[Tuple[str, Path]] = [
    ("3y_2023_2025",
     PROJECT_DIR / "cache" / "mlb_ou_cache_3y_2023_2025_candidate.json"),
    ("4y_2022_2025",
     PROJECT_DIR / "cache" / "mlb_ou_cache_4y_2022_2025_candidate.json"),
    ("5y_2021_2025",
     PROJECT_DIR / "cache" / "mlb_ou_cache_5y_baseline_2021_2025.json"),
    ("6y_2020_2025",
     PROJECT_DIR / "cache" / "mlb_ou_cache_6y_2020_2025_candidate.json"),
    ("10y_2016_2025",
     PROJECT_DIR / "cache" / "mlb_ou_cache_10y_candidate.json"),
]
BASELINE_LABEL = "10y_2016_2025"

# Verdict thresholds. Tuned conservatively so a noisy single-cohort
# fluctuation can't flip the headline; the finding must really
# break to register as STALE.
BIAS_SURVIVES_MAX_DELTA_PP = 1.5  # |bias_recent - bias_10y| within this -> survives
BIAS_STALE_MAX_DELTA_PP = 3.0     # any |delta| above this -> stale
BIAS_SIGN_FLIP_TOLERANCE_PP = 0.5  # tiny biases near zero don't count as sign flips


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class WindowResult:
    label: str
    cache_path: Path
    cache_exists: bool
    headline: Dict[str, Any]
    cache_meta: Dict[str, Any]
    aggregate_bias: Optional[float]  # overall stake-weighted bias
    by_inning_band: List[Dict[str, Any]]
    by_line: List[Dict[str, Any]]
    by_score_diff_band: List[Dict[str, Any]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z",
    )


def _aggregate_bias_from_payload(payload: Dict[str, Any]) -> Optional[float]:
    """Compute the overall stake-weighted bias from the atlas
    payload's rows. Mirrors `summarize_by` weighting -- weighted by
    market_n_ticks -- but across all qualifying rows so we get one
    headline bias per window."""
    rows = payload.get("rows") or []
    num = 0.0
    den = 0
    for r in rows:
        n_games = int(r.get("mlb_n_games") or 0)
        n_ticks = int(r.get("market_n_ticks") or 0)
        bias = r.get("bias_market_minus_empirical")
        if (
            n_games < MIN_MLB_GAMES_FOR_CELL
            or n_ticks < MIN_MARKET_OBSERVATIONS
            or bias is None
        ):
            continue
        num += float(bias) * n_ticks
        den += n_ticks
    if den == 0:
        return None
    return round(num / den, 4)


def _run_one_window(
    label: str,
    cache_path: Path,
    roots: Sequence[Path],
    max_files: Optional[int],
) -> WindowResult:
    if not cache_path.exists():
        return WindowResult(
            label=label, cache_path=cache_path, cache_exists=False,
            headline={}, cache_meta={}, aggregate_bias=None,
            by_inning_band=[], by_line=[], by_score_diff_band=[],
        )
    payload = build_atlas_payload(
        cache_path, roots, max_files=max_files,
    )
    return WindowResult(
        label=label,
        cache_path=cache_path,
        cache_exists=True,
        headline=payload.get("headline") or {},
        cache_meta=payload.get("cache_meta") or {},
        aggregate_bias=_aggregate_bias_from_payload(payload),
        by_inning_band=payload.get("by_inning_band") or [],
        by_line=payload.get("by_line") or [],
        by_score_diff_band=payload.get("by_score_diff_band") or [],
    )


def _cohort_matrix(
    cohort_name: str,
    windows: Sequence[WindowResult],
    rows_key: str,
    baseline_label: str,
) -> Dict[str, Any]:
    """Build a comparison matrix for one cohort dimension.

    Each row in the matrix is a cohort bucket (e.g., "inn_1-3" or
    line "8.5"); each column is a window. Plus delta-from-baseline
    columns for non-baseline windows.

    Returns:
      {
        "cohort_dimension": str,
        "windows": [labels in order],
        "buckets": [
            {
                "bucket": str,
                "n_cells": {window_label: int},
                "stake_weighted_bias": {window_label: float | None},
                "delta_vs_baseline_pp": {
                    non_baseline_window: float | None,
                },
                "max_abs_delta_pp": float | None,
                "sign_flip": bool,
            },
            ...
        ],
        "summary": {
            "n_buckets": int,
            "n_buckets_with_sign_flip": int,
            "max_abs_delta_pp_across_buckets": float,
            "median_abs_delta_pp": float,
        }
      }
    """
    # Find all buckets present in any window.
    all_buckets: List[str] = []
    seen = set()
    by_window: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for w in windows:
        rows = getattr(w, rows_key)
        wmap: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            bucket = str(row.get("bucket"))
            wmap[bucket] = row
            if bucket not in seen:
                seen.add(bucket)
                all_buckets.append(bucket)
        by_window[w.label] = wmap
    all_buckets.sort()

    baseline_map = by_window.get(baseline_label, {})

    matrix_rows: List[Dict[str, Any]] = []
    deltas_for_summary: List[float] = []
    sign_flips = 0
    for bucket in all_buckets:
        n_cells: Dict[str, int] = {}
        biases: Dict[str, Optional[float]] = {}
        for w in windows:
            row = by_window[w.label].get(bucket)
            if row is None:
                n_cells[w.label] = 0
                biases[w.label] = None
            else:
                n_cells[w.label] = int(row.get("n_cells") or 0)
                bias = row.get("stake_weighted_bias")
                biases[w.label] = (
                    float(bias) if bias is not None else None
                )
        baseline_bias = biases.get(baseline_label)
        deltas: Dict[str, Optional[float]] = {}
        max_abs_delta_pp: Optional[float] = None
        sign_flip = False
        for w in windows:
            if w.label == baseline_label:
                continue
            recent_bias = biases.get(w.label)
            if recent_bias is None or baseline_bias is None:
                deltas[w.label] = None
                continue
            d_pp = round(
                (recent_bias - baseline_bias) * 100.0, 2,
            )
            deltas[w.label] = d_pp
            abs_d = abs(d_pp)
            if max_abs_delta_pp is None or abs_d > max_abs_delta_pp:
                max_abs_delta_pp = abs_d
            # Sign-flip: baseline says one direction (magnitude
            # above tolerance), recent says the opposite (magnitude
            # above tolerance). Tiny noise near zero doesn't flip.
            baseline_pp = baseline_bias * 100.0
            recent_pp = recent_bias * 100.0
            if (
                abs(baseline_pp) > BIAS_SIGN_FLIP_TOLERANCE_PP
                and abs(recent_pp) > BIAS_SIGN_FLIP_TOLERANCE_PP
                and (baseline_pp * recent_pp) < 0
            ):
                sign_flip = True
        if sign_flip:
            sign_flips += 1
        if max_abs_delta_pp is not None:
            deltas_for_summary.append(max_abs_delta_pp)
        matrix_rows.append({
            "bucket": bucket,
            "n_cells": n_cells,
            "stake_weighted_bias_pp": {
                k: (round(v * 100.0, 2) if v is not None else None)
                for k, v in biases.items()
            },
            "delta_vs_baseline_pp": deltas,
            "max_abs_delta_pp": max_abs_delta_pp,
            "sign_flip": sign_flip,
        })

    summary = {
        "n_buckets": len(matrix_rows),
        "n_buckets_with_sign_flip": sign_flips,
        "max_abs_delta_pp_across_buckets": (
            max(deltas_for_summary) if deltas_for_summary else None
        ),
        "median_abs_delta_pp": (
            sorted(deltas_for_summary)[len(deltas_for_summary) // 2]
            if deltas_for_summary else None
        ),
    }
    return {
        "cohort_dimension": cohort_name,
        "windows": [w.label for w in windows],
        "baseline": baseline_label,
        "buckets": matrix_rows,
        "summary": summary,
    }


def _classify_verdict(
    cohort_matrices: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Roll cohort matrices into one verdict.

    Verdict ladder:
      BIAS_SURVIVES_RECENT: every cohort bucket has
        |max delta| < BIAS_SURVIVES_MAX_DELTA_PP AND no sign flips.
        Finding is robust across windows -> use RF1 as strong input.
      BIAS_PARTIALLY_SURVIVES: signs consistent (no flips) but some
        |delta| in [BIAS_SURVIVES_MAX_DELTA_PP, BIAS_STALE_MAX_DELTA_PP].
        Mostly holds; surface specific cohorts that drift.
      BIAS_STALE_REGIME_DRIFT: any sign flip OR any
        |delta| >= BIAS_STALE_MAX_DELTA_PP. 10y baseline misleading
        for at least some cohorts; add caveat to bidirectional pivot
        evidence section.
      INSUFFICIENT_DATA: not enough qualifying buckets to decide.
    """
    total_sign_flips = sum(
        m["summary"]["n_buckets_with_sign_flip"]
        for m in cohort_matrices
    )
    max_deltas = [
        m["summary"]["max_abs_delta_pp_across_buckets"]
        for m in cohort_matrices
        if m["summary"]["max_abs_delta_pp_across_buckets"] is not None
    ]
    total_buckets = sum(m["summary"]["n_buckets"] for m in cohort_matrices)

    if not max_deltas or total_buckets == 0:
        return {
            "status": "INSUFFICIENT_DATA",
            "summary": (
                "Not enough overlapping cohort buckets across windows "
                "to decide whether the bias pattern survives. Build "
                "more windowed caches or expand the candidate roots "
                "and re-run."
            ),
            "total_sign_flips": total_sign_flips,
            "max_abs_delta_pp_overall": None,
            "thresholds": {
                "survives_max_delta_pp": BIAS_SURVIVES_MAX_DELTA_PP,
                "stale_max_delta_pp": BIAS_STALE_MAX_DELTA_PP,
                "sign_flip_tolerance_pp": BIAS_SIGN_FLIP_TOLERANCE_PP,
            },
        }

    max_delta_overall = max(max_deltas)
    if (
        total_sign_flips > 0
        or max_delta_overall >= BIAS_STALE_MAX_DELTA_PP
    ):
        status = "BIAS_STALE_REGIME_DRIFT"
        summary_text = (
            f"10y baseline shows regime drift: "
            f"{total_sign_flips} cohort buckets flip sign in a "
            f"recent window, max |delta| = {max_delta_overall:.2f}pp "
            f">= {BIAS_STALE_MAX_DELTA_PP:.1f}pp threshold. The "
            "RF1 finding does NOT survive cleanly to recent regimes; "
            "add caveat to bidirectional pivot evidence section and "
            "treat RF1 as descriptive of lifetime baseline only."
        )
    elif max_delta_overall < BIAS_SURVIVES_MAX_DELTA_PP:
        status = "BIAS_SURVIVES_RECENT"
        summary_text = (
            f"RF1 bias survives across all {total_buckets} cohort "
            f"buckets and {len(max_deltas)} measured windows. Max "
            f"|delta| from 10y baseline = {max_delta_overall:.2f}pp "
            f"< {BIAS_SURVIVES_MAX_DELTA_PP:.1f}pp survives threshold "
            "(no sign flips). High-confidence input for post-B4 "
            "decisions on bidirectional pivot."
        )
    else:
        status = "BIAS_PARTIALLY_SURVIVES"
        summary_text = (
            f"RF1 bias survives in sign across all {total_buckets} "
            f"cohort buckets (no flips) but max |delta| = "
            f"{max_delta_overall:.2f}pp exceeds the "
            f"{BIAS_SURVIVES_MAX_DELTA_PP:.1f}pp tight tolerance "
            f"(stays under {BIAS_STALE_MAX_DELTA_PP:.1f}pp stale "
            "threshold). The directional finding is robust; the "
            "magnitude varies across regimes -- treat RF1 as "
            "directionally reliable but expect cohort-level magnitude "
            "drift."
        )

    return {
        "status": status,
        "summary": summary_text,
        "total_sign_flips": total_sign_flips,
        "total_buckets_evaluated": total_buckets,
        "max_abs_delta_pp_overall": max_delta_overall,
        "thresholds": {
            "survives_max_delta_pp": BIAS_SURVIVES_MAX_DELTA_PP,
            "stale_max_delta_pp": BIAS_STALE_MAX_DELTA_PP,
            "sign_flip_tolerance_pp": BIAS_SIGN_FLIP_TOLERANCE_PP,
        },
    }


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------


def build_comparison_payload(
    *,
    windows_ladder: Sequence[Tuple[str, Path]] = tuple(DEFAULT_WINDOW_LADDER),
    roots: Sequence[Path] = tuple(DEFAULT_ROOTS),
    baseline_label: str = BASELINE_LABEL,
    max_files: Optional[int] = None,
) -> Dict[str, Any]:
    """Run the atlas once per window and roll into a comparison
    payload. Missing caches are reported in headline.skipped_windows;
    the comparison runs over whatever windows are present.
    """
    windows: List[WindowResult] = []
    skipped: List[Dict[str, Any]] = []
    for label, path in windows_ladder:
        result = _run_one_window(label, path, roots, max_files)
        windows.append(result)
        if not result.cache_exists:
            skipped.append({
                "label": label,
                "expected_cache_path": str(path),
                "reason": "cache file does not exist",
            })

    present_windows = [w for w in windows if w.cache_exists]
    has_baseline = any(
        w.label == baseline_label for w in present_windows
    )

    # If the baseline is missing, pick the longest present window
    # as a fallback baseline so the comparison still runs.
    effective_baseline = baseline_label
    if not has_baseline and present_windows:
        # Pick the lexicographically largest year-range label as a
        # rough "longest window" heuristic.
        effective_baseline = sorted(
            present_windows, key=lambda w: w.label, reverse=True,
        )[0].label

    cohort_matrices = [
        _cohort_matrix(
            "inning_band", present_windows,
            rows_key="by_inning_band",
            baseline_label=effective_baseline,
        ),
        _cohort_matrix(
            "line", present_windows,
            rows_key="by_line",
            baseline_label=effective_baseline,
        ),
        _cohort_matrix(
            "score_diff_band", present_windows,
            rows_key="by_score_diff_band",
            baseline_label=effective_baseline,
        ),
    ]
    verdict = _classify_verdict(cohort_matrices)
    # Surface which baseline actually got used (visibility for the
    # operator when the canonical 10y baseline is missing).
    verdict["baseline_used"] = effective_baseline
    verdict["baseline_canonical"] = baseline_label
    verdict["baseline_fallback_used"] = effective_baseline != baseline_label

    per_window_summary = [
        {
            "label": w.label,
            "cache_path": str(w.cache_path),
            "cache_exists": w.cache_exists,
            "cache_meta": w.cache_meta,
            "headline": w.headline,
            "aggregate_stake_weighted_bias": w.aggregate_bias,
            "aggregate_stake_weighted_bias_pp": (
                round(w.aggregate_bias * 100.0, 2)
                if w.aggregate_bias is not None else None
            ),
        }
        for w in windows
    ]

    return {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "research_id": "RF1.a",
        "research_title": (
            "Recent-N Edge Atlas comparison -- does the +2-5pp Over "
            "premium finding survive across historical windows?"
        ),
        "windows_attempted": [label for label, _ in windows_ladder],
        "windows_skipped": skipped,
        "per_window_summary": per_window_summary,
        "cohort_matrices": cohort_matrices,
        "verdict": verdict,
        "data_roots": [str(r) for r in roots],
    }


# ---------------------------------------------------------------------------
# Markdown render
# ---------------------------------------------------------------------------


def _fmt_signed_pp(v: Optional[float], digits: int = 2) -> str:
    return "—" if v is None else f"{v:+.{digits}f}pp"


def render_markdown(payload: Dict[str, Any]) -> str:
    out: List[str] = []
    out.append("# Edge Atlas — Recent-N comparison (RF1.a)\n")
    out.append(f"_Generated {payload['generated_at_utc']}_\n")
    out.append("")
    out.append(payload["research_title"] + "\n")
    out.append("")

    verdict = payload["verdict"]
    out.append("## Verdict\n")
    out.append(f"**{verdict['status']}**\n")
    out.append("")
    out.append(verdict["summary"] + "\n")
    out.append("")
    out.append(
        f"Baseline: `{verdict['baseline_used']}`"
        + (
            f" _(fallback — canonical `{verdict['baseline_canonical']}` "
            "cache missing)_"
            if verdict.get("baseline_fallback_used") else ""
        )
        + "\n"
    )
    out.append("")

    # Per-window summary table.
    out.append("## Per-window summary\n")
    out.append(
        "| Window | Cache present | Games | Qualifying rows | "
        "Total ticks | Stake-weighted bias |"
    )
    out.append(
        "|---|---|---|---|---|---|"
    )
    for w in payload["per_window_summary"]:
        cache_present = "✓" if w["cache_exists"] else "_(missing)_"
        h = w.get("headline") or {}
        bias_pp = w.get("aggregate_stake_weighted_bias_pp")
        out.append(
            f"| `{w['label']}` "
            f"| {cache_present} "
            f"| {h.get('total_unique_game_pks', '—')} "
            f"| {h.get('n_qualifying_rows', '—')} "
            f"| {h.get('total_observations', '—')} "
            f"| {_fmt_signed_pp(bias_pp)} |"
        )
    out.append("")

    if payload.get("windows_skipped"):
        out.append("### Skipped windows\n")
        out.append(
            "These cache files do not yet exist. To include them, "
            "build via `python cache/build_mlb_ou_cache.py "
            "--season-type regular --min-season YYYY --max-season YYYY "
            "--out <expected path>`:"
        )
        out.append("")
        for s in payload["windows_skipped"]:
            out.append(f"- `{s['label']}` → `{s['expected_cache_path']}`")
        out.append("")

    # Cohort matrices.
    for matrix in payload["cohort_matrices"]:
        dim = matrix["cohort_dimension"]
        windows_labels = matrix["windows"]
        baseline = matrix["baseline"]
        out.append(f"## Bias by {dim}\n")
        out.append(
            f"_Stake-weighted bias per window (pp). Baseline: `{baseline}`._\n"
        )
        out.append("")
        header = "| Bucket |"
        sep = "|---|"
        for label in windows_labels:
            header += f" {label} |"
            sep += "---|"
        for label in windows_labels:
            if label == baseline:
                continue
            header += f" Δ vs `{baseline}` ({label}) |"
            sep += "---|"
        header += " max\\|Δ\\|pp | sign flip? |"
        sep += "---|---|"
        out.append(header)
        out.append(sep)
        for row in matrix["buckets"]:
            line = f"| `{row['bucket']}` |"
            for label in windows_labels:
                bias = row["stake_weighted_bias_pp"].get(label)
                line += f" {_fmt_signed_pp(bias)} |"
            for label in windows_labels:
                if label == baseline:
                    continue
                d = row["delta_vs_baseline_pp"].get(label)
                line += f" {_fmt_signed_pp(d)} |"
            max_d = row.get("max_abs_delta_pp")
            max_d_str = (
                "—" if max_d is None else f"{max_d:.2f}pp"
            )
            flip_str = "⚠️ YES" if row.get("sign_flip") else "no"
            line += f" {max_d_str} | {flip_str} |"
            out.append(line)
        s = matrix["summary"]
        max_d_across = s["max_abs_delta_pp_across_buckets"]
        max_d_str = (
            "—" if max_d_across is None else f"{max_d_across:.2f}pp"
        )
        out.append("")
        out.append(
            f"_{dim} summary: {s['n_buckets']} buckets, "
            f"{s['n_buckets_with_sign_flip']} sign flips, max |Δ| "
            f"across buckets = {max_d_str}._"
        )
        out.append("")

    # Method note.
    out.append("## Method\n")
    out.append(
        "1. For each window in the ladder, runs the same "
        "`build_atlas_payload` from `build_edge_atlas.py` against "
        "the corresponding cache file."
    )
    out.append(
        "2. Aggregates each window's `by_inning_band`, `by_line`, "
        "and `by_score_diff_band` into a comparison matrix vs the "
        "`10y_2016_2025` baseline."
    )
    out.append(
        "3. For each cohort bucket × non-baseline window, computes "
        "`Δ = stake_weighted_bias_recent - stake_weighted_bias_10y` "
        "in pp."
    )
    out.append(
        "4. Verdict ladder:"
    )
    out.append(
        f"   - **BIAS_SURVIVES_RECENT**: every cohort within "
        f"±{BIAS_SURVIVES_MAX_DELTA_PP}pp of baseline AND no sign flips."
    )
    out.append(
        f"   - **BIAS_PARTIALLY_SURVIVES**: signs consistent, some "
        f"cohorts in [{BIAS_SURVIVES_MAX_DELTA_PP}pp, "
        f"{BIAS_STALE_MAX_DELTA_PP}pp]."
    )
    out.append(
        f"   - **BIAS_STALE_REGIME_DRIFT**: any sign flip OR any "
        f"|Δ| ≥ {BIAS_STALE_MAX_DELTA_PP}pp."
    )
    out.append(
        f"5. Sign flips use a "
        f"±{BIAS_SIGN_FLIP_TOLERANCE_PP}pp tolerance around zero so "
        "tiny near-zero biases don't artificially count as flips."
    )
    out.append("")

    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--data-root", action="append", type=Path, default=None,
        help=(
            "Repeatable. Root directory under which candidate_universe/ "
            f"lives. Default: {[str(r) for r in DEFAULT_ROOTS]}."
        ),
    )
    p.add_argument(
        "--out-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR}).",
    )
    p.add_argument(
        "--max-files", type=int, default=0,
        help="Cap files walked per root for smoke tests. 0 = no cap.",
    )
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    roots = args.data_root or list(DEFAULT_ROOTS)
    payload = build_comparison_payload(
        roots=roots, max_files=(args.max_files or None),
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "recent_n_comparison.json"
    md_path = args.out_dir / "recent_n_comparison.md"
    json_path.write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8",
    )
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    v = payload["verdict"]
    print(
        f"RF1.a comparison: verdict={v['status']}, "
        f"max |delta|={v.get('max_abs_delta_pp_overall')}, "
        f"sign_flips={v.get('total_sign_flips', 0)}. "
        f"Wrote {md_path}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
