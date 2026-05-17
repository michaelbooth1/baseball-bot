"""
signal_interaction_features.py -- Family E (signal interaction) feature computation.

Pure arithmetic / re-packaging over decision-time signal state. Per the
design in model_improvements/fill_model_feature.txt:

    E1. fv_minus_ask        = fv - ask   (same as existing 'edge')
    E2. fv_minus_ltp        = fv - ltp   (the edge-vs-adverse-selection
                                          separator; large positive in late
                                          innings is a phantom-run signature)
    E3. inning, runs_needed, runners_on  (passthrough + per-base decode)

Several of these duplicate top-level candidate-row fields. The duplication
is intentional: it lets training and EV-policy code consume the family as
one self-contained dict rather than scraping the row at multiple levels.

Runners encoding follows models.BetRecord.runners_on:
    bit 0 (value 1) = runner on 1st
    bit 1 (value 2) = runner on 2nd
    bit 2 (value 4) = runner on 3rd
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def compute_family_e_features(
    fv: Optional[float],
    ask: Optional[float],
    ltp: Optional[float],
    inning: Optional[int],
    runs_needed: Optional[float],
    runners_on: Optional[int],
) -> Dict[str, Any]:
    """Compute Family E features from signal-time model + game-state inputs.

    Returns dict with E1/E2 plus the inning/runs_needed/runners packaging.
    Per-feature None when a required input is missing.
    """
    f = _safe_float(fv)
    a = _safe_float(ask)
    p = _safe_float(ltp)
    inn = _safe_int(inning)
    rn = _safe_float(runs_needed)
    ro = _safe_int(runners_on)

    out: Dict[str, Any] = {
        "fv_minus_ask": None,
        "fv_minus_ltp": None,
        "inning": inn,
        "runs_needed": (round(rn, 4) if rn is not None else None),
        "runners_on": ro,
        "runner_on_first": None,
        "runner_on_second": None,
        "runner_on_third": None,
        "runners_count": None,
    }

    if f is not None and a is not None:
        out["fv_minus_ask"] = round(f - a, 6)

    if f is not None and p is not None:
        out["fv_minus_ltp"] = round(f - p, 6)

    if ro is not None:
        # Defensive: clamp to valid bitmask range so junk input doesn't
        # blow up the boolean decode.
        if 0 <= ro <= 7:
            out["runner_on_first"] = bool(ro & 1)
            out["runner_on_second"] = bool(ro & 2)
            out["runner_on_third"] = bool(ro & 4)
            out["runners_count"] = (
                int(bool(ro & 1)) + int(bool(ro & 2)) + int(bool(ro & 4))
            )

    return out
