import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Sequence

from .constants import (
    DRIFT_MIN_TODAY_SAMPLE,
    DRIFT_MIN_BASELINE_SAMPLE,
    DRIFT_FILL_RATE_DROP_PP,
    DRIFT_WIN_RATE_DROP_PP,
    DRIFT_ZERO_DAY_MIN_SAMPLE,
    RECONCILER_HIGH_SHARE,
    GATE_COUNTERFACTUAL_STALE_AGE_DAYS,
    GATE_COUNTERFACTUAL_NOTES_MIN_DELTA_USD,
    GATE_COUNTERFACTUAL_NOTES_MAX_ALERTS,
    LOSS_ATTRIBUTION_STALE_AGE_DAYS,
    LOSS_ATTRIBUTION_NOTES_MIN_ABS_BIAS,
    LOSS_ATTRIBUTION_NOTES_MIN_SHARE,
)
from .helpers import (
    _load_json,
    _load_jsonl,
    _safe_float,
    _safe_int,
    _line_key,
    _wilson_upper_bound,
    _artifact_age_days,
)

def _count_log_health(log_path: Path) -> Dict[str, Any]:
    counts = Counter()
    if not log_path.exists():
        return {"log_path": str(log_path), "exists": False, "counts": dict(counts)}

    with log_path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if "Schedule refreshed" in line:
                counts["schedule_refreshed"] += 1
            if "Polling " in line and " token books" in line:
                counts["polling_token_books"] += 1
            if "Wrote " in line and " tick snapshots" in line:
                counts["wrote_tick_snapshots"] += 1
            if "Retiring book polling" in line:
                counts["retiring_book_polling"] += 1
            if "WARNING" in line:
                counts["warnings"] += 1
            if "ERROR" in line:
                counts["errors"] += 1
            if "fresh execution book missing" in line:
                counts["fresh_book_unavailable"] += 1

    return {"log_path": str(log_path), "exists": True, "counts": dict(counts)}


def _stage2_suppression_dollar_audit(
    *,
    session_date: str,
    candidate_dir: Path,
    stake_usdc: float,
) -> Dict[str, Any]:
    candidate_path = candidate_dir / f"{session_date}_candidates.jsonl"
    outcome_path = candidate_dir / f"{session_date}_outcomes.jsonl"
    raw_candidates = _load_jsonl(candidate_path)
    outcomes = _load_jsonl(outcome_path)
    outcome_by_line = {
        (
            str(row.get("mode") or "live"),
            str(row.get("session_date") or session_date),
            _safe_int(row.get("game_pk"), -1),
            _line_key(row.get("line")),
        ): row
        for row in outcomes
    }

    stage2_rows = [
        row for row in raw_candidates
        if "stage2_suppression" in str(row.get("decision_reason") or "")
    ]
    deduped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for row in stage2_rows:
        ask = _safe_float(row.get("decision_ask"), None)  # type: ignore[arg-type]
        key = (
            str(row.get("mode") or "live"),
            str(row.get("session_date") or session_date),
            _safe_int(row.get("game_pk"), -1),
            _line_key(row.get("line")),
            row.get("inning"),
            row.get("inning_state"),
            row.get("outs"),
            row.get("away_score_before"),
            row.get("home_score_before"),
            row.get("runners_on"),
            round(ask, 2) if ask is not None else None,
            row.get("decision_reason"),
        )
        deduped.setdefault(key, row)

    labeled = 0
    wins = 0
    losses = 0
    invalid_price = 0
    missing_outcome = 0
    blocked_winning_profit = 0.0
    blocked_losing_cost = 0.0
    net_profit = 0.0
    examples: List[Dict[str, Any]] = []

    for row in deduped.values():
        ask = _safe_float(row.get("decision_ask"), None)  # type: ignore[arg-type]
        if ask is None or ask <= 0:
            invalid_price += 1
            continue
        key = (
            str(row.get("mode") or "live"),
            str(row.get("session_date") or session_date),
            _safe_int(row.get("game_pk"), -1),
            _line_key(row.get("line")),
        )
        outcome = outcome_by_line.get(key)
        if not outcome:
            missing_outcome += 1
            continue
        labeled += 1
        over_hit = bool(outcome.get("over_hit"))
        shares = stake_usdc / ask
        profit = shares - stake_usdc if over_hit else -stake_usdc
        net_profit += profit
        if over_hit:
            wins += 1
            blocked_winning_profit += profit
        else:
            losses += 1
            blocked_losing_cost += stake_usdc
        if len(examples) < 8:
            examples.append({
                "game": f"{row.get('away_abbrev', '?')}@{row.get('home_abbrev', '?')}",
                "game_pk": row.get("game_pk"),
                "line": row.get("line"),
                "inning": row.get("inning"),
                "inning_state": row.get("inning_state"),
                "ask": ask,
                "fair_value": row.get("fair_value"),
                "stage2_run_env_delta": row.get("stage2_run_env_delta"),
                "over_hit": over_hit,
                "final_total": outcome.get("final_total"),
                "hypothetical_profit_usdc": round(profit, 2),
            })

    return {
        "description": (
            "Shadow dollar audit for rows blocked by gate_stage2_suppression. "
            "Profit is hypothetical taker-at-ask with the session stake; this is diagnostic only."
        ),
        "stake_usdc": round(stake_usdc, 2),
        "candidate_path": str(candidate_path),
        "outcome_path": str(outcome_path),
        "raw_rows": len(stage2_rows),
        "deduped_rows": len(deduped),
        "labeled_rows": labeled,
        "missing_outcome_rows": missing_outcome,
        "invalid_price_rows": invalid_price,
        "blocked_winning_rows": wins,
        "blocked_losing_rows": losses,
        "blocked_winning_profit_usdc": round(blocked_winning_profit, 2),
        "blocked_losing_cost_usdc": round(blocked_losing_cost, 2),
        "net_hypothetical_profit_usdc": round(net_profit, 2),
        "examples": examples,
    }


def _fill_rate_health(
    *,
    today_bet_totals: Dict[str, Any],
    trailing_reviews: List[Dict[str, Any]],
    session_mode: Optional[str] = None,
) -> Dict[str, Any]:
    # Paper mode simulates 100% fill at the ask; the fill-rate counters
    # are wired only on the live execution path. Suppress alerts when the
    # session is anything other than live so paper reviews stop firing
    # false-alarm "fill rate dropped" + "zero-fill day" notes that have
    # no actionable diagnostic value in paper mode.
    if session_mode != "live":
        return {
            "today": {
                "placed": _safe_int(today_bet_totals.get("count")),
                "filled": _safe_int(today_bet_totals.get("filled")),
                "rate": None,
            },
            "baseline": {"placed": 0, "filled": 0, "rate": None, "days": 0},
            "alerts": [],
            "status": "paper_mode_skipped" if session_mode == "paper" else "non_live_skipped",
            "session_mode": session_mode,
        }
    today_placed = _safe_int(today_bet_totals.get("count"))
    today_filled = _safe_int(today_bet_totals.get("filled"))
    today_rate = (today_filled / today_placed) if today_placed else None

    base_placed = 0
    base_filled = 0
    days_in_baseline = 0
    for review in trailing_reviews:
        bt = review.get("bet_totals") or {}
        placed = _safe_int(bt.get("count"))
        filled = _safe_int(bt.get("filled"))
        if placed > 0:
            base_placed += placed
            base_filled += filled
            days_in_baseline += 1
    base_rate = (base_filled / base_placed) if base_placed > 0 else None

    alerts: List[str] = []
    if (
        today_placed >= DRIFT_MIN_TODAY_SAMPLE
        and today_rate is not None
        and base_rate is not None
        and base_placed >= DRIFT_MIN_BASELINE_SAMPLE
    ):
        delta = today_rate - base_rate
        wilson_ub = _wilson_upper_bound(today_filled, today_placed)
        is_significant = (
            wilson_ub is not None and base_rate is not None
            and wilson_ub < base_rate
        )
        if delta <= -DRIFT_FILL_RATE_DROP_PP and is_significant:
            alerts.append(
                f"fill rate dropped {abs(delta) * 100:.0f}pp: "
                f"{today_filled}/{today_placed} ({today_rate:.0%}) today vs "
                f"{base_filled}/{base_placed} ({base_rate:.0%}) over trailing "
                f"{days_in_baseline} day(s) [Wilson UB={wilson_ub:.0%} < baseline]; "
                "investigate execution path "
                "(orphan fills, CLOB SDK errors, queue position)."
            )
    if today_placed >= DRIFT_ZERO_DAY_MIN_SAMPLE and today_rate == 0.0:
        alerts.append(
            f"zero-fill day: 0/{today_placed} placed bets filled. "
            "Check live_orders_ledger.jsonl for cancel reasons and "
            "reconciled_filled rows."
        )

    return {
        "today": {
            "placed": today_placed,
            "filled": today_filled,
            "fill_rate": round(today_rate, 4) if today_rate is not None else None,
        },
        "baseline": {
            "placed": base_placed,
            "filled": base_filled,
            "fill_rate": round(base_rate, 4) if base_rate is not None else None,
            "days_in_baseline": days_in_baseline,
        },
        "thresholds": {
            "min_today_sample": DRIFT_MIN_TODAY_SAMPLE,
            "min_baseline_sample": DRIFT_MIN_BASELINE_SAMPLE,
            "max_drop_pp": DRIFT_FILL_RATE_DROP_PP,
        },
        "alerts": alerts,
    }


def _signal_quality_health(
    *,
    today_bet_totals: Dict[str, Any],
    trailing_reviews: List[Dict[str, Any]],
) -> Dict[str, Any]:
    today_filled = _safe_int(today_bet_totals.get("filled"))
    today_wins = _safe_int(today_bet_totals.get("wins"))
    today_wr = (today_wins / today_filled) if today_filled else None

    base_filled = 0
    base_wins = 0
    days_in_baseline = 0
    for review in trailing_reviews:
        bt = review.get("bet_totals") or {}
        filled = _safe_int(bt.get("filled"))
        wins = _safe_int(bt.get("wins"))
        if filled > 0:
            base_filled += filled
            base_wins += wins
            days_in_baseline += 1
    base_wr = (base_wins / base_filled) if base_filled > 0 else None

    alerts: List[str] = []
    if (
        today_filled >= DRIFT_MIN_TODAY_SAMPLE
        and today_wr is not None
        and base_wr is not None
        and base_filled >= DRIFT_MIN_BASELINE_SAMPLE
    ):
        delta = today_wr - base_wr
        wilson_ub = _wilson_upper_bound(today_wins, today_filled)
        is_significant = (
            wilson_ub is not None and base_wr is not None
            and wilson_ub < base_wr
        )
        if delta <= -DRIFT_WIN_RATE_DROP_PP and is_significant:
            alerts.append(
                f"filled win rate dropped {abs(delta) * 100:.0f}pp: "
                f"{today_wins}/{today_filled} ({today_wr:.0%}) today vs "
                f"{base_wins}/{base_filled} ({base_wr:.0%}) over trailing "
                f"{days_in_baseline} day(s) [Wilson UB={wilson_ub:.0%} < baseline]; "
                "review FV signal quality "
                "and recent gate changes."
            )
    if today_filled >= DRIFT_ZERO_DAY_MIN_SAMPLE and today_wr == 0.0:
        alerts.append(
            f"zero-win day: 0/{today_filled} filled bets won. "
            "Investigate phantom-risk and current-state-edge cohorts."
        )

    return {
        "today": {
            "filled": today_filled,
            "wins": today_wins,
            "win_rate": round(today_wr, 4) if today_wr is not None else None,
        },
        "baseline": {
            "filled": base_filled,
            "wins": base_wins,
            "win_rate": round(base_wr, 4) if base_wr is not None else None,
            "days_in_baseline": days_in_baseline,
        },
        "thresholds": {
            "min_today_sample": DRIFT_MIN_TODAY_SAMPLE,
            "min_baseline_sample": DRIFT_MIN_BASELINE_SAMPLE,
            "max_drop_pp": DRIFT_WIN_RATE_DROP_PP,
        },
        "alerts": alerts,
    }


def _reconciler_summary(session_bets: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    bets_list = list(session_bets)
    filled_total = 0
    reconciled_total = 0
    by_source: Counter = Counter()
    examples: List[Dict[str, Any]] = []
    for bet in bets_list:
        status = str(bet.get("order_status") or "")
        if status == "filled":
            filled_total += 1
        source = bet.get("reconciliation_source")
        if not source:
            continue
        reconciled_total += 1
        by_source[str(source)] += 1
        if len(examples) < 8:
            examples.append({
                "bet_id": bet.get("bet_id"),
                "game": f"{bet.get('away_abbrev', '?')}@{bet.get('home_abbrev', '?')}",
                "line": bet.get("line"),
                "reconciliation_source": source,
                "reconciliation_trade_id": bet.get("reconciliation_trade_id"),
                "fill_price": bet.get("actual_fill_price") or bet.get("fill_price"),
            })

    reconciled_share = (
        reconciled_total / filled_total if filled_total > 0 else None
    )
    alerts: List[str] = []
    if (
        reconciled_share is not None
        and reconciled_share >= RECONCILER_HIGH_SHARE
        and filled_total >= 3
    ):
        alerts.append(
            f"orphan-fill reconciler recovered {reconciled_total}/{filled_total} "
            f"({reconciled_share:.0%}) of today's fills "
            f"(>= {RECONCILER_HIGH_SHARE:.0%} threshold). "
            "If this persists, consider promoting the public data-api to the "
            "primary fill source (see Active #2)."
        )
    return {
        "filled_total": filled_total,
        "reconciled_total": reconciled_total,
        "reconciled_share": (
            round(reconciled_share, 4) if reconciled_share is not None else None
        ),
        "by_source": dict(by_source),
        "examples": examples,
        "threshold_high_share": RECONCILER_HIGH_SHARE,
        "alerts": alerts,
    }


def _fast_demote_health(
    *,
    audit_log_path: Path,
    sessions_dir: Path,
    today: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "audit_log_path": str(audit_log_path),
        "sessions_dir": str(sessions_dir),
        "today": today,
        "alerts": [],
        "verdicts": {},
    }
    _promote = None
    try:
        from scripts.analysis import promote as _promote
    except ImportError:
        try:
            import promote as _promote  # type: ignore[no-redef]
        except ImportError:
            payload["alerts"].append(
                "promote module unavailable; fast-demote verdicts "
                "unavailable"
            )
            return payload

    try:
        events = _promote.load_promotion_events(audit_log_path)
    except OSError as exc:
        payload["alerts"].append(
            f"failed to read promotion events log: {exc!r}"
        )
        return payload

    verdict_fns = {
        "stage2": _promote.stage2_fast_demote_verdict,
        "stage3-v2": _promote.stage3_v2_fast_demote_verdict,
        "stake-scaling": _promote.stake_scaling_fast_demote_verdict,
        "gate-threshold": _promote.gate_threshold_fast_demote_verdict,
    }
    for lever, fn in verdict_fns.items():
        try:
            v = fn(
                events=events, sessions_dir=sessions_dir, today=today,
            )
        except (OSError, ValueError, KeyError) as exc:
            payload["verdicts"][lever] = {
                "verdict": "error",
                "error": repr(exc),
            }
            continue
        label = str(v.get("verdict") or "")
        payload["verdicts"][lever] = {
            "verdict": label,
            "n_post_filled": v.get("n_post_filled"),
            "wins_post": v.get("wins_post"),
            "observed_win_rate": v.get("observed_win_rate"),
            "wilson_ub_win_rate": v.get("wilson_ub_win_rate"),
            "breakeven_win_rate": v.get("breakeven_win_rate"),
            "wilson_ub_vs_breakeven_delta": (
                v.get("wilson_ub_vs_breakeven_delta")
            ),
            "promotion_event_at": (
                (v.get("promotion_event") or {}).get("generated_at_utc")
            ),
            "post_window": v.get("post_window_dates"),
        }
        if label == "fast_demote":
            payload["alerts"].append(
                f"{lever} fast_demote fired: "
                f"N={v.get('n_post_filled')} post-fills, "
                f"WR_obs={(v.get('observed_win_rate') or 0) * 100:.1f}%, "
                f"Wilson UB={(v.get('wilson_ub_win_rate') or 0) * 100:.1f}% "
                f"< breakeven {(v.get('breakeven_win_rate') or 0) * 100:.1f}%. "
                "Run `promote.py demote " + lever + "` (or daemon will act in "
                "--auto-daemon-mode act, bypassing cooldown)."
            )
    return payload


def _gate_counterfactual_health(
    *,
    report_path: Path,
    session_date: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "artifact_path": str(report_path),
        "artifact_present": report_path.exists(),
        "alerts": [],
        "top_recommendations_30d": [],
        "top_recommendations_7d": [],
    }
    if not report_path.exists():
        payload["artifact_error"] = (
            "gate_counterfactual_report missing; check refresh step ran"
        )
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    age = _artifact_age_days(
        report.get("generated_at_utc", ""), session_date,
    )
    payload["artifact_age_days"] = age
    if age is not None and age > GATE_COUNTERFACTUAL_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"gate_counterfactual_report is {age:.1f}d old "
            f"(> {GATE_COUNTERFACTUAL_STALE_AGE_DAYS}d threshold); "
            "rerun build_gate_counterfactual_report or daily refresh."
        )

    payload["n_rows"] = report.get("n_rows")
    payload["date_span"] = report.get("date_span")

    def _compact(r: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "gate": r.get("gate"),
            "from_threshold": r.get("from_threshold"),
            "to_threshold": r.get("to_threshold"),
            "counterfactual_profit_delta_usd": r.get(
                "counterfactual_profit_delta_usd",
            ),
            "blocked_n_filled": r.get("blocked_n_filled"),
            "blocked_roi": r.get("blocked_roi"),
            "kept_roi_after": r.get("kept_roi_after"),
            "kept_roi_delta_vs_current": r.get("kept_roi_delta_vs_current"),
            "confidence": r.get("confidence"),
            "window": r.get("window"),
        }

    recs_30 = report.get("top_recommendations") or []
    recs_7 = report.get("top_recommendations_trailing_7d") or []
    payload["top_recommendations_30d"] = [_compact(r) for r in recs_30]
    payload["top_recommendations_7d"] = [_compact(r) for r in recs_7]

    above_floor = [
        r for r in recs_30
        if (r.get("counterfactual_profit_delta_usd") or 0.0)
        >= GATE_COUNTERFACTUAL_NOTES_MIN_DELTA_USD
    ]
    for r in above_floor[:GATE_COUNTERFACTUAL_NOTES_MAX_ALERTS]:
        gate = r.get("gate")
        cf = float(r.get("counterfactual_profit_delta_usd") or 0.0)
        n_blocked = int(r.get("blocked_n_filled") or 0)
        blocked_roi = r.get("blocked_roi")
        kept_roi = r.get("kept_roi_after")
        roi_delta = r.get("kept_roi_delta_vs_current")
        conf = r.get("confidence")
        msg_parts = [
            f"`{gate}` tighten {r.get('from_threshold')} -> "
            f"{r.get('to_threshold')} would have saved "
            f"${cf:+,.2f} over trailing-30d ",
            f"(blocked N={n_blocked}",
        ]
        if blocked_roi is not None:
            msg_parts.append(f", blocked ROI {blocked_roi * 100:+.1f}%")
        msg_parts.append(")")
        if kept_roi is not None and roi_delta is not None:
            msg_parts.append(
                f"; kept ROI lifts to {kept_roi * 100:+.1f}% "
                f"({roi_delta * 100:+.1f}pp vs current)"
            )
        msg_parts.append(
            f"; confidence={conf}. Cross-check the cert's verdict "
            "for this gate before changing the live threshold."
        )
        payload["alerts"].append("".join(msg_parts))
    return payload


def _loss_attribution_health(
    *,
    report_path: Path,
    session_date: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "artifact_path": str(report_path),
        "artifact_present": report_path.exists(),
        "alerts": [],
        "trailing_30d": None,
        "trailing_7d": None,
    }
    if not report_path.exists():
        payload["artifact_error"] = (
            "loss_attribution_report missing; check refresh step ran"
        )
        return payload
    try:
        report = _load_json(report_path)
    except (OSError, json.JSONDecodeError) as exc:
        payload["artifact_error"] = f"failed to load: {exc}"
        return payload

    payload["artifact_generated_at_utc"] = report.get("generated_at_utc")
    age = _artifact_age_days(
        report.get("generated_at_utc", ""), session_date,
    )
    payload["artifact_age_days"] = age
    if age is not None and age > LOSS_ATTRIBUTION_STALE_AGE_DAYS:
        payload["alerts"].append(
            f"loss_attribution_report is {age:.1f}d old "
            f"(> {LOSS_ATTRIBUTION_STALE_AGE_DAYS}d threshold); "
            "rerun build_loss_attribution_report or daily refresh."
        )

    windows = report.get("windows") or {}

    def _compact(window_name: str) -> Optional[Dict[str, Any]]:
        w = windows.get(window_name) or {}
        agg = w.get("aggregate") or {}
        if not agg or agg.get("n", 0) == 0:
            return None
        return {
            "n": agg.get("n"),
            "bias": agg.get("bias"),
            "abs_bias": agg.get("abs_bias"),
            "bias_direction": agg.get("bias_direction"),
            "mean_p0": agg.get("mean_p0"),
            "mean_p3": agg.get("mean_p3"),
            "mean_won": agg.get("mean_won"),
            "top_culprits": agg.get("top_culprits") or [],
            "date_range": w.get("date_range"),
        }

    payload["trailing_30d"] = _compact("trailing_30d")
    payload["trailing_7d"] = _compact("trailing_7d")

    primary = payload["trailing_30d"]
    if not primary:
        return payload

    abs_bias = primary.get("abs_bias") or 0.0
    if abs_bias < LOSS_ATTRIBUTION_NOTES_MIN_ABS_BIAS:
        return payload

    direction = primary.get("bias_direction") or "unknown"
    bias_pp = (primary.get("bias") or 0.0) * 100
    n = primary.get("n")
    top_culprits = primary.get("top_culprits") or []
    headline_culprit: Optional[Dict[str, Any]] = None
    for c in top_culprits:
        if (c.get("attribution_share") or 0.0) >= LOSS_ATTRIBUTION_NOTES_MIN_SHARE:
            headline_culprit = c
            break
    if headline_culprit is None:
        payload["alerts"].append(
            f"trailing-30d aggregate bias {bias_pp:+.1f}pp "
            f"(model {direction}, n={n}); no single stage owns "
            f"{int(LOSS_ATTRIBUTION_NOTES_MIN_SHARE * 100)}%+ of "
            "the bias -- read the full report to triage."
        )
        return payload

    payload["alerts"].append(
        f"trailing-30d aggregate bias {bias_pp:+.1f}pp "
        f"(model {direction}, n={n}); `{headline_culprit['stage']}` "
        f"owns "
        f"{(headline_culprit['attribution_share'] or 0.0) * 100:.0f}% "
        f"of the bias direction (shift "
        f"{(headline_culprit['mean_shift_in_bias_direction'] or 0.0) * 100:+.1f}pp). "
        "This is the retrain target -- cross-check with cohort_calibration_health "
        "and concept_drift_health before changing the live cache."
    )
    return payload
