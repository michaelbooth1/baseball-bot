#!/usr/bin/env python3
"""analyze_stake_scaling_promotion.py -- Active #6 promotion-gate analyzer.

Calibrated stake scaling (Active #6 part 2) shipped 2026-05-12 in shadow:
every filled bet records a `calibrated_stake_multiplier` (and the
underlying calibrated edge) but the actual stake is unchanged. The
promotion gate to enforce mode is "high-multiplier bets out-win
low-multiplier bets after fees over ~30 sessions."

This script reads the per-date session JSONs (`data/live_trading/sessions/
*_session.json`), extracts filled+settled bets that carry
`calibrated_stake_multiplier`, buckets them by multiplier into low / mid /
high terciles, and emits a verdict:

  - need_more_data: too few sessions or too few bets in low/high buckets
  - hold: enough data but high doesn't beat low by the configured margin
  - promote: high beats low by >= configured WR delta AND ROI delta

Output (JSON + Markdown):
  data/analysis_output/stake_scaling_analysis/stake_scaling_analysis.json
  data/analysis_output/stake_scaling_analysis/stake_scaling_analysis.md

Read-only: never touches live ledgers or candidate rows. Wired as a
RefreshStep before `weekly_drift_rollup` so the verdict refreshes daily
and surfaces in the weekly rollup HTML.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SESSIONS_DIR = PROJECT_DIR / "data" / "live_trading" / "sessions"
DEFAULT_OUTPUT_DIR = (
    PROJECT_DIR / "data" / "analysis_output" / "stake_scaling_analysis"
)

# Promotion-gate defaults. Conservative on purpose; do not loosen without
# walk-forward evidence.
DEFAULT_MIN_SESSIONS = 30          # ~ a month of live data
DEFAULT_MIN_BETS_PER_BUCKET = 5    # so cohort estimates aren't a single bet
DEFAULT_PROMOTE_MIN_WR_DELTA = 0.05    # high-bucket WR exceeds low by >= 5pp
DEFAULT_PROMOTE_MIN_ROI_DELTA = 0.05   # high-bucket ROI exceeds low by >= 5pp


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class BetRow:
    """One filled+settled bet with calibrated_stake_multiplier present."""
    session_date: str
    multiplier: float
    edge_used: Optional[float]
    stake: float
    fill_cost_usdc: float
    profit: float
    won: bool


@dataclass
class Bucket:
    """Aggregate of bets in a multiplier band."""
    name: str
    n: int = 0
    wins: int = 0
    total_stake: float = 0.0
    total_fill_cost: float = 0.0
    total_profit: float = 0.0
    multipliers: List[float] = field(default_factory=list)
    edges: List[float] = field(default_factory=list)

    @property
    def win_rate(self) -> Optional[float]:
        return (self.wins / self.n) if self.n else None

    @property
    def roi(self) -> Optional[float]:
        return (self.total_profit / self.total_fill_cost) if self.total_fill_cost else None

    @property
    def avg_multiplier(self) -> Optional[float]:
        return (sum(self.multipliers) / len(self.multipliers)) if self.multipliers else None

    @property
    def avg_edge(self) -> Optional[float]:
        valid = [e for e in self.edges if e is not None]
        return (sum(valid) / len(valid)) if valid else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "n": self.n,
            "wins": self.wins,
            "total_stake": round(self.total_stake, 4),
            "total_fill_cost": round(self.total_fill_cost, 4),
            "total_profit": round(self.total_profit, 4),
            "win_rate": _round_or_none(self.win_rate, 4),
            "roi": _round_or_none(self.roi, 4),
            "avg_multiplier": _round_or_none(self.avg_multiplier, 4),
            "avg_edge_used": _round_or_none(self.avg_edge, 4),
        }


def _round_or_none(v: Optional[float], digits: int) -> Optional[float]:
    return None if v is None else round(v, digits)


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------

def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def extract_bet_rows(session_payload: Dict[str, Any]) -> List[BetRow]:
    """Pull filled+settled bets with calibrated_stake_multiplier out of one
    session JSON. Returns [] when the session has no eligible bets.
    """
    out: List[BetRow] = []
    session_date = str(session_payload.get("date") or "")
    for b in session_payload.get("bets") or []:
        if not isinstance(b, dict):
            continue
        mult = _safe_float(b.get("calibrated_stake_multiplier"))
        if mult is None:
            continue
        if b.get("order_status") not in ("filled", "reconciled_filled"):
            continue
        if not b.get("settled"):
            continue
        won = b.get("won")
        if won is None:
            continue
        stake = _safe_float(b.get("stake")) or 0.0
        fill_cost = _safe_float(b.get("fill_cost_usdc"))
        if fill_cost is None:
            # Some older rows used `fill_cost` instead.
            fill_cost = _safe_float(b.get("fill_cost")) or 0.0
        profit = _safe_float(b.get("profit")) or 0.0
        out.append(BetRow(
            session_date=session_date,
            multiplier=float(mult),
            edge_used=_safe_float(b.get("calibrated_stake_edge_used")),
            stake=stake,
            fill_cost_usdc=fill_cost,
            profit=profit,
            won=bool(won),
        ))
    return out


def discover_session_files(sessions_dir: Path) -> List[Path]:
    """Return *_session.json files sorted by name (ISO date asc)."""
    return sorted(sessions_dir.glob("*_session.json"))


def load_all_bets(sessions_dir: Path) -> List[BetRow]:
    rows: List[BetRow] = []
    for p in discover_session_files(sessions_dir):
        try:
            with open(p, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            sys.stderr.write(
                f"analyze_stake_scaling_promotion: skipping {p.name}: {exc}\n"
            )
            continue
        rows.extend(extract_bet_rows(payload))
    return rows


# ---------------------------------------------------------------------------
# Bucket
# ---------------------------------------------------------------------------

def tercile_cuts(values: List[float]) -> Tuple[float, float]:
    """Return (lo_cut, hi_cut) defining tercile boundaries.

    Bets with multiplier <= lo_cut → low bucket.
    Bets with multiplier >= hi_cut → high bucket.
    Everything strictly in between → mid.

    The bucketing uses inclusive boundaries on both sides on purpose:
    the calibrated multiplier clamps hard at the configured floor and
    ceiling (typically 0.5 and 1.5), so most live data is bimodal. With
    strict-> on the high side, [0.5]*10 + [1.5]*10 would put all the
    1.5s into mid and leave the high bucket empty. Using >= ensures the
    floor cohort and ceiling cohort are visible as low + high
    respectively, with mid carrying only the interior values.

    When all values are equal (lo_cut == hi_cut), both boundaries
    collapse and every row lands in low (the lower side wins ties).
    The verdict layer treats that as need_more_data via the
    min_bets_per_bucket gate.
    """
    if not values:
        return (0.0, 0.0)
    sv = sorted(values)
    n = len(sv)
    lo_idx = max(0, (n // 3) - 1)
    hi_idx = min(n - 1, (2 * n) // 3)
    return (sv[lo_idx], sv[hi_idx])


def assign_bucket(multiplier: float, lo_cut: float, hi_cut: float) -> str:
    if multiplier <= lo_cut:
        return "low"
    if multiplier >= hi_cut:
        return "high"
    return "mid"


def aggregate_buckets(rows: List[BetRow]) -> Dict[str, Bucket]:
    mults = [r.multiplier for r in rows]
    lo_cut, hi_cut = tercile_cuts(mults)
    buckets = {name: Bucket(name=name) for name in ("low", "mid", "high")}
    for r in rows:
        b = buckets[assign_bucket(r.multiplier, lo_cut, hi_cut)]
        b.n += 1
        if r.won:
            b.wins += 1
        b.total_stake += r.stake
        b.total_fill_cost += r.fill_cost_usdc
        b.total_profit += r.profit
        b.multipliers.append(r.multiplier)
        if r.edge_used is not None:
            b.edges.append(r.edge_used)
    return buckets


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    label: str           # "need_more_data" | "hold" | "promote"
    reason: str
    n_sessions: int
    n_filled_bets: int
    wr_delta: Optional[float]    # high - low
    roi_delta: Optional[float]   # high - low


def compute_verdict(
    rows: List[BetRow],
    buckets: Dict[str, Bucket],
    *,
    min_sessions: int,
    min_bets_per_bucket: int,
    promote_min_wr_delta: float,
    promote_min_roi_delta: float,
) -> Verdict:
    n_sessions = len({r.session_date for r in rows})
    n_bets = len(rows)
    lo = buckets["low"]
    hi = buckets["high"]

    wr_delta: Optional[float] = None
    roi_delta: Optional[float] = None
    if lo.win_rate is not None and hi.win_rate is not None:
        wr_delta = hi.win_rate - lo.win_rate
    if lo.roi is not None and hi.roi is not None:
        roi_delta = hi.roi - lo.roi

    if n_sessions < min_sessions:
        return Verdict(
            label="need_more_data",
            reason=(
                f"Have {n_sessions}/{min_sessions} sessions of shadow data "
                f"(n_filled_bets={n_bets}, low={lo.n}, mid={buckets['mid'].n}, high={hi.n})."
            ),
            n_sessions=n_sessions,
            n_filled_bets=n_bets,
            wr_delta=wr_delta,
            roi_delta=roi_delta,
        )
    if lo.n < min_bets_per_bucket or hi.n < min_bets_per_bucket:
        return Verdict(
            label="need_more_data",
            reason=(
                f"Insufficient bets per bucket (low={lo.n}, high={hi.n}; "
                f"need >= {min_bets_per_bucket} each)."
            ),
            n_sessions=n_sessions,
            n_filled_bets=n_bets,
            wr_delta=wr_delta,
            roi_delta=roi_delta,
        )

    if (
        wr_delta is not None and wr_delta >= promote_min_wr_delta
        and roi_delta is not None and roi_delta >= promote_min_roi_delta
    ):
        return Verdict(
            label="promote",
            reason=(
                f"High-multiplier cohort beats low by "
                f"{wr_delta * 100:+.1f}pp WR and {roi_delta * 100:+.1f}pp ROI "
                f"over {n_bets} filled bets across {n_sessions} sessions. "
                f"Promotion thresholds met (WR >= {promote_min_wr_delta * 100:.1f}pp "
                f"AND ROI >= {promote_min_roi_delta * 100:.1f}pp)."
            ),
            n_sessions=n_sessions,
            n_filled_bets=n_bets,
            wr_delta=wr_delta,
            roi_delta=roi_delta,
        )

    return Verdict(
        label="hold",
        reason=(
            f"High vs low cohort: WR delta {wr_delta * 100:+.1f}pp, "
            f"ROI delta {roi_delta * 100:+.1f}pp -- not enough margin to "
            f"promote (need WR >= {promote_min_wr_delta * 100:.1f}pp "
            f"AND ROI >= {promote_min_roi_delta * 100:.1f}pp)."
        ),
        n_sessions=n_sessions,
        n_filled_bets=n_bets,
        wr_delta=wr_delta,
        roi_delta=roi_delta,
    )


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return (datetime.now(timezone.utc).replace(microsecond=0)
            .isoformat().replace("+00:00", "Z"))


def build_report_payload(
    rows: List[BetRow],
    buckets: Dict[str, Bucket],
    verdict: Verdict,
    *,
    min_sessions: int,
    min_bets_per_bucket: int,
    promote_min_wr_delta: float,
    promote_min_roi_delta: float,
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "active_priority": "Active #6 part 2 (calibrated stake scaling)",
        "verdict": verdict.label,
        "verdict_reason": verdict.reason,
        "n_filled_bets": verdict.n_filled_bets,
        "n_sessions": verdict.n_sessions,
        "thresholds": {
            "min_sessions": min_sessions,
            "min_bets_per_bucket": min_bets_per_bucket,
            "promote_min_wr_delta": promote_min_wr_delta,
            "promote_min_roi_delta": promote_min_roi_delta,
        },
        "session_date_span": (
            {"first": min(r.session_date for r in rows),
             "last":  max(r.session_date for r in rows)}
            if rows else None
        ),
        "comparison_high_vs_low": {
            "wr_delta": _round_or_none(verdict.wr_delta, 4),
            "roi_delta": _round_or_none(verdict.roi_delta, 4),
        },
        "buckets": {name: buckets[name].to_dict() for name in ("low", "mid", "high")},
    }


def render_markdown(payload: Dict[str, Any]) -> str:
    verdict = payload["verdict"]
    reason = payload["verdict_reason"]
    buckets = payload["buckets"]
    comp = payload["comparison_high_vs_low"]

    def fmt_pct(v):
        return "—" if v is None else f"{v * 100:.1f}%"

    def fmt_money(v):
        return "—" if v is None else f"${v:+.2f}"

    rows_md = []
    rows_md.append(
        "| Bucket | N | Wins | WR | Stake | Profit | ROI | "
        "Avg mult | Avg edge |"
    )
    rows_md.append(
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    def fmt_mult(v):
        return "—" if v is None else f"{v:.2f}x"

    def fmt_edge(v):
        return "—" if v is None else f"{v:+.3f}"

    for name in ("low", "mid", "high"):
        b = buckets[name]
        rows_md.append(
            f"| {name} | {b['n']} | {b['wins']} | {fmt_pct(b['win_rate'])} | "
            f"${b['total_stake']:.2f} | {fmt_money(b['total_profit'])} | "
            f"{fmt_pct(b['roi'])} | "
            f"{fmt_mult(b['avg_multiplier'])} | "
            f"{fmt_edge(b['avg_edge_used'])} |"
        )
    table_md = "\n".join(rows_md)

    return (
        f"# Stake-scaling promotion analyzer\n\n"
        f"_Generated {payload['generated_at_utc']}._\n\n"
        f"**Verdict:** `{verdict}`\n\n"
        f"> {reason}\n\n"
        f"## Cohort comparison (high vs low multiplier)\n\n"
        f"- WR delta:  **{fmt_pct(comp['wr_delta'])}** (high − low)\n"
        f"- ROI delta: **{fmt_pct(comp['roi_delta'])}** (high − low)\n"
        f"- Sessions: **{payload['n_sessions']}/{payload['thresholds']['min_sessions']}**, "
        f"filled bets: **{payload['n_filled_bets']}**\n\n"
        f"## Per-bucket breakdown\n\n"
        f"{table_md}\n\n"
        f"## Thresholds\n\n"
        f"- `min_sessions`: {payload['thresholds']['min_sessions']}\n"
        f"- `min_bets_per_bucket`: {payload['thresholds']['min_bets_per_bucket']}\n"
        f"- `promote_min_wr_delta`: {payload['thresholds']['promote_min_wr_delta']}\n"
        f"- `promote_min_roi_delta`: {payload['thresholds']['promote_min_roi_delta']}\n\n"
        f"## Read this when\n\n"
        f"The calibrated stake multiplier is in shadow mode. Promotion to "
        f"enforce requires (a) at least `min_sessions` of live data and "
        f"(b) the high-multiplier cohort meaningfully outperforming the "
        f"low-multiplier cohort on filled bets. Do not promote on a single "
        f"daily spike -- the verdict is computed on rolling data and will "
        f"flip back to `hold` if the relationship is noise.\n"
    )


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--sessions-dir", type=Path, default=DEFAULT_SESSIONS_DIR)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--min-sessions", type=int, default=DEFAULT_MIN_SESSIONS)
    p.add_argument("--min-bets-per-bucket", type=int,
                   default=DEFAULT_MIN_BETS_PER_BUCKET)
    p.add_argument("--promote-min-wr-delta", type=float,
                   default=DEFAULT_PROMOTE_MIN_WR_DELTA)
    p.add_argument("--promote-min-roi-delta", type=float,
                   default=DEFAULT_PROMOTE_MIN_ROI_DELTA)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    rows = load_all_bets(Path(args.sessions_dir))
    buckets = aggregate_buckets(rows)
    verdict = compute_verdict(
        rows, buckets,
        min_sessions=args.min_sessions,
        min_bets_per_bucket=args.min_bets_per_bucket,
        promote_min_wr_delta=args.promote_min_wr_delta,
        promote_min_roi_delta=args.promote_min_roi_delta,
    )
    payload = build_report_payload(
        rows, buckets, verdict,
        min_sessions=args.min_sessions,
        min_bets_per_bucket=args.min_bets_per_bucket,
        promote_min_wr_delta=args.promote_min_wr_delta,
        promote_min_roi_delta=args.promote_min_roi_delta,
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "stake_scaling_analysis.json"
    md_path = output_dir / "stake_scaling_analysis.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print(f"Verdict: {verdict.label} -- {verdict.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
