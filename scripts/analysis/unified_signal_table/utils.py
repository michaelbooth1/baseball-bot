"""Small shared helpers for unified signal table builders."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(v)
    except Exception:
        return None


def _safe_bool(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y"}:
        return True
    if s in {"0", "false", "no", "n"}:
        return False
    return None


def _parse_iso_to_epoch(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _date_in_range(date_str: str, min_date: Optional[str], max_date: Optional[str]) -> bool:
    if not date_str:
        return True
    if min_date and date_str < min_date:
        return False
    if max_date and date_str > max_date:
        return False
    return True


def _coalesce(values: Iterable[Any]) -> Any:
    for v in values:
        if v is not None and v != "":
            return v
    return None


def _best_event_time(event: Dict[str, Any]) -> Optional[str]:
    # Prefer lifecycle-specific stamps before generic timestamps.
    return _coalesce(
        [
            event.get("filled_at"),
            event.get("cancelled_at"),
            event.get("settled_at"),
            event.get("order_placed_at"),
            event.get("placed_at"),
            event.get("ts"),
        ]
    )


def _infer_session_date(bet_id: str, record: Dict[str, Any]) -> str:
    if "session_date" in record and record["session_date"]:
        return str(record["session_date"])
    placed_at = record.get("placed_at")
    if isinstance(placed_at, str) and len(placed_at) >= 10:
        return placed_at[:10]
    ts = record.get("ts")
    if isinstance(ts, str) and len(ts) >= 10:
        return ts[:10]
    # bet_id starts with YYYY-MM-DD in both paper/live implementations
    if len(bet_id) >= 10 and bet_id[4] == "-" and bet_id[7] == "-":
        return bet_id[:10]
    return ""


def _read_json(path: Path) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            out.append(json.loads(raw))
    return out

