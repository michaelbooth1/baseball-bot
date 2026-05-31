"""Manifest writing + log dir size + Phase-6 reminder helpers."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from .config import (
    PHASE6_GATE_RECALIBRATION_DUE_DATE,
    RefreshConfig,
)
from .helpers import _valid_date


def _logs_dir_bytes(log_dir: Path) -> int:
    if not log_dir.exists():
        return 0
    total = 0
    for path in log_dir.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _phase6_reminder(active_date: str) -> Optional[str]:
    if not _valid_date(active_date):
        return None
    if active_date < PHASE6_GATE_RECALIBRATION_DUE_DATE:
        return None
    return (
        f"Phase 6 gate recalibration is due (active_date {active_date} >= "
        f"{PHASE6_GATE_RECALIBRATION_DUE_DATE}). Re-tune TR19 extreme_edge_max "
        "against post-TR20 v2 Stage-3 edge distribution. See "
        "model_improvements/handover_2026_05_07.txt."
    )


def _write_manifest(config: RefreshConfig, payload: Dict[str, object]) -> Path:
    config.output_root.mkdir(parents=True, exist_ok=True)
    date_part = config.active_date or datetime.now().strftime("%Y-%m-%d")
    suffix = "startup_refresh_plan" if config.plan_only else "startup_refresh"
    path = config.output_root / f"{date_part}_{suffix}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path
