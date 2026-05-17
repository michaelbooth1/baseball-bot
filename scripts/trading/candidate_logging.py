#!/usr/bin/env python3
"""
candidate_logging.py -- Public facade for candidate JSONL logging.

This module intentionally stays small. The heavy pieces live in focused helper
modules:
  - candidate_paths.py                IDs, paths, JSONL helpers, outcomes
  - candidate_rollups.py              compact counters and rollup JSON
  - candidate_schema_enrichment.py    logit residuals + calibration sidecar
  - candidate_score_confirmation.py   score-event confirmation sidecar

`signal_engine.py` imports from this facade, so existing engine wrappers and
tests continue to call the same public functions.
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter
from typing import Dict, Optional, TYPE_CHECKING, Tuple

from line_state import _now_iso

from candidate_paths import (
    count_none_values,
    drop_none_values,
    jsonl_dumps,
    candidate_calibration_log_path,
    candidate_log_path,
    candidate_rollup_path,
    next_candidate_id,
    outcome_log_path,
    score_confirmation_log_path,
    write_outcome_record,
)
from candidate_rollups import (
    candidate_rollup_snapshot,
    ensure_candidate_rollup_state,
    observe_candidate_rollup,
    write_candidate_rollup,
)
from candidate_schema_enrichment import (
    MARKET_COMPLEMENT_FIELD_KEYS,
    attach_candidate_shadow_fields,
    attach_modeling_observability_fields,
    is_calibration_opportunity,
    to_float,
    write_calibration_opportunity,
)
from candidate_score_confirmation import (
    SCORE_CONFIRMATION_MAX_WINDOW_SECS,
    SCORE_CONFIRMATION_WINDOWS_SECS,
    flush_expired_score_confirmations,
    observe_score_confirmation_ticks,
    register_score_confirmation_candidate,
)
from weather_client import WEATHER_FEATURE_FIELD_KEYS

if TYPE_CHECKING:
    from monitor_mlb_polymarket_ou import OUMarket, ScheduledGame
    from signal_engine import SignalEngine

LOGGER = logging.getLogger("signal_engine")

# Backward-compatible aliases for older tests/tools that reached into the old
# monolithic module. New code should import from the focused modules directly.
_drop_none_values = drop_none_values
_count_none_values = count_none_values
_jsonl_dumps = jsonl_dumps
_to_float = to_float
_is_calibration_opportunity = is_calibration_opportunity

DEPRECATED_RAW_CANDIDATE_FIELDS = {
    "weather_mlb_schedule_condition",
    "weather_mlb_schedule_wind",
    "weather_mlb_schedule_wind_direction_label",
}

COMPACT_EARLY_SKIP_WEATHER_KEEP_FIELDS = {
    "weather_cache_available",
    "weather_cache_date",
    "weather_source_provider",
    "weather_source_status",
    "weather_source_error",
    "stadium_id",
    "stadium_primary_name",
    "stadium_roof_type",
    "stadium_weather_exposure",
    "stadium_weather_sensitivity",
    "weather_active_default",
    "weather_roof_state_assumption",
    "weather_roof_uncertain",
    "weather_model_usable",
}

VERBOSE_EARLY_SKIP_WEATHER_FIELDS = (
    set(WEATHER_FEATURE_FIELD_KEYS) - COMPACT_EARLY_SKIP_WEATHER_KEEP_FIELDS
)

EARLY_SKIP_MARKET_FIELDS = set(MARKET_COMPLEMENT_FIELD_KEYS)

MODEL_BEARING_FIELD_MARKERS = (
    "fair_value",
    "fair_value_raw",
    "fair_value_calibrated",
    "base_fair_value",
    "stage2_run_env_delta",
    "team_offense_delta",
    "current_state_value_fv_raw",
    "current_state_value_edge",
    "shadow_fv_current_state",
    "shadow_fv_after_inferred_score",
)

EARLY_SKIP_RAW_SAMPLE_EVERY = 25


def is_model_bearing_candidate(row: Dict[str, object], *, calibration: bool = False) -> bool:
    """Return true for rows that should retain full modeling/weather context."""
    if calibration:
        return True
    if str(row.get("decision") or "").lower() in {"trade", "shadow_no_score_drift", "skip_with_features"}:
        return True
    return any(row.get(field) is not None for field in MODEL_BEARING_FIELD_MARKERS)


def is_raw_early_gate_candidate(row: Dict[str, object], *, calibration: bool = False) -> bool:
    """Return true for high-volume pre-FV skip rows eligible for raw sampling."""
    if calibration:
        return False
    if str(row.get("decision") or "").lower() != "skip":
        return False
    return not is_model_bearing_candidate(row, calibration=calibration)


def _bucket_float(value: object, *, width: float, decimals: int = 2) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except Exception:
        return str(value)
    if not math.isfinite(number) or width <= 0:
        return ""
    bucket = math.floor(number / width) * width
    return f"{bucket:.{decimals}f}-{bucket + width:.{decimals}f}"


def build_raw_early_gate_sample_key(row: Dict[str, object]) -> Tuple[str, ...]:
    """Coarse state/price bucket for raw early-gate sampling.

    Rollups still observe every row. This key only decides whether the raw
    candidate JSONL needs another full early-skip example for this state.
    """
    def _text(field: str) -> str:
        value = row.get(field)
        return "" if value is None else str(value)

    return (
        _text("decision_reason"),
        _text("game_pk"),
        _text("line"),
        _text("inning"),
        _text("inning_state"),
        _text("outs"),
        _text("current_total"),
        _text("away_score_before"),
        _text("home_score_before"),
        _text("runners_on"),
        _bucket_float(row.get("decision_ask"), width=0.05, decimals=2),
        _bucket_float(row.get("best_bid"), width=0.05, decimals=2),
    )


def should_write_raw_early_gate_row(engine: "SignalEngine", row: Dict[str, object]) -> bool:
    if not hasattr(engine, "_candidate_raw_sample_seen"):
        engine._candidate_raw_sample_seen = Counter()
    key = build_raw_early_gate_sample_key(row)
    engine._candidate_raw_sample_seen[key] += 1
    count = int(engine._candidate_raw_sample_seen[key])
    return count == 1 or count % EARLY_SKIP_RAW_SAMPLE_EVERY == 0


def compact_raw_candidate_row(row: Dict[str, object], *, calibration: bool = False) -> Dict[str, object]:
    """Slim raw early-skip rows while preserving model-bearing rows in full."""
    compact = dict(row)
    for field in DEPRECATED_RAW_CANDIDATE_FIELDS:
        compact.pop(field, None)
    if is_model_bearing_candidate(compact, calibration=calibration):
        if not calibration and not compact.get("under_pair_available"):
            for field in EARLY_SKIP_MARKET_FIELDS:
                compact.pop(field, None)
        return compact
    if str(compact.get("decision") or "").lower() != "skip":
        return compact
    for field in EARLY_SKIP_MARKET_FIELDS:
        compact.pop(field, None)
    for field in VERBOSE_EARLY_SKIP_WEATHER_FIELDS:
        compact.pop(field, None)
    return compact


def build_candidate_skip_dedup_key(row: Dict[str, object]) -> Optional[Tuple[str, ...]]:
    decision = str(row.get("decision") or "").lower()
    if decision != "skip":
        return None

    def _text(field: str) -> str:
        value = row.get(field)
        return "" if value is None else str(value)

    def _bucket(field: str, decimals: int = 2) -> str:
        value = row.get(field)
        if value is None or value == "":
            return ""
        try:
            return f"{float(value):.{decimals}f}"
        except Exception:
            return str(value)

    return (
        _text("decision_reason"),
        _text("game_pk"),
        _text("line"),
        _text("inning"),
        _text("inning_state"),
        _text("outs"),
        _text("current_total"),
        _text("away_score_before"),
        _text("home_score_before"),
        _text("runners_on"),
        _bucket("decision_ask", 2),
        _bucket("best_bid", 2),
        _bucket("edge", 3),
        _bucket("fair_value", 3),
        _bucket("base_fair_value", 3),
        _bucket("stage2_run_env_delta", 3),
        _bucket("team_offense_delta", 3),
        _bucket("min_edge_effective", 3),
        _text("shadow_relaxed_reason"),
        _text("shadow_relaxed_would_pass"),
        _bucket("shadow_relaxed_value", 3),
        _bucket("shadow_relaxed_threshold", 3),
        _text("shadow_relaxed_secondary_reason"),
        _text("shadow_relaxed_secondary_would_pass"),
        _bucket("shadow_relaxed_secondary_value", 3),
        _bucket("shadow_relaxed_secondary_threshold", 3),
        _text("conditional_relax_gate"),
        _text("conditional_relax_mode"),
        _text("conditional_relax_arm"),
        _text("conditional_relax_would_pass"),
        _text("conditional_relax_applied"),
    )


def record_candidate_decision(engine: "SignalEngine", payload: Dict[str, object]) -> None:
    ensure_candidate_rollup_state(engine)
    row = dict(payload)
    row.setdefault("schema_version", 1)
    row.setdefault("session_date", engine.date_str)
    row.setdefault("mode", engine._candidate_mode)
    row.setdefault("side", "over")
    row.setdefault("recorded_at", _now_iso())

    attach_candidate_shadow_fields(engine, row)
    should_write_calibration = is_calibration_opportunity(row)
    if should_write_calibration:
        row.setdefault("signal_ts_epoch", time.time())
        attach_modeling_observability_fields(engine, row)

    early_raw_sample_row = is_raw_early_gate_candidate(row, calibration=should_write_calibration)
    if early_raw_sample_row:
        if not should_write_raw_early_gate_row(engine, row):
            engine._candidate_raw_sample_suppressed += 1
            observe_candidate_rollup(engine, row, write_status="raw_sample_suppressed")
            return
    else:
        dedup_key = build_candidate_skip_dedup_key(row)
        if dedup_key is not None:
            if dedup_key in engine._candidate_skip_dedup_seen:
                engine._candidate_rows_dedup_suppressed += 1
                observe_candidate_rollup(engine, row, write_status="dedup_suppressed")
                return
            engine._candidate_skip_dedup_seen.add(dedup_key)

    try:
        raw_row = compact_raw_candidate_row(row, calibration=should_write_calibration)
        compact_row = drop_none_values(raw_row)
        omitted_nulls = count_none_values(raw_row)
        with open(engine._candidate_log_path(), "a", encoding="utf-8") as f:
            f.write(jsonl_dumps(compact_row) + "\n")
        engine._candidate_null_fields_omitted += omitted_nulls
        engine._candidate_compacted_fields_omitted += max(0, len(row) - len(raw_row))
        engine._candidate_rows_written += 1
        observe_candidate_rollup(engine, row, write_status="written")
        if should_write_calibration:
            write_calibration_opportunity(engine, row)
        register_score_confirmation_candidate(engine, row)
    except Exception as exc:
        engine._candidate_rows_write_errors += 1
        observe_candidate_rollup(engine, row, write_status="write_error")
        LOGGER.warning("Failed to write candidate decision row: %s", exc)


def log_skip_debug_once(
    engine: "SignalEngine",
    *,
    reason: str,
    game: "ScheduledGame",
    market: "OUMarket",
    inning: int,
    inning_state: str,
    outs: int,
    current_total: int,
    message: str,
    args: Tuple[object, ...],
) -> None:
    dedup_key = (
        reason,
        str(game.game_pk),
        str(market.line),
        str(inning),
        str(inning_state),
        str(outs),
        str(current_total),
    )
    if dedup_key in engine._skip_debug_seen:
        engine._skip_debug_logs_dedup_suppressed += 1
        return
    engine._skip_debug_seen.add(dedup_key)
    engine._skip_debug_logs_emitted += 1
    LOGGER.debug(message, *args)
