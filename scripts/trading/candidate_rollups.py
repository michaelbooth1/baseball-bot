#!/usr/bin/env python3
"""
candidate_rollups.py -- Compact candidate-universe counters and rollup JSON.

Candidate rollups are complete even when high-volume early-gate rows are raw
sampled. These counters give daily reviews a cheap overview by write status,
decision/reason, strategy, and game-line/reason without reading huge raw files.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Dict, TYPE_CHECKING

from line_state import _now_iso

from candidate_paths import (
    candidate_calibration_log_path,
    candidate_rollup_path,
    score_confirmation_log_path,
    path_from_engine,
)
from shadow_diagnostic_features import compute_shadow_diagnostic_fields

if TYPE_CHECKING:
    from signal_engine import SignalEngine

LOGGER = logging.getLogger("signal_engine")


def _rollup_text(row: Dict[str, object], field: str, default: str = "missing") -> str:
    value = row.get(field)
    text = "" if value is None else str(value)
    return text if text else default


def _counter_top(counter: "Counter[str]", limit: int = 50) -> Dict[str, int]:
    return {key: int(value) for key, value in counter.most_common(max(1, limit))}


def ensure_candidate_rollup_state(engine: "SignalEngine") -> None:
    """Initialize rollup counters for normal engines and lightweight test stubs."""
    if not hasattr(engine, "_candidate_rows_written"):
        engine._candidate_rows_written = 0
    if not hasattr(engine, "_candidate_rows_dedup_suppressed"):
        engine._candidate_rows_dedup_suppressed = 0
    if not hasattr(engine, "_candidate_raw_sample_suppressed"):
        engine._candidate_raw_sample_suppressed = 0
    if not hasattr(engine, "_candidate_rows_write_errors"):
        engine._candidate_rows_write_errors = 0
    if not hasattr(engine, "_candidate_null_fields_omitted"):
        engine._candidate_null_fields_omitted = 0
    if not hasattr(engine, "_candidate_compacted_fields_omitted"):
        engine._candidate_compacted_fields_omitted = 0
    if not hasattr(engine, "_candidate_calibration_rows_written"):
        engine._candidate_calibration_rows_written = 0
    if not hasattr(engine, "_candidate_calibration_write_errors"):
        engine._candidate_calibration_write_errors = 0
    if not hasattr(engine, "_score_confirmation_pending"):
        engine._score_confirmation_pending = {}
    if not hasattr(engine, "_score_confirmation_rows_written"):
        engine._score_confirmation_rows_written = 0
    if not hasattr(engine, "_score_confirmation_write_errors"):
        engine._score_confirmation_write_errors = 0
    if not hasattr(engine, "_candidate_rollup_by_write_status"):
        engine._candidate_rollup_by_write_status = Counter()
    if not hasattr(engine, "_candidate_rollup_by_decision"):
        engine._candidate_rollup_by_decision = Counter()
    if not hasattr(engine, "_candidate_rollup_by_reason"):
        engine._candidate_rollup_by_reason = Counter()
    if not hasattr(engine, "_candidate_rollup_by_strategy"):
        engine._candidate_rollup_by_strategy = Counter()
    # 2026-05-23 (audit followup): split strategy counts by side too.
    # Caught when 5/22's score_event_transition count jumped 3.5x vs
    # 5/20 (982 -> 3422). Investigation showed ~2x was just under-
    # emission shipping that day -- every OVER candidate now also
    # writes a sibling UNDER row at the FV phase. Without a per-side
    # breakdown, future audits would have to manually scan candidate
    # JSONLs to disentangle. Key is `(strategy, side)`.
    if not hasattr(engine, "_candidate_rollup_by_strategy_side"):
        engine._candidate_rollup_by_strategy_side = Counter()
    if not hasattr(engine, "_candidate_rollup_by_game_line_reason"):
        engine._candidate_rollup_by_game_line_reason = Counter()
    if not hasattr(engine, "_candidate_rollup_by_shadow_diagnostic"):
        engine._candidate_rollup_by_shadow_diagnostic = Counter()
    if not hasattr(engine, "_candidate_raw_sample_seen"):
        engine._candidate_raw_sample_seen = Counter()


def observe_candidate_rollup(
    engine: "SignalEngine",
    row: Dict[str, object],
    *,
    write_status: str,
) -> None:
    """Update compact counters while preserving the full candidate JSONL stream."""
    ensure_candidate_rollup_state(engine)
    decision = _rollup_text(row, "decision")
    reason = _rollup_text(row, "decision_reason")
    strategy = _rollup_text(row, "state_value_strategy", default="none")
    game_key = (
        f"{_rollup_text(row, 'away_abbrev', default='')}"
        f"@{_rollup_text(row, 'home_abbrev', default='')}"
    ).strip("@")
    if not game_key:
        game_key = str(row.get("game_pk") or "missing_game")
    line = _rollup_text(row, "line")
    engine._candidate_rollup_by_write_status[write_status] += 1
    engine._candidate_rollup_by_decision[decision] += 1
    engine._candidate_rollup_by_reason[f"{decision}:{reason}"] += 1
    engine._candidate_rollup_by_strategy[strategy] += 1
    side = _rollup_text(row, "side", default="over")
    engine._candidate_rollup_by_strategy_side[f"{strategy}:{side}"] += 1
    engine._candidate_rollup_by_game_line_reason[f"{game_key}|{line}|{decision}:{reason}"] += 1

    shadow_fields = compute_shadow_diagnostic_fields(row)
    if shadow_fields.get("shadow_low_ask_high_edge") is True:
        engine._candidate_rollup_by_shadow_diagnostic["low_ask_high_edge"] += 1
    if shadow_fields.get("shadow_runs_needed_exact_3p5") is True:
        engine._candidate_rollup_by_shadow_diagnostic["runs_needed_exact_3p5"] += 1
    if shadow_fields.get("shadow_score_event_current_edge_strong_ask_lt_085") is True:
        engine._candidate_rollup_by_shadow_diagnostic["score_event_current_edge_ge_0p08_ask_lt_0p85"] += 1
    for field in (
        "shadow_current_phantom_combo_bucket",
        "shadow_inning_runs_needed_bucket",
        "shadow_bottom9_home_lead_context",
        "shadow_home_skip_bottom9_risk_bucket",
        "shadow_no_score_poisson_edge_bucket",
        "shadow_no_score_empirical_edge_bucket",
        "shadow_no_score_ask_bucket",
        "shadow_no_score_drawdown_bucket",
        "shadow_no_score_poisson_empirical_ask_drawdown_bucket",
    ):
        value = shadow_fields.get(field)
        if value and value != "missing":
            engine._candidate_rollup_by_shadow_diagnostic[f"{field}:{value}"] += 1


def candidate_rollup_snapshot(engine: "SignalEngine", *, top_n: int = 50) -> Dict[str, object]:
    ensure_candidate_rollup_state(engine)
    by_write_status = getattr(engine, "_candidate_rollup_by_write_status", Counter())
    by_decision = getattr(engine, "_candidate_rollup_by_decision", Counter())
    by_reason = getattr(engine, "_candidate_rollup_by_reason", Counter())
    by_strategy = getattr(engine, "_candidate_rollup_by_strategy", Counter())
    by_strategy_side = getattr(engine, "_candidate_rollup_by_strategy_side", Counter())
    by_game_line_reason = getattr(engine, "_candidate_rollup_by_game_line_reason", Counter())
    by_shadow_diagnostic = getattr(engine, "_candidate_rollup_by_shadow_diagnostic", Counter())
    attempted = sum(by_write_status.values())
    return {
        "schema_version": 1,
        "session_date": engine.date_str,
        "mode": getattr(engine, "_candidate_mode", "paper"),
        "generated_at": _now_iso(),
        "raw_candidates_path": str(engine._candidate_log_path()),
        "calibration_opportunities_path": str(
            path_from_engine(engine, "_candidate_calibration_log_path", candidate_calibration_log_path)
        ),
        "score_confirmations_path": str(
            path_from_engine(engine, "_score_confirmation_log_path", score_confirmation_log_path)
        ),
        "attempted_rows": int(attempted),
        "written_rows": int(getattr(engine, "_candidate_rows_written", 0)),
        "dedup_suppressed_rows": int(getattr(engine, "_candidate_rows_dedup_suppressed", 0)),
        "raw_sample_suppressed_rows": int(getattr(engine, "_candidate_raw_sample_suppressed", 0)),
        "write_error_rows": int(getattr(engine, "_candidate_rows_write_errors", 0)),
        "calibration_rows_written": int(getattr(engine, "_candidate_calibration_rows_written", 0)),
        "calibration_write_error_rows": int(getattr(engine, "_candidate_calibration_write_errors", 0)),
        "score_confirmation_rows_written": int(getattr(engine, "_score_confirmation_rows_written", 0)),
        "score_confirmation_write_error_rows": int(getattr(engine, "_score_confirmation_write_errors", 0)),
        "score_confirmation_pending_rows": int(len(getattr(engine, "_score_confirmation_pending", {}) or {})),
        "null_fields_omitted": int(getattr(engine, "_candidate_null_fields_omitted", 0)),
        "compacted_fields_omitted": int(getattr(engine, "_candidate_compacted_fields_omitted", 0)),
        "by_write_status": _counter_top(by_write_status, top_n),
        "by_decision": _counter_top(by_decision, top_n),
        "by_decision_reason": _counter_top(by_reason, top_n),
        "by_state_value_strategy": _counter_top(by_strategy, top_n),
        # 2026-05-23: per-side split. When under_emission_mode is
        # shadow, by_state_value_strategy roughly doubles because every
        # OVER candidate that reaches the FV phase writes a sibling
        # UNDER row. Compare {strategy}:over vs {strategy}:under to
        # see the asymmetry directly. Audits should derive per-side
        # rates from this counter, not from by_state_value_strategy.
        "by_state_value_strategy_side": _counter_top(by_strategy_side, top_n),
        "by_shadow_diagnostic": _counter_top(by_shadow_diagnostic, top_n),
        "top_game_line_reasons": _counter_top(by_game_line_reason, top_n),
    }


def write_candidate_rollup(engine: "SignalEngine") -> None:
    if not getattr(engine, "_candidate_rollup_by_write_status", None):
        return
    try:
        with open(engine._candidate_rollup_path(), "w", encoding="utf-8") as f:
            json.dump(candidate_rollup_snapshot(engine, top_n=100), f, indent=2)
    except Exception as exc:
        LOGGER.warning("Failed to write candidate rollup: %s", exc)
