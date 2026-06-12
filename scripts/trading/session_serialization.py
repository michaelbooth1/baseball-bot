#!/usr/bin/env python3
"""
session_serialization.py -- Build session JSON payloads for paper and live engines.

Pure builders: no I/O, no logging, no throttling. The engines retain their
own _save_session methods (each with its own throttling / file write / post-
save side effects). Only the payload-construction boilerplate moves here.

Extracted from signal_engine._save_session and live_engine._save_session as
part of the Tier 1 refactor (2026-05-01). The two functions still maintain
their original divergent field sets to guarantee zero behavior change in
the on-disk session JSON; future PRs may consolidate the shared params block.

API:
  build_paper_session_payload(engine) -> Dict[str, Any]
  build_live_session_payload(engine, *, filled_settled, missed_settled,
                             filled, deployed, reserved,
                             shadow_order_diagnostics,
                             shadow_feature_diagnostics,
                             current_state_edge_band_diagnostics) -> Dict[str, Any]
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, TYPE_CHECKING

from line_state import _now_iso

from signal_config import (
    DEFAULT_ASK_EDGE_RAMP_ENABLED,
    DEFAULT_ASK_EDGE_RAMP_END,
    DEFAULT_ASK_EDGE_RAMP_MAX_BOOST,
    DEFAULT_ASK_EDGE_RAMP_START,
    DEFAULT_BLOWOUT_RELAX_MAX_INNING,
    DEFAULT_BLOWOUT_RELAX_MAX_RUNS_NEEDED,
    DEFAULT_BLOWOUT_RELAX_MIN_ASK,
    DEFAULT_COOLDOWN_TICKS,
    DEFAULT_GATE_BLOWOUT_RELAX_MODE,
    DEFAULT_GATE_MIN_CURRENT_TOTAL_RELAX_MODE,
    DEFAULT_GATE_RELAX_AB_FRACTION,
    DEFAULT_EXTREME_EDGE_MAX,
    DEFAULT_LTP_ASK_GAP_MAX,
    DEFAULT_MIN_CURRENT_TOTAL_RELAX_ASK_MIN,
    DEFAULT_MIN_CURRENT_TOTAL_RELAX_ENABLED,
    DEFAULT_MIN_CURRENT_TOTAL_RELAX_FLOOR,
    DEFAULT_MIN_CURRENT_TOTAL_RELAX_INNING,
    DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_LEAD,
    DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_RUNS_NEEDED,
    DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW,
    DEFAULT_PROB_CALIBRATION_MODE,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_ENABLED,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_ASK,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_INNING,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_SPREAD,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_ASK,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_DRAWDOWN,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_EMP_EDGE,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_INNING,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_PO_EDGE,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_AGE_SECS,
    DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_TICKS,
    DEFAULT_SHADOW_RELAXED_BLOWOUT_COND_MAX_ASK,
    DEFAULT_SHADOW_RELAXED_BLOWOUT_COND_MIN_INNING,
    DEFAULT_SHADOW_RELAXED_ENABLED,
    DEFAULT_STABLE_WINDOW,
    # 2026-05-23 (audit followup): paper params writer needs the same
    # enforced-gate defaults as live for auditability parity.
    DEFAULT_STAGE1_ALT_A_SCOPE_MODE,
    DEFAULT_MAX_CORRELATED_OVER_LINES_PER_GAME,
    DEFAULT_MIN_CORRELATED_LINE_GAP,
    DEFAULT_MAX_REFRESH_AGE_HOURS,
    # 2026-06-11 (pre-run observability fix): the 06-04 phantom-risk
    # gate and 06-03 Alt-A runtime-shadow levers were invisible in
    # session params, so post-session audits could not verify whether
    # either was active in the run that produced the bets.
    DEFAULT_MAX_PHANTOM_RISK_SCORE,
)

if TYPE_CHECKING:
    from signal_engine import SignalEngine


# ---------------------------------------------------------------------------
# Paper (signal_engine) session payload
# ---------------------------------------------------------------------------

def build_paper_session_payload(engine: "SignalEngine") -> Dict[str, Any]:
    """Return the dict that signal_engine writes to <date>_session.json.

    Field set is identical to the original SignalEngine._save_session body
    (preserved verbatim for zero behavior change). Engine must expose the
    standard counters/state set up in SignalEngine.__init__.
    """
    settled = [b for b in engine._bets if b.settled]
    wins = sum(1 for b in settled if b.won)
    total_profit = sum(b.profit for b in settled if b.profit is not None)
    total_staked = sum(b.stake for b in settled)
    trade_args = engine.trade_args

    return {
        "date": engine.date_str,
        "mode": "paper",
        "generated_at": _now_iso(),
        "params": {
            "config_label": str(getattr(trade_args, "config_label", "default") or "default"),
            "market_data_mode": str(getattr(engine, "_market_data_mode", "per_engine") or "per_engine"),
            "edge_threshold": trade_args.edge_threshold,
            "edge_threshold_high_line": trade_args.edge_threshold_high_line,
            "jump_threshold": trade_args.jump_threshold,
            "max_spread": trade_args.max_spread,
            "min_inning": trade_args.min_inning,
            "min_inning_high_line": trade_args.min_inning_high_line,
            "high_line_cutoff": trade_args.high_line_cutoff,
            "min_entry_ask": trade_args.min_entry_ask,
            "min_entry_ask_high_line": trade_args.min_entry_ask_high_line,
            "min_current_total": trade_args.min_current_total,
            "min_current_total_relax_enabled": bool(getattr(
                trade_args, "min_current_total_relax_enabled",
                DEFAULT_MIN_CURRENT_TOTAL_RELAX_ENABLED,
            )),
            "min_current_total_relax_floor": int(getattr(
                trade_args, "min_current_total_relax_floor",
                DEFAULT_MIN_CURRENT_TOTAL_RELAX_FLOOR,
            )),
            "min_current_total_relax_inning": int(getattr(
                trade_args, "min_current_total_relax_inning",
                DEFAULT_MIN_CURRENT_TOTAL_RELAX_INNING,
            )),
            "min_current_total_relax_ask_min": float(getattr(
                trade_args, "min_current_total_relax_ask_min",
                DEFAULT_MIN_CURRENT_TOTAL_RELAX_ASK_MIN,
            )),
            "min_current_total_relax_max_lead": int(getattr(
                trade_args, "min_current_total_relax_max_lead",
                DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_LEAD,
            )),
            "min_current_total_relax_max_runs_needed": float(getattr(
                trade_args, "min_current_total_relax_max_runs_needed",
                DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_RUNS_NEEDED,
            )),
            "gate_min_current_total_relax_mode": str(getattr(
                trade_args, "gate_min_current_total_relax_mode",
                DEFAULT_GATE_MIN_CURRENT_TOTAL_RELAX_MODE,
            )),
            "runs_needed_max": trade_args.runs_needed_max,
            "min_close_game_rn": trade_args.min_close_game_rn,
            "inn5_rn_max": trade_args.inn5_rn_max,
            "inn6_rn_max": trade_args.inn6_rn_max,
            "blowout_lead_min": trade_args.blowout_lead_min,
            "blowout_adj_lead_min": trade_args.blowout_adj_lead_min,
            "blowout_relax_max_inning": int(getattr(
                trade_args, "blowout_relax_max_inning",
                DEFAULT_BLOWOUT_RELAX_MAX_INNING,
            )),
            "blowout_relax_min_ask": float(getattr(
                trade_args, "blowout_relax_min_ask",
                DEFAULT_BLOWOUT_RELAX_MIN_ASK,
            )),
            "blowout_relax_max_runs_needed": float(getattr(
                trade_args, "blowout_relax_max_runs_needed",
                DEFAULT_BLOWOUT_RELAX_MAX_RUNS_NEEDED,
            )),
            "gate_blowout_relax_mode": str(getattr(
                trade_args, "gate_blowout_relax_mode",
                DEFAULT_GATE_BLOWOUT_RELAX_MODE,
            )),
            "gate_relax_ab_fraction": float(getattr(
                trade_args, "gate_relax_ab_fraction",
                DEFAULT_GATE_RELAX_AB_FRACTION,
            )),
            "s2_suppress_max": trade_args.s2_suppress_max,
            "s2_suppress_min_inning": trade_args.s2_suppress_min_inning,
            "sp_era_threshold": trade_args.sp_era_threshold,
            "sp_era_max_inning": trade_args.sp_era_max_inning,
            "sp_era_edge_boost": trade_args.sp_era_edge_boost,
            "ask_edge_ramp_enabled": bool(getattr(
                trade_args, "ask_edge_ramp_enabled",
                DEFAULT_ASK_EDGE_RAMP_ENABLED,
            )),
            "ask_edge_ramp_start": float(getattr(
                trade_args, "ask_edge_ramp_start",
                DEFAULT_ASK_EDGE_RAMP_START,
            )),
            "ask_edge_ramp_end": float(getattr(
                trade_args, "ask_edge_ramp_end",
                DEFAULT_ASK_EDGE_RAMP_END,
            )),
            "ask_edge_ramp_max_boost": float(getattr(
                trade_args, "ask_edge_ramp_max_boost",
                DEFAULT_ASK_EDGE_RAMP_MAX_BOOST,
            )),
            "max_base_fv": trade_args.max_base_fv,
            "fv_ask_gap_max": trade_args.fv_ask_gap_max,
            "fv_ask_gap_min_inning": trade_args.fv_ask_gap_min_inning,
            "prob_calibration_mode": engine._prob_calibration_mode,
            "prob_calibration_path": str(engine._prob_calibration_path),
            "prob_calibration_enforce_min_raw": float(
                getattr(engine, "_prob_calibration_enforce_min_raw", 0.0)
            ),
            # 2026-05-23 (audit followup): enforced-gate / feature flags
            # missing from prior paper sessions. Caught when the 5/22
            # daily audit could not tell what calibrator / scope / cap
            # config was active. The live writer already has some of
            # these; this block keeps paper auditable side-by-side.
            # NOTE: kept paper-relevant only — orders_*/kelly_*/daily_*
            # remain live-only because paper has no order lifecycle.
            "extreme_edge_max": float(getattr(
                trade_args, "extreme_edge_max", DEFAULT_EXTREME_EDGE_MAX,
            )),
            "ltp_ask_gap_max": float(getattr(
                trade_args, "ltp_ask_gap_max", DEFAULT_LTP_ASK_GAP_MAX,
            )),
            "stage1_alt_a_scope_mode": str(getattr(
                trade_args, "stage1_alt_a_scope_mode",
                DEFAULT_STAGE1_ALT_A_SCOPE_MODE,
            )),
            # 2026-06-11: phantom-risk gate (shipped 06-04) + Alt-A
            # runtime-shadow (shipped 06-03) were missing from params,
            # making post-session "was this lever active?" audits
            # impossible from artifacts alone.
            "max_phantom_risk_score": float(getattr(
                trade_args, "max_phantom_risk_score",
                DEFAULT_MAX_PHANTOM_RISK_SCORE,
            )),
            "stage1_shadow_empirical_mode": str(getattr(
                trade_args, "stage1_shadow_empirical_mode", "off",
            )),
            "max_correlated_over_lines_per_game": int(getattr(
                trade_args, "max_correlated_over_lines_per_game",
                DEFAULT_MAX_CORRELATED_OVER_LINES_PER_GAME,
            )),
            "min_correlated_line_gap": float(getattr(
                trade_args, "min_correlated_line_gap",
                DEFAULT_MIN_CORRELATED_LINE_GAP,
            )),
            "require_fresh_refresh": bool(getattr(
                trade_args, "require_fresh_refresh", False,
            )),
            "max_refresh_age_hours": float(getattr(
                trade_args, "max_refresh_age_hours",
                DEFAULT_MAX_REFRESH_AGE_HOURS,
            )),
            "shadow_no_score_drift_enabled": bool(getattr(
                trade_args,
                "shadow_no_score_drift_enabled",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_ENABLED,
            )),
            "shadow_no_score_drift_min_inning": int(getattr(
                trade_args,
                "shadow_no_score_drift_min_inning",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_INNING,
            )),
            "shadow_no_score_drift_max_inning": int(getattr(
                trade_args,
                "shadow_no_score_drift_max_inning",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_INNING,
            )),
            "shadow_no_score_drift_min_segment_age_secs": float(getattr(
                trade_args,
                "shadow_no_score_drift_min_segment_age_secs",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_AGE_SECS,
            )),
            "shadow_no_score_drift_min_segment_ticks": int(getattr(
                trade_args,
                "shadow_no_score_drift_min_segment_ticks",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_TICKS,
            )),
            "shadow_no_score_drift_min_drawdown": float(getattr(
                trade_args,
                "shadow_no_score_drift_min_drawdown",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_DRAWDOWN,
            )),
            "shadow_no_score_drift_min_ask": float(getattr(
                trade_args,
                "shadow_no_score_drift_min_ask",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_ASK,
            )),
            "shadow_no_score_drift_max_ask": float(getattr(
                trade_args,
                "shadow_no_score_drift_max_ask",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_ASK,
            )),
            "shadow_no_score_drift_max_spread": float(getattr(
                trade_args,
                "shadow_no_score_drift_max_spread",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_SPREAD,
            )),
            "shadow_no_score_drift_min_po_edge": float(getattr(
                trade_args,
                "shadow_no_score_drift_min_po_edge",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_PO_EDGE,
            )),
            "shadow_no_score_drift_min_emp_edge": float(getattr(
                trade_args,
                "shadow_no_score_drift_min_emp_edge",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_EMP_EDGE,
            )),
            "shadow_relaxed_enabled": bool(getattr(trade_args, "shadow_relaxed_enabled", DEFAULT_SHADOW_RELAXED_ENABLED)),
            "shadow_relaxed_min_current_total": trade_args.shadow_relaxed_min_current_total,
            "shadow_relaxed_runs_needed_max": trade_args.shadow_relaxed_runs_needed_max,
            "shadow_relaxed_min_close_game_rn": trade_args.shadow_relaxed_min_close_game_rn,
            "shadow_relaxed_inn5_rn_max": trade_args.shadow_relaxed_inn5_rn_max,
            "shadow_relaxed_inn6_rn_max": trade_args.shadow_relaxed_inn6_rn_max,
            "shadow_relaxed_blowout_lead_min": trade_args.shadow_relaxed_blowout_lead_min,
            "shadow_relaxed_blowout_adj_lead_min": trade_args.shadow_relaxed_blowout_adj_lead_min,
            "shadow_relaxed_blowout_cond_min_inning": int(getattr(
                trade_args, "shadow_relaxed_blowout_cond_min_inning",
                DEFAULT_SHADOW_RELAXED_BLOWOUT_COND_MIN_INNING,
            )),
            "shadow_relaxed_blowout_cond_max_ask": float(getattr(
                trade_args, "shadow_relaxed_blowout_cond_max_ask",
                DEFAULT_SHADOW_RELAXED_BLOWOUT_COND_MAX_ASK,
            )),
            "shadow_relaxed_s2_suppress_max": trade_args.shadow_relaxed_s2_suppress_max,
            "shadow_relaxed_s2_suppress_min_inning": trade_args.shadow_relaxed_s2_suppress_min_inning,
            "shadow_relaxed_max_base_fv": trade_args.shadow_relaxed_max_base_fv,
            "shadow_relaxed_fv_ask_gap_max": trade_args.shadow_relaxed_fv_ask_gap_max,
            "shadow_relaxed_fv_ask_gap_min_inning": trade_args.shadow_relaxed_fv_ask_gap_min_inning,
            "shadow_relaxed_min_edge_offset": trade_args.shadow_relaxed_min_edge_offset,
            "shadow_relaxed_sp_era_edge_boost": trade_args.shadow_relaxed_sp_era_edge_boost,
            "confirmation_ticks": trade_args.confirmation_ticks,
            "event_dedup_secs": trade_args.event_dedup_secs,
            "inning_dedup_gap": trade_args.inning_dedup_gap,
            "inning_dedup_edge_gap": trade_args.inning_dedup_edge_gap,
            "stake": trade_args.stake,
            "lookback_ticks": trade_args.lookback_ticks,
            "stable_window": DEFAULT_STABLE_WINDOW,
            "cooldown_ticks": DEFAULT_COOLDOWN_TICKS,
        },
        "summary": {
            "total_bets": len(engine._bets),
            "settled": len(settled),
            "pending": len(engine._bets) - len(settled),
            "wins": wins,
            "losses": len(settled) - wins,
            "win_rate": round(wins / len(settled), 4) if settled else None,
            "total_staked": round(total_staked, 2),
            "total_profit": round(total_profit, 2),
            "roi": round(total_profit / total_staked, 4) if total_staked > 0 else None,
            "candidate_rows_written": int(engine._candidate_rows_written),
            "candidate_rows_dedup_suppressed": int(engine._candidate_rows_dedup_suppressed),
            "candidate_raw_sample_suppressed": int(getattr(engine, "_candidate_raw_sample_suppressed", 0)),
            "candidate_rows_write_errors": int(engine._candidate_rows_write_errors),
            "candidate_null_fields_omitted": int(engine._candidate_null_fields_omitted),
            "candidate_compacted_fields_omitted": int(getattr(engine, "_candidate_compacted_fields_omitted", 0)),
            "candidate_calibration_rows_written": int(getattr(engine, "_candidate_calibration_rows_written", 0)),
            "candidate_calibration_write_errors": int(getattr(engine, "_candidate_calibration_write_errors", 0)),
            "candidate_calibration_log_path": str(engine._candidate_calibration_log_path()),
            "score_confirmation_rows_written": int(getattr(engine, "_score_confirmation_rows_written", 0)),
            "score_confirmation_write_errors": int(getattr(engine, "_score_confirmation_write_errors", 0)),
            "score_confirmation_pending_rows": int(len(getattr(engine, "_score_confirmation_pending", {}) or {})),
            "score_confirmation_log_path": str(engine._score_confirmation_log_path()),
            "candidate_rollup_path": str(engine._candidate_rollup_path()),
            "candidate_rollup": engine._candidate_rollup_snapshot(top_n=25),
            "skip_debug_logs_emitted": int(engine._skip_debug_logs_emitted),
            "skip_debug_logs_dedup_suppressed": int(engine._skip_debug_logs_dedup_suppressed),
            "shadow_relaxed_evaluated": int(engine._shadow_relaxed_evaluated),
            "shadow_relaxed_would_pass": int(engine._shadow_relaxed_would_pass),
            "shadow_relaxed_still_blocked": int(engine._shadow_relaxed_blocked),
            "shadow_relaxed_eval_by_reason": dict(engine._shadow_relaxed_eval_by_reason),
            "shadow_relaxed_would_pass_by_reason": dict(engine._shadow_relaxed_would_pass_by_reason),
            # Gate 1/3 shadow counts (session-level; these gates fire before candidate
            # payload is built so per-signal logging would require pipeline restructuring)
            "shadow_gate1_blocked": int(engine._shadow_gate1_blocked),
            "shadow_gate1_would_pass": int(engine._shadow_gate1_would_pass),
            "shadow_gate3_blocked": int(engine._shadow_gate3_blocked),
            "shadow_gate3_would_pass": int(engine._shadow_gate3_would_pass),
            "prob_calibration_scored": int(engine._prob_calibration_stats.get("scored", 0)),
            "prob_calibration_applied": int(engine._prob_calibration_stats.get("applied", 0)),
            "prob_calibration_shadow_scored": int(engine._prob_calibration_stats.get("shadow_scored", 0)),
            "prob_calibration_disabled_or_missing": int(engine._prob_calibration_stats.get("disabled_or_missing", 0)),
            "prob_calibration_family_missing": int(engine._prob_calibration_stats.get("family_missing", 0)),
            "prob_calibration_family_missing_fail_closed": int(engine._prob_calibration_stats.get("family_missing_fail_closed", 0)),
            "market_data_health": dict(getattr(engine, "_market_data_health", {}) or {}),
            "market_data_gap_count": int((getattr(engine, "_market_data_health", {}) or {}).get("market_data_gap_count") or 0),
            "last_market_data_sequence": int((getattr(engine, "_market_data_health", {}) or {}).get("last_market_data_sequence") or 0),
            "max_market_data_lag_ms": float((getattr(engine, "_market_data_health", {}) or {}).get("max_market_data_lag_ms") or 0.0),
            "consumer_disconnects": int((getattr(engine, "_market_data_health", {}) or {}).get("consumer_disconnects") or 0),
        },
        "bets": [asdict(b) for b in engine._bets],
    }


# ---------------------------------------------------------------------------
# Live (live_engine) session payload
# ---------------------------------------------------------------------------

def build_live_session_payload(
    engine: "SignalEngine",
    *,
    filled_settled: List[Any],
    missed_settled: List[Any],
    filled: List[Any],
    deployed: float,
    reserved: float,
    shadow_order_diagnostics: Any,
    shadow_feature_diagnostics: Any,
    current_state_edge_band_diagnostics: Any,
) -> Dict[str, Any]:
    """Return the dict that live_engine writes to <date>_live_session.json.

    Field set is identical to the original LiveTradingEngine._save_session body
    (preserved verbatim for zero behavior change). The engine method computes
    filled_settled / missed_settled / filled / deployed / reserved /
    shadow_order_diagnostics / current_state_edge_band_diagnostics once and
    passes them in (the engine method also reuses them for the post-save
    per-game summary log block).
    """
    wins = sum(1 for b in filled_settled if b.won)
    total_profit = sum((b.profit or 0) for b in filled_settled)
    total_staked = sum(engine._filled_notional(b) for b in filled_settled)
    signal_wins = sum(1 for b in (filled_settled + missed_settled) if b.won)
    trade_args = engine.trade_args
    live_args = engine.live_args
    # Live-specific defaults pulled lazily from live_engine module to avoid
    # an extra import surface here. We use getattr fallbacks identical to
    # the original code.
    from live_engine import (  # noqa: E402
        DEFAULT_FV_DECAY_MIN_AGE_SECS,
        DEFAULT_FV_DECAY_MIN_ASK_DROP,
        DEFAULT_ASK_REVERSAL_DROP,
        DEFAULT_ASK_REVERSAL_WINDOW,
        DEFAULT_PER_GAME_BUDGET_FRACTION,
        DEFAULT_STAKE_MODE,
        DEFAULT_KELLY_FRACTION,
        DEFAULT_KELLY_MAX_BET_FRACTION,
        DEFAULT_KELLY_MAX_EDGE,
        DEFAULT_KELLY_FLOOR_TO_MIN,
        FV_CANCEL_MIN_EDGE,
    )

    return {
        "date": engine.date_str,
        "generated_at": _now_iso(),
        "mode": "dry_run" if engine._dry_run else "live",
        "params": {
            "config_label": str(getattr(trade_args, "config_label", "default") or "default"),
            "edge_threshold": trade_args.edge_threshold,
            "edge_threshold_high_line": trade_args.edge_threshold_high_line,
            "jump_threshold": trade_args.jump_threshold,
            "max_spread": trade_args.max_spread,
            "min_inning": trade_args.min_inning,
            "min_inning_high_line": trade_args.min_inning_high_line,
            "high_line_cutoff": trade_args.high_line_cutoff,
            "min_entry_ask": trade_args.min_entry_ask,
            "min_entry_ask_high_line": trade_args.min_entry_ask_high_line,
            "min_current_total": trade_args.min_current_total,
            "min_current_total_relax_enabled": bool(getattr(
                trade_args, "min_current_total_relax_enabled", True
            )),
            "min_current_total_relax_floor": int(getattr(
                trade_args, "min_current_total_relax_floor", 3
            )),
            "min_current_total_relax_inning": int(getattr(
                trade_args, "min_current_total_relax_inning", 4
            )),
            "min_current_total_relax_ask_min": float(getattr(
                trade_args, "min_current_total_relax_ask_min", 0.60
            )),
            "min_current_total_relax_max_lead": int(getattr(
                trade_args, "min_current_total_relax_max_lead",
                DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_LEAD,
            )),
            "min_current_total_relax_max_runs_needed": float(getattr(
                trade_args, "min_current_total_relax_max_runs_needed",
                DEFAULT_MIN_CURRENT_TOTAL_RELAX_MAX_RUNS_NEEDED,
            )),
            "gate_min_current_total_relax_mode": str(getattr(
                trade_args, "gate_min_current_total_relax_mode",
                DEFAULT_GATE_MIN_CURRENT_TOTAL_RELAX_MODE,
            )),
            "runs_needed_max": trade_args.runs_needed_max,
            "min_close_game_rn": trade_args.min_close_game_rn,
            "inn5_rn_max": trade_args.inn5_rn_max,
            "inn6_rn_max": trade_args.inn6_rn_max,
            "blowout_lead_min": trade_args.blowout_lead_min,
            "blowout_adj_lead_min": trade_args.blowout_adj_lead_min,
            "blowout_relax_max_inning": int(getattr(
                trade_args, "blowout_relax_max_inning",
                DEFAULT_BLOWOUT_RELAX_MAX_INNING,
            )),
            "blowout_relax_min_ask": float(getattr(
                trade_args, "blowout_relax_min_ask",
                DEFAULT_BLOWOUT_RELAX_MIN_ASK,
            )),
            "blowout_relax_max_runs_needed": float(getattr(
                trade_args, "blowout_relax_max_runs_needed",
                DEFAULT_BLOWOUT_RELAX_MAX_RUNS_NEEDED,
            )),
            "gate_blowout_relax_mode": str(getattr(
                trade_args, "gate_blowout_relax_mode",
                DEFAULT_GATE_BLOWOUT_RELAX_MODE,
            )),
            "gate_relax_ab_fraction": float(getattr(
                trade_args, "gate_relax_ab_fraction",
                DEFAULT_GATE_RELAX_AB_FRACTION,
            )),
            "s2_suppress_max": trade_args.s2_suppress_max,
            "s2_suppress_min_inning": trade_args.s2_suppress_min_inning,
            "sp_era_threshold": trade_args.sp_era_threshold,
            "sp_era_max_inning": trade_args.sp_era_max_inning,
            "sp_era_edge_boost": trade_args.sp_era_edge_boost,
            "ask_edge_ramp_enabled": bool(getattr(
                trade_args, "ask_edge_ramp_enabled",
                DEFAULT_ASK_EDGE_RAMP_ENABLED,
            )),
            "ask_edge_ramp_start": float(getattr(
                trade_args, "ask_edge_ramp_start",
                DEFAULT_ASK_EDGE_RAMP_START,
            )),
            "ask_edge_ramp_end": float(getattr(
                trade_args, "ask_edge_ramp_end",
                DEFAULT_ASK_EDGE_RAMP_END,
            )),
            "ask_edge_ramp_max_boost": float(getattr(
                trade_args, "ask_edge_ramp_max_boost",
                DEFAULT_ASK_EDGE_RAMP_MAX_BOOST,
            )),
            "max_base_fv": trade_args.max_base_fv,
            "fv_ask_gap_max": trade_args.fv_ask_gap_max,
            "fv_ask_gap_min_inning": trade_args.fv_ask_gap_min_inning,
            "extreme_edge_max": float(getattr(trade_args, "extreme_edge_max", DEFAULT_EXTREME_EDGE_MAX)),
            "ltp_ask_gap_max": float(getattr(trade_args, "ltp_ask_gap_max", DEFAULT_LTP_ASK_GAP_MAX)),
            # 2026-06-11: same observability fix as the paper writer --
            # the 06-04 phantom gate + 06-03 Alt-A shadow levers must be
            # verifiable from the session artifact (override-file levers
            # only take effect on engine boot, so params is the proof
            # of what was actually live).
            "max_phantom_risk_score": float(getattr(
                trade_args, "max_phantom_risk_score",
                DEFAULT_MAX_PHANTOM_RISK_SCORE,
            )),
            "stage1_shadow_empirical_mode": str(getattr(
                trade_args, "stage1_shadow_empirical_mode", "off",
            )),
            "prob_calibration_mode": str(getattr(
                trade_args, "prob_calibration_mode",
                DEFAULT_PROB_CALIBRATION_MODE,
            )),
            "prob_calibration_path": str(getattr(trade_args, "prob_calibration_path", "")),
            "prob_calibration_enforce_min_raw": float(getattr(
                trade_args, "prob_calibration_enforce_min_raw",
                DEFAULT_PROB_CALIBRATION_ENFORCE_MIN_RAW,
            )),
            "shadow_no_score_drift_enabled": bool(getattr(
                trade_args,
                "shadow_no_score_drift_enabled",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_ENABLED,
            )),
            "shadow_no_score_drift_min_inning": int(getattr(
                trade_args,
                "shadow_no_score_drift_min_inning",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_INNING,
            )),
            "shadow_no_score_drift_max_inning": int(getattr(
                trade_args,
                "shadow_no_score_drift_max_inning",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_INNING,
            )),
            "shadow_no_score_drift_min_segment_age_secs": float(getattr(
                trade_args,
                "shadow_no_score_drift_min_segment_age_secs",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_AGE_SECS,
            )),
            "shadow_no_score_drift_min_segment_ticks": int(getattr(
                trade_args,
                "shadow_no_score_drift_min_segment_ticks",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_SEGMENT_TICKS,
            )),
            "shadow_no_score_drift_min_drawdown": float(getattr(
                trade_args,
                "shadow_no_score_drift_min_drawdown",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_DRAWDOWN,
            )),
            "shadow_no_score_drift_min_ask": float(getattr(
                trade_args,
                "shadow_no_score_drift_min_ask",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_ASK,
            )),
            "shadow_no_score_drift_max_ask": float(getattr(
                trade_args,
                "shadow_no_score_drift_max_ask",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_ASK,
            )),
            "shadow_no_score_drift_max_spread": float(getattr(
                trade_args,
                "shadow_no_score_drift_max_spread",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MAX_SPREAD,
            )),
            "shadow_no_score_drift_min_po_edge": float(getattr(
                trade_args,
                "shadow_no_score_drift_min_po_edge",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_PO_EDGE,
            )),
            "shadow_no_score_drift_min_emp_edge": float(getattr(
                trade_args,
                "shadow_no_score_drift_min_emp_edge",
                DEFAULT_SHADOW_NO_SCORE_DRIFT_MIN_EMP_EDGE,
            )),
            "confirmation_ticks": trade_args.confirmation_ticks,
            "event_dedup_secs": trade_args.event_dedup_secs,
            "inning_dedup_gap": trade_args.inning_dedup_gap,
            "inning_dedup_edge_gap": trade_args.inning_dedup_edge_gap,
            "stake": trade_args.stake,
            "lookback_ticks": trade_args.lookback_ticks,
            "stable_window": DEFAULT_STABLE_WINDOW,
            "cooldown_ticks": DEFAULT_COOLDOWN_TICKS,
            # Live-specific
            "spread_factor": live_args.spread_factor,
            # 2026-06-11: placement fill-gap floor (shipped 06-04 as a
            # live-only CLI flag). 0.02 caps the limit at ask-2c; the
            # pre-fix behavior (no cap) is represented by 1.0. Recorded
            # so fill-rate audits can split pre/post-cap cohorts.
            "max_limit_gap_below_ask": float(getattr(
                live_args, "max_limit_gap_below_ask", 1.0,
            )),
            "order_timeout_secs": live_args.order_timeout_secs,
            "fv_cancel_min_edge": getattr(live_args, "fv_cancel_min_edge", FV_CANCEL_MIN_EDGE),
            "fv_decay_min_age_secs": getattr(live_args, "fv_decay_min_age_secs", DEFAULT_FV_DECAY_MIN_AGE_SECS),
            "fv_decay_min_ask_drop": getattr(live_args, "fv_decay_min_ask_drop", DEFAULT_FV_DECAY_MIN_ASK_DROP),
            "ask_reversal_drop": getattr(live_args, "ask_reversal_drop", DEFAULT_ASK_REVERSAL_DROP),
            "ask_reversal_window": getattr(live_args, "ask_reversal_window", DEFAULT_ASK_REVERSAL_WINDOW),
            "max_open_orders": live_args.max_open_orders,
            "daily_budget": live_args.daily_budget,
            "per_game_budget_fraction": getattr(live_args, "per_game_budget_fraction", DEFAULT_PER_GAME_BUDGET_FRACTION),
            "stake_mode": getattr(live_args, "stake_mode", DEFAULT_STAKE_MODE),
            "kelly_fraction": getattr(live_args, "kelly_fraction", DEFAULT_KELLY_FRACTION),
            "kelly_max_bet_fraction": getattr(live_args, "kelly_max_bet_fraction", DEFAULT_KELLY_MAX_BET_FRACTION),
            "kelly_max_edge": getattr(live_args, "kelly_max_edge", DEFAULT_KELLY_MAX_EDGE),
            "kelly_floor_to_min": bool(getattr(live_args, "kelly_floor_to_min", DEFAULT_KELLY_FLOOR_TO_MIN)),
            "ev_policy_mode": engine._ev_policy_mode,
            "dry_run": engine._dry_run,
        },
        "summary": {
            "total_bets": len(engine._bets),
            "orders_placed": sum(1 for b in engine._bets if getattr(b, "order_id", None)),
            "orders_filled": len(filled),
            "orders_open": len(engine._open_orders),
            "orders_cancelled": sum(1 for b in engine._bets if getattr(b, "order_status", "") == "cancelled"),
            "orders_cancelled_fv_decay": sum(1 for b in engine._bets if getattr(b, "cancel_reason", "") == "fv_decay"),
            "orders_cancelled_ask_reversal": sum(1 for b in engine._bets if getattr(b, "cancel_reason", "") == "ask_reversal"),
            "orders_cancelled_timeout": sum(1 for b in engine._bets if getattr(b, "cancel_reason", "") == "timeout"),
            "orders_cancelled_game_final": sum(1 for b in engine._bets if getattr(b, "cancel_reason", "") == "game_final"),
            "orders_error": sum(1 for b in engine._bets if getattr(b, "order_status", "") == "error"),
            "settled": len(filled_settled),
            "missed": len(missed_settled),
            "pending": len(engine._bets) - len(filled_settled) - len(missed_settled),
            "wins": wins,
            "losses": len(filled_settled) - wins,
            "win_rate": round(wins / len(filled_settled), 4) if filled_settled else None,
            "total_staked": round(total_staked, 2),
            "total_profit": round(total_profit, 2),
            "roi": round(total_profit / total_staked, 4) if total_staked > 0 else None,
            "signal_wins": signal_wins,
            "signal_total": len(filled_settled) + len(missed_settled),
            "signal_win_rate": round(signal_wins / (len(filled_settled) + len(missed_settled)), 4) if (filled_settled or missed_settled) else None,
            "budget_deployed": round(deployed, 2),
            "budget_reserved": round(reserved, 2),
            "budget_remaining": round(max(0, live_args.daily_budget - deployed - reserved), 2),
            "ev_policy_scored": engine._ev_policy_stats.get("scored", 0),
            "ev_policy_shadow_allow": engine._ev_policy_stats.get("shadow_allow", 0),
            "ev_policy_shadow_block": engine._ev_policy_stats.get("shadow_block", 0),
            "ev_policy_enforce_allow": engine._ev_policy_stats.get("enforce_allow", 0),
            "ev_policy_enforce_block": engine._ev_policy_stats.get("enforce_block", 0),
            "ev_policy_missing_runtime_features": engine._ev_policy_stats.get("missing_runtime_features", 0),
            "prob_calibration_scored": int(getattr(engine, "_prob_calibration_stats", {}).get("scored", 0)),
            "prob_calibration_applied": int(getattr(engine, "_prob_calibration_stats", {}).get("applied", 0)),
            "prob_calibration_shadow_scored": int(getattr(engine, "_prob_calibration_stats", {}).get("shadow_scored", 0)),
            "prob_calibration_disabled_or_missing": int(getattr(engine, "_prob_calibration_stats", {}).get("disabled_or_missing", 0)),
            "prob_calibration_family_missing": int(getattr(engine, "_prob_calibration_stats", {}).get("family_missing", 0)),
            "prob_calibration_family_missing_fail_closed": int(getattr(engine, "_prob_calibration_stats", {}).get("family_missing_fail_closed", 0)),
            "candidate_rows_written": int(getattr(engine, "_candidate_rows_written", 0)),
            "candidate_rows_dedup_suppressed": int(getattr(engine, "_candidate_rows_dedup_suppressed", 0)),
            "candidate_raw_sample_suppressed": int(getattr(engine, "_candidate_raw_sample_suppressed", 0)),
            "candidate_rows_write_errors": int(getattr(engine, "_candidate_rows_write_errors", 0)),
            "candidate_null_fields_omitted": int(getattr(engine, "_candidate_null_fields_omitted", 0)),
            "candidate_compacted_fields_omitted": int(getattr(engine, "_candidate_compacted_fields_omitted", 0)),
            "candidate_calibration_rows_written": int(getattr(engine, "_candidate_calibration_rows_written", 0)),
            "candidate_calibration_write_errors": int(getattr(engine, "_candidate_calibration_write_errors", 0)),
            "candidate_calibration_log_path": str(engine._candidate_calibration_log_path()),
            "score_confirmation_rows_written": int(getattr(engine, "_score_confirmation_rows_written", 0)),
            "score_confirmation_write_errors": int(getattr(engine, "_score_confirmation_write_errors", 0)),
            "score_confirmation_pending_rows": int(len(getattr(engine, "_score_confirmation_pending", {}) or {})),
            "score_confirmation_log_path": str(engine._score_confirmation_log_path()),
            "candidate_rollup_path": str(engine._candidate_rollup_path()),
            "candidate_rollup": engine._candidate_rollup_snapshot(top_n=25),
            "shadow_order_diagnostics": shadow_order_diagnostics,
            "shadow_feature_diagnostics": shadow_feature_diagnostics,
            "current_state_edge_band_diagnostics": current_state_edge_band_diagnostics,
            # Wallet-aware paper-fallback (shipped 2026-05-13). Bets the
            # engine routed to a synthesized paper-fallback because the
            # CLOB rejected on insufficient balance (or because the
            # cooldown was active from a recent rejection). Counted
            # separately from real-money bets so analysis can include them
            # for signal/outcome math without polluting real-money P&L.
            "paper_fallback_placed": int(
                getattr(engine, "_paper_fallback_stats", {}).get("placed", 0)
            ),
            "paper_fallback_total_stake": round(float(
                getattr(engine, "_paper_fallback_stats", {}).get("total_stake", 0.0)
            ), 2),
            "paper_fallback_wallet_exhausted_events": int(
                getattr(engine, "_paper_fallback_stats", {}).get("wallet_exhausted_events", 0)
            ),
            "paper_fallback_wallet_exhausted_last_at": (
                getattr(engine, "_paper_fallback_stats", {}).get("wallet_exhausted_last_at")
            ),
            "paper_fallback_last_reason": (
                getattr(engine, "_paper_fallback_stats", {}).get("last_reason")
            ),
        },
        "bets": [asdict(b) for b in engine._bets],
    }
