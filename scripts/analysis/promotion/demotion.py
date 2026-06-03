"""Window-based demotion verdicts: outcome-based "did the promotion help?"

Each lever's demotion verdict reads the most recent promotion event for
that lever, computes filled-bet outcomes for the K days BEFORE and AFTER
the promotion timestamp, and fires `demote` when post is materially
worse than pre. Symmetric to the promotion stability gate, but a single
post-hoc test rather than a multi-day stability sequence.

Constants picked for ~3.4 fills/day baseline rate: 14d windows give
~48 expected fills per side; min_filled=10 catches even sparse weeks.
ROI regression threshold of 10pp is large enough that small-sample noise
doesn't false-trigger but small enough to catch a truly bad promotion.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import constants as _constants
from .constants import (
    DEMOTE_MIN_FILLED_PER_WINDOW,
    DEMOTE_PRE_POST_WINDOW_DAYS,
    DEMOTE_ROI_REGRESSION_THRESHOLD,
)
from .events import latest_promotion_event_for_lever


def _parse_iso_date(ts: str) -> Optional[datetime]:
    """Parse the YYYY-MM-DD prefix of an ISO timestamp string."""
    try:
        return datetime.strptime(str(ts)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _shift_date(date_str: str, days: int) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


def _list_session_dates_in_window(
    sessions_dir: Path, start_date: str, end_date: str
) -> List[str]:
    """Return YYYY-MM-DD dates of session files whose names fall in
    [start_date, end_date]. Inclusive on both ends."""
    if not sessions_dir.exists():
        return []
    out: List[str] = []
    import re as _re
    pat = _re.compile(r"^(\d{4}-\d{2}-\d{2})_session\.json$")
    for child in sessions_dir.iterdir():
        m = pat.match(child.name)
        if not m:
            continue
        d = m.group(1)
        if start_date <= d <= end_date:
            out.append(d)
    return sorted(out)


def _filled_bets_in_window(
    sessions_dir: Path,
    start_date: str,
    end_date: str,
    bet_filter: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Load filled bets across session files in [start_date, end_date].

    Excludes paper-fallback bets (we want REAL-money outcomes for the
    demotion verdict; paper fallbacks have no real P&L attached). If
    `bet_filter(bet) -> bool` is supplied, only bets passing the filter
    are kept (used by stake-scaling to filter to multiplier-affected bets).
    """
    out: List[Dict[str, Any]] = []
    for date in _list_session_dates_in_window(sessions_dir, start_date, end_date):
        path = sessions_dir / f"{date}_session.json"
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for bet in session.get("bets") or []:
            if str(bet.get("placement_mode") or "live") != "live":
                continue
            if str(bet.get("order_status") or "") != "filled":
                continue
            if bet_filter is not None and not bet_filter(bet):
                continue
            out.append(bet)
    return out


def _summarize_filled_bets(bets: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate ROI / WR / counts. Used for both pre and post windows."""
    n = len(bets)
    wins = sum(1 for b in bets if (float(b.get("profit") or 0)) > 0)
    losses = sum(1 for b in bets if (float(b.get("profit") or 0)) < 0)
    profit = sum(float(b.get("profit") or 0) for b in bets)
    stake = sum(float(b.get("fill_cost") or b.get("stake") or 0) for b in bets)
    roi = (profit / stake) if stake else None
    wr = (wins / n) if n else None
    return {
        "n_filled": n,
        "wins": wins,
        "losses": losses,
        "total_profit": round(profit, 2),
        "total_stake": round(stake, 2),
        "roi": round(roi, 4) if roi is not None else None,
        "wr": round(wr, 4) if wr is not None else None,
    }


def _demotion_verdict_from_summaries(
    pre_summary: Dict[str, Any],
    post_summary: Dict[str, Any],
    *,
    min_filled: int = DEMOTE_MIN_FILLED_PER_WINDOW,
    regression_threshold: float = DEMOTE_ROI_REGRESSION_THRESHOLD,
) -> Dict[str, Any]:
    """Common verdict logic: enough data on both sides + post-pre ROI
    regression past threshold => demote, else hold."""
    base: Dict[str, Any] = {
        "pre_window": pre_summary,
        "post_window": post_summary,
        "min_filled_per_window": min_filled,
        "regression_threshold": regression_threshold,
    }
    if pre_summary["n_filled"] < min_filled:
        return {**base, "verdict": "insufficient_pre_data"}
    if post_summary["n_filled"] < min_filled:
        return {**base, "verdict": "insufficient_post_data"}
    pre_roi = pre_summary["roi"]
    post_roi = post_summary["roi"]
    if pre_roi is None or post_roi is None:
        return {**base, "verdict": "insufficient_pre_data"}
    delta = post_roi - pre_roi
    base["roi_delta"] = round(delta, 4)
    if delta <= regression_threshold:
        return {**base, "verdict": "demote"}
    return {**base, "verdict": "hold"}


def _per_lever_demotion_verdict(
    *,
    lever: str,
    promotion_event: Optional[Dict[str, Any]],
    sessions_dir: Path,
    bet_filter: Optional[Any] = None,
    window_days: int = DEMOTE_PRE_POST_WINDOW_DAYS,
    min_filled: int = DEMOTE_MIN_FILLED_PER_WINDOW,
    regression_threshold: float = DEMOTE_ROI_REGRESSION_THRESHOLD,
) -> Dict[str, Any]:
    """Compute a demotion verdict for one lever from its most recent
    promotion event + session bet outcomes."""
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
    pre_start = _shift_date(pdate, -window_days)
    pre_end = _shift_date(pdate, -1)
    post_start = pdate
    post_end = _shift_date(pdate, window_days - 1)
    pre_bets = _filled_bets_in_window(sessions_dir, pre_start, pre_end, bet_filter=bet_filter)
    post_bets = _filled_bets_in_window(sessions_dir, post_start, post_end, bet_filter=bet_filter)
    verdict = _demotion_verdict_from_summaries(
        _summarize_filled_bets(pre_bets),
        _summarize_filled_bets(post_bets),
        min_filled=min_filled,
        regression_threshold=regression_threshold,
    )
    verdict["lever"] = lever
    verdict["promotion_event"] = {
        # Compact subset; full event is already in the audit log.
        "generated_at_utc": promotion_event.get("generated_at_utc"),
        "operator": promotion_event.get("operator"),
        "action": promotion_event.get("action"),
        "from_state": promotion_event.get("from_state"),
        "to_state": promotion_event.get("to_state"),
        "backup_path": promotion_event.get("backup_path"),
    }
    verdict["pre_window_dates"] = {"start": pre_start, "end": pre_end}
    verdict["post_window_dates"] = {"start": post_start, "end": post_end}
    return verdict


# Per-lever bet filters. stage2/stage3-v2 affect every prediction so no
# filter; stake-scaling only affects bets where the multiplier deviated
# from 1.0; gate-threshold uses overall ROI as a proxy (correctly attributing
# "would have been blocked by old threshold" is messy and at our sample
# sizes the noise from the overall comparison is similar).
def _stake_scaling_bet_filter(bet: Dict[str, Any]) -> bool:
    m = bet.get("calibrated_stake_multiplier")
    if m is None:
        return False
    try:
        return abs(float(m) - 1.0) > 1e-6
    except (TypeError, ValueError):
        return False


def stage2_demotion_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    window_days: int = DEMOTE_PRE_POST_WINDOW_DAYS,
    min_filled: int = DEMOTE_MIN_FILLED_PER_WINDOW,
    regression_threshold: float = DEMOTE_ROI_REGRESSION_THRESHOLD,
) -> Dict[str, Any]:
    return _per_lever_demotion_verdict(
        lever="stage2",
        promotion_event=latest_promotion_event_for_lever(events, "stage2"),
        sessions_dir=sessions_dir,
        bet_filter=None,
        window_days=window_days,
        min_filled=min_filled,
        regression_threshold=regression_threshold,
    )


def stage3_v2_demotion_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    window_days: int = DEMOTE_PRE_POST_WINDOW_DAYS,
    min_filled: int = DEMOTE_MIN_FILLED_PER_WINDOW,
    regression_threshold: float = DEMOTE_ROI_REGRESSION_THRESHOLD,
) -> Dict[str, Any]:
    return _per_lever_demotion_verdict(
        lever="stage3_v2",
        promotion_event=latest_promotion_event_for_lever(events, "stage3_v2"),
        sessions_dir=sessions_dir,
        bet_filter=None,
        window_days=window_days,
        min_filled=min_filled,
        regression_threshold=regression_threshold,
    )


def stake_scaling_demotion_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    window_days: int = DEMOTE_PRE_POST_WINDOW_DAYS,
    min_filled: int = DEMOTE_MIN_FILLED_PER_WINDOW,
    regression_threshold: float = DEMOTE_ROI_REGRESSION_THRESHOLD,
) -> Dict[str, Any]:
    return _per_lever_demotion_verdict(
        lever="stake_scaling",
        promotion_event=latest_promotion_event_for_lever(events, "stake_scaling"),
        sessions_dir=sessions_dir,
        bet_filter=_stake_scaling_bet_filter,
        window_days=window_days,
        min_filled=min_filled,
        regression_threshold=regression_threshold,
    )


def gate_threshold_demotion_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    window_days: int = DEMOTE_PRE_POST_WINDOW_DAYS,
    min_filled: int = DEMOTE_MIN_FILLED_PER_WINDOW,
    regression_threshold: float = DEMOTE_ROI_REGRESSION_THRESHOLD,
) -> Dict[str, Any]:
    # gate-threshold demote-verdict uses overall ROI as a proxy. A more
    # precise version would filter to bets allowed by the new threshold
    # but blocked by the old, but at our sample sizes (~3.4 fills/day)
    # filtering further makes the verdict undecidable. Operator can use
    # the broader signal, then inspect cohort-ROI alerts for finer detail.
    return _per_lever_demotion_verdict(
        lever="gate_threshold",
        promotion_event=latest_promotion_event_for_lever(events, "gate_threshold"),
        sessions_dir=sessions_dir,
        bet_filter=None,
        window_days=window_days,
        min_filled=min_filled,
        regression_threshold=regression_threshold,
    )
