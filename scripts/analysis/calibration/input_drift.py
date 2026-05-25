"""Calibration input-drift status loader (used by daily review's
calibration_health to annotate alerts with current concept-drift).

Extracted from calibrate_signal_probabilities.py on 2026-05-25.

Public surface (also re-exported for back-compat): _load_input_drift_status.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional



INPUT_DRIFT_TRIGGER_PSI_THRESHOLD = 0.25
INPUT_DRIFT_TRIGGER_MIN_MAJOR_FEATURES = 2

def _load_input_drift_status(
    path: Path,
    *,
    min_features: int = INPUT_DRIFT_TRIGGER_MIN_MAJOR_FEATURES,
    psi_threshold: float = INPUT_DRIFT_TRIGGER_PSI_THRESHOLD,
) -> Dict[str, Any]:
    """Read the concept-drift report and decide whether the input
    distribution has drifted enough that today's calibration choice
    should be flagged. Returns a dict suitable for merging into
    `selection_audit`. Always returns a well-formed result -- missing
    or unreadable report falls back to `triggered=false` with a
    diagnostic `input_drift_status`.

    The trigger looks at CONTINUOUS features only (metric=='PSI');
    categorical TVD doesn't carry the same calibration-relevance
    signal.
    """
    base: Dict[str, Any] = {
        "input_drift_triggered": False,
        "input_drift_status": "ok",
        "input_drift_major_features": [],
        "input_drift_threshold": psi_threshold,
        "input_drift_min_features_to_trigger": min_features,
        "input_drift_report_generated_at_utc": None,
    }
    if not path.exists():
        base["input_drift_status"] = "report_missing"
        return base
    try:
        with open(path, "r", encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        base["input_drift_status"] = "report_unreadable"
        base["input_drift_error"] = f"{exc!r}"
        return base
    base["input_drift_report_generated_at_utc"] = report.get("generated_at_utc")
    features = report.get("features") or {}
    major: List[Dict[str, Any]] = []
    for fname, info in features.items():
        # The report writes metric in lowercase ("psi"/"tvd") but tests
        # and docs often use uppercase -- normalise.
        metric = str(info.get("metric") or "").upper()
        if metric != "PSI":
            continue
        if str(info.get("verdict") or "") != "major":
            continue
        value = info.get("value")
        try:
            psi = float(value)
        except (TypeError, ValueError):
            continue
        major.append({"feature": str(fname), "psi": round(psi, 4)})
    major.sort(key=lambda r: r["psi"], reverse=True)
    base["input_drift_major_features"] = major
    if len(major) >= min_features:
        base["input_drift_triggered"] = True
    return base


