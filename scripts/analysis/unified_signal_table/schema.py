"""Unified signal table schema and column builders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from scripts.trading.stage1_support import STAGE1_SUPPORT_SUFFIXES, stage1_support_field_names
from scripts.trading.scoring_path_features import SCORING_PATH_FIELD_KEYS
from scripts.trading.weather_client import WEATHER_FEATURE_FIELD_KEYS

SCHEMA_VERSION = 2

MARKET_COMPLEMENT_COLUMNS = [
    "over_best_bid",
    "over_best_ask",
    "over_mid",
    "over_spread",
    "over_ltp",
    "over_book_source",
    "decision_mid",
    "under_token_id",
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
]

INFERENCE_PANEL_COLUMNS = [
    "inference_panel_runs_considered",
    "inference_panel_selected_rule",
    "inference_panel_selected_runs",
    *[
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
    ],
]

PARAM_KEYS_COMMON = [
    "edge_threshold",
    "edge_threshold_high_line",
    "jump_threshold",
    "max_spread",
    "min_inning",
    "min_inning_high_line",
    "high_line_cutoff",
    "min_entry_ask",
    "min_entry_ask_high_line",
    "min_current_total",
    "runs_needed_max",
    "min_close_game_rn",
    "inn5_rn_max",
    "inn6_rn_max",
    "max_base_fv",
    "fv_ask_gap_max",
    "fv_ask_gap_min_inning",
    "confirmation_ticks",
    "event_dedup_secs",
    "inning_dedup_gap",
    "inning_dedup_edge_gap",
    "lookback_ticks",
]

PARAM_KEYS_LIVE = [
    "spread_factor",
    "order_timeout_secs",
    "fv_cancel_min_edge",
    "fv_decay_min_age_secs",
    "fv_decay_min_ask_drop",
    "ask_reversal_drop",
    "ask_reversal_window",
    "max_open_orders",
    "daily_budget",
    "per_game_budget_fraction",
    "stake_mode",
    "kelly_fraction",
    "kelly_max_bet_fraction",
    "kelly_max_edge",
]


BASE_MASTER_COLUMNS = [
    "schema_version",
    "mode",
    "config_label",
    "bet_id",
    "session_date",
    "session_path",
    "source_has_session_bet",
    "source_has_ledger_events",
    "source_has_capture",
    "capture_path",
    "placed_at",
    "order_placed_at",
    "filled_at",
    "cancelled_at",
    "settled_at",
    "signal_epoch_s",
    "game_pk",
    "away_abbrev",
    "home_abbrev",
    "line",
    "side",
    "signal_model_family",
    "over_token_id",
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
    "lead_abs",
    "runs_needed",
    "inferred_runs",
    "inferred_away_after",
    "inferred_home_after",
    "entry_ask",
    "decision_ask",
    *MARKET_COMPLEMENT_COLUMNS,
    "fair_value",
    "base_fair_value",
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
    *INFERENCE_PANEL_COLUMNS,
    # Active #8 (2026-05-17): Stage-1 Alt-A shadow fields populated at
    # signal time by `signal_pipeline_gates_post_fv._attach_stage1_
    # shadow_empirical_fields`. Logged on every score-event-family
    # candidate so the shadow-override + loss-attribution reports can
    # read runtime Alt-A (instead of recomputing offline). Wired into
    # the schema 2026-05-19 to close the candidate->table propagation
    # gap discovered during the 2026-05-18 paper-trading audit.
    "stage1_shadow_empirical_mode",
    "fair_value_alt_empirical",
    "fair_value_alt_empirical_raw",
    "fair_value_alt_empirical_delta_vs_prod",
    "fair_value_alt_empirical_used_empirical",
    "fair_value_alt_empirical_p0",
    "stage2_run_env_delta",
    "team_offense_delta",
    "edge_at_ask",
    # Forward-compat alias for edge_at_ask (Fix #2, 2026-05-15). Source
    # bet/candidate rows use the plain `edge` name; carrying both here
    # keeps both naming conventions valid for downstream consumers.
    "edge",
    "state_value_strategy",
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
    "current_state_value_edge",
    "current_state_value_empirical_edge",
    "current_state_value_away_score",
    "current_state_value_home_score",
    "current_state_value_total",
    "shadow_fv_current_state",
    "shadow_fv_after_inferred_score",
    "shadow_fv_inferred_lift",
    "shadow_no_event_edge",
    "shadow_after_event_edge",
    "shadow_p_score_event_proxy",
    "shadow_phantom_risk_score",
    "shadow_phantom_risk_band",
    "shadow_transition_model",
    "shadow_transition_inferred_runs",
    "shadow_extreme_edge",
    "shadow_extreme_edge_threshold",
    "shadow_ltp_at_signal",
    "shadow_ltp_ask_gap",
    "shadow_ltp_ask_gap_threshold",
    "shadow_ltp_ask_gap_exceeded",
    "shadow_risk_tags",
    "shadow_low_ask_high_edge",
    "shadow_runs_needed_exact_3p5",
    "shadow_current_state_edge_bucket",
    "shadow_phantom_risk_bucket",
    "shadow_current_phantom_combo_bucket",
    "shadow_inning_bucket",
    "shadow_inning_runs_needed_bucket",
    "shadow_bottom9_home_lead_context",
    "shadow_home_skip_bottom9_risk_bucket",
    "shadow_post_tr20_extreme_020_pass",
    "shadow_post_tr20_ask_ramp_v2_pass",
    "shadow_post_tr20_gate6_relax_enforce_pass",
    "shadow_post_tr20_combined_pass",
    "limit_price",
    "posted_limit",
    "edge_at_limit",
    "stake",
    "stake_mode",
    "kelly_full_fraction",
    "kelly_fraction_used",
    "order_status_final",
    "cancel_reason_final",
    "t0_best_bid",
    "t0_best_ask",
    "t0_spread",
    "t0_mid",
    "t0_ltp",
    "t0_latency_ms",
    "t0_total_bid_depth",
    "t0_total_ask_depth",
    "settled",
    "won_counterfactual",
    "final_away",
    "final_home",
    "final_total",
    "realized_executed",
    "realized_win",
    "realized_payout",
    "realized_profit",
    "actual_fill_price",
    "flag_missing_settlement",
    "flag_missing_capture",
    "flag_missing_ledger",
    "flag_settled_with_null_fill_price",
    "flag_profit_nonzero_without_fill",
    "flag_order_status_conflict",
    "flag_capture_token_mismatch",
    "flag_ts_order_invalid",
    "quality_issue_count",
]

PHASE2_STATIC_COLUMNS = [
    "ask_move_2s",
    "ask_move_5s",
    "ask_velocity_5s_cents",
    "min_ask_30s",
    "max_ask_30s",
    "min_spread_30s",
    "max_spread_30s",
]

PARAM_COLUMNS = [f"param_{k}" for k in PARAM_KEYS_COMMON + PARAM_KEYS_LIVE]


ORDER_EVENT_COLUMNS = [
    "mode",
    "bet_id",
    "event_seq",
    "event_type",
    "event_time",
    "raw_order_status",
    "raw_cancel_reason",
    "fill_price",
    "fill_size",
    "stake",
    "realized_profit_at_event",
]


SNAPSHOT_COLUMNS = [
    "mode",
    "bet_id",
    "seq",
    "elapsed_s",
    "ts",
    "best_bid",
    "best_ask",
    "spread",
    "mid",
    "ltp",
    "latency_ms",
    "total_bid_depth",
    "total_ask_depth",
    "ok",
    "error",
]


@dataclass
class CaptureData:
    bet_id: str
    path: str
    header: Dict[str, Any]
    t0_book: Dict[str, Any]
    snapshots: List[Dict[str, Any]]

def _build_master_columns(horizons: List[int], fill_window_secs: int) -> List[str]:
    horizon_columns: List[str] = []
    for h in horizons:
        horizon_columns.extend([f"ask_{h}s", f"bid_{h}s"])

    fill_columns = [
        f"sim_fill_time_{fill_window_secs}s",
        f"sim_filled_{fill_window_secs}s",
        f"sim_cents_saved_vs_taker_{fill_window_secs}s",
        f"sim_fill_time_{fill_window_secs}s_p1c",
        f"sim_filled_{fill_window_secs}s_p1c",
        f"sim_cents_saved_vs_taker_{fill_window_secs}s_p1c",
        f"sim_fill_time_{fill_window_secs}s_p2c",
        f"sim_filled_{fill_window_secs}s_p2c",
        f"sim_cents_saved_vs_taker_{fill_window_secs}s_p2c",
    ]
    return BASE_MASTER_COLUMNS + horizon_columns + PHASE2_STATIC_COLUMNS + fill_columns + PARAM_COLUMNS
