from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .constants import (
    DEFAULT_CANDIDATE_DIR,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_PAPER_SESSIONS_DIR,
    DEFAULT_SESSIONS_DIR,
    UNDER_COVERAGE_RATE_LOW_WARN,
    UNDER_COVERAGE_MIN_N_FOR_ALERT,
    UNDER_SHADOW_UNDER_RATE_HIGH_WARN,
    UNDER_SHADOW_UNDER_MIN_N_FOR_ALERT_HIGH,
    UNDER_SHADOW_UNDER_RATE_LOW_WARN,
    UNDER_SHADOW_UNDER_MIN_N_FOR_ALERT_LOW,
    UNDER_FV_BUCKETS,
    UNDER_OUTCOMES_DEFAULT_STAKE,
    UNDER_OUTCOMES_TRAILING_DAYS,
    UNDER_OUTCOMES_PROFITABLE_ROI_WARN,
    UNDER_OUTCOMES_UNPROFITABLE_ROI_WARN,
    UNDER_OUTCOMES_MIN_N_FOR_ALERT,
    UNDER_OUTCOMES_TRAILING_MIN_N_FOR_ALERT,
    B4_EXTRA_PAPER_SESSION_ROOTS,
    B4_MILESTONE_TRAILING_DAYS,
    B4_MILESTONE_MIN_SESSIONS,
    B4_MILESTONE_MIN_SETTLED,
    B4_MILESTONE_MIN_ROI,
    B4_MILESTONE_CALIBRATION_TOLERANCE_PP,
    B4_MILESTONE_DRIFT_ALERT_LOOKBACK_DAYS,
    B4_MILESTONE_DRIFT_PERSISTENCE_THRESHOLD,
    B4_MILESTONE_MIN_N_FOR_FAILURE_ALERT,
    B4_MILESTONE_DORMANT,
)

from .helpers import (
    _load_jsonl,
    _drift_ask_bucket,
    _drift_current_state_edge_bucket,
)

from .calibration_health import (
    _cohort_edge_bucket,
    _cohort_inning_bucket,
    _cohort_line_bucket,
)


def _under_emission_health(
    *,
    session_date: str,
    candidate_dir: Path = DEFAULT_CANDIDATE_DIR,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "alerts": [],
        "thresholds": {
            "coverage_rate_low_warn": UNDER_COVERAGE_RATE_LOW_WARN,
            "coverage_min_n_for_alert": UNDER_COVERAGE_MIN_N_FOR_ALERT,
            "shadow_under_rate_high_warn": UNDER_SHADOW_UNDER_RATE_HIGH_WARN,
            "shadow_under_min_n_for_alert_high": (
                UNDER_SHADOW_UNDER_MIN_N_FOR_ALERT_HIGH
            ),
            "shadow_under_rate_low_warn": UNDER_SHADOW_UNDER_RATE_LOW_WARN,
            "shadow_under_min_n_for_alert_low": (
                UNDER_SHADOW_UNDER_MIN_N_FOR_ALERT_LOW
            ),
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

    over_fv_count = 0
    under_rows: List[Dict[str, Any]] = []
    for row in rows:
        side = str(row.get("side") or "").strip().lower()
        if side == "under":
            under_rows.append(row)
            continue
        if row.get("fair_value") is not None:
            over_fv_count += 1

    under_emitted = len(under_rows)
    payload["over_post_fv_count"] = over_fv_count
    payload["under_emitted_count"] = under_emitted

    if under_emitted == 0:
        payload["status"] = "not_emitting"
        payload["coverage_rate"] = None
        return payload

    coverage_rate = (
        under_emitted / over_fv_count if over_fv_count > 0 else None
    )
    payload["coverage_rate"] = (
        round(coverage_rate, 4) if coverage_rate is not None else None
    )

    n_shadow_under = 0
    n_gate_min_edge = 0
    n_gate_no_liq = 0
    n_other_skip = 0
    for r in under_rows:
        decision = str(r.get("decision") or "").strip().lower()
        reason = str(r.get("decision_reason") or "").strip().lower()
        if decision == "shadow_under":
            n_shadow_under += 1
        elif reason == "gate_no_under_liquidity":
            n_gate_no_liq += 1
        elif reason == "gate_min_edge":
            n_gate_min_edge += 1
        else:
            n_other_skip += 1
    payload["decision_breakdown"] = {
        "shadow_under": n_shadow_under,
        "gate_min_edge": n_gate_min_edge,
        "gate_no_under_liquidity": n_gate_no_liq,
        "other_skip": n_other_skip,
    }
    payload["shadow_under_rate"] = (
        round(n_shadow_under / under_emitted, 4)
        if under_emitted else None
    )
    payload["liquidity_skip_rate"] = (
        round(n_gate_no_liq / under_emitted, 4)
        if under_emitted else None
    )

    under_pair_available_count = sum(
        1 for r in under_rows if bool(r.get("under_pair_available"))
    )
    payload["under_pair_available_rate"] = (
        round(under_pair_available_count / under_emitted, 4)
        if under_emitted else None
    )

    if (
        n_gate_no_liq == under_emitted
        and under_emitted > 0
    ):
        payload["status"] = "no_liquidity"
    else:
        payload["status"] = "ok"

    fvs: List[float] = []
    fvs_raw: List[float] = []
    asks: List[float] = []
    edges: List[float] = []
    bucket_counts = {label: 0 for label, _, _ in UNDER_FV_BUCKETS}
    for r in under_rows:
        fv = r.get("fair_value")
        ask = r.get("entry_ask")
        edge = r.get("edge")
        fv_raw = r.get("fair_value_raw")
        if isinstance(fv, (int, float)):
            fvs.append(float(fv))
            for label, low, high in UNDER_FV_BUCKETS:
                if low <= float(fv) < high or (
                    high == 1.00 and float(fv) <= 1.00 and float(fv) >= low
                ):
                    bucket_counts[label] += 1
                    break
        if isinstance(fv_raw, (int, float)):
            fvs_raw.append(float(fv_raw))
        if isinstance(ask, (int, float)):
            asks.append(float(ask))
        if isinstance(edge, (int, float)):
            edges.append(float(edge))

    def _mean(xs: List[float]) -> Optional[float]:
        return round(sum(xs) / len(xs), 4) if xs else None

    payload["price_quality"] = {
        "mean_under_fv": _mean(fvs),
        "mean_under_fv_raw": _mean(fvs_raw),
        "mean_under_ask": _mean(asks),
        "mean_under_edge": _mean(edges),
        "mean_under_calibration_delta": (
            round(
                (sum(fvs) / len(fvs)) - (sum(fvs_raw) / len(fvs_raw)),
                4,
            )
            if fvs and fvs_raw and len(fvs) == len(fvs_raw)
            else None
        ),
        "n_under_with_fv": len(fvs),
        "n_under_with_ask": len(asks),
        "fv_buckets": bucket_counts,
    }

    if payload["status"] == "ok":
        if (
            coverage_rate is not None
            and coverage_rate < UNDER_COVERAGE_RATE_LOW_WARN
            and under_emitted >= UNDER_COVERAGE_MIN_N_FOR_ALERT
        ):
            payload["alerts"].append(
                f"UNDER coverage rate {coverage_rate:.0%} is below "
                f"{UNDER_COVERAGE_RATE_LOW_WARN:.0%} ({under_emitted} "
                f"UNDER rows vs {over_fv_count} OVER FV-phase ticks). "
                "Either UNDER side has thin book liquidity OR the "
                "_maybe_emit_under_candidate helper is skipping more "
                "than expected. Inspect candidate rows for missing "
                "under_best_ask."
            )
        shadow_rate = payload["shadow_under_rate"] or 0.0
        if (
            shadow_rate > UNDER_SHADOW_UNDER_RATE_HIGH_WARN
            and under_emitted >= UNDER_SHADOW_UNDER_MIN_N_FOR_ALERT_HIGH
        ):
            payload["alerts"].append(
                f"`shadow_under` rate {shadow_rate:.0%} is above "
                f"{UNDER_SHADOW_UNDER_RATE_HIGH_WARN:.0%} (n="
                f"{under_emitted}). Either UNDER has genuine edge "
                "OR the OVER-borrowed min_edge threshold is too "
                "loose for UNDER price dynamics. Read the per-bet "
                "detail before tuning UNDER-specific min_edge."
            )
        if (
            shadow_rate < UNDER_SHADOW_UNDER_RATE_LOW_WARN
            and under_emitted >= UNDER_SHADOW_UNDER_MIN_N_FOR_ALERT_LOW
        ):
            payload["alerts"].append(
                f"`shadow_under` rate {shadow_rate:.1%} is "
                f"suspiciously low (n={under_emitted}). The OVER "
                "edge_threshold (default 0.15) is likely wrong for "
                "UNDER's price dynamics; consider tuning UNDER-"
                "specific min_edge from accumulated shadow data."
            )

    return payload


def _collect_under_settled_rows(
    *,
    session_date: str,
    candidate_dir: Path,
    stake_usdc: float,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "session_date": session_date,
        "settled_rows": [],
        "n_shadow_under_candidates": 0,
        "n_missing_outcome": 0,
        "n_missing_ask": 0,
    }
    candidate_path = candidate_dir / f"{session_date}_candidates.jsonl"
    outcomes_path = candidate_dir / f"{session_date}_outcomes.jsonl"
    out["candidate_path"] = str(candidate_path)
    out["outcomes_path"] = str(outcomes_path)

    if not candidate_path.exists():
        out["status"] = "check_error"
        out["error"] = "candidate log not found"
        return out
    try:
        candidates = _load_jsonl(candidate_path)
    except (OSError, json.JSONDecodeError) as exc:
        out["status"] = "check_error"
        out["error"] = f"candidates load: {exc!r}"
        return out

    shadow_under = [
        r for r in candidates
        if str(r.get("decision") or "") == "shadow_under"
        and str(r.get("side") or "") == "under"
    ]
    out["n_shadow_under_candidates"] = len(shadow_under)
    if not shadow_under:
        out["status"] = "no_shadow_under_candidates"
        return out

    outcomes: List[Dict[str, Any]] = []
    if outcomes_path.exists():
        try:
            outcomes = _load_jsonl(outcomes_path)
        except (OSError, json.JSONDecodeError):
            outcomes = []
    final_total_by_key: Dict[Tuple[int, str], int] = {}
    for o in outcomes:
        gpk = o.get("game_pk")
        ln = o.get("line")
        tot = o.get("final_total")
        if isinstance(gpk, int) and ln is not None and isinstance(tot, int):
            final_total_by_key[(int(gpk), str(ln))] = int(tot)

    # 2026-06-03: dedup shadow_under tick-rows by (game_pk, line, side)
    # before counting them as settled counterfactual bets. Same fix
    # pattern as _calibrator_enforce_shipment_health: each game ticks
    # through the engine many times while in the shadow-emission
    # range, and each tick emits an independent shadow_under row that
    # shares the same final game total. Counting all of them inflates
    # n_settled / counterfactual_roi by 10-100x, which produced the
    # persistent "-100% ROI on 229 settled" alert from earlier this
    # window (true unique count is more like 5-15 / day).
    #
    # Picking strategy: keep the row with the largest UNDER raw_edge
    # (under_fv - under_ask) per (game, line, side) group -- the
    # moment the bot would have most wanted to fire under-mode-shadow
    # had paper UNDER not yet been enabled. Mirrors the calibrator-
    # enforce fix.
    n_dedup_collapsed = 0
    best_by_key: Dict[Tuple[int, str, str], Dict[str, Any]] = {}
    for r in shadow_under:
        gpk = r.get("game_pk")
        ln = r.get("line")
        if not isinstance(gpk, int) or ln is None:
            continue
        side = str(r.get("side") or "under").lower()
        key = (int(gpk), str(ln), side)
        try:
            cand_edge = (
                float(r.get("fair_value") or 0.0)
                - float(r.get("entry_ask") or 0.0)
            )
        except (TypeError, ValueError):
            cand_edge = float("-inf")
        cur = best_by_key.get(key)
        if cur is None:
            best_by_key[key] = r
            continue
        n_dedup_collapsed += 1
        try:
            cur_edge = (
                float(cur.get("fair_value") or 0.0)
                - float(cur.get("entry_ask") or 0.0)
            )
        except (TypeError, ValueError):
            cur_edge = float("-inf")
        if cand_edge > cur_edge:
            best_by_key[key] = r
    deduped_shadow_under = list(best_by_key.values())
    out["n_dedup_collapsed_tick_rows"] = n_dedup_collapsed

    settled_rows: List[Dict[str, Any]] = []
    n_missing_outcome = 0
    n_missing_ask = 0
    for r in deduped_shadow_under:
        gpk = r.get("game_pk")
        ln = r.get("line")
        if not isinstance(gpk, int) or ln is None:
            n_missing_outcome += 1
            continue
        ft = final_total_by_key.get((int(gpk), str(ln)))
        if ft is None:
            n_missing_outcome += 1
            continue
        try:
            line_val = float(ln)
        except (TypeError, ValueError):
            n_missing_outcome += 1
            continue
        ask_raw = r.get("entry_ask")
        try:
            ask = float(ask_raw)
        except (TypeError, ValueError):
            n_missing_ask += 1
            continue
        if not (0.0 < ask < 1.0):
            n_missing_ask += 1
            continue
        won = int(ft < line_val)
        if won:
            profit = stake_usdc * (1.0 / ask - 1.0)
        else:
            profit = -stake_usdc
        settled_rows.append({
            "session_date": session_date,
            "candidate": r,
            "final_total": ft,
            "line": line_val,
            "ask": ask,
            "won": won,
            "profit": profit,
        })

    out["settled_rows"] = settled_rows
    out["n_missing_outcome"] = n_missing_outcome
    out["n_missing_ask"] = n_missing_ask
    out["status"] = "ok" if settled_rows else "no_settled"
    return out


def _aggregate_under_settled(
    settled_rows: List[Dict[str, Any]],
    *,
    stake_usdc: float,
) -> Dict[str, Any]:
    n = len(settled_rows)
    if n == 0:
        return {
            "n": 0, "n_won": 0, "n_lost": 0, "win_rate": None,
            "total_counterfactual_pnl": 0.0,
            "total_counterfactual_stake": 0.0,
            "counterfactual_roi": None,
            "mean_under_ask": None, "mean_under_fv": None,
        }
    n_won = sum(1 for s in settled_rows if s["won"])
    n_lost = n - n_won
    total_pnl = sum(s["profit"] for s in settled_rows)
    total_stake = n * stake_usdc
    roi = total_pnl / total_stake if total_stake else None
    mean_ask = sum(s["ask"] for s in settled_rows) / n
    mean_fv = sum(
        float(s["candidate"].get("fair_value") or 0.0)
        for s in settled_rows
    ) / n
    return {
        "n": n,
        "n_won": n_won,
        "n_lost": n_lost,
        "win_rate": round(n_won / n, 4),
        "total_counterfactual_pnl": round(total_pnl, 2),
        "total_counterfactual_stake": round(total_stake, 2),
        "counterfactual_roi": (
            round(roi, 4) if roi is not None else None
        ),
        "mean_under_ask": round(mean_ask, 4),
        "mean_under_fv": round(mean_fv, 4),
    }


def _under_settled_by_cohort(
    settled_rows: List[Dict[str, Any]],
    *,
    stake_usdc: float,
) -> Dict[str, Any]:
    cohort_dims = [
        ("edge_bucket",
         lambda r: _cohort_edge_bucket(r["candidate"].get("edge"))),
        ("inning_bucket",
         lambda r: _cohort_inning_bucket(r["candidate"].get("inning"))),
        ("line_bucket",
         lambda r: _cohort_line_bucket(r["candidate"].get("line"))),
        ("ask_bucket",
         lambda r: _drift_ask_bucket(r["candidate"].get("entry_ask"))),
        ("current_state_edge_bucket",
         lambda r: _drift_current_state_edge_bucket(
             r["candidate"].get("current_state_value_edge"),
         )),
    ]
    by_cohort: Dict[str, Any] = {}
    for dim_name, keyer in cohort_dims:
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for s in settled_rows:
            try:
                key = keyer(s)
            except Exception:
                key = "missing"
            buckets.setdefault(key, []).append(s)
        per_bucket: Dict[str, Any] = {}
        for k in sorted(buckets.keys()):
            grp = buckets[k]
            n_b = len(grp)
            n_won_b = sum(1 for s in grp if s["won"])
            pnl_b = sum(s["profit"] for s in grp)
            stake_b = n_b * stake_usdc
            per_bucket[k] = {
                "n": n_b,
                "n_won": n_won_b,
                "win_rate": round(n_won_b / n_b, 4) if n_b else None,
                "counterfactual_pnl": round(pnl_b, 2),
                "counterfactual_roi": (
                    round(pnl_b / stake_b, 4) if stake_b else None
                ),
            }
        by_cohort[dim_name] = per_bucket
    return by_cohort


def _under_outcomes_counterfactual_health(
    *,
    session_date: str,
    candidate_dir: Path = DEFAULT_CANDIDATE_DIR,
    stake_usdc: float = UNDER_OUTCOMES_DEFAULT_STAKE,
    trailing_days: int = UNDER_OUTCOMES_TRAILING_DAYS,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "session_date": session_date,
        "stake_usdc": stake_usdc,
        "alerts": [],
        "thresholds": {
            "profitable_roi_warn": UNDER_OUTCOMES_PROFITABLE_ROI_WARN,
            "unprofitable_roi_warn": UNDER_OUTCOMES_UNPROFITABLE_ROI_WARN,
            "min_n_for_alert": UNDER_OUTCOMES_MIN_N_FOR_ALERT,
            "trailing_days": trailing_days,
            "trailing_min_n_for_alert": UNDER_OUTCOMES_TRAILING_MIN_N_FOR_ALERT,
        },
    }

    today = _collect_under_settled_rows(
        session_date=session_date,
        candidate_dir=candidate_dir,
        stake_usdc=stake_usdc,
    )
    payload["candidate_path"] = today.get("candidate_path")
    payload["outcomes_path"] = today.get("outcomes_path")
    payload["status"] = today["status"]
    payload["n_shadow_under_candidates"] = today["n_shadow_under_candidates"]
    # 2026-06-03 dedup-bias fix: n_settled is computed on the
    # (game_pk, line, side)-deduped set, not the raw tick-rows.
    # n_dedup_collapsed_tick_rows exposes how many tick-rows were
    # collapsed so the operator can audit the inflation factor.
    payload["n_settled"] = len(today["settled_rows"])
    payload["n_dedup_collapsed_tick_rows"] = today.get(
        "n_dedup_collapsed_tick_rows", 0,
    )
    payload["n_missing_outcome"] = today["n_missing_outcome"]
    payload["n_missing_ask"] = today["n_missing_ask"]
    if "error" in today:
        payload["error"] = today["error"]

    if today["status"] == "ok":
        today_settled = today["settled_rows"]
        payload["aggregate"] = _aggregate_under_settled(
            today_settled, stake_usdc=stake_usdc,
        )
        payload["by_cohort"] = _under_settled_by_cohort(
            today_settled, stake_usdc=stake_usdc,
        )
        agg = payload["aggregate"]
        roi = agg.get("counterfactual_roi")
        n = agg["n"]
        if (
            roi is not None
            and n >= UNDER_OUTCOMES_MIN_N_FOR_ALERT
        ):
            pnl = agg["total_counterfactual_pnl"]
            stake_tot = agg["total_counterfactual_stake"]
            if roi >= UNDER_OUTCOMES_PROFITABLE_ROI_WARN:
                payload["alerts"].append(
                    f"UNDER candidates would have netted "
                    f"{roi:+.1%} ROI on {n} settled "
                    f"(${pnl:+,.2f} on ${stake_tot:,.2f} stake). "
                    "If durable across the 7-day paper runway, consider "
                    "the Phase B4 UNDER paper-bet validation milestone."
                )
            elif roi <= UNDER_OUTCOMES_UNPROFITABLE_ROI_WARN:
                payload["alerts"].append(
                    f"UNDER signal is loss-making at "
                    f"{roi:+.1%} ROI on {n} settled "
                    f"(${pnl:+,.2f}). Tune UNDER-specific gates "
                    "(currently borrowed from OVER's min_edge) BEFORE "
                    "any Phase B4 flip; the runtime would lose money in "
                    "the current regime."
                )

    trailing: Dict[str, Any] = {
        "trailing_days": trailing_days,
        "anchor_date": session_date,
        "dates_with_data": [],
        "dates_missing": [],
        "n_dates_with_data": 0,
        "n_dates_missing": 0,
        "n_shadow_under_candidates_total": 0,
        "n_settled_total": 0,
        "n_missing_outcome_total": 0,
        "n_missing_ask_total": 0,
        "by_date": [],
        "status": "no_session_history",
    }

    trailing_settled: List[Dict[str, Any]] = []
    try:
        anchor_dt = datetime.strptime(session_date, "%Y-%m-%d")
    except ValueError:
        anchor_dt = None

    if anchor_dt is not None:
        for offset in range(trailing_days):
            dt = anchor_dt - timedelta(days=offset)
            d_str = dt.strftime("%Y-%m-%d")
            if d_str == session_date:
                day = today
            else:
                day = _collect_under_settled_rows(
                    session_date=d_str,
                    candidate_dir=candidate_dir,
                    stake_usdc=stake_usdc,
                )
            if day["status"] == "check_error":
                trailing["dates_missing"].append(d_str)
                trailing["n_dates_missing"] += 1
                continue
            trailing["dates_with_data"].append(d_str)
            trailing["n_dates_with_data"] += 1
            trailing["n_shadow_under_candidates_total"] += (
                day["n_shadow_under_candidates"]
            )
            trailing["n_missing_outcome_total"] += day["n_missing_outcome"]
            trailing["n_missing_ask_total"] += day["n_missing_ask"]
            day_settled = day["settled_rows"]
            trailing_settled.extend(day_settled)
            day_agg = _aggregate_under_settled(
                day_settled, stake_usdc=stake_usdc,
            )
            trailing["by_date"].append({
                "date": d_str,
                "n_shadow_under": day["n_shadow_under_candidates"],
                "n_settled": day_agg["n"],
                "win_rate": day_agg["win_rate"],
                "counterfactual_pnl": day_agg["total_counterfactual_pnl"],
                "counterfactual_roi": day_agg["counterfactual_roi"],
            })
        trailing["by_date"].sort(key=lambda r: r["date"])
        if trailing["dates_with_data"]:
            sorted_dates = sorted(trailing["dates_with_data"])
            trailing["date_range"] = [sorted_dates[0], sorted_dates[-1]]

    trailing["n_settled_total"] = len(trailing_settled)
    if trailing_settled:
        trailing["aggregate"] = _aggregate_under_settled(
            trailing_settled, stake_usdc=stake_usdc,
        )
        trailing["by_cohort"] = _under_settled_by_cohort(
            trailing_settled, stake_usdc=stake_usdc,
        )
        trailing["status"] = "ok"
        agg = trailing["aggregate"]
        roi = agg["counterfactual_roi"]
        n = agg["n"]
        if (
            roi is not None
            and n >= UNDER_OUTCOMES_TRAILING_MIN_N_FOR_ALERT
        ):
            pnl = agg["total_counterfactual_pnl"]
            stake_tot = agg["total_counterfactual_stake"]
            window_str = (
                f"{trailing['date_range'][0]} -> {trailing['date_range'][1]}"
                if trailing.get("date_range")
                else f"trailing {trailing_days}d"
            )
            if roi >= UNDER_OUTCOMES_PROFITABLE_ROI_WARN:
                payload["alerts"].append(
                    f"(7d) trailing-{trailing_days}d UNDER counterfactual "
                    f"{roi:+.1%} ROI on {n} settled across "
                    f"{trailing['n_dates_with_data']} dates ({window_str}); "
                    f"${pnl:+,.2f} on ${stake_tot:,.2f} stake. "
                    f"Phase B4 paper-bet milestone progress: "
                    f"{trailing['n_dates_with_data']}/60 sessions of "
                    "UNDER signal data accumulated."
                )
            elif roi <= UNDER_OUTCOMES_UNPROFITABLE_ROI_WARN:
                payload["alerts"].append(
                    f"(7d) trailing-{trailing_days}d UNDER signal is "
                    f"loss-making at {roi:+.1%} ROI on {n} settled "
                    f"({window_str}); ${pnl:+,.2f}. The aggregate is "
                    "more stable than the per-day view; tune UNDER-"
                    "specific gates before any B4 flip."
                )
    elif anchor_dt is not None and trailing["n_dates_with_data"]:
        if trailing["n_shadow_under_candidates_total"] == 0:
            trailing["status"] = "no_shadow_under_candidates"
        else:
            trailing["status"] = "no_settled"

    payload["trailing_7d"] = trailing
    return payload


# ----------------------------------------------------------------------
# Phase C-paper follow-up (2026-05-27): UNDER paper-bet B4 milestone
# dashboard. Closes the Phase C-paper loop by giving the operator a
# daily-review block that explicitly tracks B4 verdict progress
# against ACTUAL `side="under"` paper bets across the trailing window.
# ----------------------------------------------------------------------


def _load_session_bets(session_path: Path) -> List[Dict[str, Any]]:
    """Load and return the `bets` list from one session JSON file.

    Defensive: returns [] on missing file, IO error, JSON decode
    error, or unexpected schema. The B4 milestone block walks 60
    session files per refresh and should never abort the entire
    block because a single file is malformed.
    """
    if not session_path.exists():
        return []
    try:
        with session_path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    bets = payload.get("bets")
    if not isinstance(bets, list):
        return []
    return [b for b in bets if isinstance(b, dict)]


def _collect_paper_under_bets_for_date(
    *,
    session_date: str,
    paper_sessions_dir: Path,
    live_sessions_dir: Path,
    extra_paper_sessions_dirs: Tuple[Path, ...] = (),
) -> Dict[str, Any]:
    """Walk paper_root + live_root session JSONs for one date and
    return the union of side="under" bets across both. Operator may
    accumulate evidence on either root depending on whether they're
    running the paper engine or the live engine with `--under-mode
    paper`; this function ignores the source and reports the merged
    set so a "session with paper UNDER bets" counts once per date.

    `extra_paper_sessions_dirs` (2026-06-10): additional fleet roots
    (e.g. data/paper_M_under_paper/sessions) whose UNDER paper bets
    also advance B4. The parallel-engine fleet writes per-preset roots
    that the two default roots never see; without this the milestone
    undercounts (the M preset accumulated UNDER bets invisible to the
    dashboard from 2026-05-30 to 2026-06-10).

    Returns:
      {
        "session_date": str,
        "n_paper_under_bets": int,
        "settled_under_bets": List[Dict],   # only side=under AND
                                            # settled
        "sources": ["paper" | "live" | "fleet:<name>", ...],
      }
    """
    out: Dict[str, Any] = {
        "session_date": session_date,
        "n_paper_under_bets": 0,
        "settled_under_bets": [],
        "sources": [],
    }
    seen_bet_ids: set = set()

    roots: List[Tuple[str, Path]] = [
        ("paper", paper_sessions_dir),
        ("live", live_sessions_dir),
    ]
    for extra in extra_paper_sessions_dirs:
        # Label fleet roots by their paper_<name> directory so the
        # by_date sources drill-down shows which engine contributed.
        name = extra.parent.name if extra.name == "sessions" else extra.name
        roots.append((f"fleet:{name}", extra))

    for label, root in roots:
        session_path = root / f"{session_date}_session.json"
        bets = _load_session_bets(session_path)
        if not bets:
            continue
        under_bets = [
            b for b in bets
            if str(b.get("side") or "over").lower() == "under"
        ]
        if not under_bets:
            continue
        out["sources"].append(label)
        for b in under_bets:
            bet_id = str(b.get("bet_id") or "")
            if bet_id and bet_id in seen_bet_ids:
                continue
            if bet_id:
                seen_bet_ids.add(bet_id)
            out["n_paper_under_bets"] += 1
            if bool(b.get("settled")):
                out["settled_under_bets"].append(b)
    return out


def _aggregate_paper_under_bets(
    settled_bets: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute aggregate metrics over a list of settled paper UNDER
    bets. Bets without numeric stake / fair_value are skipped from
    the relevant aggregate but still counted in n.
    """
    n = len(settled_bets)
    if n == 0:
        return {
            "n_settled": 0,
            "n_wins": 0,
            "n_losses": 0,
            "realized_wr": None,
            "predicted_wr": None,
            "calibration_delta_pp": None,
            "total_profit_usdc": 0.0,
            "total_stake_usdc": 0.0,
            "taker_roi": None,
            "mean_under_ask": None,
        }
    n_wins = 0
    n_losses = 0
    total_profit = 0.0
    total_stake = 0.0
    fv_sum = 0.0
    fv_count = 0
    ask_sum = 0.0
    ask_count = 0
    for b in settled_bets:
        won = bool(b.get("won"))
        if won:
            n_wins += 1
        else:
            n_losses += 1
        try:
            total_profit += float(b.get("profit") or 0.0)
        except (TypeError, ValueError):
            pass
        try:
            total_stake += float(b.get("stake") or 0.0)
        except (TypeError, ValueError):
            pass
        fv = b.get("fair_value")
        if fv is not None:
            try:
                fv_sum += float(fv)
                fv_count += 1
            except (TypeError, ValueError):
                pass
        ask = b.get("entry_ask")
        if ask is not None:
            try:
                ask_sum += float(ask)
                ask_count += 1
            except (TypeError, ValueError):
                pass

    realized_wr = n_wins / n
    predicted_wr = (fv_sum / fv_count) if fv_count else None
    calibration_delta_pp = (
        round((realized_wr - predicted_wr) * 100.0, 2)
        if predicted_wr is not None else None
    )
    taker_roi = (
        round(total_profit / total_stake, 4)
        if total_stake > 0 else None
    )
    return {
        "n_settled": n,
        "n_wins": n_wins,
        "n_losses": n_losses,
        "realized_wr": round(realized_wr, 4),
        "predicted_wr": (
            round(predicted_wr, 4) if predicted_wr is not None else None
        ),
        "calibration_delta_pp": calibration_delta_pp,
        "total_profit_usdc": round(total_profit, 2),
        "total_stake_usdc": round(total_stake, 2),
        "taker_roi": taker_roi,
        "mean_under_ask": (
            round(ask_sum / ask_count, 4) if ask_count else None
        ),
    }


def _count_persistent_under_drift_alerts(
    *,
    session_date: str,
    output_root: Path,
    lookback_days: int,
) -> Dict[str, Any]:
    """Walk the last N daily review JSONs for `under:` / `Under-`
    prefixed alerts (the side-aware convention shipped 2026-05-19 +
    extended through 2026-05-27).

    Returns:
      {
        "lookback_days": int,
        "days_scanned": int,
        "days_with_alert": int,
        "alert_days": List[str],
      }
    """
    out: Dict[str, Any] = {
        "lookback_days": lookback_days,
        "days_scanned": 0,
        "days_with_alert": 0,
        "alert_days": [],
    }
    try:
        anchor_dt = datetime.strptime(session_date, "%Y-%m-%d")
    except ValueError:
        return out

    # Prefixes that mark a *drift* alert (B1 dimension family). The
    # B4 milestone verdict alert uses prefix `under-b4:` and is
    # explicitly excluded: it's a verdict, not a drift signal, and
    # counting it as drift would create a self-loop where yesterday's
    # B4 status alert pollutes today's drift count.
    under_alert_include_prefixes = (
        "under:",
        "under-",
    )
    under_alert_exclude_prefixes = (
        "under-b4:",
    )

    for offset in range(lookback_days):
        dt = anchor_dt - timedelta(days=offset)
        d_str = dt.strftime("%Y-%m-%d")
        review_path = output_root / f"{d_str}_human_review.json"
        if not review_path.exists():
            continue
        try:
            with review_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        out["days_scanned"] += 1
        notes = payload.get("notes") or []
        if not isinstance(notes, list):
            continue
        for note in notes:
            note_str = str(note or "").strip().lower()
            if any(
                note_str.startswith(p)
                for p in under_alert_exclude_prefixes
            ):
                continue
            if any(
                note_str.startswith(p)
                for p in under_alert_include_prefixes
            ):
                out["days_with_alert"] += 1
                out["alert_days"].append(d_str)
                break  # one alert per day is enough to count
    return out


def _under_paper_b4_milestone_health(
    *,
    session_date: str,
    paper_sessions_dir: Path = DEFAULT_PAPER_SESSIONS_DIR,
    live_sessions_dir: Path = DEFAULT_SESSIONS_DIR,
    extra_paper_sessions_dirs: Tuple[Path, ...] = B4_EXTRA_PAPER_SESSION_ROOTS,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    trailing_days: int = B4_MILESTONE_TRAILING_DAYS,
    min_sessions: int = B4_MILESTONE_MIN_SESSIONS,
    min_settled: int = B4_MILESTONE_MIN_SETTLED,
    min_roi: float = B4_MILESTONE_MIN_ROI,
    calibration_tolerance_pp: float = B4_MILESTONE_CALIBRATION_TOLERANCE_PP,
    drift_lookback_days: int = B4_MILESTONE_DRIFT_ALERT_LOOKBACK_DAYS,
    drift_persistence_threshold: int = B4_MILESTONE_DRIFT_PERSISTENCE_THRESHOLD,
    min_n_for_failure_alert: int = B4_MILESTONE_MIN_N_FOR_FAILURE_ALERT,
    dormant: bool = B4_MILESTONE_DORMANT,
) -> Dict[str, Any]:
    """Phase C-paper follow-up (2026-05-27): B4 milestone tracker.

    Walks `paper_sessions_dir` + `live_sessions_dir` (+ any
    `extra_paper_sessions_dirs` fleet roots, 2026-06-10) for the
    trailing `trailing_days` dates, accumulates ACTUAL `side="under"`
    paper bets, and reports verdict status across the 5 B4 conditions:

    1. sessions_with_under_bets >= min_sessions
    2. n_settled >= min_settled
    3. taker_roi > min_roi
    4. |calibration_delta_pp| <= calibration_tolerance_pp
    5. UNDER drift alerts < drift_persistence_threshold in last
       drift_lookback_days

    Verdict ladder (one-line summary):
      NOT_EMITTING -> INSUFFICIENT_SESSIONS -> INSUFFICIENT_OUTCOMES
        -> SUB_ZERO_ROI -> CALIBRATION_OFF
        -> DRIFT_ALERT_PERSISTENT -> READY

    The block is descriptive; no decisions are taken. Operator reads
    the daily-review JSON / Notes to see how close B4 is to clearing
    and which condition is currently the limiter.
    """
    payload: Dict[str, Any] = {
        "session_date": session_date,
        "trailing_days": trailing_days,
        "scanned_roots": [
            str(paper_sessions_dir),
            str(live_sessions_dir),
            *[str(p) for p in extra_paper_sessions_dirs],
        ],
        "thresholds": {
            "min_sessions": min_sessions,
            "min_settled": min_settled,
            "min_roi": min_roi,
            "calibration_tolerance_pp": calibration_tolerance_pp,
            "drift_alert_lookback_days": drift_lookback_days,
            "drift_persistence_threshold": drift_persistence_threshold,
            "min_n_for_failure_alert": min_n_for_failure_alert,
        },
        "alerts": [],
    }

    try:
        anchor_dt = datetime.strptime(session_date, "%Y-%m-%d")
    except ValueError:
        payload["status"] = "check_error"
        payload["error"] = f"unparseable session_date: {session_date!r}"
        return payload

    by_date: List[Dict[str, Any]] = []
    all_settled: List[Dict[str, Any]] = []
    n_sessions_scanned = 0
    n_sessions_with_under_bets = 0
    n_under_bets_total = 0
    first_under_date: Optional[str] = None
    last_under_date: Optional[str] = None

    for offset in range(trailing_days):
        dt = anchor_dt - timedelta(days=offset)
        d_str = dt.strftime("%Y-%m-%d")
        n_sessions_scanned += 1
        day = _collect_paper_under_bets_for_date(
            session_date=d_str,
            paper_sessions_dir=paper_sessions_dir,
            live_sessions_dir=live_sessions_dir,
            extra_paper_sessions_dirs=extra_paper_sessions_dirs,
        )
        if day["n_paper_under_bets"] == 0:
            continue
        n_sessions_with_under_bets += 1
        n_under_bets_total += day["n_paper_under_bets"]
        all_settled.extend(day["settled_under_bets"])
        if first_under_date is None or d_str < first_under_date:
            first_under_date = d_str
        if last_under_date is None or d_str > last_under_date:
            last_under_date = d_str
        day_agg = _aggregate_paper_under_bets(day["settled_under_bets"])
        by_date.append({
            "date": d_str,
            "n_paper_under_bets": day["n_paper_under_bets"],
            "n_settled": day_agg["n_settled"],
            "wins": day_agg["n_wins"],
            "losses": day_agg["n_losses"],
            "profit_usdc": day_agg["total_profit_usdc"],
            "taker_roi": day_agg["taker_roi"],
            "sources": day["sources"],
        })

    by_date.sort(key=lambda r: r["date"])
    aggregate = _aggregate_paper_under_bets(all_settled)
    aggregate.update({
        "trailing_days": trailing_days,
        "n_sessions_scanned": n_sessions_scanned,
        "n_sessions_with_under_bets": n_sessions_with_under_bets,
        "first_under_session_date": first_under_date,
        "last_under_session_date": last_under_date,
        "n_paper_under_bets_total": n_under_bets_total,
    })
    payload["aggregate"] = aggregate
    payload["by_date"] = by_date

    drift = _count_persistent_under_drift_alerts(
        session_date=session_date,
        output_root=output_root,
        lookback_days=drift_lookback_days,
    )
    payload["drift_alerts"] = drift

    # Per-condition status (computed regardless of verdict ladder so
    # the operator can see all 5 in one place).
    sessions_pass = n_sessions_with_under_bets >= min_sessions
    settled_pass = aggregate["n_settled"] >= min_settled
    roi_value = aggregate.get("taker_roi")
    roi_pass = (
        roi_value is not None and roi_value > min_roi
    )
    cal_value = aggregate.get("calibration_delta_pp")
    cal_pass = (
        cal_value is not None
        and abs(cal_value) <= calibration_tolerance_pp
    )
    drift_pass = (
        drift["days_with_alert"] < drift_persistence_threshold
    )
    payload["conditions"] = {
        "sessions": {
            "value": n_sessions_with_under_bets,
            "target": min_sessions,
            "remaining": max(
                0, min_sessions - n_sessions_with_under_bets
            ),
            "pass": sessions_pass,
        },
        "n_settled": {
            "value": aggregate["n_settled"],
            "target": min_settled,
            "remaining": max(0, min_settled - aggregate["n_settled"]),
            "pass": settled_pass,
        },
        "roi": {
            "value": roi_value,
            "min_roi": min_roi,
            "pass": roi_pass,
        },
        "calibration_delta_pp": {
            "value": cal_value,
            "tolerance_pp": calibration_tolerance_pp,
            "pass": cal_pass,
        },
        "under_drift_alerts": {
            "days_with_alert": drift["days_with_alert"],
            "lookback_days": drift["lookback_days"],
            "persistence_threshold": drift_persistence_threshold,
            "pass": drift_pass,
        },
    }

    # Verdict ladder. Each step is hit only when the prior conditions
    # are NOT the current limiter, so the operator sees the highest-
    # priority gap first.
    if n_under_bets_total == 0:
        status = "NOT_EMITTING"
        summary = (
            f"No paper UNDER bets in trailing {trailing_days}d. Run "
            "`--under-mode paper` to start accumulating B4 evidence."
        )
    elif not sessions_pass:
        status = "INSUFFICIENT_SESSIONS"
        remaining = min_sessions - n_sessions_with_under_bets
        summary = (
            f"{n_sessions_with_under_bets}/{min_sessions} sessions "
            f"({n_sessions_with_under_bets * 100 // min_sessions}%); "
            f"need {remaining} more sessions with at least one paper "
            "UNDER bet to clear the session milestone."
        )
    elif not settled_pass:
        status = "INSUFFICIENT_OUTCOMES"
        remaining = min_settled - aggregate["n_settled"]
        summary = (
            f"Sessions met ({n_sessions_with_under_bets}/{min_sessions}); "
            f"need {remaining} more settled outcomes to hit "
            f"{min_settled}."
        )
    elif not roi_pass:
        status = "SUB_ZERO_ROI"
        roi_str = (
            f"{roi_value:+.1%}" if roi_value is not None else "n/a"
        )
        summary = (
            f"All n thresholds met (sessions={n_sessions_with_under_bets}, "
            f"settled={aggregate['n_settled']}) but UNDER taker ROI is "
            f"{roi_str} (need > {min_roi:.0%}). Tune UNDER gates from "
            "cohort data before any flip to live."
        )
    elif not cal_pass:
        status = "CALIBRATION_OFF"
        cal_str = (
            f"{cal_value:+.2f}pp" if cal_value is not None else "n/a"
        )
        summary = (
            f"n + ROI conditions met but UNDER calibration delta is "
            f"{cal_str} (tolerance ±{calibration_tolerance_pp:.1f}pp). "
            "Refit the UNDER calibrator on the accumulated sample."
        )
    elif not drift_pass:
        status = "DRIFT_ALERT_PERSISTENT"
        summary = (
            f"Numeric thresholds met but UNDER drift alerts fired on "
            f"{drift['days_with_alert']}/{drift['lookback_days']} of "
            f"the last {drift_lookback_days}d "
            f"(>= {drift_persistence_threshold} = persistent). "
            "Investigate the UNDER drift source before flipping."
        )
    else:
        status = "READY"
        roi_str = (
            f"{roi_value:+.1%}" if roi_value is not None else "n/a"
        )
        cal_str = (
            f"{cal_value:+.2f}pp" if cal_value is not None else "n/a"
        )
        summary = (
            f"B4 cleared: {n_sessions_with_under_bets}/{min_sessions} "
            f"sessions, {aggregate['n_settled']}/{min_settled} settled, "
            f"ROI={roi_str}, calibration {cal_str}, no persistent "
            "UNDER drift. Operator can ship `--under-mode live` + "
            "`--quote-engine-mode act` after a fresh design review."
        )
    payload["status"] = status
    payload["verdict_summary"] = summary

    # T5 (2026-06-15): DORMANT short-circuit. The B4 limiter is UNDER signal
    # QUALITY, not session count, so forcing volume can't clear it -- treating
    # it as an active clock just emitted ~a year of INSUFFICIENT_SESSIONS
    # noise. Preserve the underlying ladder + progress for anyone who looks,
    # report status=DORMANT, and suppress the verdict-ladder Notes alerts.
    if dormant:
        payload["dormant"] = True
        payload["underlying_status"] = status
        payload["underlying_verdict_summary"] = summary
        payload["status"] = "DORMANT"
        payload["verdict_summary"] = (
            "B4 is DORMANT (operator decision 2026-06-15): the limiter is "
            "UNDER signal QUALITY, not session count -- score_event UNDER FV "
            "is near-flat (~0.30), per-line UNDER overfits, under-pair "
            "liquidity ~49%, so honest enforce-mode emits few defensible bets "
            "(~8 in 2 weeks) and forcing volume would only churn the ladder. "
            f"Underlying ladder still computed: {status}. M_under_paper keeps "
            "running so honest UNDER data accrues passively; re-activate when "
            "the UNDER calibrator discriminates (e.g. no_score_drift "
            "market-anchored alpha) or liquidity rises."
        )
        return payload

    # ------------------------------------------------------------------
    # Alerts (Notes-feed emission). Only the actionable transitions
    # fire; quiet states (NOT_EMITTING, INSUFFICIENT_*) stay silent in
    # Notes because the milestone progress is already visible in the
    # trailing-7d under_outcomes block.
    # ------------------------------------------------------------------
    if status == "READY":
        payload["alerts"].append(
            f"Under-B4: READY -- {summary}"
        )
    elif (
        status in {"SUB_ZERO_ROI", "CALIBRATION_OFF",
                   "DRIFT_ALERT_PERSISTENT"}
        and aggregate["n_settled"] >= min_n_for_failure_alert
    ):
        payload["alerts"].append(
            f"Under-B4: {status} -- {summary}"
        )

    return payload
