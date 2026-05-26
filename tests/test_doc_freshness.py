"""tests/test_doc_freshness.py -- guard against AGENT_CONTEXT.md staleness.

Each AGENT_CONTEXT.md carries a "Last checked" date line near the top.
This test asserts the date is no more than `MAX_STALENESS_DAYS` behind
the newest mtime of any .py file under the same folder. A failure
means either (a) the doc actually went stale and needs a sweep, or
(b) the date should be bumped to acknowledge the sweep was already
done.

The check is intentionally low-friction: it emits a *warning* at
`WARN_STALENESS_DAYS` (the agent can re-affirm by bumping the date)
and a *hard fail* at `MAX_STALENESS_DAYS` (the doc is unreliable
enough that an agent reading it might be misled).

The shared logic + thresholds live in
`scripts/analysis/doc_freshness.py` so the runtime
`refresh_health_rollup` step uses the same rules.

To bump a date without rewriting the doc, change the "Last checked
against the active ... files: YYYY-MM-DD" line. The "Recent changes"
section is the right place to log what changed when the date moves.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))

from doc_freshness import (  # noqa: E402
    MAX_STALENESS_DAYS,
    TEST_COUNT_TOLERANCE,
    WARN_STALENESS_DAYS,
    agent_context_targets,
    classify,
    parse_claimed_test_count,
    compare_test_count,
)


@pytest.mark.parametrize(
    "doc_path,folder",
    agent_context_targets(),
    ids=lambda v: str(v).replace(str(PROJECT_DIR), "").lstrip("/\\"),
)
def test_agent_context_last_checked_within_max(doc_path: Path, folder: Path) -> None:
    """Hard fail when the doc's Last-checked date is > MAX_STALENESS_DAYS
    older than the newest .py file in the same folder."""
    finding = classify(doc_path, folder)

    if finding.severity == "doc-missing":
        pytest.skip(f"{doc_path} missing")
    if finding.severity == "no-py":
        pytest.skip(f"No .py files under {folder}")
    if finding.severity == "missing-date":
        pytest.fail(f"{doc_path} has no parseable 'Last checked: YYYY-MM-DD' line")

    assert finding.delta_days is not None
    assert finding.delta_days <= MAX_STALENESS_DAYS, (
        f"{doc_path} 'Last checked: {finding.last_checked}' is "
        f"{finding.delta_days} days behind the newest .py mtime in {folder} "
        f"({finding.newest_py}). Either sweep the doc and bump the date, "
        f"or document the gap in the doc's 'Recent changes' section."
    )


def test_tests_agent_context_module_count_within_tolerance() -> None:
    """Assert tests/AGENT_CONTEXT.md's claimed module count is within
    TEST_COUNT_TOLERANCE of the real disk count."""
    doc_path = PROJECT_DIR / "tests" / "AGENT_CONTEXT.md"
    if not doc_path.exists():
        pytest.skip(f"{doc_path} missing")

    claimed = parse_claimed_test_count(doc_path)
    assert claimed is not None, (
        f"{doc_path} missing 'There are N test modules' line. Add one."
    )

    _claimed, on_disk, delta = compare_test_count()
    assert delta is not None
    assert abs(delta) <= TEST_COUNT_TOLERANCE, (
        f"{doc_path} claims {claimed} test modules but disk has {on_disk} "
        f"(delta={delta}, tolerance={TEST_COUNT_TOLERANCE}). Update the "
        f"count and add new modules to the categorized list."
    )


def test_agent_context_warn_stale_summary(capsys) -> None:
    """Soft summary: emit a warning per AGENT_CONTEXT.md whose
    Last-checked is past WARN_STALENESS_DAYS but still within
    MAX_STALENESS_DAYS. Never fails -- this is the early-warning
    gauge so docs don't silently rot toward the hard limit."""
    warnings: List[str] = []
    for doc_path, folder in agent_context_targets():
        finding = classify(doc_path, folder)
        if finding.severity == "warn":
            warnings.append(
                f"STALE: {finding.short_label} -- "
                f"Last checked: {finding.last_checked}, "
                f"newest .py: {finding.newest_py} "
                f"({finding.delta_days}d behind)"
            )
        elif finding.severity in {"missing-date", "doc-missing"}:
            warnings.append(f"NO-DATE: {finding.short_label}")
    if warnings:
        print("\nAGENT_CONTEXT.md staleness warnings:")
        for w in warnings:
            print(f"  - {w}")
    # Always passes; the print() output surfaces in -s mode and to the
    # refresh_health_rollup wrapper.
