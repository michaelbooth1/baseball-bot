#!/usr/bin/env python3
"""
live_ev_policy_runtime.py -- EV-policy artifact loading and per-signal scoring.

Free functions extracted from live_engine.LiveTradingEngine (Tier 3 refactor,
2026-05-01). Loads the win/fill scorer artifacts on engine startup and scores
each candidate signal at decision time. Engine retains thin method wrappers
(`_load_ev_policy_runtime`, `_build_ev_feature_row`, `_evaluate_ev_policy`)
so test stubs and `_place_bet` call-sites continue to work unchanged.

Surfaces:
  - load_ev_policy_runtime(engine) -> None      # mutates engine._ev_policy_runtime
  - build_ev_feature_row(engine, ...) -> dict   # pure
  - evaluate_ev_policy(engine, feature_row, stake, price) -> (allow, diag)

Engine attrs read/written:
  - live_args.ev_policy_report_path / win_model_path / fill_model_path (read)
  - engine._ev_policy_mode  (read; may be flipped to "off" on load failure)
  - engine._ev_policy_runtime  (written: dict of scorers + thresholds, or None)
  - engine._ev_policy_stats    (written: scored/shadow_*/enforce_* counters)
  - trade_args.* (read for feature_row construction)

Failure modes:
  - "off"     mode: load failure logs WARNING and disables policy.
  - "enforce" mode: load failure logs ERROR; runtime stores load_error and the
                    scorer fails closed (blocks live orders) until artifacts
                    are fixed.
  - Per-signal score failure in enforce mode also fails closed.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from ev_policy import LogisticJsonScorer
from model_families import SCORE_EVENT_TRANSITION
from remaining_opportunity import (
    REMAINING_OPPORTUNITY_FIELD_KEYS,
    compute_remaining_opportunity_fields,
)
from scoring_path_features import SCORING_PATH_FIELD_KEYS, compute_scoring_path_fields
from shadow_diagnostic_features import compute_shadow_diagnostic_fields
from stage1_support import stage1_support_field_names
from weather_client import WEATHER_FEATURE_FIELD_KEYS

if TYPE_CHECKING:
    from live_engine import LiveTradingEngine

LOGGER = logging.getLogger("live_engine")

RUNTIME_UNSAFE_FEATURE_RE = re.compile(
    r"^(?:"
    r"(?:ask|bid)_\d+s|"
    r"ask_move_\d+s|"
    r"ask_velocity_\d+s_cents|"
    r"(?:min_ask|max_ask|min_spread|max_spread)_\d+s|"
    r"sim_.*"
    r")$"
)


def _stable_sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


STATE_VALUE_EV_FEATURE_KEYS: Tuple[str, ...] = (
    "state_value_strategy",
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
)

NO_SCORE_REGIME_EV_FEATURE_KEYS: Tuple[str, ...] = (
    "score_segment_key",
    "score_segment_age_secs",
    "score_segment_ticks",
    "score_segment_high_ask",
    "score_segment_drawdown",
    "shadow_no_score_drift_min_age_secs",
    "shadow_no_score_drift_min_ticks",
    "shadow_no_score_drift_min_drawdown",
    "shadow_no_score_drift_min_ask",
    "shadow_no_score_drift_max_ask",
    "shadow_no_score_drift_max_spread",
    "shadow_no_score_drift_min_po_edge",
    "shadow_no_score_drift_min_emp_edge",
    "shadow_no_score_drift_trigger",
    "baseline_ask",
    "ask_jump",
    "lookback_ticks",
)


def _get_attr(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default) if obj is not None else default


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        fv = float(v)
        if not math.isfinite(fv):
            return None
        return fv
    except (TypeError, ValueError):
        return None


def _copy_optional_features(
    row: Dict[str, Any],
    source: Dict[str, object],
    keys: Tuple[str, ...],
) -> None:
    for key in keys:
        row[key] = source.get(key) if key in source else None


def _missing_feature_diagnostics(
    *,
    win_scorer: Optional[LogisticJsonScorer],
    fill_scorer: Optional[LogisticJsonScorer],
    feature_row: Dict[str, Any],
) -> Dict[str, List[str]]:
    missing: Dict[str, List[str]] = {}
    if win_scorer is not None:
        win_missing = win_scorer.missing_input_cols(feature_row)
        if win_missing:
            missing["win"] = win_missing
    if fill_scorer is not None:
        fill_missing = fill_scorer.missing_input_cols(feature_row)
        if fill_missing:
            missing["fill"] = fill_missing
    return missing


def _warn_missing_runtime_features_once(
    runtime: Dict[str, Any],
    missing: Dict[str, List[str]],
) -> None:
    warned = runtime.setdefault("_missing_feature_warning_keys", set())
    key = tuple(
        (model, tuple(cols))
        for model, cols in sorted(missing.items())
    )
    if key in warned:
        return
    warned.add(key)
    LOGGER.warning(
        "EV policy artifact requires runtime features that are absent/null; "
        "shadow will score with artifact imputations, enforce will fail closed "
        "(missing=%s)",
        {
            model: cols[:12] + (["..."] if len(cols) > 12 else [])
            for model, cols in missing.items()
        },
    )


def _runtime_unsafe_feature_cols(scorer: LogisticJsonScorer) -> List[str]:
    """Return post-signal columns that cannot exist at live decision time."""
    return [
        col
        for col in scorer.required_input_cols()
        if RUNTIME_UNSAFE_FEATURE_RE.match(str(col))
    ]


def _extract_best_config(report: Dict[str, Any]) -> Dict[str, Any]:
    """Read policy thresholds from current and legacy EV report schemas."""
    if isinstance(report.get("best_config"), dict):
        return report.get("best_config", {})
    policy_selection = report.get("policy_selection", {})
    if isinstance(policy_selection, dict) and isinstance(policy_selection.get("best_validation_config"), dict):
        return policy_selection.get("best_validation_config", {})
    if isinstance(report.get("best_validation_config"), dict):
        return report.get("best_validation_config", {})
    return {}


# ---------------------------------------------------------------------------
# Artifact loading
# ---------------------------------------------------------------------------

def load_ev_policy_runtime(engine: "LiveTradingEngine") -> None:
    """Load EV-policy artifacts when ev_policy_mode is enabled."""
    from live_engine import (
        DEFAULT_EV_POLICY_REPORT_PATH,
        DEFAULT_EV_WIN_MODEL_PATH,
        DEFAULT_EV_FILL_MODEL_PATH,
    )
    report_path = getattr(engine.live_args, "ev_policy_report_path", DEFAULT_EV_POLICY_REPORT_PATH)
    win_model_path = getattr(engine.live_args, "ev_policy_win_model_path", DEFAULT_EV_WIN_MODEL_PATH)
    fill_model_path = getattr(engine.live_args, "ev_policy_fill_model_path", DEFAULT_EV_FILL_MODEL_PATH)

    try:
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
        with open(win_model_path, encoding="utf-8") as f:
            win_payload = json.load(f)
        with open(fill_model_path, encoding="utf-8") as f:
            fill_payload = json.load(f)

        # Find the best config from the report
        best_cfg = _extract_best_config(report)
        min_ev_per_stake = float(best_cfg.get("min_ev_per_stake", 0.0))
        min_p_fill = float(best_cfg.get("min_p_fill", 0.0))
        win_scorer = LogisticJsonScorer(win_payload)
        fill_scorer = LogisticJsonScorer(fill_payload)

        unsafe = {
            "win": _runtime_unsafe_feature_cols(win_scorer),
            "fill": _runtime_unsafe_feature_cols(fill_scorer),
        }
        unsafe = {k: v for k, v in unsafe.items() if v}
        if unsafe:
            raise ValueError(
                "EV policy runtime artifact includes post-signal features "
                f"that are unavailable at decision time: {unsafe}"
            )
        if fill_payload.get("runtime_safe") is False:
            raise ValueError(
                "EV policy fill artifact is marked runtime_safe=false. "
                "Use ev_execution_fill_runtime_model.json for live/shadow runtime."
            )
        if win_payload.get("runtime_safe") is False:
            raise ValueError(
                "EV policy win artifact is marked runtime_safe=false. "
                "Use a decision-time win artifact for live/shadow runtime."
            )
        win_family = str(win_payload.get("model_family") or SCORE_EVENT_TRANSITION)
        fill_family = str(fill_payload.get("model_family") or SCORE_EVENT_TRANSITION)
        if win_family != SCORE_EVENT_TRANSITION or fill_family != SCORE_EVENT_TRANSITION:
            raise ValueError(
                "Live score-event EV runtime can only load score_event_transition artifacts "
                f"(win_model_family={win_family}, fill_model_family={fill_family}). "
                "No-score drift must use its separate paper/walk-forward path."
            )

        engine._ev_policy_runtime = {
            "win_scorer": win_scorer,
            "fill_scorer": fill_scorer,
            "min_ev_per_stake": min_ev_per_stake,
            "min_p_fill": min_p_fill,
            "feature_policy": str(fill_payload.get("feature_policy") or "decision_time_runtime_reliable"),
        }
        LOGGER.info(
            "EV policy runtime loaded (mode=%s): min_ev_per_stake=%.3f  min_p_fill=%.3f",
            engine._ev_policy_mode, min_ev_per_stake, min_p_fill,
        )
    except Exception as exc:
        if engine._ev_policy_mode == "enforce":
            LOGGER.error(
                "EV policy runtime load failed in enforce mode (%s). "
                "Policy will fail closed and block live orders until artifacts are fixed.",
                exc,
            )
            engine._ev_policy_runtime = {"load_error": str(exc)}
        else:
            LOGGER.warning(
                "EV policy runtime load failed (%s). Policy will be disabled.", exc,
            )
            engine._ev_policy_mode = "off"
            engine._ev_policy_runtime = None


# ---------------------------------------------------------------------------
# Feature-row construction
# ---------------------------------------------------------------------------

def build_ev_feature_row(
    engine: "LiveTradingEngine",
    game,
    market,
    line_val: float,
    best_ask: float,
    bid: float,
    fair_value: float,
    base_fair_value: float,
    stage2_run_env_delta: float,
    team_offense_delta: float,
    edge: float,
    inferred_runs: int,
    inning: int,
    inning_state: str,
    outs: int,
    away_score_before: int,
    home_score_before: int,
    runners_on: int,
    limit_price: float,
    stake: float,
    ltp: Optional[float] = None,
    execution_book: Optional[Dict[str, Any]] = None,
    state_value_diagnostics: Optional[Dict[str, object]] = None,
) -> Dict[str, Any]:
    """Build feature row for EV policy scoring."""
    spread = best_ask - bid if bid is not None else None
    mid = (best_ask + bid) / 2.0 if bid is not None else None
    current_total = away_score_before + home_score_before
    lead_abs = abs(away_score_before - home_score_before)
    runs_needed = line_val - current_total
    half_text = str(inning_state or "").strip().lower()
    batting_top = half_text.startswith("top") or half_text.startswith("t")
    inf_away = away_score_before + inferred_runs if batting_top else away_score_before
    inf_home = home_score_before if batting_top else home_score_before + inferred_runs
    state_diag = state_value_diagnostics or {}
    book = execution_book or {}
    trade_args = getattr(engine, "trade_args", None)
    live_args = getattr(engine, "live_args", None)

    row: Dict[str, Any] = {
        "mode": "live",
        "line": market.line,
        "side": "over",
        "signal_model_family": SCORE_EVENT_TRANSITION,
        "source_has_session_bet": True,
        "source_has_ledger_events": False,
        "source_has_capture": bool(book.get("ok")) if isinstance(book, dict) else False,
        "over_token_id": getattr(market, "over_token_id", ""),
        "away_abbrev": game.away_abbrev,
        "home_abbrev": game.home_abbrev,
        "edge_threshold": _get_attr(trade_args, "edge_threshold"),
        "edge_threshold_high_line": _get_attr(trade_args, "edge_threshold_high_line"),
        "jump_threshold": _get_attr(trade_args, "jump_threshold"),
        "min_inning": _get_attr(trade_args, "min_inning"),
        "min_inning_high_line": _get_attr(trade_args, "min_inning_high_line"),
        "high_line_cutoff": _get_attr(trade_args, "high_line_cutoff"),
        "min_entry_ask": _get_attr(trade_args, "min_entry_ask"),
        "min_entry_ask_high_line": _get_attr(trade_args, "min_entry_ask_high_line"),
        "min_current_total": _get_attr(trade_args, "min_current_total"),
        "runs_needed_max": _get_attr(trade_args, "runs_needed_max"),
        "min_close_game_rn": _get_attr(trade_args, "min_close_game_rn"),
        "inn5_rn_max": _get_attr(trade_args, "inn5_rn_max"),
        "inn6_rn_max": _get_attr(trade_args, "inn6_rn_max"),
        "entry_ask": best_ask,
        "decision_ask": best_ask,
        "best_bid": bid,
        "spread": spread,
        "t0_best_bid": bid,
        "t0_best_ask": best_ask,
        "t0_spread": spread,
        "t0_mid": mid,
        "t0_ltp": ltp if ltp is not None else book.get("ltp"),
        "t0_latency_ms": book.get("latency_ms"),
        "t0_total_bid_depth": book.get("total_bid_depth"),
        "t0_total_ask_depth": book.get("total_ask_depth"),
        "fair_value": fair_value,
        "base_fair_value": base_fair_value,
        "stage2_run_env_delta": stage2_run_env_delta,
        "team_offense_delta": team_offense_delta,
        "edge": edge,
        "edge_at_ask": edge,
        "edge_at_limit": fair_value - limit_price,
        "inferred_runs": inferred_runs,
        "inferred_away_after": inf_away,
        "inferred_home_after": inf_home,
        "inning": inning,
        "inning_state": inning_state,
        "outs": outs,
        "away_score_before": away_score_before,
        "home_score_before": home_score_before,
        "current_total": current_total,
        "lead_abs": lead_abs,
        "runs_needed": runs_needed,
        "runners_on": runners_on,
        "limit_price": limit_price,
        "posted_limit": limit_price,
        "stake": stake,
        "stake_mode": _get_attr(live_args, "stake_mode", _get_attr(trade_args, "stake_mode")),
        "venue_name": game.venue_name,
    }

    # Training-table artifacts use param_* names. Keep the legacy unprefixed
    # names above for backward compatibility, and add the canonical aliases so
    # runtime scoring is not silently median-imputing operator configuration.
    param_sources = {
        "param_ask_reversal_drop": _get_attr(live_args, "ask_reversal_drop"),
        "param_ask_reversal_window": _get_attr(live_args, "ask_reversal_window"),
        "param_confirmation_ticks": _get_attr(trade_args, "confirmation_ticks"),
        "param_daily_budget": _get_attr(live_args, "daily_budget"),
        "param_edge_threshold": _get_attr(trade_args, "edge_threshold"),
        "param_edge_threshold_high_line": _get_attr(trade_args, "edge_threshold_high_line"),
        "param_event_dedup_secs": _get_attr(trade_args, "event_dedup_secs"),
        "param_fv_ask_gap_max": _get_attr(trade_args, "fv_ask_gap_max"),
        "param_fv_ask_gap_min_inning": _get_attr(trade_args, "fv_ask_gap_min_inning"),
        "param_fv_cancel_min_edge": _get_attr(live_args, "fv_cancel_min_edge"),
        "param_fv_decay_min_age_secs": _get_attr(live_args, "fv_decay_min_age_secs"),
        "param_fv_decay_min_ask_drop": _get_attr(live_args, "fv_decay_min_ask_drop"),
        "param_high_line_cutoff": _get_attr(trade_args, "high_line_cutoff"),
        "param_inn5_rn_max": _get_attr(trade_args, "inn5_rn_max"),
        "param_inn6_rn_max": _get_attr(trade_args, "inn6_rn_max"),
        "param_inning_dedup_edge_gap": _get_attr(trade_args, "inning_dedup_edge_gap"),
        "param_inning_dedup_gap": _get_attr(trade_args, "inning_dedup_gap"),
        "param_jump_threshold": _get_attr(trade_args, "jump_threshold"),
        "param_kelly_fraction": _get_attr(live_args, "kelly_fraction"),
        "param_kelly_max_bet_fraction": _get_attr(live_args, "kelly_max_bet_fraction"),
        "param_kelly_max_edge": _get_attr(live_args, "kelly_max_edge"),
        "param_lookback_ticks": _get_attr(trade_args, "lookback_ticks"),
        "param_max_base_fv": _get_attr(trade_args, "max_base_fv"),
        "param_max_open_orders": _get_attr(live_args, "max_open_orders"),
        "param_max_spread": _get_attr(trade_args, "max_spread"),
        "param_min_close_game_rn": _get_attr(trade_args, "min_close_game_rn"),
        "param_min_current_total": _get_attr(trade_args, "min_current_total"),
        "param_min_entry_ask": _get_attr(trade_args, "min_entry_ask"),
        "param_min_entry_ask_high_line": _get_attr(trade_args, "min_entry_ask_high_line"),
        "param_min_inning": _get_attr(trade_args, "min_inning"),
        "param_min_inning_high_line": _get_attr(trade_args, "min_inning_high_line"),
        "param_order_timeout_secs": _get_attr(live_args, "order_timeout_secs"),
        "param_per_game_budget_fraction": _get_attr(live_args, "per_game_budget_fraction"),
        "param_runs_needed_max": _get_attr(trade_args, "runs_needed_max"),
        "param_spread_factor": _get_attr(live_args, "spread_factor", _get_attr(trade_args, "spread_factor")),
        "param_stake_mode": _get_attr(live_args, "stake_mode", _get_attr(trade_args, "stake_mode")),
    }
    row.update(param_sources)
    row.update(
        compute_remaining_opportunity_fields(
            away_score=away_score_before,
            home_score=home_score_before,
            inning=inning,
            inning_state=inning_state,
        )
    )
    row.update(
        compute_scoring_path_fields(
            away_inning_runs=getattr(getattr(game, "score", None), "away_inning_runs", ()),
            home_inning_runs=getattr(getattr(game, "score", None), "home_inning_runs", ()),
            current_inning=inning,
        )
    )

    _copy_optional_features(row, state_diag, STATE_VALUE_EV_FEATURE_KEYS)
    _copy_optional_features(row, state_diag, NO_SCORE_REGIME_EV_FEATURE_KEYS)
    weather_getter = getattr(engine, "_weather_fields_for_game", None)
    if callable(weather_getter):
        row.update(weather_getter(getattr(game, "game_pk", -1)))
    for key in REMAINING_OPPORTUNITY_FIELD_KEYS:
        if row.get(key) is None and state_diag.get(key) not in (None, ""):
            row[key] = state_diag.get(key)
    for key in SCORING_PATH_FIELD_KEYS:
        if row.get(key) is None and state_diag.get(key) not in (None, ""):
            row[key] = state_diag.get(key)
    for key in WEATHER_FEATURE_FIELD_KEYS:
        if row.get(key) is None and state_diag.get(key) not in (None, ""):
            row[key] = state_diag.get(key)

    row["state_value_strategy"] = row.get("state_value_strategy") or SCORE_EVENT_TRANSITION
    row["inferred_state_base_poisson"] = (
        row.get("inferred_state_base_poisson")
        if row.get("inferred_state_base_poisson") is not None
        else base_fair_value
    )
    if (
        row.get("inferred_state_poisson_minus_empirical") is None
        and _safe_float(row.get("inferred_state_base_poisson")) is not None
        and _safe_float(row.get("inferred_state_base_empirical")) is not None
    ):
        row["inferred_state_poisson_minus_empirical"] = (
            float(row["inferred_state_base_poisson"]) - float(row["inferred_state_base_empirical"])
        )
    if (
        row.get("inferred_state_empirical_edge") is None
        and _safe_float(row.get("inferred_state_base_empirical")) is not None
    ):
        row["inferred_state_empirical_edge"] = float(row["inferred_state_base_empirical"]) - best_ask
    row["inferred_state_base_source"] = row.get("inferred_state_base_source") or "poisson_runtime"
    row["current_state_value_away_score"] = (
        row.get("current_state_value_away_score")
        if row.get("current_state_value_away_score") is not None
        else away_score_before
    )
    row["current_state_value_home_score"] = (
        row.get("current_state_value_home_score")
        if row.get("current_state_value_home_score") is not None
        else home_score_before
    )
    row["current_state_value_total"] = (
        row.get("current_state_value_total")
        if row.get("current_state_value_total") is not None
        else current_total
    )
    row["shadow_fv_after_inferred_score"] = (
        row.get("shadow_fv_after_inferred_score")
        if row.get("shadow_fv_after_inferred_score") is not None
        else fair_value
    )
    row["shadow_transition_inferred_runs"] = (
        row.get("shadow_transition_inferred_runs")
        if row.get("shadow_transition_inferred_runs") is not None
        else inferred_runs
    )
    row["shadow_ltp_at_signal"] = (
        row.get("shadow_ltp_at_signal")
        if row.get("shadow_ltp_at_signal") is not None
        else row.get("t0_ltp")
    )
    if row.get("shadow_ltp_ask_gap") is None and _safe_float(row.get("shadow_ltp_at_signal")) is not None:
        row["shadow_ltp_ask_gap"] = abs(best_ask - float(row["shadow_ltp_at_signal"]))

    for key, value in compute_shadow_diagnostic_fields(row).items():
        if row.get(key) in (None, ""):
            row[key] = value

    return row


# ---------------------------------------------------------------------------
# Per-signal evaluation
# ---------------------------------------------------------------------------

def evaluate_ev_policy(
    engine: "LiveTradingEngine",
    feature_row: Dict[str, Any],
    stake: float,
    price: float,
) -> Tuple[bool, Dict[str, Any]]:
    """Evaluate EV policy for a candidate signal. Returns (allow, diagnostics)."""
    if engine._ev_policy_mode == "off":
        return True, {}
    if engine._ev_policy_runtime is None:
        if engine._ev_policy_mode == "enforce":
            engine._ev_policy_stats["scored"] += 1
            engine._ev_policy_stats["enforce_block"] += 1
            return False, {"reason": "ev_policy_unavailable", "ev_allow": False}
        return True, {}

    runtime = engine._ev_policy_runtime
    load_error = runtime.get("load_error") if isinstance(runtime, dict) else None
    if load_error:
        engine._ev_policy_stats["scored"] += 1
        if engine._ev_policy_mode == "enforce":
            engine._ev_policy_stats["enforce_block"] += 1
            return False, {
                "reason": "ev_policy_load_error",
                "error": load_error,
                "ev_allow": False,
            }
        return True, {
            "reason": "ev_policy_load_error",
            "error": load_error,
            "ev_allow": True,
        }
    win_scorer = runtime.get("win_scorer")
    fill_scorer = runtime.get("fill_scorer")
    min_ev_per_stake = float(runtime.get("min_ev_per_stake", 0.0))
    min_p_fill = float(runtime.get("min_p_fill", 0.0))

    missing_features = _missing_feature_diagnostics(
        win_scorer=win_scorer,
        fill_scorer=fill_scorer,
        feature_row=feature_row,
    )
    if missing_features:
        engine._ev_policy_stats["missing_runtime_features"] = (
            engine._ev_policy_stats.get("missing_runtime_features", 0) + 1
        )
        diag_missing = {
            "reason": "ev_policy_missing_runtime_features",
            "missing_runtime_features": missing_features,
            "missing_runtime_feature_count": sum(len(v) for v in missing_features.values()),
        }
        _warn_missing_runtime_features_once(runtime, missing_features)
        if engine._ev_policy_mode == "enforce":
            engine._ev_policy_stats["scored"] += 1
            engine._ev_policy_stats["enforce_block"] += 1
            return False, {
                **diag_missing,
                "ev_allow": False,
            }

    try:
        p_win_if_filled = win_scorer.score(feature_row) if win_scorer else 0.5
        p_fill = fill_scorer.score(feature_row) if fill_scorer else 0.5
    except Exception as exc:
        LOGGER.error("EV policy scoring failed: %s", exc)
        engine._ev_policy_stats["scored"] += 1
        if engine._ev_policy_mode == "enforce":
            engine._ev_policy_stats["enforce_block"] += 1
            return False, {
                "reason": "ev_policy_score_error",
                "error": str(exc),
                "ev_allow": False,
            }
        return True, {
            "reason": "ev_policy_score_error",
            "error": str(exc),
            "ev_allow": True,
        }

    win_profit = stake / price - stake if price > 0 else 0.0
    ev_if_filled = p_win_if_filled * win_profit - (1 - p_win_if_filled) * stake
    ev_realized = p_fill * ev_if_filled
    ev_per_stake = ev_realized / stake if stake > 0 else 0.0

    ev_allow = (p_fill >= min_p_fill) and (ev_per_stake >= min_ev_per_stake)

    diag = {
        "p_win_if_filled": round(p_win_if_filled, 4),
        "p_fill": round(p_fill, 4),
        "ev_if_filled": round(ev_if_filled, 4),
        "ev_realized": round(ev_realized, 4),
        "ev_per_stake": round(ev_per_stake, 4),
        "min_ev_per_stake": min_ev_per_stake,
        "min_p_fill": min_p_fill,
        "ev_allow": ev_allow,
    }
    if missing_features:
        diag["missing_runtime_features"] = missing_features
        diag["missing_runtime_feature_count"] = sum(len(v) for v in missing_features.values())

    engine._ev_policy_stats["scored"] += 1
    if engine._ev_policy_mode == "shadow":
        if ev_allow:
            engine._ev_policy_stats["shadow_allow"] += 1
        else:
            engine._ev_policy_stats["shadow_block"] += 1
    elif engine._ev_policy_mode == "enforce":
        if ev_allow:
            engine._ev_policy_stats["enforce_allow"] += 1
        else:
            engine._ev_policy_stats["enforce_block"] += 1

    return ev_allow, diag
