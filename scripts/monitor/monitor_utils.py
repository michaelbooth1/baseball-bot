"""Small utility helpers shared across the monitor package."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional


def _safe_float(v: object) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _safe_int(v: object) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except Exception:
        return None


def _normalize_slug_piece(txt: str) -> str:
    return re.sub(r"[^a-z0-9-]", "", txt.lower())


def _game_dir_name(away: str, home: str, game_pk: int) -> str:
    return f"{away}_at_{home}_{game_pk}"


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"
