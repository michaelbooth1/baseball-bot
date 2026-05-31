"""Stage-2 staging-vs-production Brier comparison + promotion stability gate.

We auto-rebuild Stage-2 every refresh but write to a STAGING path so the
live cache is never silently swapped. This module's helpers compare the
two payloads and surface promotion-readiness alerts only when staging
beats production consistently across a trailing window.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .config import (
    LOGGER,
    STAGE2_PROMOTION_MIN_CONSECUTIVE,
    STAGE2_PROMOTION_MIN_DELTA,
    STAGE2_PROMOTION_MIN_HISTORY,
    STAGE2_PROMOTION_WINDOW,
)


def _artifact_age_days(path: Path) -> Optional[float]:
    try:
        if not path.exists():
            return None
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        return round((datetime.now() - mtime).total_seconds() / 86400.0, 2)
    except OSError:
        return None


def _stage2_validation_brier(payload: object) -> Optional[float]:
    """Pull the validation Brier from a Stage-2 model payload, if present.

    Canonical schema (build_mlb_stage2_run_env.py):
        payload["validation_metrics"][<line>]["stage2_brier"]
    Older / hand-built payloads may use a flat "validation_brier" key or
    a per-line "lines"/"by_line" dict; those legacy shapes are kept as
    fallbacks so a hand-rolled fixture still works.
    """
    if not isinstance(payload, dict):
        return None
    # Canonical: average stage2_brier across the validation_metrics dict.
    vm = payload.get("validation_metrics")
    if isinstance(vm, dict):
        scores: List[float] = []
        for entry in vm.values():
            if not isinstance(entry, dict):
                continue
            for key in ("stage2_brier", "validation_brier", "val_brier", "brier"):
                v = entry.get(key)
                if isinstance(v, (int, float)):
                    scores.append(float(v))
                    break
        if scores:
            return sum(scores) / len(scores)
    # Legacy flat key.
    for key in ("validation_brier", "val_brier", "brier"):
        val = payload.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    summary = payload.get("summary") or {}
    if isinstance(summary, dict):
        for key in ("validation_brier", "val_brier", "brier"):
            val = summary.get(key)
            if isinstance(val, (int, float)):
                return float(val)
    # Legacy per-line shape.
    lines = payload.get("lines") or payload.get("by_line") or {}
    if isinstance(lines, dict):
        scores = []
        for entry in lines.values():
            if not isinstance(entry, dict):
                continue
            for key in ("validation_brier", "val_brier", "brier"):
                v = entry.get(key)
                if isinstance(v, (int, float)):
                    scores.append(float(v))
                    break
        if scores:
            return sum(scores) / len(scores)
    return None


def _load_stage2_brier_history(path: Path) -> List[Dict[str, object]]:
    """Read prior staging-vs-prod Brier observations. Missing/malformed lines
    are skipped silently -- this is a research-output history, not a
    contract; corruption should not break the refresh.
    """
    rows: List[Dict[str, object]] = []
    if not path.exists():
        return rows
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rows.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def _stage2_history_row_date(row: Dict[str, object]) -> str:
    d = row.get("data_max_date")
    if d:
        return str(d)[:10]
    g = row.get("generated_at_utc") or ""
    return str(g)[:10] if g else ""


def _trailing_stage2_history(
    history_rows: List[Dict[str, object]],
    *,
    window: int,
    exclude_date: Optional[str] = None,
) -> List[Dict[str, object]]:
    """Return the last `window` distinct-date Stage-2 observations, oldest
    first. Same-date rows dedupe to the latest entry."""
    by_date: Dict[str, Dict[str, object]] = {}
    for row in history_rows:
        d = _stage2_history_row_date(row)
        if not d:
            continue
        if exclude_date and d == exclude_date:
            continue
        by_date[d] = row
    if not by_date:
        return []
    ordered = sorted(by_date.items(), key=lambda kv: kv[0])
    return [v for _, v in ordered[-window:]]


def _stage2_promotion_verdict(
    history_rows: List[Dict[str, object]],
    *,
    window: int = STAGE2_PROMOTION_WINDOW,
    min_history: int = STAGE2_PROMOTION_MIN_HISTORY,
    min_consecutive: int = STAGE2_PROMOTION_MIN_CONSECUTIVE,
    min_delta: float = STAGE2_PROMOTION_MIN_DELTA,
    exclude_date: Optional[str] = None,
) -> Dict[str, object]:
    """Decide whether staging has consistently beaten production.

    Returns a dict with `verdict` in {"insufficient_history", "hold",
    "promote"} plus diagnostic counts so the alert line can explain itself.
    """
    trailing = _trailing_stage2_history(
        history_rows, window=window, exclude_date=exclude_date
    )
    n_history = len(trailing)
    if n_history < min_history:
        return {
            "verdict": "insufficient_history",
            "n_history": n_history,
            "n_history_required": min_history,
            "n_improving": 0,
            "n_consecutive_required": min_consecutive,
            "min_delta": min_delta,
        }
    n_improving = 0
    for row in trailing:
        delta = row.get("delta")
        if isinstance(delta, (int, float)) and float(delta) <= -min_delta:
            n_improving += 1
    if n_improving >= min_consecutive:
        verdict = "promote"
    else:
        verdict = "hold"
    return {
        "verdict": verdict,
        "n_history": n_history,
        "n_history_required": min_history,
        "n_improving": n_improving,
        "n_consecutive_required": min_consecutive,
        "min_delta": min_delta,
    }


def _write_stage2_brier_history_row(
    path: Path,
    *,
    production_brier: Optional[float],
    staging_brier: Optional[float],
    delta: Optional[float],
    data_max_date: Optional[str],
    generated_at_utc: str,
) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "generated_at_utc": generated_at_utc,
            "data_max_date": data_max_date,
            "production_brier": production_brier,
            "staging_brier": staging_brier,
            "delta": delta,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as exc:
        LOGGER.warning(
            "Failed to append stage2 brier history row to %s: %s. "
            "Promotion stability gate has nothing to read on the next refresh.",
            path, exc,
        )
