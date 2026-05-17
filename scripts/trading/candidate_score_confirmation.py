#!/usr/bin/env python3
"""
candidate_score_confirmation.py -- Official-score labels for score-event rows.

Post-FV score-event opportunities are registered in memory, then resolved into
a sidecar JSONL when the official score changes within 10/30/60 seconds or the
60-second window expires. These labels are diagnostic-only and never change
trade decisions.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple, TYPE_CHECKING

from candidate_paths import (
    drop_none_values,
    jsonl_dumps,
    path_from_engine,
    score_confirmation_log_path,
)
from candidate_rollups import ensure_candidate_rollup_state
from candidate_schema_enrichment import to_float

if TYPE_CHECKING:
    from signal_engine import SignalEngine

LOGGER = logging.getLogger("signal_engine")

SCORE_CONFIRMATION_WINDOWS_SECS = (10.0, 30.0, 60.0)
SCORE_CONFIRMATION_MAX_WINDOW_SECS = max(SCORE_CONFIRMATION_WINDOWS_SECS)


def _is_score_confirmation_candidate(row: Dict[str, object]) -> bool:
    if str(row.get("state_value_strategy") or "") == "no_score_drift":
        return False
    if row.get("candidate_id") is None or row.get("fair_value") is None:
        return False
    decision = str(row.get("decision") or "").lower()
    return decision in {"trade", "skip", "skip_with_features"}


def register_score_confirmation_candidate(engine: "SignalEngine", row: Dict[str, object]) -> None:
    """Track post-FV score-event opportunities until official score confirms."""
    if not _is_score_confirmation_candidate(row):
        return
    ensure_candidate_rollup_state(engine)
    candidate_id = str(row.get("candidate_id"))
    if candidate_id in engine._score_confirmation_pending:
        return
    now = time.time()
    signal_ts = to_float(row.get("signal_ts_epoch")) or now
    engine._score_confirmation_pending[candidate_id] = {
        "schema_version": 1,
        "session_date": row.get("session_date") or getattr(engine, "date_str", ""),
        "mode": row.get("mode") or getattr(engine, "_candidate_mode", ""),
        "candidate_id": candidate_id,
        "outcome_join_key": row.get("outcome_join_key"),
        "game_pk": row.get("game_pk"),
        "away_abbrev": row.get("away_abbrev"),
        "home_abbrev": row.get("home_abbrev"),
        "line": row.get("line"),
        "decision": row.get("decision"),
        "decision_reason": row.get("decision_reason"),
        "signal_model_family": row.get("signal_model_family"),
        "state_value_strategy": row.get("state_value_strategy"),
        "signal_ts_epoch": signal_ts,
        "away_score_before": row.get("away_score_before"),
        "home_score_before": row.get("home_score_before"),
        "current_total": row.get("current_total"),
        "inferred_runs": row.get("inferred_runs"),
        "decision_ask": row.get("decision_ask"),
        "fair_value": row.get("fair_value"),
        "edge": row.get("edge"),
        "shadow_p_score_event_proxy": row.get("shadow_p_score_event_proxy"),
        "shadow_phantom_risk_score": row.get("shadow_phantom_risk_score"),
        "shadow_phantom_risk_band": row.get("shadow_phantom_risk_band"),
    }


def _write_score_confirmation_record(
    engine: "SignalEngine",
    record: Dict[str, object],
) -> None:
    try:
        path = path_from_engine(engine, "_score_confirmation_log_path", score_confirmation_log_path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(jsonl_dumps(drop_none_values(record)) + "\n")
        engine._score_confirmation_rows_written += 1
    except Exception as exc:
        engine._score_confirmation_write_errors += 1
        LOGGER.warning("Failed to write score-confirmation row: %s", exc)


def _finish_score_confirmation(
    engine: "SignalEngine",
    candidate_id: str,
    *,
    status: str,
    observed_away: Optional[int] = None,
    observed_home: Optional[int] = None,
    observed_ts: Optional[float] = None,
) -> None:
    pending = engine._score_confirmation_pending.pop(candidate_id, None)
    if not pending:
        return
    now = float(observed_ts if observed_ts is not None else time.time())
    signal_ts = to_float(pending.get("signal_ts_epoch")) or now
    latency = max(0.0, now - signal_ts)
    start_away = int(to_float(pending.get("away_score_before")) or 0)
    start_home = int(to_float(pending.get("home_score_before")) or 0)
    start_total = int(to_float(pending.get("current_total")) or (start_away + start_home))
    observed_total = (
        int(observed_away) + int(observed_home)
        if observed_away is not None and observed_home is not None
        else None
    )
    record = dict(pending)
    record.update(
        {
            "resolved_at_epoch": now,
            "confirmation_status": status,
            "score_confirmation_latency_secs": round(latency, 3),
            "observed_away_score": observed_away,
            "observed_home_score": observed_home,
            "observed_total": observed_total,
            "score_delta_away": (
                int(observed_away) - start_away if observed_away is not None else None
            ),
            "score_delta_home": (
                int(observed_home) - start_home if observed_home is not None else None
            ),
            "score_delta_total": (
                observed_total - start_total if observed_total is not None else None
            ),
        }
    )
    for window in SCORE_CONFIRMATION_WINDOWS_SECS:
        key = f"score_confirmed_within_{int(window)}s"
        record[key] = bool(status == "score_changed" and latency <= window)
    _write_score_confirmation_record(engine, record)


def observe_score_confirmation_ticks(engine: "SignalEngine", tick_batch: list) -> None:
    """Resolve pending score-event labels from the latest official tick scores."""
    ensure_candidate_rollup_state(engine)
    if not engine._score_confirmation_pending:
        return
    observed: Dict[object, Tuple[int, int]] = {}
    for game, _market, _side, payload in tick_batch:
        try:
            away = payload.get("away_score")
            home = payload.get("home_score")
        except AttributeError:
            continue
        if away is None or home is None:
            continue
        observed[getattr(game, "game_pk", None)] = (int(away), int(home))

    now = time.time()
    for candidate_id, pending in list(engine._score_confirmation_pending.items()):
        game_pk = pending.get("game_pk")
        score = observed.get(game_pk)
        start_total = int(to_float(pending.get("current_total")) or 0)
        if score is not None:
            away, home = score
            if (away + home) > start_total:
                _finish_score_confirmation(
                    engine,
                    candidate_id,
                    status="score_changed",
                    observed_away=away,
                    observed_home=home,
                    observed_ts=now,
                )
                continue
        signal_ts = to_float(pending.get("signal_ts_epoch")) or now
        if (now - signal_ts) >= SCORE_CONFIRMATION_MAX_WINDOW_SECS:
            away, home = score if score is not None else (None, None)
            _finish_score_confirmation(
                engine,
                candidate_id,
                status="no_score_change_within_60s",
                observed_away=away,
                observed_home=home,
                observed_ts=now,
            )


def flush_expired_score_confirmations(engine: "SignalEngine") -> None:
    """Emit no-score-change labels whose 60s window elapsed between ticks."""
    ensure_candidate_rollup_state(engine)
    if not engine._score_confirmation_pending:
        return
    now = time.time()
    for candidate_id, pending in list(engine._score_confirmation_pending.items()):
        signal_ts = to_float(pending.get("signal_ts_epoch")) or now
        if (now - signal_ts) >= SCORE_CONFIRMATION_MAX_WINDOW_SECS:
            _finish_score_confirmation(
                engine,
                candidate_id,
                status="no_score_change_within_60s",
                observed_ts=now,
            )
