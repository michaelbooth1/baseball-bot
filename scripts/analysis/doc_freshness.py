"""Doc-freshness helpers shared by `tests/test_doc_freshness.py` and the
runtime `_run_refresh_health_rollup` block in `run_daily_refresh.py`.

Both consumers ask the same two questions:
  1. Are any AGENT_CONTEXT.md files badly behind the newest .py mtime
     in the folder they describe?
  2. Does `tests/AGENT_CONTEXT.md`'s claimed test-module count match
     what's on disk?

Pulled into its own module so the rule of "warn at 14d, fail at 60d"
lives in exactly one place. Pure stdlib; no IO outside of stat/glob/read.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]

# Thresholds (used by both the pytest gate and the runtime rollup).
WARN_STALENESS_DAYS = 14
MAX_STALENESS_DAYS = 60
TEST_COUNT_TOLERANCE = 5

LAST_CHECKED_RE = re.compile(
    r"Last checked[^0-9]*?(\d{4}-\d{2}-\d{2})",
    re.MULTILINE,
)
TEST_COUNT_RE = re.compile(
    r"There are\s+\*{0,2}\s*(\d+)\s*test modules", re.IGNORECASE
)


@dataclass(frozen=True)
class FreshnessFinding:
    doc_path: Path
    folder: Path
    last_checked: Optional[dt.date]
    newest_py: Optional[dt.date]
    delta_days: Optional[int]  # newest_py - last_checked
    severity: str               # "ok" | "warn" | "alert" | "missing-date" | "no-py" | "doc-missing"

    @property
    def short_label(self) -> str:
        return str(self.doc_path).replace(str(PROJECT_DIR), "").lstrip("/\\")


def agent_context_targets() -> List[Tuple[Path, Path]]:
    """The 6 (AGENT_CONTEXT.md, folder-to-scan) pairs."""
    return [
        (PROJECT_DIR / "cache" / "AGENT_CONTEXT.md", PROJECT_DIR / "cache"),
        (PROJECT_DIR / "data" / "AGENT_CONTEXT.md", PROJECT_DIR / "data"),
        (
            PROJECT_DIR / "scripts" / "analysis" / "AGENT_CONTEXT.md",
            PROJECT_DIR / "scripts" / "analysis",
        ),
        (
            PROJECT_DIR / "scripts" / "monitor" / "AGENT_CONTEXT.md",
            PROJECT_DIR / "scripts" / "monitor",
        ),
        (
            PROJECT_DIR / "scripts" / "trading" / "AGENT_CONTEXT.md",
            PROJECT_DIR / "scripts" / "trading",
        ),
        (PROJECT_DIR / "tests" / "AGENT_CONTEXT.md", PROJECT_DIR / "tests"),
    ]


def parse_last_checked(path: Path) -> Optional[dt.date]:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    match = LAST_CHECKED_RE.search(text)
    if not match:
        return None
    try:
        return dt.datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def newest_py_mtime(folder: Path) -> Optional[dt.date]:
    if not folder.is_dir():
        return None
    newest: Optional[float] = None
    for root, dirs, files in os.walk(folder):
        # Skip vendored / generated directories that would dominate the mtime.
        dirs[:] = [d for d in dirs if d not in {"__pycache__", ".git"}]
        for name in files:
            if name.endswith(".py"):
                mtime = os.path.getmtime(os.path.join(root, name))
                if newest is None or mtime > newest:
                    newest = mtime
    if newest is None:
        return None
    return dt.datetime.fromtimestamp(newest).date()


def classify(doc_path: Path, folder: Path) -> FreshnessFinding:
    """Return a `FreshnessFinding` for the (doc, folder) pair."""
    if not doc_path.exists():
        return FreshnessFinding(doc_path, folder, None, None, None, "doc-missing")
    last_checked = parse_last_checked(doc_path)
    if last_checked is None:
        return FreshnessFinding(doc_path, folder, None, None, None, "missing-date")
    newest_py = newest_py_mtime(folder)
    if newest_py is None:
        return FreshnessFinding(doc_path, folder, last_checked, None, None, "no-py")
    delta = (newest_py - last_checked).days
    if delta > MAX_STALENESS_DAYS:
        severity = "alert"
    elif delta > WARN_STALENESS_DAYS:
        severity = "warn"
    else:
        severity = "ok"
    return FreshnessFinding(doc_path, folder, last_checked, newest_py, delta, severity)


def scan_all() -> List[FreshnessFinding]:
    return [classify(doc, folder) for (doc, folder) in agent_context_targets()]


def parse_claimed_test_count(doc_path: Path) -> Optional[int]:
    if not doc_path.exists():
        return None
    text = doc_path.read_text(encoding="utf-8", errors="replace")
    match = TEST_COUNT_RE.search(text)
    return int(match.group(1)) if match else None


def disk_test_module_count() -> int:
    return len(list((PROJECT_DIR / "tests").glob("test_*.py")))


def compare_test_count() -> Tuple[Optional[int], int, Optional[int]]:
    """Return (claimed, on_disk, delta_or_None). delta = on_disk - claimed."""
    doc_path = PROJECT_DIR / "tests" / "AGENT_CONTEXT.md"
    claimed = parse_claimed_test_count(doc_path)
    on_disk = disk_test_module_count()
    if claimed is None:
        return None, on_disk, None
    return claimed, on_disk, on_disk - claimed


def render_summary_lines() -> List[str]:
    """Build the lines the refresh-health rollup prints. Always returns
    something; never raises -- the rollup itself is fail-open."""
    lines: List[str] = []
    findings = scan_all()
    alerts = [f for f in findings if f.severity == "alert"]
    warns = [f for f in findings if f.severity == "warn"]
    missing = [f for f in findings if f.severity in {"missing-date", "doc-missing"}]
    if alerts:
        lines.append(
            f"ALERT Doc freshness: {len(alerts)} AGENT_CONTEXT.md over "
            f"{MAX_STALENESS_DAYS}d behind newest .py mtime."
        )
        for f in alerts[:5]:
            lines.append(
                f"  - {f.short_label}: Last checked={f.last_checked}, "
                f"newest_py={f.newest_py} ({f.delta_days}d behind)"
            )
    if warns:
        lines.append(
            f"Doc freshness: {len(warns)} AGENT_CONTEXT.md between "
            f"{WARN_STALENESS_DAYS}-{MAX_STALENESS_DAYS}d behind newest .py "
            "(soft warning, no action required)."
        )
        for f in warns[:5]:
            lines.append(
                f"  - {f.short_label}: Last checked={f.last_checked}, "
                f"newest_py={f.newest_py} ({f.delta_days}d behind)"
            )
    if missing:
        lines.append(
            f"Doc freshness: {len(missing)} AGENT_CONTEXT.md with no "
            "parseable 'Last checked' date."
        )
        for f in missing[:5]:
            lines.append(f"  - {f.short_label}: {f.severity}")
    if not (alerts or warns or missing):
        lines.append("Doc freshness: all AGENT_CONTEXT.md within "
                     f"{WARN_STALENESS_DAYS}d of newest .py.")

    # Test count check (Task 33).
    claimed, on_disk, delta = compare_test_count()
    if claimed is None:
        lines.append(
            f"Test count: {on_disk} test modules on disk; "
            "tests/AGENT_CONTEXT.md has no parseable 'There are N test modules' line."
        )
    else:
        if delta is not None and abs(delta) > TEST_COUNT_TOLERANCE:
            sign = "+" if delta > 0 else ""
            lines.append(
                f"ALERT Test count: tests/AGENT_CONTEXT.md claims {claimed}, "
                f"disk has {on_disk} ({sign}{delta} delta, tolerance "
                f"{TEST_COUNT_TOLERANCE})."
            )
        else:
            lines.append(
                f"Test count: {on_disk} on disk, claimed {claimed} "
                f"(within {TEST_COUNT_TOLERANCE} tolerance)."
            )
    return lines
