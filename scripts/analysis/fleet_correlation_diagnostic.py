#!/usr/bin/env python3
"""fleet_correlation_diagnostic.py -- collapse multi-engine A/B signal density.

Added 2026-06-01. The 7-day review at that date surfaced two stats hidden
inside the parallel-comparison report:

  1. Most days, most paper presets bet the same game-lines. The headline
     fleet ROI is therefore dominated by slate variance, not config
     discrimination. We want one number per day that says "how often did
     configs disagree on what to bet?" -- the actual signal density of
     the multi-engine A/B.

  2. When a consensus bet loses, the WHOLE fleet takes the hit at once.
     The per-model correlated-line cap (2 over-bets per game) is per-MODEL,
     not per-FLEET. Days where 12/13 models lose on one game are a real
     concentration-risk we should be able to measure.

For each date in the lookback window this script computes:

  split_density      = split / (split + unanimous_trade + unanimous_skip)
                       reads from the parallel_engine_comparison JSON

  max_correlated_loss_share = max over loser game-lines of
                              (n_models_lost_same_key / n_models_traded_today)

  fleet_correlated_loss_density = sum_over_loser_keys(n_losers) /
                                  sum_over_all_loser_bets(1)

Both `max_correlated_loss_share` and `fleet_correlated_loss_density` are
read from session.json files (the canonical source of bet outcomes).

Output:
  data/analysis_output/fleet_correlation/fleet_correlation_diagnostic.{json,md}

Exit code is always 0 (fail-open). When `max_correlated_loss_share` exceeds
`--warn-threshold` for any day in the window, a stderr WARN is printed.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_PAPER_PREFIX = PROJECT_DIR / "data"
DEFAULT_AGGREGATOR_DIR = PROJECT_DIR / "data" / "analysis_output" / "parallel_engine_comparison"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "data" / "analysis_output" / "fleet_correlation"
DEFAULT_OUTPUT_STEM = "fleet_correlation_diagnostic"

DEFAULT_RECENT_DAYS = 7
DEFAULT_WARN_THRESHOLD = 0.5  # max_correlated_loss_share >= this = warn


@dataclass
class DayDiagnostic:
    date: str
    n_presets_active: int
    n_paper_bets_fleet: int
    n_paper_losses_fleet: int
    # parallel-comparison disagreement
    keys_compared: int
    n_split: int
    n_unanimous_trade: int
    n_unanimous_skip: int
    split_density: float
    # correlated-loss exposure
    n_unique_loser_keys: int  # distinct (game_pk, line, side) loser-bets across the fleet
    max_correlated_loss_share: float
    max_correlated_loss_n: int
    max_correlated_loss_key: Optional[str]
    fleet_correlated_loss_density: float
    warn: bool


def _iter_dates(today: _dt.date, window: int) -> List[str]:
    out: List[str] = []
    for d in range(window):
        out.append((today - _dt.timedelta(days=d)).isoformat())
    return sorted(out)


def _read_aggregator(date: str, aggregator_dir: Path) -> Optional[Dict[str, Any]]:
    path = aggregator_dir / f"parallel_engine_comparison_{date}_{date}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_paper_sessions(date: str, paper_prefix: Path) -> Dict[str, Dict[str, Any]]:
    """Return {preset_label: session_dict} for every paper_<label>/sessions/<date>_session.json."""
    out: Dict[str, Dict[str, Any]] = {}
    for d in sorted(paper_prefix.glob("paper_*")):
        if not d.is_dir():
            continue
        label = d.name.replace("paper_", "")
        sp = d / "sessions" / f"{date}_session.json"
        if not sp.exists():
            continue
        try:
            out[label] = json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return out


def _compute_correlated_loss(
    sessions_by_label: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """For the day's session set, group losing bets by (game_pk, line, side)
    and find the cluster that hit the most models at once."""
    # cluster losers by (game_pk, line, side)
    clusters: Dict[tuple, List[str]] = {}
    total_loser_rows = 0
    n_models_with_bets = 0
    for label, sess in sessions_by_label.items():
        bets = sess.get("bets") or []
        had_bet = False
        for b in bets:
            if b.get("won") is False:
                key = (b.get("game_pk"), str(b.get("line")), str(b.get("side") or ""))
                clusters.setdefault(key, []).append(label)
                total_loser_rows += 1
            if b.get("total_bets", 0) >= 0:  # crude "this model is active"
                had_bet = True
        if had_bet:
            n_models_with_bets += 1
    # n_presets_active = total presets that placed >=1 settled bet today
    n_active = sum(
        1 for s in sessions_by_label.values()
        if (s.get("summary") or {}).get("total_bets", 0) > 0
    )
    if not clusters:
        return {
            "n_unique_loser_keys": 0,
            "max_correlated_loss_share": 0.0,
            "max_correlated_loss_n": 0,
            "max_correlated_loss_key": None,
            "fleet_correlated_loss_density": 0.0,
            "n_active_presets": n_active,
            "total_loser_rows": 0,
        }
    # Largest cluster: how many models took the same loser?
    max_cluster_n = 0
    max_cluster_key = None
    sum_cluster_size = 0
    for key, models in clusters.items():
        size = len(models)
        sum_cluster_size += size
        if size > max_cluster_n:
            max_cluster_n = size
            max_cluster_key = key
    max_share = max_cluster_n / max(n_active, 1)
    # "fleet correlated loss density" = average cluster size weighted by membership;
    # equivalently, the average number of fellow-loser models a losing bet had.
    # When clusters are all size 1 (no fleet-correlation), this is 1.0.
    # When all losses are one big cluster, this is the cluster size.
    fleet_density = sum_cluster_size / max(len(clusters), 1)
    key_str = (
        f"{max_cluster_key[0]}_{max_cluster_key[1]}_{max_cluster_key[2]}"
        if max_cluster_key else None
    )
    return {
        "n_unique_loser_keys": len(clusters),
        "max_correlated_loss_share": round(max_share, 4),
        "max_correlated_loss_n": max_cluster_n,
        "max_correlated_loss_key": key_str,
        "fleet_correlated_loss_density": round(fleet_density, 3),
        "n_active_presets": n_active,
        "total_loser_rows": total_loser_rows,
    }


def _analyze_day(
    date: str,
    aggregator_dir: Path,
    paper_prefix: Path,
    warn_threshold: float,
) -> Optional[DayDiagnostic]:
    sessions = _read_paper_sessions(date, paper_prefix)
    if not sessions:
        return None
    n_paper_bets = 0
    n_paper_losses = 0
    for s in sessions.values():
        sm = s.get("summary") or {}
        n_paper_bets += sm.get("total_bets", 0) or 0
        n_paper_losses += sm.get("losses", 0) or 0
    corr = _compute_correlated_loss(sessions)
    agg = _read_aggregator(date, aggregator_dir)
    if agg:
        gl = (agg.get("shared_candidate_disagreement") or {}).get("game_line") or {}
        counts = gl.get("counts") or {}
        keys_compared = counts.get("keys_compared", 0) or 0
        n_split = counts.get("split", 0) or 0
        n_unanimous_trade = counts.get("unanimous_trade", 0) or 0
        n_unanimous_skip = counts.get("unanimous_skip", 0) or 0
        split_density = (
            n_split / keys_compared if keys_compared > 0 else 0.0
        )
    else:
        keys_compared = 0
        n_split = 0
        n_unanimous_trade = 0
        n_unanimous_skip = 0
        split_density = 0.0
    warn = corr["max_correlated_loss_share"] >= warn_threshold
    return DayDiagnostic(
        date=date,
        n_presets_active=corr["n_active_presets"],
        n_paper_bets_fleet=n_paper_bets,
        n_paper_losses_fleet=n_paper_losses,
        keys_compared=keys_compared,
        n_split=n_split,
        n_unanimous_trade=n_unanimous_trade,
        n_unanimous_skip=n_unanimous_skip,
        split_density=round(split_density, 4),
        n_unique_loser_keys=corr["n_unique_loser_keys"],
        max_correlated_loss_share=corr["max_correlated_loss_share"],
        max_correlated_loss_n=corr["max_correlated_loss_n"],
        max_correlated_loss_key=corr["max_correlated_loss_key"],
        fleet_correlated_loss_density=corr["fleet_correlated_loss_density"],
        warn=warn,
    )


def _render_markdown(
    findings: List[DayDiagnostic],
    args: argparse.Namespace,
) -> str:
    lines = [
        "# Fleet Correlation Diagnostic",
        "",
        f"Generated: {_dt.datetime.now(tz=_dt.timezone.utc).isoformat()}",
        f"Window: last {args.recent_days} days",
        f"Warn threshold: max_correlated_loss_share >= {args.warn_threshold:.0%}",
        "",
        ("**split_density** = fraction of compared (game,line) keys where "
         "configs disagreed (signal density of the A/B). Higher = more "
         "config-discriminating evidence per day."),
        "",
        ("**max_correlated_loss_share** = of the day's loser bets, what "
         "fraction of active presets fell into the single worst cluster. "
         "1.0 = every model took the same loser; 1/N = losers spread perfectly."),
        "",
        ("**fleet_correlated_loss_density** = average cluster size of losses. "
         "1.0 = independent (no fleet correlation). Higher = same game took "
         "out more models at once."),
        "",
        "| date | presets | bets | losses | unique loser keys | split_density | max_share | max_n | max_key | corr_density |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|---:|",
    ]
    for f in findings:
        flag = " :warning:" if f.warn else ""
        lines.append(
            f"| {f.date}{flag} | {f.n_presets_active} | {f.n_paper_bets_fleet} | "
            f"{f.n_paper_losses_fleet} | {f.n_unique_loser_keys} | "
            f"{f.split_density:.1%} | {f.max_correlated_loss_share:.1%} | "
            f"{f.max_correlated_loss_n} | {f.max_correlated_loss_key or '-'} | "
            f"{f.fleet_correlated_loss_density:.2f} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--paper-prefix",
        type=Path,
        default=DEFAULT_PAPER_PREFIX,
        help=f"Root containing paper_<label>/sessions/ (default: {DEFAULT_PAPER_PREFIX}).",
    )
    p.add_argument(
        "--aggregator-dir",
        type=Path,
        default=DEFAULT_AGGREGATOR_DIR,
        help=f"Directory of parallel_engine_comparison_<range>.json files (default: {DEFAULT_AGGREGATOR_DIR}).",
    )
    p.add_argument(
        "--recent-days",
        type=int,
        default=DEFAULT_RECENT_DAYS,
        help=f"How many days back to scan (default: {DEFAULT_RECENT_DAYS}).",
    )
    p.add_argument(
        "--warn-threshold",
        type=float,
        default=DEFAULT_WARN_THRESHOLD,
        help=(
            "max_correlated_loss_share triggering a stderr WARN "
            f"(default: {DEFAULT_WARN_THRESHOLD})."
        ),
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR}).",
    )
    p.add_argument(
        "--output-stem",
        type=str,
        default=DEFAULT_OUTPUT_STEM,
        help=f"Output file stem (default: {DEFAULT_OUTPUT_STEM}).",
    )
    p.add_argument(
        "--today",
        type=str,
        default="",
        help="Override 'today' date (YYYY-MM-DD). Default: real today.",
    )
    args = p.parse_args(argv)

    today = (
        _dt.date.fromisoformat(args.today)
        if args.today else _dt.date.today()
    )
    dates = _iter_dates(today, args.recent_days)

    findings: List[DayDiagnostic] = []
    for date in dates:
        d = _analyze_day(date, args.aggregator_dir, args.paper_prefix, args.warn_threshold)
        if d is not None:
            findings.append(d)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"{args.output_stem}.json"
    md_path = args.output_dir / f"{args.output_stem}.md"

    # Aggregate over the window.
    n_with_warn = sum(1 for f in findings if f.warn)
    avg_split_density = (
        sum(f.split_density for f in findings) / len(findings)
        if findings else 0.0
    )
    avg_corr_density = (
        sum(f.fleet_correlated_loss_density for f in findings) / len(findings)
        if findings else 0.0
    )

    payload: Dict[str, Any] = {
        "generated_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
        "window_days": args.recent_days,
        "warn_threshold": args.warn_threshold,
        "days_scanned": len(findings),
        "days_flagged": n_with_warn,
        "avg_split_density": round(avg_split_density, 4),
        "avg_fleet_correlated_loss_density": round(avg_corr_density, 3),
        "findings": [asdict(f) for f in findings],
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(findings, args), encoding="utf-8")

    for f in findings:
        if f.warn:
            sys.stderr.write(
                f"[fleet-correlation] WARN {f.date}: "
                f"max_correlated_loss_share={f.max_correlated_loss_share:.1%} "
                f"({f.max_correlated_loss_n}/{f.n_presets_active} models lost on "
                f"{f.max_correlated_loss_key}).\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
