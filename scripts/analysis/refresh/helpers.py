"""Generic date + path helpers used across the refresh package."""
from __future__ import annotations

import calendar
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from . import config as _config
from .config import STAGE1_HISTORY_FULL_SEASONS


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_date(date_str: str) -> bool:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except Exception:
        return False


def _stage1_expected_season_window(active_date: str) -> Tuple[int, int]:
    active_year = int(str(active_date)[:4])
    return active_year - STAGE1_HISTORY_FULL_SEASONS, active_year - 1


def _script(path: str) -> str:
    return str(_config.PROJECT_DIR / path)


def _python() -> str:
    return sys.executable or "python"


def _parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d")


def _shift_days(date_str: str, days: int) -> str:
    return (_parse_date(date_str) + timedelta(days=days)).strftime("%Y-%m-%d")


def _first_of_month(date_str: str) -> str:
    d = _parse_date(date_str)
    return d.replace(day=1).strftime("%Y-%m-%d")


def _last_of_month(date_str: str) -> str:
    d = _parse_date(date_str)
    last_day = calendar.monthrange(d.year, d.month)[1]
    return d.replace(day=last_day).strftime("%Y-%m-%d")


def _first_of_prior_month(date_str: str) -> str:
    """First day of the calendar month before `date_str`. Wraps to
    December of the prior year when called in January. Used by the
    schedule refresh to cover the active month PLUS the prior month
    so late-added games near month boundaries don't fall through the
    cracks (see Active #12 root-cause analysis 2026-05-17)."""
    d = _parse_date(date_str)
    if d.month == 1:
        prior_year, prior_month = d.year - 1, 12
    else:
        prior_year, prior_month = d.year, d.month - 1
    return date(prior_year, prior_month, 1).strftime("%Y-%m-%d")


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None
