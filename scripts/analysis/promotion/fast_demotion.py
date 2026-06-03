"""Fast Wilson-UB demote verdicts (Active #13, 2026-05-17).

Parallel to the window-based verdicts in demotion.py. Fires when N >= 20
post-promotion bets show a Wilson upper bound on win rate BELOW the
average entry_ask (the implied probability we paid for). At 95% one-sided
confidence this means "even the most generous estimate of true win rate
puts us below breakeven."

The fast check typically fires sooner (~5-6 days vs 14+ days for the
windowed check) when the evidence is statistically clear.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .constants import (
    FAST_DEMOTE_GRACE_DAYS,
    FAST_DEMOTE_MIN_POST_FILLS,
    FAST_DEMOTE_Z,
)
from .demotion import (
    _filled_bets_in_window,
    _parse_iso_date,
    _shift_date,
    _stake_scaling_bet_filter,
)
from .events import latest_promotion_event_for_lever


def _wilson_upper_bound(*, wins: int, n: int, z: float) -> float:
    """One-sided Wilson upper bound on the success rate of n trials
    with `wins` successes. Used by the fast demote check.

    The Wilson interval is more accurate than the normal approximation
    at small N (which is the regime we operate in: ~20 fills/week per
    lever). The formula:

        p_hat = wins / n
        denom = 1 + z^2/n
        center = (p_hat + z^2/(2n)) / denom
        half_width = z/denom * sqrt(p_hat*(1-p_hat)/n + z^2/(4n^2))
        ub = center + half_width

    Returns 1.0 when n <= 0 (no evidence; assume the best case for
    the policy so we don't demote on zero-data).
    """
    if n <= 0:
        return 1.0
    p_hat = wins / n
    denom = 1.0 + (z * z) / n
    center = (p_hat + (z * z) / (2.0 * n)) / denom
    inside = p_hat * (1.0 - p_hat) / n + (z * z) / (4.0 * n * n)
    if inside < 0:
        inside = 0.0
    half_width = (z / denom) * math.sqrt(inside)
    ub = center + half_width
    # Clamp to [0, 1]; the Wilson formula stays inside the unit
    # interval for n >= 1, but floating-point round-off can push by
    # 1e-15 at the boundaries.
    return min(1.0, max(0.0, ub))


def _fast_wilson_demote_from_post_bets(
    post_bets: List[Dict[str, Any]],
    *,
    min_post_fills: int = FAST_DEMOTE_MIN_POST_FILLS,
    z: float = FAST_DEMOTE_Z,
) -> Dict[str, Any]:
    """Compute the fast Wilson-UB demote verdict from one window of
    post-promotion filled bets.

    Verdict taxonomy:
      - `fast_demote`: N >= min_post_fills AND Wilson UB on win rate
        is below the average entry_ask (breakeven). Statistically
        confident the policy is losing money.
      - `hold`: N >= min_post_fills but Wilson UB still >= breakeven.
        No evidence of failure yet.
      - `insufficient_post_data`: N < min_post_fills. UB too wide to
        be useful.

    The breakeven rate uses MEAN entry_ask across the post-window
    bets. At a 0.70 average ask, payout per win = 1/0.70 - 1 = 0.43;
    you must win >= 70% of the time to break even on that mix. If
    Wilson UB on win rate is < 0.70, even the most generous estimate
    of the true win rate falls below breakeven.
    """
    n = len(post_bets)
    wins = sum(
        1 for b in post_bets if (float(b.get("profit") or 0)) > 0
    )
    asks = [
        float(b["entry_ask"])
        for b in post_bets
        if b.get("entry_ask") is not None
    ]
    mean_ask = (sum(asks) / len(asks)) if asks else None

    base: Dict[str, Any] = {
        "n_post_filled": n,
        "wins_post": wins,
        "observed_win_rate": round(wins / n, 4) if n else None,
        "mean_entry_ask": (
            round(mean_ask, 4) if mean_ask is not None else None
        ),
        "wilson_ub_win_rate": None,
        "min_post_fills": min_post_fills,
        "z": z,
    }

    if n < min_post_fills:
        return {**base, "verdict": "insufficient_post_data"}
    if mean_ask is None:
        # No entry_ask -> can't compute breakeven. Treat as
        # insufficient_post_data rather than firing demote.
        return {**base, "verdict": "insufficient_post_data"}

    ub = _wilson_upper_bound(wins=wins, n=n, z=z)
    base["wilson_ub_win_rate"] = round(ub, 4)
    base["breakeven_win_rate"] = round(mean_ask, 4)
    base["wilson_ub_vs_breakeven_delta"] = round(ub - mean_ask, 4)

    if ub < mean_ask:
        return {**base, "verdict": "fast_demote"}
    return {**base, "verdict": "hold"}


def _per_lever_fast_demote_verdict(
    *,
    lever: str,
    promotion_event: Optional[Dict[str, Any]],
    sessions_dir: Path,
    bet_filter: Optional[Any] = None,
    min_post_fills: int = FAST_DEMOTE_MIN_POST_FILLS,
    z: float = FAST_DEMOTE_Z,
    grace_days: int = FAST_DEMOTE_GRACE_DAYS,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute the fast Wilson-UB demote verdict for one lever.

    Reads the most recent promotion event's post-promotion bet
    window (promotion_date + 1 -> today) and runs the Wilson check.
    `grace_days` enforces a minimum gap between promotion timestamp
    and the earliest day we count post-window bets (default 1 -- so
    same-day bets after a morning promotion don't dominate the
    sample with intraday noise).
    """
    if promotion_event is None:
        return {
            "verdict": "no_promotion_to_demote",
            "lever": lever,
            "promotion_event": None,
        }
    pdate_dt = _parse_iso_date(promotion_event.get("generated_at_utc") or "")
    if pdate_dt is None:
        return {
            "verdict": "no_promotion_to_demote",
            "lever": lever,
            "promotion_event": promotion_event,
            "block_reason": "promotion event has unparseable timestamp",
        }
    pdate = pdate_dt.strftime("%Y-%m-%d")
    post_start = _shift_date(pdate, grace_days)
    today_str = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today_str < post_start:
        # Within grace period -- not enough time has elapsed since
        # promotion to start counting.
        return {
            "verdict": "within_grace_period",
            "lever": lever,
            "promotion_event": {
                "generated_at_utc": promotion_event.get("generated_at_utc"),
                "operator": promotion_event.get("operator"),
                "action": promotion_event.get("action"),
            },
            "post_window_dates": {"start": post_start, "end": today_str},
            "grace_days": grace_days,
        }
    post_bets = _filled_bets_in_window(
        sessions_dir, post_start, today_str, bet_filter=bet_filter,
    )
    verdict = _fast_wilson_demote_from_post_bets(
        post_bets, min_post_fills=min_post_fills, z=z,
    )
    verdict["lever"] = lever
    verdict["promotion_event"] = {
        "generated_at_utc": promotion_event.get("generated_at_utc"),
        "operator": promotion_event.get("operator"),
        "action": promotion_event.get("action"),
        "from_state": promotion_event.get("from_state"),
        "to_state": promotion_event.get("to_state"),
        "backup_path": promotion_event.get("backup_path"),
    }
    verdict["post_window_dates"] = {"start": post_start, "end": today_str}
    verdict["grace_days"] = grace_days
    return verdict


def stage2_fast_demote_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    min_post_fills: int = FAST_DEMOTE_MIN_POST_FILLS,
    z: float = FAST_DEMOTE_Z,
    grace_days: int = FAST_DEMOTE_GRACE_DAYS,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    return _per_lever_fast_demote_verdict(
        lever="stage2",
        promotion_event=latest_promotion_event_for_lever(events, "stage2"),
        sessions_dir=sessions_dir, bet_filter=None,
        min_post_fills=min_post_fills, z=z, grace_days=grace_days, today=today,
    )


def stage3_v2_fast_demote_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    min_post_fills: int = FAST_DEMOTE_MIN_POST_FILLS,
    z: float = FAST_DEMOTE_Z,
    grace_days: int = FAST_DEMOTE_GRACE_DAYS,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    return _per_lever_fast_demote_verdict(
        lever="stage3_v2",
        promotion_event=latest_promotion_event_for_lever(events, "stage3_v2"),
        sessions_dir=sessions_dir, bet_filter=None,
        min_post_fills=min_post_fills, z=z, grace_days=grace_days, today=today,
    )


def stake_scaling_fast_demote_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    min_post_fills: int = FAST_DEMOTE_MIN_POST_FILLS,
    z: float = FAST_DEMOTE_Z,
    grace_days: int = FAST_DEMOTE_GRACE_DAYS,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    return _per_lever_fast_demote_verdict(
        lever="stake_scaling",
        promotion_event=latest_promotion_event_for_lever(events, "stake_scaling"),
        sessions_dir=sessions_dir, bet_filter=_stake_scaling_bet_filter,
        min_post_fills=min_post_fills, z=z, grace_days=grace_days, today=today,
    )


def gate_threshold_fast_demote_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    min_post_fills: int = FAST_DEMOTE_MIN_POST_FILLS,
    z: float = FAST_DEMOTE_Z,
    grace_days: int = FAST_DEMOTE_GRACE_DAYS,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    return _per_lever_fast_demote_verdict(
        lever="gate_threshold",
        promotion_event=latest_promotion_event_for_lever(events, "gate_threshold"),
        sessions_dir=sessions_dir, bet_filter=None,
        min_post_fills=min_post_fills, z=z, grace_days=grace_days, today=today,
    )
