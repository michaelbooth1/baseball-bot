"""Stage-1 cache promotion verdict (2026-05-17).

Unlike Stage-2's Brier-history-driven verdict, Stage-1 has no automated
quality metric at the cache layer (the cache is just the empirical+
Poisson lookup table). Promotion gating is based on source-file
existence + lineage freshness vs production. The operator is expected
to have validated the source artifact via the offline analysis suite
(loss attribution, shadow-override report, cell-conditional drill)
BEFORE running `promote stage1`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .constants import (
    DEMOTE_MIN_FILLED_PER_WINDOW,
    DEMOTE_PRE_POST_WINDOW_DAYS,
    DEMOTE_ROI_REGRESSION_THRESHOLD,
    FAST_DEMOTE_GRACE_DAYS,
    FAST_DEMOTE_MIN_POST_FILLS,
    FAST_DEMOTE_Z,
)
from .demotion import _per_lever_demotion_verdict
from .events import latest_promotion_event_for_lever
from .fast_demotion import _per_lever_fast_demote_verdict


def _read_lineage_built_at(path: Path) -> Optional[str]:
    """Read `lineage.built_at_utc` from a Stage-1 cache JSON. Returns
    None when the file is missing, unreadable, or has no lineage block.
    """
    if not path.exists() or not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    lineage = payload.get("lineage")
    if not isinstance(lineage, dict):
        return None
    return lineage.get("built_at_utc")


def _stage1_promotion_verdict(
    *,
    source_path: Path,
    production_path: Path,
) -> Dict[str, Any]:
    """Promote Stage-1 source -> production when source exists AND has
    lineage that's newer than (or equal to) production's lineage.

    Verdict values:
      - `promote` -- source exists, source built >= production
      - `staging_missing` -- source file absent
      - `source_older_than_production` -- source's built_at_utc is
        BEFORE production's; refusing would be a no-op but we surface
        as a blocker so `--force` is required (prevents silent
        downgrade)
      - `no_lineage_comparison` -- one or both files lack lineage
        blocks; allow with --force after operator inspection

    `from_lineage` / `to_lineage` are surfaced on the verdict for the
    audit trail so the operator sees what they're swapping.
    """
    prod_built = _read_lineage_built_at(production_path)
    src_built = _read_lineage_built_at(source_path)
    payload: Dict[str, Any] = {
        "source_path": str(source_path),
        "production_path": str(production_path),
        "source_exists": source_path.exists(),
        "production_exists": production_path.exists(),
        "source_built_at_utc": src_built,
        "production_built_at_utc": prod_built,
    }
    if not source_path.exists():
        payload["verdict"] = "staging_missing"
        payload["verdict_reason"] = (
            f"Source file {source_path.name} does not exist. The Stage-1 "
            "cache builder does not write to this path automatically; "
            "either rebuild Stage-1 to this path or pass --stage1-source-path "
            "pointing at the cache you want to promote."
        )
        return payload
    if not production_path.exists():
        # First-ever promotion. Allowed (no backup to take, but no
        # downgrade risk either). Set verdict=promote so the operator
        # doesn't need --force on the first-time bootstrap path.
        payload["verdict"] = "promote"
        payload["verdict_reason"] = (
            f"No production Stage-1 cache at {production_path.name} -- "
            "first-time promotion. No backup will be written."
        )
        return payload
    if src_built is None or prod_built is None:
        payload["verdict"] = "no_lineage_comparison"
        payload["verdict_reason"] = (
            "One or both files lack a `lineage.built_at_utc` field. "
            "Cannot verify the source is fresher than production. "
            "Pass --force to proceed after manual inspection."
        )
        return payload
    if src_built < prod_built:
        payload["verdict"] = "source_older_than_production"
        payload["verdict_reason"] = (
            f"Source built {src_built} is BEFORE production built "
            f"{prod_built}. Swapping would downgrade Stage-1 to an older "
            "version. Pass --force to override (e.g. intentional rollback)."
        )
        return payload
    payload["verdict"] = "promote"
    payload["verdict_reason"] = (
        f"Source built {src_built} is at or after production built "
        f"{prod_built}; safe to swap."
    )
    return payload


def stage1_demotion_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    window_days: int = DEMOTE_PRE_POST_WINDOW_DAYS,
    min_filled: int = DEMOTE_MIN_FILLED_PER_WINDOW,
    regression_threshold: float = DEMOTE_ROI_REGRESSION_THRESHOLD,
) -> Dict[str, Any]:
    """Outcome-regression demote verdict for Stage-1.

    Same pre/post 14d ROI window pattern as Stage-2/Stage-3. Stage-1
    affects every bet (it's the base FV), so no bet_filter is needed.
    """
    return _per_lever_demotion_verdict(
        lever="stage1",
        promotion_event=latest_promotion_event_for_lever(events, "stage1"),
        sessions_dir=sessions_dir,
        bet_filter=None,
        window_days=window_days,
        min_filled=min_filled,
        regression_threshold=regression_threshold,
    )


def stage1_fast_demote_verdict(
    *, events: List[Dict[str, Any]], sessions_dir: Path,
    min_post_fills: int = FAST_DEMOTE_MIN_POST_FILLS,
    z: float = FAST_DEMOTE_Z,
    grace_days: int = FAST_DEMOTE_GRACE_DAYS,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """Wilson-UB fast demote verdict for Stage-1 (parallel to the
    windowed verdict above; fires sooner when evidence is clear)."""
    return _per_lever_fast_demote_verdict(
        lever="stage1",
        promotion_event=latest_promotion_event_for_lever(events, "stage1"),
        sessions_dir=sessions_dir, bet_filter=None,
        min_post_fills=min_post_fills, z=z, grace_days=grace_days, today=today,
    )
