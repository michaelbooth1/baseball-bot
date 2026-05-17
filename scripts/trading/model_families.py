"""
model_families.py -- Canonical strategy/model-family labels.

Keep this tiny and dependency-free. Several analysis scripts need to agree on
whether a row belongs to the score-event transition family or the no-score
drift family, and string drift here would poison calibration comparisons.
"""

from __future__ import annotations

from typing import Any, Dict


SCORE_EVENT_TRANSITION = "score_event_transition"
NO_SCORE_DRIFT = "no_score_drift"
UNKNOWN_MODEL_FAMILY = "unknown"

KNOWN_MODEL_FAMILIES = (
    SCORE_EVENT_TRANSITION,
    NO_SCORE_DRIFT,
)


def infer_signal_model_family(row: Dict[str, Any]) -> str:
    """Infer the canonical model family for a candidate/signal row."""
    explicit = str(row.get("signal_model_family") or "").strip()
    if explicit:
        return explicit

    strategy = str(row.get("state_value_strategy") or "").strip()
    if strategy == NO_SCORE_DRIFT:
        return NO_SCORE_DRIFT
    if strategy == SCORE_EVENT_TRANSITION:
        return SCORE_EVENT_TRANSITION

    decision = str(row.get("decision") or "").strip()
    reason = str(row.get("decision_reason") or "").strip()
    if decision == "shadow_no_score_drift" or reason == "state_value_no_score_drift":
        return NO_SCORE_DRIFT

    # Trade and skip rows emitted by signal_pipeline are from the score-event
    # candidate path unless explicitly overwritten by the no-score writer.
    if decision in {"trade", "skip", "skip_with_features"}:
        return SCORE_EVENT_TRANSITION

    # Historical unified signal rows may not carry candidate decision fields,
    # but placed/session/ledger rows in signals_master are score-event trades.
    if (
        row.get("bet_id")
        or row.get("source_has_session_bet")
        or row.get("source_has_ledger_events")
        or row.get("order_status_final")
    ):
        return SCORE_EVENT_TRANSITION

    return UNKNOWN_MODEL_FAMILY
