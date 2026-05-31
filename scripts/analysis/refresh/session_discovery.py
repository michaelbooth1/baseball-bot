"""Session discovery + daily-review staleness logic.

Globs session/candidate/review directories to figure out which dates
need a fresh daily_human_review built, and which paper-only dates
should get one even without a live session (added 2026-05-31).
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from . import config as _config
from .config import (
    DEFAULT_SESSIONS_DIR,
    SESSION_RE,
)
from .helpers import _mtime


def discover_session_dates(sessions_dir: Path = DEFAULT_SESSIONS_DIR) -> List[str]:
    """Return sorted YYYY-MM-DD dates with live session JSON files."""
    if not sessions_dir.exists():
        return []
    dates: List[str] = []
    for path in sessions_dir.glob("*_session.json"):
        match = SESSION_RE.match(path.name)
        if match:
            dates.append(match.group("date"))
    return sorted(set(dates))


def latest_refreshable_date(
    session_dates: Sequence[str],
    *,
    active_date: str,
    max_date: str = "",
    include_run_date: bool = False,
) -> Optional[str]:
    """Pick the newest completed session date to fold into canonical outputs."""
    if not session_dates:
        return None
    if max_date:
        candidates = [d for d in session_dates if d <= max_date]
    elif include_run_date:
        candidates = [d for d in session_dates if d <= active_date]
    else:
        candidates = [d for d in session_dates if d < active_date]
    return max(candidates) if candidates else None


def _daily_review_is_current(
    *,
    date_str: str,
    sessions_dir: Path,
    candidate_dir: Path,
    log_dir: Path,
    review_dir: Path,
) -> bool:
    outputs = [
        review_dir / f"{date_str}_human_review.json",
        review_dir / f"{date_str}_human_review.md",
    ]
    output_mtimes = [_mtime(path) for path in outputs]
    if any(value is None for value in output_mtimes):
        return False

    sources = [
        sessions_dir / f"{date_str}_session.json",
        candidate_dir / f"{date_str}_candidate_rollup.json",
        log_dir / f"{date_str}.log",
    ]
    source_mtimes = [value for value in (_mtime(path) for path in sources) if value is not None]
    if not source_mtimes:
        return False
    return min(output_mtimes) >= max(source_mtimes)


def daily_review_dates_needing_refresh(
    session_dates: Iterable[str],
    *,
    max_date: str,
    sessions_dir: Path,
    candidate_dir: Path,
    log_dir: Path,
    review_dir: Path,
) -> List[str]:
    dates = [d for d in session_dates if d <= max_date]
    out: List[str] = []
    for date_str in dates:
        if not _daily_review_is_current(
            date_str=date_str,
            sessions_dir=sessions_dir,
            candidate_dir=candidate_dir,
            log_dir=log_dir,
            review_dir=review_dir,
        ):
            out.append(date_str)
    return out


def discover_paper_only_review_targets(
    *,
    live_session_dates: Iterable[str],
    max_date: str,
    review_dir: Path,
    log_dir: Path,
    paper_roots: Optional[Tuple[Tuple[Path, Path], ...]] = None,
) -> List[Tuple[str, Path, Path]]:
    """Return (date, sessions_dir, candidate_dir) tuples for dates
    that have a paper session but NO live session. The first paper
    root in `paper_roots` that holds a session for a given date wins
    -- giving the legacy `paper_trading/` root priority over the
    multi-engine production-mirror `paper_A_current/`.

    The 2026-05-31 frontend handles paper-only dates with a fallback
    session-only view, but a full daily_human_review JSON gives the
    operator the rich cohort/calibration health blocks that
    paper-only sessions would otherwise miss out on.
    """
    if paper_roots is None:
        # Resolve at call time so tests + future operator-overrides
        # can patch DEFAULT_PAPER_REVIEW_ROOTS at module level.
        paper_roots = _config.DEFAULT_PAPER_REVIEW_ROOTS
    live_set = {d for d in live_session_dates}
    targets: List[Tuple[str, Path, Path]] = []
    seen: set = set()
    for sessions_dir, candidate_dir in paper_roots:
        if not sessions_dir.exists():
            continue
        for path in sessions_dir.glob("*_session.json"):
            date_str = path.name[: len("YYYY-MM-DD")]
            if len(date_str) != 10 or date_str[4] != "-" or date_str[7] != "-":
                continue
            if date_str > max_date or date_str in live_set or date_str in seen:
                continue
            if _daily_review_is_current(
                date_str=date_str,
                sessions_dir=sessions_dir,
                candidate_dir=candidate_dir,
                log_dir=log_dir,
                review_dir=review_dir,
            ):
                seen.add(date_str)
                continue
            targets.append((date_str, sessions_dir, candidate_dir))
            seen.add(date_str)
    targets.sort(key=lambda t: t[0])
    return targets
