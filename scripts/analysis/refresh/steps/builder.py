"""Top-level build_refresh_steps orchestrator.

Concatenates each cluster's list in the canonical pre-refactor order.
DO NOT reorder without verifying tests + the manifest step list. The
step name + sequence is part of the public contract.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from ..config import RefreshConfig, RefreshStep
from ._canonical_tables import build_canonical_table_steps
from ._preflight_and_caches import (
    build_preflight_and_caches_steps,
    build_settlement_and_review_steps,
)
from ._walkforward_and_audits import (
    build_audit_research_steps,
    build_walk_forward_steps,
)


def build_refresh_steps(
    config: RefreshConfig,
    session_dates: Sequence[str],
    max_date: Optional[str],
) -> List[RefreshStep]:
    steps: List[RefreshStep] = []
    steps.extend(build_preflight_and_caches_steps(config, session_dates, max_date))
    if not max_date:
        return steps
    steps.extend(build_settlement_and_review_steps(config, session_dates, max_date))
    steps.extend(build_canonical_table_steps(config, max_date))
    steps.extend(build_walk_forward_steps(config, max_date))
    steps.extend(build_audit_research_steps(config))
    return steps
