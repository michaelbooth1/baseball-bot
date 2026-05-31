#!/usr/bin/env python3
"""audit_under_gate_bottleneck.py -- detect single-gate UNDER bottlenecks.

Added 2026-05-30 after the paper UNDER post-mortem: the mirrored
`gate_under_min_entry_ask` floor (0.55) blocked 877/877 emitted UNDER
candidates on 2026-05-29 -- one gate ate 100% of UNDER skip rows. This
guardrail surfaces that class of config bug in 24h instead of however
long it would otherwise take to notice "the bot isn't placing UNDER."

For each recent session JSON (paper + live), this script:
  1. Reads `summary.candidate_rollup.by_decision_reason`.
  2. Sums every key that starts with `skip:gate_under_` (UNDER-side skips).
  3. If the total is >= --min-n AND any single reason owns >= --bottleneck
     of the UNDER skips, flag the session as a single-gate bottleneck.

Output:
  data/analysis_output/under_gate_bottleneck_audit/under_gate_bottleneck_audit.{json,md}

Exit code is always 0 (fail-open). The status lands in the JSON/MD report
and is printed to stderr at WARN level when a bottleneck is detected.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_DIR = Path(__file__).resolve().parents[2]


def _discover_default_session_roots() -> List[Path]:
    """Default session roots include the legacy single-engine paths
    (data/paper_trading/, data/live_trading/) PLUS every multi-engine
    per-preset paper root (data/paper_<label>/) discovered at call
    time. The 2026-05-30 audit revealed the bottleneck guardrail was
    blind to per-preset roots, so an M_under_paper bottleneck went
    unflagged. Globbing per-call keeps new presets covered without
    a config change."""
    roots: List[Path] = [
        PROJECT_DIR / "data" / "paper_trading" / "sessions",
        PROJECT_DIR / "data" / "live_trading" / "sessions",
    ]
    data_dir = PROJECT_DIR / "data"
    if data_dir.exists():
        for sub in sorted(data_dir.glob("paper_*")):
            if not sub.is_dir():
                continue
            if sub.name == "paper_trading":
                continue  # already in the legacy list above
            sess = sub / "sessions"
            if sess.exists():
                roots.append(sess)
    return roots


DEFAULT_SESSION_ROOTS = _discover_default_session_roots()
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "data" / "analysis_output" / "under_gate_bottleneck_audit"
DEFAULT_OUTPUT_STEM = "under_gate_bottleneck_audit"

DEFAULT_RECENT_DAYS = 7
DEFAULT_BOTTLENECK_FRACTION = 0.95
DEFAULT_MIN_N_FOR_VERDICT = 100

UNDER_SKIP_PREFIX = "skip:gate_under_"


@dataclass
class SessionFinding:
    session_path: str
    date: str
    mode: str
    under_skip_total: int
    top_reason: Optional[str]
    top_reason_share: float
    top_reason_count: int
    is_bottleneck: bool
    reasons: Dict[str, int]


def _iter_session_files(roots: List[Path], since: _dt.date) -> List[Path]:
    out: List[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for p in sorted(root.glob("*_session.json")):
            stem = p.stem
            date_part = stem.split("_session")[0]
            try:
                d = _dt.date.fromisoformat(date_part)
            except ValueError:
                continue
            if d >= since:
                out.append(p)
    return out


def _analyze_session(path: Path) -> Optional[SessionFinding]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rollup = (
        (data.get("summary") or {}).get("candidate_rollup") or {}
    )
    reasons = rollup.get("by_decision_reason") or {}
    under_reasons = {
        k: int(v) for k, v in reasons.items() if k.startswith(UNDER_SKIP_PREFIX)
    }
    total = sum(under_reasons.values())
    if total == 0:
        return SessionFinding(
            session_path=str(path),
            date=str(data.get("date") or ""),
            mode=str(data.get("mode") or ""),
            under_skip_total=0,
            top_reason=None,
            top_reason_share=0.0,
            top_reason_count=0,
            is_bottleneck=False,
            reasons={},
        )
    top_reason, top_count = max(under_reasons.items(), key=lambda kv: kv[1])
    share = top_count / total
    return SessionFinding(
        session_path=str(path),
        date=str(data.get("date") or ""),
        mode=str(data.get("mode") or ""),
        under_skip_total=total,
        top_reason=top_reason,
        top_reason_share=share,
        top_reason_count=top_count,
        is_bottleneck=False,
        reasons=under_reasons,
    )


def _apply_verdict(
    finding: SessionFinding,
    min_n: int,
    bottleneck_fraction: float,
) -> SessionFinding:
    finding.is_bottleneck = (
        finding.under_skip_total >= min_n
        and finding.top_reason_share >= bottleneck_fraction
    )
    return finding


def _render_markdown(
    findings: List[SessionFinding],
    args: argparse.Namespace,
) -> str:
    lines = [
        "# UNDER Gate Bottleneck Audit",
        "",
        f"Generated: {_dt.datetime.now(tz=_dt.timezone.utc).isoformat()}",
        f"Window: last {args.recent_days} days",
        f"Verdict thresholds: top reason >= {args.bottleneck:.0%}, "
        f"n >= {args.min_n}",
        "",
    ]
    bottlenecks = [f for f in findings if f.is_bottleneck]
    if bottlenecks:
        lines.append(f"**STATUS: {len(bottlenecks)} session(s) flagged.**")
    else:
        lines.append("**STATUS: clean.**")
    lines.append("")
    lines.append("| date | mode | under_skips | top reason | share |")
    lines.append("|---|---|---|---|---|")
    for f in findings:
        tag = " :rotating_light:" if f.is_bottleneck else ""
        share = f"{f.top_reason_share:.1%}" if f.top_reason else "-"
        lines.append(
            f"| {f.date}{tag} | {f.mode} | {f.under_skip_total} | "
            f"{f.top_reason or '-'} | {share} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--session-root",
        action="append",
        type=Path,
        default=None,
        help="Session JSON root(s). Repeatable. Default: paper + live sessions/.",
    )
    p.add_argument(
        "--recent-days",
        type=int,
        default=DEFAULT_RECENT_DAYS,
        help=f"Look back this many days (default: {DEFAULT_RECENT_DAYS}).",
    )
    p.add_argument(
        "--bottleneck",
        type=float,
        default=DEFAULT_BOTTLENECK_FRACTION,
        help=(
            "Single-gate share of UNDER skips that triggers the flag "
            f"(default: {DEFAULT_BOTTLENECK_FRACTION})."
        ),
    )
    p.add_argument(
        "--min-n",
        type=int,
        default=DEFAULT_MIN_N_FOR_VERDICT,
        help=(
            "Minimum UNDER skip count required before flagging "
            f"(default: {DEFAULT_MIN_N_FOR_VERDICT})."
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
    args = p.parse_args(argv)

    roots = args.session_root or DEFAULT_SESSION_ROOTS
    since = _dt.date.today() - _dt.timedelta(days=args.recent_days)
    files = _iter_session_files([Path(r) for r in roots], since)

    findings: List[SessionFinding] = []
    for path in files:
        f = _analyze_session(path)
        if f is None:
            continue
        _apply_verdict(f, args.min_n, args.bottleneck)
        findings.append(f)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"{args.output_stem}.json"
    md_path = args.output_dir / f"{args.output_stem}.md"

    bottlenecks = [f for f in findings if f.is_bottleneck]

    payload: Dict[str, Any] = {
        "generated_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
        "window_days": args.recent_days,
        "bottleneck_fraction_threshold": args.bottleneck,
        "min_n_for_verdict": args.min_n,
        "sessions_scanned": len(findings),
        "sessions_flagged": len(bottlenecks),
        "status": "bottleneck_detected" if bottlenecks else "clean",
        "findings": [asdict(f) for f in findings],
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(findings, args), encoding="utf-8")

    if bottlenecks:
        for f in bottlenecks:
            sys.stderr.write(
                f"[under-gate-bottleneck] WARN {f.date} ({f.mode}): "
                f"{f.top_reason} owns {f.top_reason_share:.1%} of "
                f"{f.under_skip_total} UNDER skips. Likely misconfigured "
                f"UNDER gate threshold.\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
