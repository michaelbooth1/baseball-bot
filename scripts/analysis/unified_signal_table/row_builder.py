"""Canonical signals_master row builder and row-level quality checks."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from scripts.trading.model_families import infer_signal_model_family
from scripts.trading.remaining_opportunity import compute_remaining_opportunity_fields
from scripts.trading.scoring_path_features import SCORING_PATH_FIELD_KEYS
from scripts.trading.shadow_diagnostic_features import compute_shadow_diagnostic_fields
from scripts.trading.weather_client import WEATHER_FEATURE_FIELD_KEYS
from scripts.analysis.unified_signal_table.schema import (
    CaptureData,
    INFERENCE_PANEL_COLUMNS,
    MARKET_COMPLEMENT_COLUMNS,
    PARAM_KEYS_COMMON,
    PARAM_KEYS_LIVE,
    SCHEMA_VERSION,
)
from scripts.analysis.unified_signal_table.snapshot_features import _compute_phase2_capture_features, _extract_book_levels
from scripts.analysis.unified_signal_table.utils import (
    _best_event_time,
    _coalesce,
    _date_in_range,
    _infer_session_date,
    _parse_iso_to_epoch,
    _safe_bool,
    _safe_float,
    _safe_int,
)

def _extract_param_values(session_params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    params = session_params or {}
    out = {f"param_{k}": params.get(k) for k in PARAM_KEYS_COMMON + PARAM_KEYS_LIVE}
    return out


def _timestamp_order_invalid(
    placed_at: Optional[str],
    order_placed_at: Optional[str],
    filled_at: Optional[str],
    cancelled_at: Optional[str],
    settled_at: Optional[str],
) -> bool:
    t_placed = _parse_iso_to_epoch(placed_at)
    t_order = _parse_iso_to_epoch(order_placed_at)
    t_filled = _parse_iso_to_epoch(filled_at)
    t_cancelled = _parse_iso_to_epoch(cancelled_at)
    t_settled = _parse_iso_to_epoch(settled_at)

    if t_placed is not None and t_order is not None and t_order < t_placed:
        return True
    if t_order is not None and t_filled is not None and t_filled < t_order:
        return True
    if t_order is not None and t_cancelled is not None and t_cancelled < t_order:
        return True
    if t_settled is not None and t_filled is not None and t_settled < t_filled:
        return True
    if t_settled is not None and t_cancelled is not None and t_settled < t_cancelled:
        return True
    return False


def _build_order_events_rows(
    mode: str,
    ledger_events: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for bet_id, events in sorted(ledger_events.items()):
        for idx, event in enumerate(events, start=1):
            rows.append(
                {
                    "mode": mode,
                    "bet_id": bet_id,
                    "event_seq": idx,
                    "event_type": event.get("_event") or event.get("order_status") or "unknown",
                    "event_time": _best_event_time(event),
                    "raw_order_status": event.get("order_status"),
                    "raw_cancel_reason": event.get("cancel_reason"),
                    "fill_price": _safe_float(event.get("fill_price")),
                    "fill_size": _safe_float(event.get("fill_size")),
                    "stake": _safe_float(event.get("stake")),
                    "realized_profit_at_event": _safe_float(event.get("profit")),
                }
            )
    return rows


def _canonical_value(
    field: str,
    session_bet: Optional[Dict[str, Any]],
    ledger_events: List[Dict[str, Any]],
    capture_header: Optional[Dict[str, Any]],
) -> Any:
    values: List[Any] = []
    if session_bet:
        values.append(session_bet.get(field))
    for event in reversed(ledger_events):
        values.append(event.get(field))
    if capture_header:
        values.append(capture_header.get(field))
    return _coalesce(values)


def _state_value_value(
    field: str,
    candidate_row: Optional[Dict[str, Any]],
    session_bet: Optional[Dict[str, Any]],
    ledger_events: List[Dict[str, Any]],
    capture_header: Optional[Dict[str, Any]],
) -> Any:
    values: List[Any] = []
    if candidate_row:
        values.append(candidate_row.get(field))
    if session_bet:
        values.append(session_bet.get(field))
    for event in reversed(ledger_events):
        values.append(event.get(field))
    if capture_header:
        values.append(capture_header.get(field))
    return _coalesce(values)


def _build_state_value_fields(
    *,
    candidate_row: Optional[Dict[str, Any]],
    session_bet: Optional[Dict[str, Any]],
    ledger_events: List[Dict[str, Any]],
    capture_header: Optional[Dict[str, Any]],
    fair_value: Optional[float],
    inferred_runs: Optional[int],
) -> Dict[str, Any]:
    current_fv = _safe_float(_state_value_value(
        "current_state_value_fv_raw",
        candidate_row,
        session_bet,
        ledger_events,
        capture_header,
    ))
    after_fv = _safe_float(_state_value_value(
        "shadow_fv_after_inferred_score",
        candidate_row,
        session_bet,
        ledger_events,
        capture_header,
    ))
    if after_fv is None:
        after_fv = fair_value

    return {
        "state_value_strategy": _state_value_value("state_value_strategy", candidate_row, session_bet, ledger_events, capture_header),
        "current_state_value_base_poisson": _safe_float(_state_value_value("current_state_value_base_poisson", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_base_empirical": _safe_float(_state_value_value("current_state_value_base_empirical", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_line_key_poisson": _state_value_value("current_state_value_line_key_poisson", candidate_row, session_bet, ledger_events, capture_header),
        "current_state_value_line_key_empirical": _state_value_value("current_state_value_line_key_empirical", candidate_row, session_bet, ledger_events, capture_header),
        "current_state_value_used_fallback": _safe_bool(_state_value_value("current_state_value_used_fallback", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_state_fallback_level": _safe_int(_state_value_value("current_state_value_state_fallback_level", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_state_fallback_label": _state_value_value("current_state_value_state_fallback_label", candidate_row, session_bet, ledger_events, capture_header),
        "current_state_value_state_cell_key": _state_value_value("current_state_value_state_cell_key", candidate_row, session_bet, ledger_events, capture_header),
        "current_state_value_line_fallback_mode": _state_value_value("current_state_value_line_fallback_mode", candidate_row, session_bet, ledger_events, capture_header),
        "current_state_value_line_source_key": _state_value_value("current_state_value_line_source_key", candidate_row, session_bet, ledger_events, capture_header),
        "current_state_value_empirical_line_fallback_mode": _state_value_value("current_state_value_empirical_line_fallback_mode", candidate_row, session_bet, ledger_events, capture_header),
        "current_state_value_empirical_line_source_key": _state_value_value("current_state_value_empirical_line_source_key", candidate_row, session_bet, ledger_events, capture_header),
        "current_state_value_effective_n_proxy": _safe_float(_state_value_value("current_state_value_effective_n_proxy", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_stage1_trust_weight": _safe_float(_state_value_value("current_state_value_stage1_trust_weight", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_stage1_support_bucket": _state_value_value("current_state_value_stage1_support_bucket", candidate_row, session_bet, ledger_events, capture_header),
        "current_state_value_exact_cell_support": _safe_bool(_state_value_value("current_state_value_exact_cell_support", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_poisson_line_exact": _safe_bool(_state_value_value("current_state_value_poisson_line_exact", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_empirical_line_exact": _safe_bool(_state_value_value("current_state_value_empirical_line_exact", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_empirical_sample_support": _safe_float(_state_value_value("current_state_value_empirical_sample_support", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_empirical_sample_bucket": _state_value_value("current_state_value_empirical_sample_bucket", candidate_row, session_bet, ledger_events, capture_header),
        "current_state_value_state_fallback_penalty": _safe_float(_state_value_value("current_state_value_state_fallback_penalty", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_line_fallback_penalty": _safe_float(_state_value_value("current_state_value_line_fallback_penalty", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_fv_raw": current_fv,
        "current_state_value_stage2_run_env_delta": _safe_float(_state_value_value("current_state_value_stage2_run_env_delta", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_team_offense_delta": _safe_float(_state_value_value("current_state_value_team_offense_delta", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_edge": _safe_float(_state_value_value("current_state_value_edge", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_empirical_edge": _safe_float(_state_value_value("current_state_value_empirical_edge", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_away_score": _safe_int(_state_value_value("current_state_value_away_score", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_home_score": _safe_int(_state_value_value("current_state_value_home_score", candidate_row, session_bet, ledger_events, capture_header)),
        "current_state_value_total": _safe_int(_state_value_value("current_state_value_total", candidate_row, session_bet, ledger_events, capture_header)),
        "shadow_fv_current_state": _safe_float(_coalesce([
            _state_value_value("shadow_fv_current_state", candidate_row, session_bet, ledger_events, capture_header),
            current_fv,
        ])),
        "shadow_fv_after_inferred_score": after_fv,
        "shadow_fv_inferred_lift": _safe_float(_state_value_value("shadow_fv_inferred_lift", candidate_row, session_bet, ledger_events, capture_header)),
        "shadow_no_event_edge": _safe_float(_state_value_value("shadow_no_event_edge", candidate_row, session_bet, ledger_events, capture_header)),
        "shadow_after_event_edge": _safe_float(_state_value_value("shadow_after_event_edge", candidate_row, session_bet, ledger_events, capture_header)),
        "shadow_p_score_event_proxy": _safe_float(_state_value_value("shadow_p_score_event_proxy", candidate_row, session_bet, ledger_events, capture_header)),
        "shadow_phantom_risk_score": _safe_float(_state_value_value("shadow_phantom_risk_score", candidate_row, session_bet, ledger_events, capture_header)),
        "shadow_phantom_risk_band": _state_value_value("shadow_phantom_risk_band", candidate_row, session_bet, ledger_events, capture_header),
        "shadow_transition_model": _state_value_value("shadow_transition_model", candidate_row, session_bet, ledger_events, capture_header),
        "shadow_transition_inferred_runs": _safe_int(_coalesce([
            _state_value_value("shadow_transition_inferred_runs", candidate_row, session_bet, ledger_events, capture_header),
            inferred_runs,
        ])),
        "shadow_extreme_edge": _safe_bool(_state_value_value("shadow_extreme_edge", candidate_row, session_bet, ledger_events, capture_header)),
        "shadow_extreme_edge_threshold": _safe_float(_state_value_value("shadow_extreme_edge_threshold", candidate_row, session_bet, ledger_events, capture_header)),
        "shadow_ltp_at_signal": _safe_float(_state_value_value("shadow_ltp_at_signal", candidate_row, session_bet, ledger_events, capture_header)),
        "shadow_ltp_ask_gap": _safe_float(_state_value_value("shadow_ltp_ask_gap", candidate_row, session_bet, ledger_events, capture_header)),
        "shadow_ltp_ask_gap_threshold": _safe_float(_state_value_value("shadow_ltp_ask_gap_threshold", candidate_row, session_bet, ledger_events, capture_header)),
        "shadow_ltp_ask_gap_exceeded": _safe_bool(_state_value_value("shadow_ltp_ask_gap_exceeded", candidate_row, session_bet, ledger_events, capture_header)),
        "shadow_risk_tags": _state_value_value("shadow_risk_tags", candidate_row, session_bet, ledger_events, capture_header),
        "shadow_low_ask_high_edge": _safe_bool(_state_value_value("shadow_low_ask_high_edge", candidate_row, session_bet, ledger_events, capture_header)),
        "shadow_runs_needed_exact_3p5": _safe_bool(_state_value_value("shadow_runs_needed_exact_3p5", candidate_row, session_bet, ledger_events, capture_header)),
        "shadow_current_state_edge_bucket": _state_value_value("shadow_current_state_edge_bucket", candidate_row, session_bet, ledger_events, capture_header),
        "shadow_phantom_risk_bucket": _state_value_value("shadow_phantom_risk_bucket", candidate_row, session_bet, ledger_events, capture_header),
        "shadow_current_phantom_combo_bucket": _state_value_value("shadow_current_phantom_combo_bucket", candidate_row, session_bet, ledger_events, capture_header),
        "shadow_inning_bucket": _state_value_value("shadow_inning_bucket", candidate_row, session_bet, ledger_events, capture_header),
        "shadow_inning_runs_needed_bucket": _state_value_value("shadow_inning_runs_needed_bucket", candidate_row, session_bet, ledger_events, capture_header),
        "shadow_bottom9_home_lead_context": _state_value_value("shadow_bottom9_home_lead_context", candidate_row, session_bet, ledger_events, capture_header),
        "shadow_home_skip_bottom9_risk_bucket": _state_value_value("shadow_home_skip_bottom9_risk_bucket", candidate_row, session_bet, ledger_events, capture_header),
        "shadow_post_tr20_extreme_020_pass": _safe_bool(_state_value_value("shadow_post_tr20_extreme_020_pass", candidate_row, session_bet, ledger_events, capture_header)),
        "shadow_post_tr20_ask_ramp_v2_pass": _safe_bool(_state_value_value("shadow_post_tr20_ask_ramp_v2_pass", candidate_row, session_bet, ledger_events, capture_header)),
        "shadow_post_tr20_gate6_relax_enforce_pass": _safe_bool(_state_value_value("shadow_post_tr20_gate6_relax_enforce_pass", candidate_row, session_bet, ledger_events, capture_header)),
        "shadow_post_tr20_combined_pass": _safe_bool(_state_value_value("shadow_post_tr20_combined_pass", candidate_row, session_bet, ledger_events, capture_header)),
    }


def _build_inferred_state_fields(
    *,
    candidate_row: Optional[Dict[str, Any]],
    session_bet: Optional[Dict[str, Any]],
    ledger_events: List[Dict[str, Any]],
    capture_header: Optional[Dict[str, Any]],
    base_fair_value: Optional[float],
    decision_ask: Optional[float],
) -> Dict[str, Any]:
    base_poisson = _safe_float(_coalesce([
        _state_value_value("inferred_state_base_poisson", candidate_row, session_bet, ledger_events, capture_header),
        base_fair_value,
    ]))
    base_empirical = _safe_float(_state_value_value(
        "inferred_state_base_empirical",
        candidate_row,
        session_bet,
        ledger_events,
        capture_header,
    ))
    poisson_minus_empirical = _safe_float(_state_value_value(
        "inferred_state_poisson_minus_empirical",
        candidate_row,
        session_bet,
        ledger_events,
        capture_header,
    ))
    if poisson_minus_empirical is None and base_poisson is not None and base_empirical is not None:
        poisson_minus_empirical = base_poisson - base_empirical
    empirical_edge = _safe_float(_state_value_value(
        "inferred_state_empirical_edge",
        candidate_row,
        session_bet,
        ledger_events,
        capture_header,
    ))
    if empirical_edge is None and base_empirical is not None and decision_ask is not None:
        empirical_edge = base_empirical - decision_ask
    fields = {
        "inferred_state_base_poisson": base_poisson,
        "inferred_state_base_empirical": base_empirical,
        "inferred_state_poisson_minus_empirical": poisson_minus_empirical,
        "inferred_state_empirical_edge": empirical_edge,
        "inferred_state_n": _safe_float(_state_value_value("inferred_state_n", candidate_row, session_bet, ledger_events, capture_header)),
        "inferred_state_n_samples": _safe_float(_state_value_value("inferred_state_n_samples", candidate_row, session_bet, ledger_events, capture_header)),
        "inferred_state_weighted_n": _safe_float(_state_value_value("inferred_state_weighted_n", candidate_row, session_bet, ledger_events, capture_header)),
        "inferred_state_effective_n": _safe_float(_state_value_value("inferred_state_effective_n", candidate_row, session_bet, ledger_events, capture_header)),
        "inferred_state_effective_n_proxy": _safe_float(_state_value_value("inferred_state_effective_n_proxy", candidate_row, session_bet, ledger_events, capture_header)),
        "inferred_state_stage1_trust_weight": _safe_float(_state_value_value("inferred_state_stage1_trust_weight", candidate_row, session_bet, ledger_events, capture_header)),
        "inferred_state_stage1_support_bucket": _state_value_value("inferred_state_stage1_support_bucket", candidate_row, session_bet, ledger_events, capture_header),
        "inferred_state_exact_cell_support": _safe_bool(_state_value_value("inferred_state_exact_cell_support", candidate_row, session_bet, ledger_events, capture_header)),
        "inferred_state_poisson_line_exact": _safe_bool(_state_value_value("inferred_state_poisson_line_exact", candidate_row, session_bet, ledger_events, capture_header)),
        "inferred_state_empirical_line_exact": _safe_bool(_state_value_value("inferred_state_empirical_line_exact", candidate_row, session_bet, ledger_events, capture_header)),
        "inferred_state_empirical_sample_support": _safe_float(_state_value_value("inferred_state_empirical_sample_support", candidate_row, session_bet, ledger_events, capture_header)),
        "inferred_state_empirical_sample_bucket": _state_value_value("inferred_state_empirical_sample_bucket", candidate_row, session_bet, ledger_events, capture_header),
        "inferred_state_state_fallback_penalty": _safe_float(_state_value_value("inferred_state_state_fallback_penalty", candidate_row, session_bet, ledger_events, capture_header)),
        "inferred_state_line_fallback_penalty": _safe_float(_state_value_value("inferred_state_line_fallback_penalty", candidate_row, session_bet, ledger_events, capture_header)),
        "inferred_state_fallback_level": _safe_int(_state_value_value("inferred_state_fallback_level", candidate_row, session_bet, ledger_events, capture_header)),
        "inferred_state_fallback_label": _state_value_value("inferred_state_fallback_label", candidate_row, session_bet, ledger_events, capture_header),
        "inferred_state_cell_key": _state_value_value("inferred_state_cell_key", candidate_row, session_bet, ledger_events, capture_header),
        "inferred_state_line_key_poisson": _state_value_value("inferred_state_line_key_poisson", candidate_row, session_bet, ledger_events, capture_header),
        "inferred_state_line_key_empirical": _state_value_value("inferred_state_line_key_empirical", candidate_row, session_bet, ledger_events, capture_header),
        "inferred_state_line_fallback_mode": _state_value_value("inferred_state_line_fallback_mode", candidate_row, session_bet, ledger_events, capture_header),
        "inferred_state_empirical_line_fallback_mode": _state_value_value("inferred_state_empirical_line_fallback_mode", candidate_row, session_bet, ledger_events, capture_header),
        "inferred_state_empirical_line_source_key": _state_value_value("inferred_state_empirical_line_source_key", candidate_row, session_bet, ledger_events, capture_header),
        "inferred_state_empirical_line_source_key_low": _state_value_value("inferred_state_empirical_line_source_key_low", candidate_row, session_bet, ledger_events, capture_header),
        "inferred_state_empirical_line_source_key_high": _state_value_value("inferred_state_empirical_line_source_key_high", candidate_row, session_bet, ledger_events, capture_header),
        "inferred_state_used_fallback": _safe_bool(_state_value_value("inferred_state_used_fallback", candidate_row, session_bet, ledger_events, capture_header)),
        "inferred_state_base_source": _state_value_value("inferred_state_base_source", candidate_row, session_bet, ledger_events, capture_header),
    }
    for key in INFERENCE_PANEL_COLUMNS:
        value = _state_value_value(key, candidate_row, session_bet, ledger_events, capture_header)
        if key.endswith(("_selected", "_support_exact_cell_support", "_support_poisson_line_exact", "_support_empirical_line_exact")):
            fields[key] = _safe_bool(value)
        elif key.endswith(("_away_score", "_home_score", "_total", "_fallback_level", "_selected_runs")):
            fields[key] = _safe_int(value)
        elif key.endswith((
            "_base_poisson",
            "_base_empirical",
            "_poisson_minus_empirical",
            "_distance_to_ask",
            "_empirical_distance_to_ask",
            "_n",
            "_n_samples",
            "_effective_n",
            "_support_effective_n_proxy",
            "_support_stage1_trust_weight",
            "_support_empirical_sample_support",
            "_support_state_fallback_penalty",
            "_support_line_fallback_penalty",
        )):
            fields[key] = _safe_float(value)
        else:
            fields[key] = value
    return fields


def _build_market_complement_fields(
    *,
    candidate_row: Optional[Dict[str, Any]],
    session_bet: Optional[Dict[str, Any]],
    ledger_events: List[Dict[str, Any]],
    capture_header: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for key in MARKET_COMPLEMENT_COLUMNS:
        value = _state_value_value(key, candidate_row, session_bet, ledger_events, capture_header)
        if key == "under_pair_available":
            fields[key] = _safe_bool(value)
        elif key.endswith("_source") or key == "under_token_id":
            fields[key] = value
        else:
            fields[key] = _safe_float(value)
    return fields


def _build_weather_fields(
    *,
    candidate_row: Optional[Dict[str, Any]],
    session_bet: Optional[Dict[str, Any]],
    ledger_events: List[Dict[str, Any]],
    capture_header: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        key: _state_value_value(key, candidate_row, session_bet, ledger_events, capture_header)
        for key in WEATHER_FEATURE_FIELD_KEYS
    }


def _build_scoring_path_fields(
    *,
    candidate_row: Optional[Dict[str, Any]],
    session_bet: Optional[Dict[str, Any]],
    ledger_events: List[Dict[str, Any]],
    capture_header: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    int_keys = {"scoring_path_innings_observed", "scoring_path_runs_observed", "scoreless_streak"}
    bool_keys = {"scoring_path_available"}
    text_keys = {"scoring_path_inning_runs"}
    out: Dict[str, Any] = {}
    for key in SCORING_PATH_FIELD_KEYS:
        value = _state_value_value(key, candidate_row, session_bet, ledger_events, capture_header)
        if key in int_keys:
            out[key] = _safe_int(value)
        elif key in bool_keys:
            out[key] = _safe_bool(value)
        elif key in text_keys:
            out[key] = value
        else:
            out[key] = _safe_float(value)
    return out


def build_master_rows_for_mode(
    mode: str,
    session_rows: Dict[str, Dict[str, Any]],
    ledger_events: Dict[str, List[Dict[str, Any]]],
    captures: Dict[str, CaptureData],
    candidate_rows: Dict[str, Dict[str, Any]],
    min_date: Optional[str],
    max_date: Optional[str],
    horizons: List[int],
    fill_window_secs: int,
    master_columns: List[str],
    warnings: List[str],
    hard_errors: List[str],
) -> List[Dict[str, Any]]:
    all_ids = set(session_rows.keys()) | set(ledger_events.keys()) | set(captures.keys()) | set(candidate_rows.keys())
    out: List[Dict[str, Any]] = []

    for bet_id in sorted(all_ids):
        srow = session_rows.get(bet_id)
        session_bet = srow["bet"] if srow else None
        session_params = srow["session_params"] if srow else {}
        events = ledger_events.get(bet_id, [])
        final_event = events[-1] if events else {}
        cap = captures.get(bet_id)
        cap_header = cap.header if cap else None
        candidate_row = candidate_rows.get(bet_id)

        base = session_bet or final_event or candidate_row or cap_header or {}
        if not isinstance(base, dict):
            warnings.append(f"[{mode}] base record is not a dict for bet_id={bet_id}")
            continue

        session_date = ""
        if srow:
            session_date = srow["session_date"]
        else:
            session_date = _infer_session_date(bet_id, base)
        if not _date_in_range(session_date, min_date, max_date):
            continue

        placed_at = _canonical_value("placed_at", session_bet, events, cap_header)
        order_placed_at = _canonical_value("order_placed_at", session_bet, events, cap_header)
        filled_at = _canonical_value("filled_at", session_bet, events, cap_header)
        cancelled_at = _canonical_value("cancelled_at", session_bet, events, cap_header)
        settled_at = _canonical_value("settled_at", session_bet, events, cap_header)

        line_val = _safe_float(_canonical_value("line", session_bet, events, cap_header))
        away_score_before = _safe_int(_canonical_value("away_score_before", session_bet, events, cap_header))
        home_score_before = _safe_int(_canonical_value("home_score_before", session_bet, events, cap_header))
        current_total = (
            away_score_before + home_score_before
            if away_score_before is not None and home_score_before is not None
            else None
        )
        lead_abs = (
            abs(away_score_before - home_score_before)
            if away_score_before is not None and home_score_before is not None
            else None
        )
        runs_needed = (line_val - current_total) if line_val is not None and current_total is not None else None

        entry_ask = _safe_float(_canonical_value("entry_ask", session_bet, events, cap_header))
        decision_ask = _safe_float(
            _coalesce(
                [
                    _canonical_value("decision_ask", session_bet, events, cap_header),
                    entry_ask,
                ]
            )
        )
        fair_value = _safe_float(_canonical_value("fair_value", session_bet, events, cap_header))
        base_fair_value = _safe_float(_canonical_value("base_fair_value", session_bet, events, cap_header))
        limit_price = _safe_float(_canonical_value("limit_price", session_bet, events, cap_header))
        posted_limit = _safe_float(
            _coalesce(
                [
                    _canonical_value("posted_limit", session_bet, events, cap_header),
                    limit_price,
                ]
            )
        )
        edge_at_limit = (
            fair_value - posted_limit if fair_value is not None and posted_limit is not None else None
        )
        inferred_runs = _safe_int(_canonical_value("inferred_runs", session_bet, events, cap_header))
        state_value_fields = _build_state_value_fields(
            candidate_row=candidate_row,
            session_bet=session_bet,
            ledger_events=events,
            capture_header=cap_header,
            fair_value=fair_value,
            inferred_runs=inferred_runs,
        )
        inferred_state_fields = _build_inferred_state_fields(
            candidate_row=candidate_row,
            session_bet=session_bet,
            ledger_events=events,
            capture_header=cap_header,
            base_fair_value=base_fair_value,
            decision_ask=decision_ask,
        )
        market_complement_fields = _build_market_complement_fields(
            candidate_row=candidate_row,
            session_bet=session_bet,
            ledger_events=events,
            capture_header=cap_header,
        )
        weather_fields = _build_weather_fields(
            candidate_row=candidate_row,
            session_bet=session_bet,
            ledger_events=events,
            capture_header=cap_header,
        )
        scoring_path_fields = _build_scoring_path_fields(
            candidate_row=candidate_row,
            session_bet=session_bet,
            ledger_events=events,
            capture_header=cap_header,
        )
        family_probe: Dict[str, Any] = {}
        for source in (cap_header, final_event, session_bet, candidate_row, state_value_fields):
            if isinstance(source, dict):
                family_probe.update(source)
        signal_model_family = infer_signal_model_family(family_probe)

        order_status_session = session_bet.get("order_status") if session_bet else None
        order_status_ledger = final_event.get("order_status") if final_event else None
        order_status_final = _coalesce([order_status_session, order_status_ledger])

        cancel_reason_final = _coalesce(
            [
                session_bet.get("cancel_reason") if session_bet else None,
                final_event.get("cancel_reason") if final_event else None,
            ]
        )

        settled_raw = _canonical_value("settled", session_bet, events, cap_header)
        settled = bool(settled_raw) if settled_raw is not None else False
        won_counterfactual = _canonical_value("won", session_bet, events, cap_header)

        if mode == "paper":
            realized_executed = True
        else:
            realized_executed = str(order_status_final).lower() == "filled"

        payout_raw = _safe_float(_canonical_value("payout", session_bet, events, cap_header))
        profit_raw = _safe_float(_canonical_value("profit", session_bet, events, cap_header))

        if settled:
            if realized_executed:
                realized_payout = payout_raw
                realized_profit = profit_raw
            else:
                realized_payout = 0.0
                realized_profit = 0.0
        else:
            realized_payout = None
            realized_profit = None

        realized_win: Optional[bool]
        if settled and won_counterfactual is not None:
            realized_win = bool(realized_executed and bool(won_counterfactual))
        else:
            realized_win = None

        t0_book = cap.t0_book if cap else {}
        t0_best_bid, t0_best_ask, t0_spread, t0_mid = _extract_book_levels(t0_book)

        phase2_capture_features = _compute_phase2_capture_features(
            cap=cap,
            horizons=horizons,
            fill_window_secs=fill_window_secs,
            t0_best_ask=t0_best_ask,
            entry_ask=decision_ask,
            limit_price=posted_limit,
        )

        # Quality flags
        flag_missing_settlement = not settled
        flag_missing_capture = cap is None
        flag_missing_ledger = mode == "live" and not events
        fill_price = _safe_float(_canonical_value("fill_price", session_bet, events, cap_header))
        actual_fill_price = _safe_float(
            _coalesce(
                [
                    _canonical_value("actual_fill_price", session_bet, events, cap_header),
                    fill_price,
                ]
            )
        )
        flag_settled_with_null_fill_price = bool(
            settled and realized_executed and actual_fill_price is None and mode == "live"
        )
        flag_profit_nonzero_without_fill = bool(
            settled
            and (not realized_executed)
            and profit_raw is not None
            and abs(profit_raw) > 1e-9
        )
        flag_order_status_conflict = bool(
            order_status_session is not None
            and order_status_ledger is not None
            and str(order_status_session).lower() != str(order_status_ledger).lower()
        )
        cap_token = cap_header.get("token_id") if cap_header else None
        over_token_id = _coalesce(
            [
                _canonical_value("over_token_id", session_bet, events, cap_header),
                cap_token,
            ]
        )
        flag_capture_token_mismatch = bool(
            cap_token and over_token_id and str(cap_token) != str(over_token_id)
        )
        flag_ts_order_invalid = _timestamp_order_invalid(
            placed_at=placed_at,
            order_placed_at=order_placed_at,
            filled_at=filled_at,
            cancelled_at=cancelled_at,
            settled_at=settled_at,
        )
        quality_issue_count = sum(
            [
                flag_missing_settlement,
                flag_missing_capture,
                flag_missing_ledger,
                flag_settled_with_null_fill_price,
                flag_profit_nonzero_without_fill,
                flag_order_status_conflict,
                flag_capture_token_mismatch,
                flag_ts_order_invalid,
            ]
        )

        row = {k: None for k in master_columns}
        row.update(
            {
                "schema_version": SCHEMA_VERSION,
                "mode": mode,
                "bet_id": bet_id,
                "session_date": session_date,
                "session_path": srow["session_path"] if srow else None,
                "source_has_session_bet": bool(srow),
                "source_has_ledger_events": bool(events),
                "source_has_capture": bool(cap),
                "capture_path": cap.path if cap else None,
                "placed_at": placed_at,
                "order_placed_at": order_placed_at,
                "filled_at": filled_at,
                "cancelled_at": cancelled_at,
                "settled_at": settled_at,
                "signal_epoch_s": _parse_iso_to_epoch(placed_at),
                "game_pk": _safe_int(_canonical_value("game_pk", session_bet, events, cap_header)),
                "away_abbrev": _canonical_value("away_abbrev", session_bet, events, cap_header),
                "home_abbrev": _canonical_value("home_abbrev", session_bet, events, cap_header),
                "line": _canonical_value("line", session_bet, events, cap_header),
                "side": _canonical_value("side", session_bet, events, cap_header),
                "signal_model_family": signal_model_family,
                "over_token_id": over_token_id,
                "inning": _safe_int(_canonical_value("inning", session_bet, events, cap_header)),
                "inning_state": _canonical_value("inning_state", session_bet, events, cap_header),
                "outs": _safe_int(_canonical_value("outs", session_bet, events, cap_header)),
                "runners_on": _safe_int(_canonical_value("runners_on", session_bet, events, cap_header)),
                "away_score_before": away_score_before,
                "home_score_before": home_score_before,
                "current_total": current_total,
                "lead_abs": lead_abs,
                "runs_needed": runs_needed,
                "inferred_runs": inferred_runs,
                "inferred_away_after": _safe_int(_canonical_value("inferred_away_after", session_bet, events, cap_header)),
                "inferred_home_after": _safe_int(_canonical_value("inferred_home_after", session_bet, events, cap_header)),
                "entry_ask": entry_ask,
                "decision_ask": decision_ask,
                **market_complement_fields,
                "fair_value": fair_value,
                "base_fair_value": base_fair_value,
                **inferred_state_fields,
                "stage2_run_env_delta": _safe_float(_canonical_value("stage2_run_env_delta", session_bet, events, cap_header)),
                "team_offense_delta": _safe_float(_canonical_value("team_offense_delta", session_bet, events, cap_header)),
                "edge_at_ask": _safe_float(_canonical_value("edge", session_bet, events, cap_header)),
                # Forward-compat alias: source bet/candidate rows use the
                # plain `edge` name, and ad-hoc consumers (audit scripts,
                # future investigations) intuitively reach for `row.get("edge")`
                # and silently get None/0 today. Same value as edge_at_ask;
                # carrying both keeps both naming conventions valid.
                "edge": _safe_float(_canonical_value("edge", session_bet, events, cap_header)),
                "limit_price": limit_price,
                "posted_limit": posted_limit,
                "edge_at_limit": edge_at_limit,
                "stake": _safe_float(_canonical_value("stake", session_bet, events, cap_header)),
                "stake_mode": _canonical_value("stake_mode", session_bet, events, cap_header),
                "kelly_full_fraction": _safe_float(_canonical_value("kelly_full_fraction", session_bet, events, cap_header)),
                "kelly_fraction_used": _safe_float(_canonical_value("kelly_fraction_used", session_bet, events, cap_header)),
                "order_status_final": order_status_final,
                "cancel_reason_final": cancel_reason_final,
                "t0_best_bid": t0_best_bid,
                "t0_best_ask": t0_best_ask,
                "t0_spread": t0_spread,
                "t0_mid": t0_mid,
                "t0_ltp": _safe_float(t0_book.get("ltp")),
                "t0_latency_ms": _safe_float(t0_book.get("latency_ms")),
                "t0_total_bid_depth": _safe_float(t0_book.get("total_bid_depth")),
                "t0_total_ask_depth": _safe_float(t0_book.get("total_ask_depth")),
                "settled": settled,
                "won_counterfactual": won_counterfactual,
                "final_away": _safe_int(_canonical_value("final_away", session_bet, events, cap_header)),
                "final_home": _safe_int(_canonical_value("final_home", session_bet, events, cap_header)),
                "final_total": _safe_int(_canonical_value("final_total", session_bet, events, cap_header)),
                "realized_executed": realized_executed,
                "realized_win": realized_win,
                "realized_payout": realized_payout,
                "realized_profit": realized_profit,
                "actual_fill_price": actual_fill_price,
                "flag_missing_settlement": flag_missing_settlement,
                "flag_missing_capture": flag_missing_capture,
                "flag_missing_ledger": flag_missing_ledger,
                "flag_settled_with_null_fill_price": flag_settled_with_null_fill_price,
                "flag_profit_nonzero_without_fill": flag_profit_nonzero_without_fill,
                "flag_order_status_conflict": flag_order_status_conflict,
                "flag_capture_token_mismatch": flag_capture_token_mismatch,
                "flag_ts_order_invalid": flag_ts_order_invalid,
                "quality_issue_count": quality_issue_count,
            }
        )
        row.update(
            compute_remaining_opportunity_fields(
                away_score=away_score_before,
                home_score=home_score_before,
                inning=row.get("inning"),
                inning_state=row.get("inning_state"),
            )
        )
        row.update(state_value_fields)
        row.update(weather_fields)
        row.update(scoring_path_fields)
        for key, value in compute_shadow_diagnostic_fields(row).items():
            if row.get(key) in (None, ""):
                row[key] = value
        row.update(phase2_capture_features)
        row.update(_extract_param_values(session_params))

        # Hard checks at row level
        if not row["mode"] or not row["bet_id"]:
            hard_errors.append(f"[{mode}] missing mandatory mode/bet_id in row {bet_id}")
        if row["game_pk"] is None:
            hard_errors.append(f"[{mode}] missing mandatory game_pk for bet_id={bet_id}")
        if row["line"] in (None, ""):
            hard_errors.append(f"[{mode}] missing mandatory line for bet_id={bet_id}")

        out.append(row)
    return out

