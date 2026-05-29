#!/usr/bin/env python3
"""audit_over_gate_ev.py -- is each OVER gate actually +EV? (2026-05-28).

The existing per-gate analyzers (`build_walk_forward_certification.py`,
`build_gate_counterfactual_report.py`) both run on the ~227 FILLED+settled
bets in the signal_training_table. That table only contains bets that
PASSED every gate, so for a gate that blocks 0 filled bets at its current
threshold (every pre-FV gate: min_inning, runs_needed, blowout, dead zones,
pace, min_current_total, ...) those reports literally cannot see what the
gate filters -- they degrade to "insufficient evidence."

This audit closes that blind spot. For each gate it reads the
candidate-universe SKIP rows (the bets the gate actually blocked), dedupes
them to unique game-state opportunities, joins realized outcomes
(`over_hit`), and asks the only question that matters:

    Would the bets this gate blocked have made money?

  - If the blocked cohort would have LOST (taker ROI < 0), the gate is
    correctly filtering -> +EV.
  - If the blocked cohort would have WON (taker ROI > 0), the gate is
    throwing away profit -> -EV (over-blocking).

Method + honest caveats:
  - Counterfactual is at the TAKER price (decision_ask), assuming a fill.
    The win/loss label (`over_hit`) is REAL (the over either hit or it
    didn't); only the price/fill is assumed. So this is an upper-ish bound
    on what the blocked bets were worth as immediate takers.
  - ROI is computed on the BETTABLE subset (ask >= --min-ask, default 0.55)
    because we would never have placed a sub-floor-ask bet anyway.
  - It does NOT model downstream gates (a blocked bet might also be caught
    by another gate) or fill probability on never-placed bets. It answers
    "were the blocked opportunities good?", not "exactly how much P&L."
  - Dedupe collapses tick-level re-polling of the same state to one
    opportunity, so the n's are unique opportunities, not ticks.
  - OVER side only (UNDER has no settled data yet).

Compare against the placed-bet baseline printed at the top: our actually-
placed OVER bets are themselves ~breakeven as takers, so a gate that
blocks a cohort no worse than that baseline is not clearly earning its keep.

Output:
  data/analysis_output/over_gate_ev_audit/over_gate_ev_audit.{json,md}
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ROOTS = [
    PROJECT_DIR / "data" / "live_trading" / "candidate_universe",
    PROJECT_DIR / "data" / "paper_trading" / "candidate_universe",
    PROJECT_DIR / "data" / "paper_A_current" / "candidate_universe",
]
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "data" / "analysis_output" / "over_gate_ev_audit"
DEFAULT_OUTPUT_STEM = "over_gate_ev_audit"

DEFAULT_MIN_ASK = 0.55          # below this we'd never place (gate_min_entry_ask floor)
DEFAULT_MIN_N_FOR_VERDICT = 30  # bettable blocked opportunities needed to rule
# ROI thresholds for the verdict (taker ROI per unit staked).
EV_GATE_ROI_CEILING = -0.05     # blocked cohort loses >=5% -> gate clearly +EV
ANTI_EV_GATE_ROI_FLOOR = 0.05   # blocked cohort wins >=5% -> gate clearly -EV

# Gate display order (pipeline order). Anything else is appended.
GATE_ORDER = [
    "placed_bet",
    "gate_min_inning",
    "gate_inactive_inning_state",
    "gate_min_entry_ask",
    "gate_crossed_book",
    "gate_wide_spread",
    "gate_ask_jump_unconfirmed",
    "gate_min_current_total",
    "gate_runs_pace",
    "gate_runs_needed_max",
    "gate_close_game_runs_needed",
    "gate_inning5_runs_needed",
    "gate_inning6_runs_needed",
    "gate_blowout",
    "inference_no_match",
    "gate_fv_saturation",
    "gate_stage2_suppression",
    "gate_min_edge",
    "gate_extreme_edge",
    "gate_fv_ask_gap",
    "gate_sp_era",
]


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
def wilson_interval(wins: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n <= 0:
        return (0.0, 0.0)
    p = wins / n
    d = 1.0 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / d
    return (max(0.0, c - m), min(1.0, c + m))


def taker_roi(over_hit: bool, ask: float) -> float:
    """Per-unit taker P&L: win -> (1/ask - 1), loss -> -1."""
    return (1.0 / ask - 1.0) if over_hit else -1.0


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def classify_gate(
    *,
    bettable_n: int,
    roi: Optional[float],
    wr: Optional[float],
    breakeven: Optional[float],
    min_n: int,
) -> str:
    """Verdict for one gate's blocked cohort."""
    if bettable_n < min_n or roi is None:
        return "INSUFFICIENT"
    if roi <= EV_GATE_ROI_CEILING:
        return "PLUS_EV"               # blocked cohort loses -> gate earns its keep
    if roi >= ANTI_EV_GATE_ROI_FLOOR:
        return "MINUS_EV"              # blocked cohort wins -> gate throws away profit
    return "MARGINAL"                  # ~breakeven; no clearer than our own placed bets


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------
def load_outcomes(roots: Sequence[Path]) -> Dict[Tuple[Any, str], bool]:
    """(game_pk, line_str) -> over_hit."""
    outc: Dict[Tuple[Any, str], bool] = {}
    for r in roots:
        for p in glob.glob(os.path.join(str(r), "*_outcomes.jsonl")):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    key = (d.get("game_pk"), str(d.get("line")))
                    oh = d.get("over_hit")
                    if oh is None:
                        ft = _safe_float(d.get("final_total"))
                        ln = _safe_float(d.get("line"))
                        if ft is not None and ln is not None:
                            oh = ft > ln
                    if oh is not None:
                        outc[key] = bool(oh)
    return outc


def _state_key(d: Dict[str, Any], reason: str) -> Tuple:
    return (
        d.get("game_pk"), str(d.get("line")), d.get("inning"),
        d.get("inning_state"), d.get("outs"),
        d.get("away_score_before"), d.get("home_score_before"), reason,
    )


@dataclass
class GateCohort:
    reason: str
    rows: List[Tuple[bool, Optional[float]]] = field(default_factory=list)  # (over_hit, ask)


def collect_blocked_cohorts(
    roots: Sequence[Path],
    outcomes: Dict[Tuple[Any, str], bool],
) -> Dict[str, GateCohort]:
    """Dedup OVER candidate rows by state key per decision_reason; keep only
    those with a known outcome."""
    seen: set = set()
    cohorts: Dict[str, GateCohort] = {}
    for r in roots:
        for p in glob.glob(os.path.join(str(r), "*_candidates.jsonl")):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if str(d.get("side", "over")).lower() != "over":
                        continue
                    reason = d.get("decision_reason") or d.get("decision")
                    if not reason:
                        continue
                    key = _state_key(d, reason)
                    if key in seen:
                        continue
                    seen.add(key)
                    okey = (d.get("game_pk"), str(d.get("line")))
                    if okey not in outcomes:
                        continue
                    ask = _safe_float(d.get("decision_ask"))
                    cohorts.setdefault(reason, GateCohort(reason)).rows.append(
                        (outcomes[okey], ask)
                    )
    return cohorts


# --------------------------------------------------------------------------
# Per-gate aggregation
# --------------------------------------------------------------------------
def summarize_gate(cohort: GateCohort, *, min_ask: float, min_n: int) -> Dict[str, Any]:
    rows = cohort.rows
    n = len(rows)
    wins = sum(1 for oh, _ in rows if oh)
    wr = wins / n if n else None
    wlo, whi = wilson_interval(wins, n)

    bett = [(oh, a) for oh, a in rows if a is not None and min_ask <= a < 1.0]
    bn = len(bett)
    if bn:
        bwins = sum(1 for oh, _ in bett if oh)
        avg_ask = sum(a for _, a in bett) / bn
        roi = sum(taker_roi(oh, a) for oh, a in bett) / bn
        bwr = bwins / bn
        bwlo, bwhi = wilson_interval(bwins, bn)
    else:
        bwins = 0
        avg_ask = roi = bwr = None
        bwlo = bwhi = None

    verdict = classify_gate(
        bettable_n=bn, roi=roi, wr=bwr, breakeven=avg_ask, min_n=min_n,
    )
    return {
        "reason": cohort.reason,
        "n_blocked": n,
        "over_win_rate": wr,
        "over_win_rate_wilson_lo": wlo,
        "over_win_rate_wilson_hi": whi,
        "bettable_n": bn,
        "bettable_avg_ask": avg_ask,
        "bettable_over_win_rate": bwr,
        "bettable_wr_wilson_lo": bwlo,
        "bettable_wr_wilson_hi": bwhi,
        "bettable_breakeven_wr": avg_ask,
        "bettable_ev_margin": (bwr - avg_ask) if (bwr is not None and avg_ask is not None) else None,
        "bettable_taker_roi": roi,
        "verdict": verdict,
    }


VERDICT_LABEL = {
    "PLUS_EV": "+EV (earns its keep)",
    "MINUS_EV": "-EV (blocks winners)",
    "MARGINAL": "marginal (~breakeven)",
    "INSUFFICIENT": "insufficient data",
}


def build_report(
    roots: Sequence[Path],
    *,
    min_ask: float,
    min_n: int,
) -> Dict[str, Any]:
    outcomes = load_outcomes(roots)
    cohorts = collect_blocked_cohorts(roots, outcomes)

    placed = summarize_gate(cohorts.get("placed_bet", GateCohort("placed_bet")),
                            min_ask=min_ask, min_n=min_n)

    ordered_reasons = [g for g in GATE_ORDER if g in cohorts]
    extra = sorted(r for r in cohorts if r not in GATE_ORDER)
    gates = [
        summarize_gate(cohorts[r], min_ask=min_ask, min_n=min_n)
        for r in ordered_reasons + extra
        if r != "placed_bet"
    ]

    counts: Dict[str, int] = {}
    for g in gates:
        counts[g["verdict"]] = counts.get(g["verdict"], 0) + 1

    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": {
            "roots": [str(r) for r in roots],
            "min_ask": min_ask,
            "min_n_for_verdict": min_n,
            "ev_gate_roi_ceiling": EV_GATE_ROI_CEILING,
            "anti_ev_gate_roi_floor": ANTI_EV_GATE_ROI_FLOOR,
        },
        "outcomes_loaded": len(outcomes),
        "placed_bet_baseline": placed,
        "verdict_counts": counts,
        "gates": gates,
    }


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------
def _f(v: Any, nd: int = 3, pct: bool = False) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v * 100:+.1f}%" if pct else f"{v:.{nd}f}"
    return str(v)


def render_markdown(report: Dict[str, Any]) -> str:
    cfg = report["config"]
    pb = report["placed_bet_baseline"]
    L: List[str] = []
    L.append("# OVER gate EV audit")
    L.append("")
    L.append(f"_Generated {report['generated_at_utc']}. Outcomes loaded: "
             f"{report['outcomes_loaded']}._")
    L.append("")
    L.append("**Question:** would the bets each gate blocked have made money? "
             "If the blocked cohort loses, the gate is +EV; if it wins, the gate "
             "is throwing away profit (-EV).")
    L.append("")
    L.append(f"**Placed-bet baseline (what we actually bet):** "
             f"bettable n={pb['bettable_n']}, avg ask {_f(pb['bettable_avg_ask'])}, "
             f"WR {_f(pb['bettable_over_win_rate'])}, taker ROI "
             f"{_f(pb['bettable_taker_roi'], pct=True)}. A gate only clearly earns "
             f"its keep if its blocked cohort is worse than this.")
    L.append("")
    vc = report["verdict_counts"]
    L.append("**Verdict tally:** " + ", ".join(
        f"{VERDICT_LABEL[k]}: {vc.get(k, 0)}"
        for k in ("PLUS_EV", "MARGINAL", "MINUS_EV", "INSUFFICIENT")
    ))
    L.append("")
    L.append(f"Method: counterfactual at taker ask (assumes fill); real over-hit "
             f"labels; bettable subset ask >= {cfg['min_ask']}; verdict needs "
             f"bettable n >= {cfg['min_n_for_verdict']}. +EV if ROI <= "
             f"{cfg['ev_gate_roi_ceiling']:+.2f}, -EV if ROI >= "
             f"{cfg['anti_ev_gate_roi_floor']:+.2f}. Does NOT model downstream "
             f"gates or fill probability. OVER only.")
    L.append("")
    L.append("| Gate | blocked n | over WR | bettable n | avg ask | WR | WR Wilson-lo | taker ROI | verdict |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for g in report["gates"]:
        L.append(
            "| `{r}` | {n} | {wr} | {bn} | {ask} | {bwr} | {wlo} | {roi} | **{v}** |".format(
                r=g["reason"], n=g["n_blocked"], wr=_f(g["over_win_rate"]),
                bn=g["bettable_n"], ask=_f(g["bettable_avg_ask"]),
                bwr=_f(g["bettable_over_win_rate"]),
                wlo=_f(g["bettable_wr_wilson_lo"]),
                roi=_f(g["bettable_taker_roi"], pct=True),
                v=VERDICT_LABEL[g["verdict"]],
            )
        )
    L.append("")
    L.append("## How to read")
    L.append("- **taker ROI < 0** → the blocked overs would have lost as takers "
             "→ the gate is correctly filtering (+EV).")
    L.append("- **taker ROI > 0** → the blocked overs would have won → the gate "
             "is over-blocking (-EV); re-examine it.")
    L.append("- **WR Wilson-lo vs avg ask (breakeven):** if Wilson-lo of the "
             "blocked WR is still above the avg ask, we're confident the blocked "
             "cohort beats breakeven (a -EV gate).")
    L.append("")
    L.append("_Complements `walk_forward_certification` (which sweeps thresholds "
             "on filled bets) by measuring the cohort each gate ACTUALLY blocked "
             "in the candidate universe -- the pre-FV gates the certification "
             "report can't see._")
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--roots", type=str, default=None,
                   help="Comma-separated candidate_universe dirs (default: live + paper + paper_A_current).")
    p.add_argument("--min-ask", type=float, default=DEFAULT_MIN_ASK)
    p.add_argument("--min-n", type=int, default=DEFAULT_MIN_N_FOR_VERDICT)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--output-stem", type=str, default=DEFAULT_OUTPUT_STEM)
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.roots:
        roots = [Path(s.strip()) for s in args.roots.split(",") if s.strip()]
    else:
        roots = list(DEFAULT_ROOTS)

    report = build_report(roots, min_ask=args.min_ask, min_n=args.min_n)

    args.output_root.mkdir(parents=True, exist_ok=True)
    json_path = args.output_root / f"{args.output_stem}.json"
    md_path = args.output_root / f"{args.output_stem}.md"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(report))

    vc = report["verdict_counts"]
    print(f"[over_gate_ev_audit] gates audited={len(report['gates'])} "
          f"+EV={vc.get('PLUS_EV', 0)} marginal={vc.get('MARGINAL', 0)} "
          f"-EV={vc.get('MINUS_EV', 0)} insufficient={vc.get('INSUFFICIENT', 0)}")
    for g in report["gates"]:
        if g["verdict"] == "MINUS_EV":
            print(f"  !! -EV gate: {g['reason']} "
                  f"(blocked bettable n={g['bettable_n']} WR={_f(g['bettable_over_win_rate'])} "
                  f"ask={_f(g['bettable_avg_ask'])} ROI={_f(g['bettable_taker_roi'], pct=True)})")
    print(f"  wrote {json_path}")
    print(f"  wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
