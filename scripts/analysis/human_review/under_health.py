from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .constants import (
    DEFAULT_CANDIDATE_DIR,
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

    settled_rows: List[Dict[str, Any]] = []
    n_missing_outcome = 0
    n_missing_ask = 0
    for r in shadow_under:
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
    payload["n_settled"] = len(today["settled_rows"])
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
