"""The `_same_game_multi_fire_health` block (2026-06-03).

Generic plumbing detector for the bug class that ate the 2026-06-02
M_under_paper session: the UNDER pipeline fired 5 paper bets on
TEX@STL 10.5 within 17 seconds because it had zero dedup state
checks. The leak went unnoticed for a day, surfaced only via P&L
pattern-spotting in an audit.

This block scans today's session.bets for any (game_pk, line, side)
group containing more than one bet and surfaces:
  - count of multi-fire groups
  - tightness classification (same-inning vs cross-inning)
  - stake at risk + realized pnl per group

Alerts fire when a same-inning multi-fire is detected (the dedup-leak
fingerprint) or when the total wasted stake on a single multi-fire
group exceeds a threshold. The OVER pipeline's Gate 9 + Gate 10 cap
real placements at one per (game, line, side) by design, so any
multi-fire is a bug; the cross-inning case is softer-signal because
edge-improvement-driven re-firing on a different inning IS supported
by Gate 10's edge_gap escape hatch, but should still be rare.

Reuses no external constants -- a small detector with its own
thresholds. The threshold for "tight" is same-inning (the dedup
fingerprint we already lived through).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple


# Tightness thresholds. Same-inning refires are the dedup-leak
# fingerprint -- the OVER pipeline's Gate 9 (event_dedup_secs=60s
# default) + Gate 10 (inning_dedup_gap=3 with edge-improvement
# unlock) would block them in production code; surfacing them in
# the daily review catches dedup-leak bugs the same day they happen
# rather than days later via P&L pattern-spotting.
SAME_GAME_MULTI_FIRE_ALERT_MIN_GROUPS_TIGHT = 1
SAME_GAME_MULTI_FIRE_ALERT_MIN_GROUPS_LOOSE = 3
SAME_GAME_MULTI_FIRE_WASTED_STAKE_ALERT = 30.0


def _same_game_multi_fire_health(
    *,
    session_date: str,
    bets: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Detect (game_pk, line, side) groups that contain multiple bets
    in today's session. Returns groups, severity tags, and alerts.

    `bets` is the raw `session["bets"]` list (not the summarized rows
    from helpers._summarize_bets) so we have access to game_pk,
    placed_at, and inning directly.
    """
    payload: Dict[str, Any] = {
        "session_date": session_date,
        "alerts": [],
        "groups": [],
        "n_multi_fire_groups": 0,
        "n_tight_groups": 0,
        "n_loose_groups": 0,
        "total_bets_in_multi_fire_groups": 0,
        "total_stake_at_risk": 0.0,
        "total_pnl_in_multi_fire_groups": 0.0,
        "thresholds": {
            "tight_definition": "same_inning",
            "loose_definition": "cross_inning",
            "wasted_stake_alert_usdc": SAME_GAME_MULTI_FIRE_WASTED_STAKE_ALERT,
        },
    }

    grouped: Dict[Tuple[Any, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for bet in bets or []:
        if not isinstance(bet, dict):
            continue
        gpk = bet.get("game_pk")
        ln = bet.get("line")
        side = str(bet.get("side") or "over").lower()
        if gpk is None or ln is None:
            continue
        grouped[(gpk, str(ln), side)].append(bet)

    if not grouped:
        payload["status"] = "no_bets"
        return payload

    multi_fire_groups = {k: v for k, v in grouped.items() if len(v) > 1}
    if not multi_fire_groups:
        payload["status"] = "ok"
        return payload

    n_tight = 0
    n_loose = 0
    total_pnl = 0.0
    total_stake = 0.0
    total_bets_affected = 0
    groups_out: List[Dict[str, Any]] = []
    for (gpk, ln, side), group_bets in sorted(
        multi_fire_groups.items(), key=lambda kv: -len(kv[1]),
    ):
        innings = {
            int(b["inning"]) for b in group_bets
            if isinstance(b.get("inning"), int)
        }
        tightness = (
            "tight" if len(innings) <= 1 else "loose"
        )
        if tightness == "tight":
            n_tight += 1
        else:
            n_loose += 1
        group_stake = sum(
            float(b.get("stake") or 0.0) for b in group_bets
        )
        group_pnl = sum(
            float(b.get("profit") or 0.0)
            for b in group_bets
            if b.get("profit") is not None
        )
        placed_ats = sorted(
            str(b.get("placed_at") or "") for b in group_bets
        )
        # First-to-last gap in seconds when timestamps look ISO-8601.
        spread_seconds = _compute_spread_seconds(placed_ats)
        # Game label (away@home from first bet that has it).
        away = next(
            (b.get("away_abbrev") for b in group_bets if b.get("away_abbrev")),
            "?",
        )
        home = next(
            (b.get("home_abbrev") for b in group_bets if b.get("home_abbrev")),
            "?",
        )
        groups_out.append({
            "game_pk": gpk,
            "matchup": f"{away}@{home}",
            "line": ln,
            "side": side,
            "n_bets": len(group_bets),
            "innings": sorted(innings),
            "tightness": tightness,
            "spread_seconds": spread_seconds,
            "first_placed_at": placed_ats[0] if placed_ats else None,
            "last_placed_at": placed_ats[-1] if placed_ats else None,
            "total_stake": round(group_stake, 2),
            "total_pnl": round(group_pnl, 2),
            "bet_ids": [b.get("bet_id") for b in group_bets],
        })
        total_pnl += group_pnl
        total_stake += group_stake
        total_bets_affected += len(group_bets)

    payload["groups"] = groups_out
    payload["n_multi_fire_groups"] = len(multi_fire_groups)
    payload["n_tight_groups"] = n_tight
    payload["n_loose_groups"] = n_loose
    payload["total_bets_in_multi_fire_groups"] = total_bets_affected
    payload["total_stake_at_risk"] = round(total_stake, 2)
    payload["total_pnl_in_multi_fire_groups"] = round(total_pnl, 2)

    if n_tight >= SAME_GAME_MULTI_FIRE_ALERT_MIN_GROUPS_TIGHT:
        payload["status"] = "alert"
        # Use the worst (highest n_bets) tight group as the headline
        worst_tight = next(
            (g for g in groups_out if g["tightness"] == "tight"), None,
        )
        headline = ""
        if worst_tight:
            spread = worst_tight.get("spread_seconds")
            spread_s = (
                f" within {spread:.0f}s" if isinstance(spread, (int, float))
                and spread is not None and spread <= 600
                else ""
            )
            headline = (
                f" Worst: {worst_tight['matchup']} "
                f"{worst_tight['side'].upper()} {worst_tight['line']} "
                f"fired {worst_tight['n_bets']}x in inning "
                f"{worst_tight['innings']}{spread_s} "
                f"(stake=${worst_tight['total_stake']:.0f}, "
                f"pnl=${worst_tight['total_pnl']:+.2f})."
            )
        payload["alerts"].append(
            f"DEDUP LEAK: {n_tight} same-inning multi-fire group(s) "
            f"detected across {total_bets_affected} bets "
            f"(${total_stake:.0f} stake total).{headline} "
            "Same-(game,line,side) refires within one inning are "
            "the fingerprint of a missing dedup check -- the OVER "
            "pipeline's Gate 9 (event_dedup_secs=60s) + Gate 10 "
            "(inning_dedup_gap=3) cap real placements at one per "
            "group. The 2026-06-02 M_under_paper 5x-fire bug "
            "(TEX@STL 10.5 UNDER, lost $50) had this exact shape."
        )
    elif n_loose >= SAME_GAME_MULTI_FIRE_ALERT_MIN_GROUPS_LOOSE:
        payload["status"] = "alert"
        payload["alerts"].append(
            f"Cross-inning multi-fire: {n_loose} (game,line,side) "
            f"group(s) had >1 bet across multiple innings "
            f"({total_bets_affected} bets total, ${total_stake:.0f} "
            "stake). Gate 10's edge-improvement escape hatch allows "
            "this when the edge materially improves, but the rate is "
            "high enough to audit -- cross-check that each refire "
            "had a real edge improvement vs the prior bet."
        )
    elif total_stake >= SAME_GAME_MULTI_FIRE_WASTED_STAKE_ALERT:
        payload["status"] = "alert"
        payload["alerts"].append(
            f"Multi-fire stake exposure: ${total_stake:.0f} placed "
            f"across {payload['n_multi_fire_groups']} multi-fire "
            f"group(s) (total pnl ${total_pnl:+.2f}). Even when "
            "individual groups don't fire the dedup-leak alert, "
            "the cumulative wasted stake is worth a look."
        )
    else:
        payload["status"] = "ok"

    return payload


def _compute_spread_seconds(
    placed_ats: List[str],
) -> Optional[float]:
    """Return last-minus-first in seconds when both endpoints look
    ISO-8601 with a 'T' separator. Returns None on parse failure --
    the upstream caller treats None as 'unknown', not 'zero'.
    """
    if len(placed_ats) < 2:
        return 0.0
    first = placed_ats[0]
    last = placed_ats[-1]
    if not (first and last):
        return None
    try:
        from datetime import datetime
        # Normalize trailing Z to +00:00 for fromisoformat (Python 3.11
        # accepts Z directly but 3.10 does not; the repo runs 3.11 but
        # the normalization is cheap and future-proof).
        def _parse(s: str):
            s = s.rstrip("Z")
            return datetime.fromisoformat(s)
        delta = _parse(last) - _parse(first)
        return delta.total_seconds()
    except (TypeError, ValueError):
        return None
