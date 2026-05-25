"""Alert-attribution helpers used by calibration_health (and any
other health block that wants to annotate alerts with recent
promotion/demotion or concept-drift context).

Extracted from calibration_health.py on 2026-05-25 as part of the
human_review subpackage refactor. These functions are largely
standalone (only depend on .constants + .helpers + the
promotion_events.jsonl log on disk) so they move cleanly.

Public surface (also re-exported by calibration_health for back-compat):
  - _recent_events_by_direction
  - _recent_promotions
  - _recent_demotions
  - _attribute_alert_to_promotions
  - _attribute_alert_to_demotions
  - _major_drift_features
  - _attribute_alert_to_concept_drift
  - CONCEPT_DRIFT_ATTRIBUTION_TOP_N
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .constants import PROMOTION_ATTRIBUTION_WINDOW_DAYS
from .helpers import _shift_date


def _recent_events_by_direction(
    *,
    today: str,
    log_path: Path,
    direction: str,
    success_actions: Tuple[str, ...],
    window_days: int = PROMOTION_ATTRIBUTION_WINDOW_DAYS,
) -> List[Dict[str, Any]]:
    if not log_path.exists():
        return []
    try:
        rows = []
        with open(log_path, "r", encoding="utf-8") as f:
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
    cutoff = _shift_date(today, -window_days)
    out: List[Dict[str, Any]] = []
    for r in rows:
        row_direction = str(r.get("direction") or "promote")
        if row_direction != direction:
            continue
        if str(r.get("action") or "") not in success_actions:
            continue
        ts = str(r.get("generated_at_utc") or "")
        if not ts:
            continue
        ts_date = ts[:10]
        if ts_date < cutoff or ts_date > today:
            continue
        out.append(r)
    return out


def _recent_promotions(
    *,
    today: str,
    log_path: Path,
    window_days: int = PROMOTION_ATTRIBUTION_WINDOW_DAYS,
) -> List[Dict[str, Any]]:
    return _recent_events_by_direction(
        today=today, log_path=log_path,
        direction="promote", success_actions=("promoted", "forced"),
        window_days=window_days,
    )


def _recent_demotions(
    *,
    today: str,
    log_path: Path,
    window_days: int = PROMOTION_ATTRIBUTION_WINDOW_DAYS,
) -> List[Dict[str, Any]]:
    return _recent_events_by_direction(
        today=today, log_path=log_path,
        direction="demote", success_actions=("demoted", "forced"),
        window_days=window_days,
    )


def _attribute_alert_to_promotions(
    alert: str,
    promotions: List[Dict[str, Any]],
    *,
    today: str,
) -> str:
    if not promotions:
        return alert
    by_lever: Dict[str, str] = {}
    for p in promotions:
        lever = str(p.get("lever") or "?")
        ts = str(p.get("generated_at_utc") or "")
        if lever not in by_lever or ts > by_lever[lever]:
            by_lever[lever] = ts
    parts: List[str] = []
    for lever, ts in sorted(by_lever.items(), key=lambda kv: kv[1], reverse=True):
        ts_date = ts[:10]
        try:
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            ts_dt = datetime.strptime(ts_date, "%Y-%m-%d")
            days_ago = (today_dt - ts_dt).days
            parts.append(f"{lever} promotion {days_ago}d ago")
        except ValueError:
            parts.append(f"{lever} promotion at {ts_date}")
    return alert + f"  [coincides with: {', '.join(parts)}]"


def _attribute_alert_to_demotions(
    alert: str,
    demotions: List[Dict[str, Any]],
    *,
    today: str,
) -> str:
    if not demotions:
        return alert
    by_lever: Dict[str, str] = {}
    for d in demotions:
        lever = str(d.get("lever") or "?")
        ts = str(d.get("generated_at_utc") or "")
        if lever not in by_lever or ts > by_lever[lever]:
            by_lever[lever] = ts
    parts: List[str] = []
    for lever, ts in sorted(by_lever.items(), key=lambda kv: kv[1], reverse=True):
        ts_date = ts[:10]
        try:
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            ts_dt = datetime.strptime(ts_date, "%Y-%m-%d")
            days_ago = (today_dt - ts_dt).days
            parts.append(f"{lever} demotion {days_ago}d ago")
        except ValueError:
            parts.append(f"{lever} demotion at {ts_date}")
    return alert + f"  [follows: {', '.join(parts)}]"


CONCEPT_DRIFT_ATTRIBUTION_TOP_N = 5


def _major_drift_features(
    concept_drift_health: Optional[Dict[str, Any]],
) -> List[Tuple[str, str, float]]:
    if not concept_drift_health:
        return []
    feature_verdicts = concept_drift_health.get("feature_verdicts") or {}
    out: List[Tuple[str, str, float]] = []
    for fname, info in feature_verdicts.items():
        if str(info.get("verdict") or "") != "major":
            continue
        metric = str(info.get("metric") or "PSI")
        value = info.get("value")
        if value is None:
            continue
        try:
            out.append((str(fname), metric, float(value)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda t: t[2], reverse=True)
    return out


def _attribute_alert_to_concept_drift(
    alert: str,
    drift_features: List[Tuple[str, str, float]],
    *,
    top_n: int = CONCEPT_DRIFT_ATTRIBUTION_TOP_N,
) -> str:
    if not drift_features:
        return alert
    top = drift_features[:top_n]
    parts = [f"{f} {m} {v:.2f}" for (f, m, v) in top]
    extra = len(drift_features) - len(top)
    suffix = ", ".join(parts)
    if extra > 0:
        suffix += f", (+{extra} more)"
    return alert + f"  [concept-drift: {suffix}]"
