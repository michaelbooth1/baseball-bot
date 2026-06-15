"""fleet_health.py -- paired-delta comparison of fleet presets vs baseline.

2026-06-11. The parallel-engine fleet (launch_parallel_engines.py)
runs N paper configs against the same live market day. The existing
aggregator (aggregate_parallel_engines.py) publishes per-engine
MARGINAL tables -- n / WR / ROI per config -- with a standing "treat
as diagnostic, not promotion evidence" warning. That warning exists
because marginal tables are the wrong lens: every engine sees the
same games, so most bets are identical across configs and the
marginal numbers are dominated by shared variance.

The decision information lives in the PAIRED DELTA: the bets a config
took that the baseline didn't (and vice versa). This block computes
that per preset every day:

  - shared / unique bet counts (key: game_pk, line, side, inning)
  - unique-cohort W/L/P&L on each side
  - delta_net = pnl(engine uniques) - pnl(baseline uniques)
  - Welch t on per-bet profit between the two unique cohorts
  - a verdict ladder:
      NO_SHARED_DAYS -> DEAD -> CONCLUSIVE_{POSITIVE,NEGATIVE}
        -> TRENDING_{POSITIVE,NEGATIVE} -> COLLECTING
  - days_to_significance estimate while COLLECTING / TRENDING

The 2026-06-10 fleet audit found three presets whose conclusions had
been sitting unread in this exact arithmetic for ~2 weeks
(E_tight_edge / G_loose_edge: edge floor locally optimal both
directions; N_extreme_edge_022: zero delta decisions). This block
exists so the next conclusion surfaces in the daily review without a
manual audit.

Fail-open: per-engine errors degrade that engine's entry; the block
never raises into build_report.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .constants import (
    FLEET_BASELINE_LABEL,
    FLEET_CONCLUSIVE_MIN_DELTA_N,
    FLEET_CONCLUSIVE_T,
    FLEET_DATA_ROOT,
    FLEET_DEAD_MAX_DELTA_FLOW_PER_DAY,
    FLEET_DEAD_MIN_SHARED_DAYS,
    FLEET_EXCLUDED_ROOT_NAMES,
    FLEET_RETIRED_ROOT_NAMES,
    FLEET_EXECUTION_SENSITIVE_LABELS,
    FLEET_PAIRED_DELTA_TRAILING_DAYS,
    FLEET_TRENDING_MIN_DELTA_N,
    FLEET_TRENDING_T,
)


def _load_settled_bets(session_path: Path) -> Optional[List[Dict[str, Any]]]:
    """Load settled bets from one session JSON. Returns None when the
    file is missing (engine didn't run that day) vs [] when it ran but
    has no settled bets -- the distinction drives shared-day counting."""
    if not session_path.exists():
        return None
    try:
        with open(session_path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    bets = payload.get("bets")
    if not isinstance(bets, list):
        return []
    return [
        b for b in bets
        if isinstance(b, dict) and bool(b.get("settled"))
    ]


def _bet_key(session_date: str, bet: Dict[str, Any]) -> Tuple:
    """Config-independent identity of a signal. bet_ids embed
    per-engine sequence counters, so identical signals get different
    ids across engines; (date, game, line, side, inning) is the
    stable join key the 2026-06-10 fleet audit used."""
    return (
        session_date,
        bet.get("game_pk"),
        str(bet.get("line")),
        str(bet.get("side") or "over").lower(),
        bet.get("inning"),
    )


def _unique_cohort_stats(
    keys: List[Tuple], bets_by_key: Dict[Tuple, Dict[str, Any]],
) -> Dict[str, Any]:
    wins = losses = 0
    pnl = 0.0
    stake = 0.0
    profits: List[float] = []
    for k in keys:
        b = bets_by_key[k]
        won = b.get("won")
        profit = float(b.get("profit") or 0.0)
        pnl += profit
        stake += float(b.get("stake") or 0.0)
        profits.append(profit)
        if won is True:
            wins += 1
        elif won is False:
            losses += 1
    n = wins + losses
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "wr": round(wins / n, 4) if n else None,
        "pnl": round(pnl, 2),
        "stake": round(stake, 2),
        "_profits": profits,  # stripped before payload emission
    }


def _one_sample_t(xs: List[float]) -> Optional[float]:
    """One-sample t of mean profit vs 0. None for n<2 or zero variance."""
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    v = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    if v <= 0:
        return None
    return m / math.sqrt(v / len(xs))


def _welch_t(a: List[float], b: List[float]) -> Optional[float]:
    """Test statistic for "is config X's delta cohort better than the
    baseline's?". Positive favors X.

    - Both cohorts non-trivial: Welch two-sample t on per-bet profit.
    - One cohort empty (strictly-additive presets like F_no_dedup add
      bets and never skip any): one-sample t of the non-empty cohort's
      profit vs 0. Sign convention: if X's ADDED bets profit, X wins
      (t>0); if the bets X SKIPS profit, X loses (negated t).
    - None when neither path has enough data.
    """
    if len(a) >= 2 and len(b) >= 2:
        ma = sum(a) / len(a)
        mb = sum(b) / len(b)
        va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
        vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
        denom = math.sqrt(va / len(a) + vb / len(b))
        if denom <= 0:
            return None
        return (ma - mb) / denom
    if len(b) == 0 and len(a) >= 2:
        return _one_sample_t(a)
    if len(a) == 0 and len(b) >= 2:
        t = _one_sample_t(b)
        return -t if t is not None else None
    return None


def _engine_verdict(
    *,
    shared_days: int,
    n_delta: int,
    delta_flow: float,
    welch_t: Optional[float],
    dead_min_shared_days: int,
    dead_max_flow: float,
    conclusive_t: float,
    conclusive_min_n: int,
    trending_t: float,
    trending_min_n: int,
) -> str:
    if shared_days == 0:
        return "NO_SHARED_DAYS"
    if shared_days >= dead_min_shared_days and delta_flow < dead_max_flow:
        return "DEAD"
    if (
        welch_t is not None
        and abs(welch_t) >= conclusive_t
        and n_delta >= conclusive_min_n
    ):
        return "CONCLUSIVE_POSITIVE" if welch_t > 0 else "CONCLUSIVE_NEGATIVE"
    if (
        welch_t is not None
        and abs(welch_t) >= trending_t
        and n_delta >= trending_min_n
    ):
        return "TRENDING_POSITIVE" if welch_t > 0 else "TRENDING_NEGATIVE"
    return "COLLECTING"


def _days_to_significance(
    *,
    welch_t: Optional[float],
    n_delta: int,
    delta_flow: float,
    conclusive_t: float,
) -> Optional[int]:
    """Rough sample-size extrapolation: t scales ~sqrt(n), so the
    delta-bet count needed for significance at the current effect size
    is n * (t_target / t_now)^2. Returns remaining days at the current
    delta-bet flow; None when not estimable; capped at 365."""
    if welch_t is None or welch_t == 0 or delta_flow <= 0 or n_delta <= 0:
        return None
    if abs(welch_t) >= conclusive_t:
        return 0
    n_required = n_delta * (conclusive_t / abs(welch_t)) ** 2
    days = math.ceil((n_required - n_delta) / delta_flow)
    return min(days, 365)


def _fleet_paired_delta_health(
    *,
    session_date: str,
    data_root: Path = FLEET_DATA_ROOT,
    baseline_label: str = FLEET_BASELINE_LABEL,
    excluded_root_names: Tuple[str, ...] = (
        FLEET_EXCLUDED_ROOT_NAMES + FLEET_RETIRED_ROOT_NAMES
    ),
    trailing_days: int = FLEET_PAIRED_DELTA_TRAILING_DAYS,
    dead_min_shared_days: int = FLEET_DEAD_MIN_SHARED_DAYS,
    dead_max_flow: float = FLEET_DEAD_MAX_DELTA_FLOW_PER_DAY,
    conclusive_t: float = FLEET_CONCLUSIVE_T,
    conclusive_min_n: int = FLEET_CONCLUSIVE_MIN_DELTA_N,
    trending_t: float = FLEET_TRENDING_T,
    trending_min_n: int = FLEET_TRENDING_MIN_DELTA_N,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "session_date": session_date,
        "trailing_days": trailing_days,
        "baseline_label": baseline_label,
        "thresholds": {
            "dead_min_shared_days": dead_min_shared_days,
            "dead_max_delta_flow_per_day": dead_max_flow,
            "conclusive_t": conclusive_t,
            "conclusive_min_delta_n": conclusive_min_n,
            "trending_t": trending_t,
            "trending_min_delta_n": trending_min_n,
        },
        "engines": {},
        "alerts": [],
    }

    try:
        anchor_dt = datetime.strptime(session_date, "%Y-%m-%d")
    except ValueError:
        payload["status"] = "check_error"
        payload["error"] = f"unparseable session_date: {session_date!r}"
        return payload

    dates = [
        (anchor_dt - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(trailing_days)
    ]

    baseline_root = data_root / f"paper_{baseline_label}" / "sessions"
    if not baseline_root.is_dir():
        payload["status"] = "no_baseline_sessions"
        return payload

    # Engine discovery: every data/paper_<label>/sessions dir except
    # the baseline itself and excluded legacy roots. Retired presets
    # whose dirs still exist naturally show up until their last
    # session ages out of the trailing window, then drop to
    # NO_SHARED_DAYS and are omitted.
    engine_roots: Dict[str, Path] = {}
    try:
        for child in sorted(data_root.iterdir()):
            if not child.is_dir() or not child.name.startswith("paper_"):
                continue
            if child.name in excluded_root_names:
                continue
            label = child.name[len("paper_"):]
            if label == baseline_label:
                continue
            sessions = child / "sessions"
            if sessions.is_dir():
                engine_roots[label] = sessions
    except OSError as exc:
        payload["status"] = "check_error"
        payload["error"] = f"engine discovery failed: {exc!r}"
        return payload

    if not engine_roots:
        payload["status"] = "no_fleet_roots"
        return payload

    # Preload baseline bets per date once (engines reuse).
    baseline_by_date: Dict[str, Optional[Dict[Tuple, Dict[str, Any]]]] = {}
    for d in dates:
        bets = _load_settled_bets(baseline_root / f"{d}_session.json")
        if bets is None:
            baseline_by_date[d] = None
        else:
            baseline_by_date[d] = {_bet_key(d, b): b for b in bets}

    for label, sessions_dir in engine_roots.items():
        try:
            shared_days = 0
            shared_keys: List[Tuple] = []
            only_engine_keys: List[Tuple] = []
            only_base_keys: List[Tuple] = []
            engine_bets_by_key: Dict[Tuple, Dict[str, Any]] = {}
            base_bets_by_key: Dict[Tuple, Dict[str, Any]] = {}

            for d in dates:
                base_map = baseline_by_date.get(d)
                eng_bets = _load_settled_bets(sessions_dir / f"{d}_session.json")
                # Shared day = BOTH engines produced a session file
                # (a day an engine ran but placed 0 bets still counts:
                # "took no bets" is itself a decision).
                if base_map is None or eng_bets is None:
                    continue
                shared_days += 1
                eng_map = {_bet_key(d, b): b for b in eng_bets}
                engine_bets_by_key.update(eng_map)
                base_bets_by_key.update(base_map)
                eng_keys = set(eng_map)
                base_keys = set(base_map)
                shared_keys.extend(eng_keys & base_keys)
                only_engine_keys.extend(eng_keys - base_keys)
                only_base_keys.extend(base_keys - eng_keys)

            if shared_days == 0:
                # Engine root exists but never overlapped the baseline
                # in the window (e.g. retired preset aged out). Omit.
                continue

            only_engine = _unique_cohort_stats(only_engine_keys, engine_bets_by_key)
            only_base = _unique_cohort_stats(only_base_keys, base_bets_by_key)
            profits_engine = only_engine.pop("_profits")
            profits_base = only_base.pop("_profits")

            n_delta = only_engine["n"] + only_base["n"]
            delta_flow = n_delta / shared_days if shared_days else 0.0
            t = _welch_t(profits_engine, profits_base)
            delta_net = round(only_engine["pnl"] - only_base["pnl"], 2)
            verdict = _engine_verdict(
                shared_days=shared_days,
                n_delta=n_delta,
                delta_flow=delta_flow,
                welch_t=t,
                dead_min_shared_days=dead_min_shared_days,
                dead_max_flow=dead_max_flow,
                conclusive_t=conclusive_t,
                conclusive_min_n=conclusive_min_n,
                trending_t=trending_t,
                trending_min_n=trending_min_n,
            )

            execution_sensitive = label in FLEET_EXECUTION_SENSITIVE_LABELS
            entry: Dict[str, Any] = {
                "shared_days": shared_days,
                "n_shared_bets": len(shared_keys),
                "only_engine": only_engine,
                "only_baseline": only_base,
                "n_delta_bets": n_delta,
                "delta_flow_per_shared_day": round(delta_flow, 3),
                "delta_net_pnl": delta_net,
                "welch_t": round(t, 3) if t is not None else None,
                "verdict": verdict,
                "execution_sensitive": execution_sensitive,
                "days_to_significance": _days_to_significance(
                    welch_t=t,
                    n_delta=n_delta,
                    delta_flow=delta_flow,
                    conclusive_t=conclusive_t,
                ),
            }
            payload["engines"][label] = entry

            # T6 (2026-06-15): execution-sensitive presets' deltas depend on
            # fill behaviour paper (fills at ask) overstates -- caveat their
            # verdicts so a paper-week CONCLUSIVE isn't shipped at face value.
            exec_caveat = (
                " EXECUTION-SENSITIVE: paper fills at ask overstate this "
                "preset's unique bets (live fill selection differs); confirm "
                "on live re-entry before acting."
                if execution_sensitive else ""
            )
            if verdict in ("CONCLUSIVE_POSITIVE", "CONCLUSIVE_NEGATIVE"):
                direction = (
                    "beats" if verdict == "CONCLUSIVE_POSITIVE" else "loses to"
                )
                payload["alerts"].append(
                    f"Fleet-delta: {label} {verdict} -- its unique bets "
                    f"{direction} {baseline_label}'s unique bets "
                    f"(delta=${delta_net:+.2f} on {n_delta} delta bets over "
                    f"{shared_days} shared days, Welch t={t:.2f}). "
                    "Review for promotion/retirement; see "
                    "fleet_paired_delta_health.engines for the cohort detail."
                    + exec_caveat
                )
            elif verdict == "DEAD":
                payload["alerts"].append(
                    f"Fleet-delta: {label} DEAD -- only {n_delta} delta "
                    f"bet(s) across {shared_days} shared days "
                    f"({delta_flow:.2f}/day). The preset produces almost no "
                    f"distinct decisions vs {baseline_label}; it can never "
                    "conclude. Retire it or fix its premise (cf. the "
                    "N_extreme_edge_022 null experiment, retired 2026-06-10)."
                )
        except Exception as exc:  # noqa: BLE001 -- fail-open per engine
            payload["engines"][label] = {
                "verdict": "check_error",
                "error": repr(exc),
            }

    payload["status"] = "ok" if payload["engines"] else "no_overlapping_engines"
    return payload
