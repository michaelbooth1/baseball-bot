"""Calibration method-stability gate (shipped 2026-05-14): stops
the platt<->isotonic flip-flop by overriding today's pick to the
trailing-7d modal selection when the two differ. Owns the
selection_history.jsonl file.

Extracted from calibrate_signal_probabilities.py on 2026-05-25.

Public surface (also re-exported by calibrate_signal_probabilities for
back-compat): _load_selection_history, _history_row_date,
_trailing_family_history, _modal_selection, _apply_stability_gate,
_write_selection_history_row.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence



import logging

DEFAULT_STABILITY_WINDOW = 7
DEFAULT_STABILITY_MIN_HISTORY = 5
LOGGER = logging.getLogger("calibrate_signal_probabilities")

def _load_selection_history(path: Path) -> List[Dict[str, Any]]:
    """Read the selection-history JSONL. Returns rows in file order."""
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _history_row_date(row: Dict[str, Any]) -> str:
    """Pick a stable date key for the history row.

    Prefer the explicit `data_max_date` (data-window upper bound), since
    that's the operationally meaningful day this calibration represents.
    Fall back to the YYYY-MM-DD prefix of `generated_at_utc`.
    """
    d = row.get("data_max_date")
    if d:
        return str(d)[:10]
    g = row.get("generated_at_utc") or ""
    return str(g)[:10] if g else ""


def _trailing_family_history(
    history_rows: List[Dict[str, Any]],
    family: str,
    *,
    window: int,
    exclude_date: Optional[str] = None,
) -> List[str]:
    """Return the last `window` distinct-date pre-override selections for
    `family`, oldest first. Same-date rows are deduped to the latest entry.

    `exclude_date` skips an effective date (typically today's max_date) so
    a re-run on the same day doesn't compare against its own earlier write.
    """
    by_date: "Dict[str, str]" = {}
    for row in history_rows:
        d = _history_row_date(row)
        if not d:
            continue
        if exclude_date and d == exclude_date:
            continue
        sel = (row.get("selections") or {}).get(family) or {}
        pre = sel.get("pre_override_selected")
        if pre:
            by_date[d] = str(pre)
    if not by_date:
        return []
    ordered = sorted(by_date.items(), key=lambda kv: kv[0])
    tail = ordered[-window:]
    return [v for _, v in tail]


def _modal_selection(history_picks: Sequence[str]) -> Optional[str]:
    """Most-frequent selection. Returns None on tie or empty input.

    On tie, we deliberately return None so the gate falls back to today's
    pick rather than locking in an arbitrary tie-breaker.
    """
    if not history_picks:
        return None
    counts: Dict[str, int] = {}
    for v in history_picks:
        counts[v] = counts.get(v, 0) + 1
    sorted_counts = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    top_name, top_count = sorted_counts[0]
    if len(sorted_counts) > 1 and sorted_counts[1][1] == top_count:
        return None
    return top_name


def _apply_stability_gate(
    pre_override_selected: str,
    history_rows: List[Dict[str, Any]],
    family: str,
    *,
    window: int = DEFAULT_STABILITY_WINDOW,
    min_history: int = DEFAULT_STABILITY_MIN_HISTORY,
    exclude_date: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Override today's calibration method if it differs from the trailing
    modal selection. Returns (final_method, audit_dict).

    The gate only fires when:
      - We have at least `min_history` distinct prior dates for this family.
      - The modal of the last `window` dates is unambiguous (no tie).
      - Today's pre-override selection differs from the modal.

    Otherwise today's pre-override pick passes through unchanged. This
    keeps validation-noise-induced flip-flops from churning the runtime
    calibrator while still allowing genuine drift through after several
    consistent days.
    """
    audit: Dict[str, Any] = {
        "stability_gate_enabled": True,
        "stability_window": window,
        "stability_min_history": min_history,
        "stability_history_count": 0,
        "stability_history": [],
        "stability_modal": None,
        "stability_gate_applied": False,
    }
    family_history = _trailing_family_history(
        history_rows, family, window=window, exclude_date=exclude_date
    )
    audit["stability_history_count"] = len(family_history)
    audit["stability_history"] = list(family_history)
    if len(family_history) < min_history:
        return pre_override_selected, audit
    modal = _modal_selection(family_history)
    audit["stability_modal"] = modal
    if modal is None or modal == pre_override_selected:
        return pre_override_selected, audit
    audit["stability_gate_applied"] = True
    audit["stability_override_from"] = pre_override_selected
    return modal, audit


def _write_selection_history_row(
    path: Path,
    *,
    selections: Dict[str, Dict[str, Any]],
    data_max_date: Optional[str],
    generated_at_utc: str,
) -> None:
    """Append one row to selection_history.jsonl. Creates the directory
    + file on first use. Atomic-append best-effort: if the write fails we
    log a warning but don't fail the calibration run."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "generated_at_utc": generated_at_utc,
            "data_max_date": data_max_date,
            "selections": selections,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as exc:
        LOGGER.warning(
            "Failed to append selection-history row to %s: %s. "
            "Calibration succeeded but the stability gate has nothing to "
            "read on the next refresh.",
            path, exc,
        )


