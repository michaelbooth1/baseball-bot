#!/usr/bin/env python3
"""
candidate_schema_enrichment.py -- Modeling fields for candidate opportunities.

This module owns derived, observability-only fields for post-FV candidates:
ask/edge/runs-needed buckets, logit-scale model-vs-market residuals, execution
policy prices, and the compact calibration-opportunity JSONL sidecar.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, Optional, TYPE_CHECKING

from candidate_paths import (
    candidate_calibration_log_path,
    drop_none_values,
    jsonl_dumps,
    path_from_engine,
)
from pricing_features import resolve_spread_factor
from scoring_path_features import SCORING_PATH_FIELD_KEYS
from shadow_post_tr20 import SHADOW_POST_TR20_FIELDS, attach_post_tr20_shadow_fields
from shadow_diagnostic_features import compute_shadow_diagnostic_fields
from stage1_support import STAGE1_SUPPORT_SUFFIXES, stage1_support_field_names
from weather_client import WEATHER_FEATURE_FIELD_KEYS

if TYPE_CHECKING:
    from signal_engine import SignalEngine

LOGGER = logging.getLogger("signal_engine")

MARKET_COMPLEMENT_FIELD_KEYS = (
    "over_token_id",
    "under_token_id",
    "over_best_bid",
    "over_best_ask",
    "over_mid",
    "over_spread",
    "over_ltp",
    "over_book_source",
    "decision_mid",
    "under_pair_available",
    "under_best_bid",
    "under_best_ask",
    "under_mid",
    "under_spread",
    "under_ltp",
    "under_book_source",
    "over_under_ask_sum",
    "over_under_bid_sum",
    "over_under_mid_sum",
    "over_mid_no_vig",
    "under_mid_no_vig",
    "decision_market_mid_no_vig",
)

INFERENCE_PANEL_FIELD_KEYS = (
    "inference_panel_runs_considered",
    "inference_panel_selected_rule",
    "inference_panel_selected_runs",
    *(
        f"inference_run{runs}_{suffix}"
        for runs in (1, 2, 3)
        for suffix in (
            "selected",
            "away_score",
            "home_score",
            "total",
            "base_poisson",
            "base_empirical",
            "poisson_minus_empirical",
            "distance_to_ask",
            "empirical_distance_to_ask",
            "n",
            "n_samples",
            "effective_n",
            *(f"support_{suffix}" for suffix in STAGE1_SUPPORT_SUFFIXES),
            "fallback_level",
            "fallback_label",
            "cell_key",
            "line_fallback_mode",
            "line_source_key",
            "empirical_line_fallback_mode",
            "empirical_line_source_key",
        )
    ),
)

SELECTED_INFERRED_STATE_FIELD_KEYS = (
    "inferred_state_base_poisson",
    "inferred_state_base_empirical",
    "inferred_state_poisson_minus_empirical",
    "inferred_state_empirical_edge",
    "inferred_state_n",
    "inferred_state_n_samples",
    "inferred_state_weighted_n",
    "inferred_state_effective_n",
    *stage1_support_field_names("inferred_state"),
    "inferred_state_fallback_level",
    "inferred_state_fallback_label",
    "inferred_state_cell_key",
    "inferred_state_line_key_poisson",
    "inferred_state_line_key_empirical",
    "inferred_state_line_fallback_mode",
    "inferred_state_empirical_line_fallback_mode",
    "inferred_state_empirical_line_source_key",
    "inferred_state_empirical_line_source_key_low",
    "inferred_state_empirical_line_source_key_high",
    "inferred_state_used_fallback",
    "inferred_state_base_source",
)


def to_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _safe_logit(value) -> Optional[float]:
    p = to_float(value)
    if p is None:
        return None
    eps = 1e-6
    p = min(max(p, eps), 1.0 - eps)
    return math.log(p / (1.0 - p))


def _price_bucket(value) -> str:
    p = to_float(value)
    if p is None:
        return "missing"
    if p < 0.55:
        return "<0.55"
    if p < 0.65:
        return "0.55-0.65"
    if p < 0.75:
        return "0.65-0.75"
    if p < 0.85:
        return "0.75-0.85"
    return ">=0.85"


def _edge_bucket(value) -> str:
    e = to_float(value)
    if e is None:
        return "missing"
    if e < 0.00:
        return "<0.00"
    if e < 0.10:
        return "0.00-0.10"
    if e < 0.15:
        return "0.10-0.15"
    if e < 0.20:
        return "0.15-0.20"
    if e < 0.22:
        return "0.20-0.22"
    if e < 0.25:
        return "0.22-0.25"
    if e < 0.30:
        return "0.25-0.30"
    return ">=0.30"


def _runs_needed_bucket(value) -> str:
    rn = to_float(value)
    if rn is None:
        return "missing"
    if rn <= 1.5:
        return "<=1.5"
    if rn <= 2.5:
        return "1.5-2.5"
    if rn <= 3.5:
        return "2.5-3.5"
    return ">3.5"


def _clamp_price(value) -> Optional[float]:
    p = to_float(value)
    if p is None:
        return None
    return round(min(max(p, 0.01), 0.99), 4)


def _add_logit_residual(
    row: Dict[str, object],
    *,
    output_key: str,
    probability_key: str,
    price_key: str = "decision_ask",
) -> None:
    model_logit = _safe_logit(row.get(probability_key))
    price_logit = _safe_logit(row.get(price_key))
    if model_logit is None or price_logit is None:
        row.setdefault(output_key, None)
        return
    row[output_key] = round(model_logit - price_logit, 6)


def attach_modeling_observability_fields(
    engine: "SignalEngine",
    row: Dict[str, object],
) -> None:
    """Attach derived calibration/execution fields before writing a row."""
    trade_args = getattr(engine, "trade_args", None)
    extreme_edge_max = to_float(getattr(trade_args, "extreme_edge_max", None))
    if extreme_edge_max is not None:
        row.setdefault("extreme_edge_max", extreme_edge_max)
        row.setdefault("gate_policy_version", f"TR19_extreme_edge_max_{extreme_edge_max:.2f}")
    row.setdefault("config_label", str(getattr(trade_args, "config_label", "default") or "default"))

    row.setdefault(
        "outcome_join_key",
        f"{row.get('session_date') or getattr(engine, 'date_str', '')}|{row.get('game_pk')}|{row.get('line')}",
    )
    row.setdefault("ask_bucket", _price_bucket(row.get("decision_ask")))
    row.setdefault("edge_bucket", _edge_bucket(row.get("edge")))
    row.setdefault("runs_needed_bucket", _runs_needed_bucket(row.get("runs_needed")))
    row.setdefault("phantom_risk_band", row.get("shadow_phantom_risk_band") or "missing")
    row.setdefault("model_price_residual_version", "logit_v1")
    for key, value in compute_shadow_diagnostic_fields(row).items():
        if row.get(key) in (None, ""):
            row[key] = value

    _add_logit_residual(row, output_key="model_market_logit_residual", probability_key="fair_value")
    _add_logit_residual(
        row,
        output_key="model_market_mid_no_vig_logit_residual",
        probability_key="fair_value",
        price_key="decision_market_mid_no_vig",
    )
    _add_logit_residual(row, output_key="raw_model_market_logit_residual", probability_key="fair_value_raw")
    _add_logit_residual(
        row,
        output_key="current_state_market_logit_residual",
        probability_key="current_state_value_fv_raw",
    )
    _add_logit_residual(
        row,
        output_key="after_event_market_logit_residual",
        probability_key="shadow_fv_after_inferred_score",
    )
    _add_logit_residual(
        row,
        output_key="empirical_state_market_logit_residual",
        probability_key="current_state_value_base_empirical",
    )
    _add_logit_residual(
        row,
        output_key="empirical_state_mid_no_vig_logit_residual",
        probability_key="current_state_value_base_empirical",
        price_key="decision_market_mid_no_vig",
    )

    bid = to_float(row.get("best_bid"))
    ask = to_float(row.get("decision_ask"))
    fv = to_float(row.get("fair_value"))
    spread_factor = resolve_spread_factor(engine)
    current_limit = to_float(row.get("hypothetical_limit_price"))
    if current_limit is None and bid is not None and ask is not None and ask > bid:
        current_limit = bid + (ask - bid) * spread_factor
    current_limit = _clamp_price(current_limit)
    if current_limit is not None:
        row.setdefault("hypothetical_limit_price", current_limit)

    policies = {
        "current_limit": current_limit,
        "limit_plus_1c": _clamp_price((current_limit + 0.01) if current_limit is not None else None),
        "limit_plus_2c": _clamp_price((current_limit + 0.02) if current_limit is not None else None),
        "taker_like": _clamp_price(ask),
    }
    for name, price in policies.items():
        row[f"execution_policy_{name}_price"] = price
        row[f"execution_policy_{name}_ev_if_filled_per_share"] = (
            round(fv - price, 6) if fv is not None and price is not None else None
        )
        row[f"execution_policy_{name}_breakeven_prob"] = price


def attach_candidate_shadow_fields(
    engine: "SignalEngine",
    row: Dict[str, object],
) -> None:
    """Attach lightweight shadow-only policy probes to every candidate row."""
    attach_post_tr20_shadow_fields(row, getattr(engine, "trade_args", None))


CALIBRATION_OPPORTUNITY_FIELDS = (
    "schema_version",
    "session_date",
    "mode",
    "candidate_id",
    "ts",
    "signal_ts_epoch",
    "recorded_at",
    "outcome_join_key",
    "game_pk",
    "away_abbrev",
    "home_abbrev",
    "line",
    "side",
    "decision",
    "decision_reason",
    "signal_model_family",
    "state_value_strategy",
    "gate_policy_version",
    "inning",
    "inning_state",
    "outs",
    "runners_on",
    "away_score_before",
    "home_score_before",
    "current_total",
    "home_leading_late",
    "batting_team_is_home",
    "bottom9_available_if_needed",
    "expected_remaining_half_innings",
    "expected_remaining_pa_bucket",
    "home_skip_bottom9_risk",
    *SCORING_PATH_FIELD_KEYS,
    *WEATHER_FEATURE_FIELD_KEYS,
    *MARKET_COMPLEMENT_FIELD_KEYS,
    "runs_needed",
    "lead_abs",
    "inferred_runs",
    "decision_ask",
    "best_bid",
    "spread",
    "ask_bucket",
    "edge_bucket",
    "runs_needed_bucket",
    "phantom_risk_band",
    "shadow_low_ask_high_edge",
    "shadow_runs_needed_exact_3p5",
    "shadow_score_event_current_edge_strong_ask_lt_085",
    "shadow_current_state_edge_bucket",
    "shadow_phantom_risk_bucket",
    "shadow_current_phantom_combo_bucket",
    "shadow_inning_bucket",
    "shadow_inning_runs_needed_bucket",
    "shadow_bottom9_home_lead_context",
    "shadow_home_skip_bottom9_risk_bucket",
    "shadow_no_score_poisson_edge_bucket",
    "shadow_no_score_empirical_edge_bucket",
    "shadow_no_score_ask_bucket",
    "shadow_no_score_drawdown_bucket",
    "shadow_no_score_poisson_empirical_ask_drawdown_bucket",
    *SHADOW_POST_TR20_FIELDS,
    "fair_value",
    "fair_value_raw",
    "fair_value_calibrated",
    "base_fair_value",
    *SELECTED_INFERRED_STATE_FIELD_KEYS,
    "stage2_run_env_delta",
    "stage2_weather_source",
    "stage2_weather_model_usable",
    "team_offense_delta",
    "edge",
    "min_edge_effective",
    "model_market_logit_residual",
    "raw_model_market_logit_residual",
    "current_state_market_logit_residual",
    "after_event_market_logit_residual",
    "empirical_state_market_logit_residual",
    "model_market_mid_no_vig_logit_residual",
    "empirical_state_mid_no_vig_logit_residual",
    *INFERENCE_PANEL_FIELD_KEYS,
    "current_state_value_edge",
    "current_state_value_empirical_edge",
    "current_state_value_base_poisson",
    "current_state_value_base_empirical",
    "current_state_value_line_key_poisson",
    "current_state_value_line_key_empirical",
    "current_state_value_used_fallback",
    "current_state_value_state_fallback_level",
    "current_state_value_state_fallback_label",
    "current_state_value_state_cell_key",
    "current_state_value_line_fallback_mode",
    "current_state_value_line_source_key",
    "current_state_value_empirical_line_fallback_mode",
    "current_state_value_empirical_line_source_key",
    *stage1_support_field_names("current_state_value"),
    "current_state_value_fv_raw",
    "current_state_value_stage2_run_env_delta",
    "current_state_value_team_offense_delta",
    "shadow_fv_current_state",
    "shadow_fv_after_inferred_score",
    "shadow_fv_inferred_lift",
    "shadow_p_score_event_proxy",
    "shadow_phantom_risk_score",
    "shadow_phantom_risk_band",
    "shadow_risk_tags",
    "score_segment_key",
    "score_segment_age_secs",
    "score_segment_drawdown",
    "shadow_no_score_drift_trigger",
    "hypothetical_limit_price",
    "posted_limit",
    "execution_bid",
    "execution_ask",
    "execution_policy_current_limit_price",
    "execution_policy_current_limit_ev_if_filled_per_share",
    "execution_policy_limit_plus_1c_price",
    "execution_policy_limit_plus_1c_ev_if_filled_per_share",
    "execution_policy_limit_plus_2c_price",
    "execution_policy_limit_plus_2c_ev_if_filled_per_share",
    "execution_policy_taker_like_price",
    "execution_policy_taker_like_ev_if_filled_per_share",
)


def is_calibration_opportunity(row: Dict[str, object]) -> bool:
    decision = str(row.get("decision") or "").lower()
    if decision in {"trade", "shadow_no_score_drift"}:
        return True
    if row.get("fair_value") is not None:
        return True
    return str(row.get("decision_reason") or "") == "gate_extreme_edge"


def write_calibration_opportunity(engine: "SignalEngine", row: Dict[str, object]) -> None:
    if not is_calibration_opportunity(row):
        return
    compact = {key: row.get(key) for key in CALIBRATION_OPPORTUNITY_FIELDS if key in row}
    compact["schema_version"] = 1
    try:
        path = path_from_engine(engine, "_candidate_calibration_log_path", candidate_calibration_log_path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(jsonl_dumps(drop_none_values(compact)) + "\n")
        engine._candidate_calibration_rows_written += 1
    except Exception as exc:
        engine._candidate_calibration_write_errors += 1
        LOGGER.warning("Failed to write calibration opportunity row: %s", exc)
