"""Leaf bucket helpers used by calibration_health (and re-exported
through it). Extracted from calibration_health.py on 2026-05-25 as
part of the human_review subpackage refactor — these are pure
functions with no dependencies on the rest of the module, so they
move cleanly.

Public surface (also re-exported by calibration_health for back-compat):
  - _cohort_edge_bucket
  - _cohort_inning_bucket
  - _cohort_line_bucket
  - COHORT_DIMENSIONS
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

from .helpers import _drift_ask_bucket, _drift_current_state_edge_bucket


def _cohort_edge_bucket(value: Any) -> str:
    try:
        e = float(value)
    except (TypeError, ValueError):
        return "missing"
    if e < 0.15:
        return "<0.15"
    if e < 0.18:
        return "0.15-0.18"
    if e < 0.22:
        return "0.18-0.22"
    return ">=0.22"


def _cohort_inning_bucket(value: Any) -> str:
    try:
        i = int(value)
    except (TypeError, ValueError):
        return "missing"
    if i <= 5:
        return "<=5"
    if i == 6:
        return "6"
    if i == 7:
        return "7"
    return ">=8"


def _cohort_line_bucket(value: Any) -> str:
    try:
        ln = float(value)
    except (TypeError, ValueError):
        return "missing"
    if ln <= 7.5:
        return "<=7.5"
    if ln <= 8.5:
        return "8.5"
    if ln <= 9.5:
        return "9.5"
    return ">=10.5"


COHORT_DIMENSIONS: Tuple[Tuple[str, Callable[[Dict[str, Any]], str]], ...] = (
    ("edge_bucket", lambda b: _cohort_edge_bucket(b.get("edge"))),
    ("ask_bucket", lambda b: _drift_ask_bucket(b.get("entry_ask"))),
    ("inning_bucket", lambda b: _cohort_inning_bucket(b.get("inning"))),
    ("line_bucket", lambda b: _cohort_line_bucket(b.get("line"))),
    (
        "current_state_edge_bucket",
        lambda b: _drift_current_state_edge_bucket(b.get("current_state_value_edge")),
    ),
)
