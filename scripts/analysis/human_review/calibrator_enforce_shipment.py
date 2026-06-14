"""The `_calibrator_enforce_shipment_health` block.

Extracted from calibration_health.py on 2026-05-25 — at 370 lines
it's the single largest function in that module and is the most
isolated (reads only the per-date candidate JSONL + outcomes JSONL;
no shared helpers with the rest of calibration_health).

Public surface (also re-exported by calibration_health for back-compat):
  - _calibrator_enforce_shipment_health
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .constants import (
    CALIBRATOR_ENFORCE_BAND_GATE_THRESHOLD,
    CALIBRATOR_ENFORCE_BASELINE_MIN_DAYS,
    CALIBRATOR_ENFORCE_BASELINE_WINDOW_DAYS,
    CALIBRATOR_ENFORCE_BLOCKED_NEGATIVE_SAVE_ALERT,
    CALIBRATOR_ENFORCE_BLOCKED_OUTCOMES_DEFAULT_STAKE,
    CALIBRATOR_ENFORCE_BLOCKED_OUTCOMES_MIN_FOR_ALERT,
    CALIBRATOR_ENFORCE_BLOCKED_WR_MUTING_WINNERS,
    CALIBRATOR_ENFORCE_HIGH_BLOCK_RATE_ALERT,
    CALIBRATOR_ENFORCE_HIGH_LINE_CUTOFF,
    CALIBRATOR_ENFORCE_MIN_BAND_GATED_CANDIDATES_FOR_ZERO_ALERT,
    CALIBRATOR_ENFORCE_MIN_EDGE_HIGH_LINE,
    CALIBRATOR_ENFORCE_MIN_EDGE_LOW_LINE,
    CALIBRATOR_ENFORCE_VOLUME_DROP_ALERT_PP,
)
from .helpers import _load_jsonl


def _calibrator_enforce_shipment_health(
    *,
    session_date: str,
    candidate_dir: Path,
    trailing_reviews: List[Dict[str, Any]],
    band_gate_threshold: float = CALIBRATOR_ENFORCE_BAND_GATE_THRESHOLD,
    min_edge_low: float = CALIBRATOR_ENFORCE_MIN_EDGE_LOW_LINE,
    min_edge_high: float = CALIBRATOR_ENFORCE_MIN_EDGE_HIGH_LINE,
    high_line_cutoff: float = CALIBRATOR_ENFORCE_HIGH_LINE_CUTOFF,
) -> Dict[str, Any]:
    """Surface the shipment effect of band-gated calibrator-enforce
    (shipped 2026-05-19, takes effect on next engine boot).

    Two read modes:

    1. Pre-enforce / shadow: session was decided under
       `fair_value_calibration_mode = shadow` so the candidate log
       carries `fair_value_calibrated` alongside raw FV but the
       decision used raw. We REPLAY each `trade` decision against the
       enforce rule (calibrated FV >= ask + min_edge when raw_fv >=
       band_gate_threshold) and count counterfactual blocks.

    2. Post-enforce: attribute today's `skip:gate_min_edge` rows to
       calibrator-enforce by checking which would have passed under
       raw FV.

    Both modes also produce today vs trailing-7d baseline trade
    volume ratio, calibrator applied-share metrics, and a per-raw-fv
    bucket breakdown so the operator can see whether the gate is
    biting at the right tail.
    """
    payload: Dict[str, Any] = {
        "alerts": [],
        "notes": [],
        "thresholds": {
            "band_gate_threshold": band_gate_threshold,
            "min_edge_low_line": min_edge_low,
            "min_edge_high_line": min_edge_high,
            "high_line_cutoff": high_line_cutoff,
            "high_block_rate_alert": CALIBRATOR_ENFORCE_HIGH_BLOCK_RATE_ALERT,
            "volume_drop_alert_pp": CALIBRATOR_ENFORCE_VOLUME_DROP_ALERT_PP,
        },
        "session_date": session_date,
    }

    candidate_path = candidate_dir / f"{session_date}_candidates.jsonl"
    if not candidate_path.exists():
        payload["status"] = "check_error"
        payload["error"] = "candidate log not found"
        payload["candidate_path"] = str(candidate_path)
        return payload
    payload["candidate_path"] = str(candidate_path)

    try:
        rows = _load_jsonl(candidate_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["status"] = "check_error"
        payload["error"] = repr(exc)
        return payload

    modes_seen = sorted({
        str(r.get("fair_value_calibration_mode") or "")
        for r in rows
        if r.get("fair_value_calibration_mode") is not None
    })
    decision_mode = "/".join(m for m in modes_seen if m) or "unknown"
    payload["session_mode_at_decision_time"] = decision_mode
    payload["read_mode"] = (
        "counterfactual" if decision_mode == "shadow" else "attribution"
    )

    trade_rows = [r for r in rows if r.get("decision") == "trade"]
    skip_min_edge_rows = [
        r for r in rows
        if (r.get("decision") in ("skip", "skip_with_features"))
        and r.get("decision_reason") == "gate_min_edge"
    ]

    rows_with_calibrated = [
        r for r in rows
        if r.get("fair_value_calibrated") is not None
        and r.get("fair_value_raw") is not None
    ]
    applied_rows = [
        r for r in rows_with_calibrated
        if bool(r.get("fair_value_calibration_applied"))
    ]
    in_band_rows = [
        r for r in rows_with_calibrated
        if float(r.get("fair_value_raw", 0.0) or 0.0) >= band_gate_threshold
    ]

    def _abs_delta(row: Dict[str, Any]) -> Optional[float]:
        raw = row.get("fair_value_raw")
        cal = row.get("fair_value_calibrated")
        if raw is None or cal is None:
            return None
        try:
            return abs(float(cal) - float(raw))
        except (TypeError, ValueError):
            return None

    in_band_deltas = [
        d for d in (_abs_delta(r) for r in in_band_rows) if d is not None
    ]
    mean_in_band_abs_delta = (
        sum(in_band_deltas) / len(in_band_deltas)
        if in_band_deltas else None
    )

    payload["today"] = {
        "total_candidates_evaluated": len(rows),
        "trade_decisions": len(trade_rows),
        "skip_due_to_gate_min_edge": len(skip_min_edge_rows),
        "calibrator_metrics": {
            "rows_with_calibrated_fv": len(rows_with_calibrated),
            "rows_with_calibration_applied": len(applied_rows),
            "applied_share": (
                len(applied_rows) / len(rows_with_calibrated)
                if rows_with_calibrated else None
            ),
            "in_band_gate_range_count": len(in_band_rows),
            "mean_abs_delta_in_band": mean_in_band_abs_delta,
        },
    }

    if decision_mode == "shadow":
        candidate_pool = trade_rows
        attribution_label = "would_block"
    else:
        candidate_pool = skip_min_edge_rows
        attribution_label = "attributed_to_enforce"

    blocked = 0
    blocked_bucket: Dict[str, int] = {">=0.95": 0, "0.90-0.95": 0, "<0.90": 0}
    preserved_trades_cal_applied = 0
    blocked_rows: List[Dict[str, Any]] = []
    for row in candidate_pool:
        raw_fv = row.get("fair_value_raw")
        cal_fv = row.get("fair_value_calibrated")
        ask = row.get("decision_ask")
        line_raw = row.get("line")
        if (
            raw_fv is None or cal_fv is None
            or ask is None or line_raw is None
        ):
            continue
        try:
            raw_fv = float(raw_fv)
            cal_fv = float(cal_fv)
            ask = float(ask)
            line = float(line_raw)
        except (TypeError, ValueError):
            continue
        if raw_fv < band_gate_threshold:
            continue
        min_edge = (
            min_edge_high if line >= high_line_cutoff else min_edge_low
        )
        post_cal_edge = cal_fv - ask
        if post_cal_edge < min_edge:
            blocked += 1
            if raw_fv >= 0.95:
                blocked_bucket[">=0.95"] += 1
            else:
                blocked_bucket["0.90-0.95"] += 1
            blocked_rows.append({
                "game_pk": row.get("game_pk"),
                "line": line_raw,
                "side": row.get("side"),
                "decision_ask": ask,
                "raw_fv": raw_fv,
                "cal_fv": cal_fv,
            })
        else:
            if decision_mode == "shadow":
                preserved_trades_cal_applied += 1

    pool_size = len(candidate_pool)
    block_rate = (blocked / pool_size) if pool_size else None

    outcomes_path = candidate_dir / f"{session_date}_outcomes.jsonl"
    outcome_lookup: Dict[Tuple[Any, str, str], bool] = {}
    outcomes_status = "loaded"
    if outcomes_path.exists():
        try:
            outcome_rows = _load_jsonl(outcomes_path)
            for o in outcome_rows:
                key = (
                    o.get("game_pk"),
                    str(o.get("line") or ""),
                    str(o.get("side") or "over").lower(),
                )
                ov = o.get("over_hit")
                if ov is not None:
                    outcome_lookup[key] = bool(ov)
                    under_key = (
                        o.get("game_pk"),
                        str(o.get("line") or ""),
                        "under",
                    )
                    if under_key not in outcome_lookup:
                        outcome_lookup[under_key] = (not bool(ov))
        except (OSError, json.JSONDecodeError):
            outcomes_status = "unreadable"
    else:
        outcomes_status = "missing"

    # 2026-06-03 fix: dedup blocked_rows to one entry per
    # (game_pk, line, side) before computing outcomes / counterfactual
    # P&L. Without this, a single game that stays in the band-gated
    # range for 100 ticks contributes 100 "blocks" sharing the same
    # final outcome, inflating both the muting-winners WR alert and
    # the counterfactual saved dollars by 10-100x. The OVER pipeline
    # caps real placements at one per (game, line, side) via Gate 9
    # (same-event 60s) + Gate 10 (cross-inning same-line), so the
    # truthful counterfactual is also one bet per group.
    #
    # Example from the 2026-06-01 audit: report showed 533 blocks /
    # 533 would-win / saved=-$1106.53. Dedup-corrected: 15 unique
    # opportunities / 15 would-win / saved=-$36 -- same WR (100%) but
    # ~30x smaller counterfactual P&L.
    #
    # Picking strategy: for each (game, line, side) group, keep the
    # row with the largest raw_edge (raw_fv - decision_ask). That's
    # the moment the bot would have most wanted to fire, matching
    # OVER's "edge improvement unlocks dedup" semantics.
    raw_blocked_count = blocked
    deduped_best: Dict[Tuple[Any, str, str], Dict[str, Any]] = {}
    for br in blocked_rows:
        key = (
            br.get("game_pk"),
            str(br.get("line") or ""),
            str(br.get("side") or "over").lower(),
        )
        try:
            cand_edge = (
                float(br.get("raw_fv") or 0.0)
                - float(br.get("decision_ask") or 0.0)
            )
        except (TypeError, ValueError):
            cand_edge = float("-inf")
        cur = deduped_best.get(key)
        if cur is None:
            deduped_best[key] = br
            continue
        try:
            cur_edge = (
                float(cur.get("raw_fv") or 0.0)
                - float(cur.get("decision_ask") or 0.0)
            )
        except (TypeError, ValueError):
            cur_edge = float("-inf")
        if cand_edge > cur_edge:
            deduped_best[key] = br
    deduped_blocked_rows = list(deduped_best.values())
    unique_blocked_count = len(deduped_blocked_rows)
    blocks_per_opportunity = (
        raw_blocked_count / unique_blocked_count
        if unique_blocked_count else None
    )

    settled = 0
    would_win = 0
    would_lose = 0
    undecided = 0
    saved_dollars = 0.0
    stake = CALIBRATOR_ENFORCE_BLOCKED_OUTCOMES_DEFAULT_STAKE
    # 2026-06-14: stratify blocked outcomes by raw-FV band. The pooled
    # WR / counterfactual lumps the toxic [0.95,1.0) tail (realized
    # ~-17% ROI per the 2026-06-13 edge-shaving deep dive) together
    # with the ~breakeven [0.90,0.95) band. That made the "muting
    # winners" alert fire indiscriminately and disagree with the
    # band-stratified edge_shaving verdict (which said the floor should
    # move 0.90 -> 0.95, not that the gate is broken). Splitting the
    # outcomes shows WHICH band is being muted, so the alert can point
    # at "raise enforce_min_raw to 0.95" instead of a blanket refit.
    band_stats: Dict[str, Dict[str, Any]] = {
        "0.90-0.95": {
            "settled": 0, "would_win": 0, "would_lose": 0,
            "saved_dollars": 0.0,
        },
        ">=0.95": {
            "settled": 0, "would_win": 0, "would_lose": 0,
            "saved_dollars": 0.0,
        },
    }
    for br in deduped_blocked_rows:
        side = str(br.get("side") or "over").lower()
        key = (br.get("game_pk"), str(br.get("line") or ""), side)
        ov = outcome_lookup.get(key)
        if ov is None:
            undecided += 1
            continue
        try:
            raw_fv_row = float(br.get("raw_fv") or 0.0)
        except (TypeError, ValueError):
            raw_fv_row = 0.0
        bs = band_stats[">=0.95" if raw_fv_row >= 0.95 else "0.90-0.95"]
        won_if_placed = bool(ov)
        settled += 1
        bs["settled"] += 1
        if won_if_placed:
            would_win += 1
            bs["would_win"] += 1
            ask_d = float(br.get("decision_ask") or 0.0)
            if ask_d > 0:
                lost_profit = (stake / ask_d) - stake
                saved_dollars -= lost_profit
                bs["saved_dollars"] -= lost_profit
        else:
            would_lose += 1
            bs["would_lose"] += 1
            saved_dollars += stake
            bs["saved_dollars"] += stake
    wr_settled = (would_win / settled) if settled else None
    for _bs in band_stats.values():
        _bs["saved_dollars"] = round(_bs["saved_dollars"], 2)
        _bs["win_rate_among_settled"] = (
            _bs["would_win"] / _bs["settled"] if _bs["settled"] else None
        )

    payload["today"]["enforce_effect"] = {
        "attribution_label": attribution_label,
        "candidate_pool_size": pool_size,
        "blocked_count": blocked,
        "blocked_rate": block_rate,
        "blocked_by_raw_fv_bucket": blocked_bucket,
        # 2026-06-03: dedup transparency. `unique_blocked_opportunities`
        # is the count after dedup-by-(game,line,side); the outcomes /
        # counterfactual P&L below are computed on this set. The raw
        # `blocked_count` is preserved above for backward-compat and
        # for diagnosing how many tick-rows the gate fired on (a high
        # blocks_per_opportunity ratio means the bot would have been
        # heavily dedup-suppressed even without calibrator-enforce).
        "unique_blocked_opportunities": unique_blocked_count,
        "blocks_per_opportunity": blocks_per_opportunity,
        "preserved_trades_with_calibrator_applied": (
            preserved_trades_cal_applied
        ),
        "blocked_outcomes": {
            "outcomes_source_status": outcomes_status,
            "outcomes_path": str(outcomes_path),
            "settled_count": settled,
            "would_have_won": would_win,
            "would_have_lost": would_lose,
            "undecided_count": undecided,
            "win_rate_among_settled": wr_settled,
            "counterfactual_pnl": {
                "saved_dollars": round(saved_dollars, 2),
                "default_stake": stake,
                "computed_on": "unique_opportunities_dedup_by_game_line_side",
            },
            # Per raw-FV band, so the operator can see the toxic
            # [0.95,1.0) tail (enforce JUSTIFIED) separately from the
            # ~breakeven [0.90,0.95) band (the one enforce mutes). A
            # negative saved_dollars on [0.90,0.95) with a positive one
            # on >=0.95 is the signature that says "raise the floor".
            "by_raw_fv_band": band_stats,
        },
    }

    trade_counts: List[int] = []
    for r in trailing_reviews:
        bt = r.get("bet_totals") or {}
        n = bt.get("count")
        if isinstance(n, (int, float)) and n > 0:
            trade_counts.append(int(n))
    baseline_days = len(trade_counts)
    mean_daily_trades = (
        sum(trade_counts) / baseline_days if baseline_days else None
    )
    volume_ratio = None
    if mean_daily_trades and mean_daily_trades > 0:
        volume_ratio = len(trade_rows) / mean_daily_trades
    payload["trailing_baseline"] = {
        "window_days": CALIBRATOR_ENFORCE_BASELINE_WINDOW_DAYS,
        "baseline_days_used": baseline_days,
        "mean_daily_trades": mean_daily_trades,
        "today_trades": len(trade_rows),
        "today_volume_ratio_vs_baseline": volume_ratio,
    }

    if pool_size == 0:
        payload["status"] = "no_candidate_pool"
    elif blocked == 0 and len(in_band_rows) >= (
        CALIBRATOR_ENFORCE_MIN_BAND_GATED_CANDIDATES_FOR_ZERO_ALERT
    ):
        payload["status"] = "alert"
        payload["alerts"].append(
            f"calibrator-enforce blocked 0 bets despite "
            f"{len(in_band_rows)} candidates in the band-gated range "
            f"(raw_fv >= {band_gate_threshold:.2f}); calibrator may be "
            "returning identity, OR every in-band candidate had "
            "calibrated edge still above min_edge -- cross-check "
            "calibration_health.artifact_methods_by_family."
        )
    elif (
        block_rate is not None
        and block_rate >= CALIBRATOR_ENFORCE_HIGH_BLOCK_RATE_ALERT
        and pool_size >= 5
    ):
        payload["status"] = "alert"
        label = "would_block" if decision_mode == "shadow" else "blocked"
        # The pool_size + blocked counts are TICK-ROWS (many per game).
        # Surface the dedup-corrected unique-opportunity count alongside
        # so the operator can see both: the high block_rate is real (the
        # gate fires on a lot of evaluations) but the actual betting
        # opportunities affected is smaller.
        bpo_clause = (
            f", spanning {unique_blocked_count} unique "
            f"(game,line,side) opportunities"
            f" (avg {blocks_per_opportunity:.1f} ticks/opportunity)"
            if unique_blocked_count else ""
        )
        payload["alerts"].append(
            f"calibrator-enforce {label} {blocked}/{pool_size} "
            f"({block_rate:.0%}) of candidates today{bpo_clause} (>= "
            f"{CALIBRATOR_ENFORCE_HIGH_BLOCK_RATE_ALERT:.0%} alert "
            "threshold); gate may be too aggressive for the current "
            "regime. Cross-check concept_drift_health -- if PSI is "
            "major on stage2/team_offense, the calibrator was trained "
            "on a distribution that's no longer current."
        )
    else:
        payload["status"] = "ok"

    if (
        volume_ratio is not None
        and baseline_days >= CALIBRATOR_ENFORCE_BASELINE_MIN_DAYS
        and (1.0 - volume_ratio) >= CALIBRATOR_ENFORCE_VOLUME_DROP_ALERT_PP
        and decision_mode != "shadow"
    ):
        payload["alerts"].append(
            f"trade volume dropped "
            f"{(1.0 - volume_ratio) * 100:.0f}pp: today {len(trade_rows)} "
            f"vs trailing-{baseline_days}d mean "
            f"{mean_daily_trades:.1f}/day. If this is the first "
            "post-enforce day, expect a step-down; check the "
            "blocked-rate above to confirm the drop is "
            "calibrator-attributable."
        )

    b_low = band_stats["0.90-0.95"]
    b_high = band_stats[">=0.95"]
    band_clause = (
        f" By raw-FV band: [0.90,0.95) {b_low['would_win']}/"
        f"{b_low['settled']} won, saved ${b_low['saved_dollars']:+.2f}; "
        f"[0.95,1.0) {b_high['would_win']}/{b_high['settled']} won, "
        f"saved ${b_high['saved_dollars']:+.2f}."
    )
    # Signature of "raise the floor, don't refit": the breakeven
    # [0.90,0.95) band is net-negative to block (muting winners) while
    # the [0.95,1.0) tail is net-positive to block (correctly toxic).
    floor_raise_indicated = (
        b_low["saved_dollars"] < 0 <= b_high["saved_dollars"]
        and b_low["settled"] > 0
    )
    if floor_raise_indicated:
        band_clause += (
            " The mute is concentrated in [0.90,0.95) while the "
            "[0.95,1.0) tail is correctly blocked -- raise "
            "enforce_min_raw to 0.95 (cf. L_enforce_min_raw_095 + the "
            "edge-shaving deep dive), not a full refit."
        )
    if (
        settled >= CALIBRATOR_ENFORCE_BLOCKED_OUTCOMES_MIN_FOR_ALERT
        and wr_settled is not None
        and wr_settled >= CALIBRATOR_ENFORCE_BLOCKED_WR_MUTING_WINNERS
    ):
        payload["alerts"].append(
            f"calibrator-enforce may be muting winners: would-block WR "
            f"is {would_win}/{settled} ({wr_settled:.0%}) on unique "
            f"(game,line,side) opportunities >= "
            f"{CALIBRATOR_ENFORCE_BLOCKED_WR_MUTING_WINNERS:.0%} alert "
            "threshold. The gate is blocking bets that win at a rate "
            "close to the post-calibrated break-even, suggesting the "
            "Platt fit is too aggressive at the current regime "
            "(consider band-gate raise or per-line refit)."
            + band_clause
        )
    if (
        settled >= CALIBRATOR_ENFORCE_BLOCKED_OUTCOMES_MIN_FOR_ALERT
        and saved_dollars < CALIBRATOR_ENFORCE_BLOCKED_NEGATIVE_SAVE_ALERT
    ):
        payload["alerts"].append(
            f"calibrator-enforce blocking is net-NEGATIVE on outcomes: "
            f"counterfactual saved=${saved_dollars:+.2f} over "
            f"{settled} unique (game,line,side) opportunities "
            f"(would-win={would_win}, would-lose={would_lose}). The "
            "blocked set was profitable in expectation; the gate's "
            "blocking the wrong tail."
        )

    return payload
