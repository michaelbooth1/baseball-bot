"""Promotion event log: dataclass, write/load helpers, operator helper.

Append-only JSONL audit log at `data/analysis_output/promotion_events.jsonl`.
Every promote / demote / dry-run / blocked attempt writes a row here.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import constants as _constants


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_operator(arg_value: Optional[str]) -> str:
    if arg_value:
        return str(arg_value)
    env = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    return str(env) or "unknown"


@dataclass
class PromotionEvent:
    lever: str            # one of KNOWN_LEVERS (see constants.py)
    # action: short label. Interpretation depends on `direction`:
    #   direction=promote: "promoted" | "dry_run" | "blocked" | "forced"
    #   direction=demote:  "demoted"  | "dry_run" | "blocked" | "forced"
    # ("forced" means --force was used to override a no-go verdict.)
    action: str
    operator: str
    direction: str = "promote"  # "promote" | "demote"
    # Side this event affects. Phase B B2 (2026-05-16). "both" is the
    # default for side-symmetric levers (stage2 + stage3-v2 weights
    # affect both Over and Under inference equally because the FV is
    # over-side and Under is derived as 1-FV through the per-side
    # calibrator). Side-asymmetric levers (stake-scaling, gate-threshold,
    # future per-side calibrator promotions) record "over" or "under".
    # Legacy rows without `side` are read as "both" for back-compat.
    side: str = "both"     # "over" | "under" | "both"
    verdict_snapshot: Optional[Dict[str, Any]] = None
    from_state: Optional[Dict[str, Any]] = None
    to_state: Optional[Dict[str, Any]] = None
    block_reason: Optional[str] = None
    subprocess_returncode: Optional[int] = None
    notes: Optional[str] = None
    # Path to the prior-state backup file written at promotion time, so
    # demotion can locate the rollback target without guessing. None for
    # CLI-flag levers (stake-scaling, gate-threshold) and for first-time
    # promotions where no prior production file existed.
    backup_path: Optional[str] = None
    # Active #16 (2026-05-17): lineage tracking. `source_artifact_lineage`
    # is the BUILD-time lineage pulled from the artifact JSON being
    # promoted (which builder produced it, from which git_sha, with
    # which input hashes). `promotion_lineage` is the lineage stamped
    # AT PROMOTION (current git_sha + timestamp). Together they let
    # operators trace: "fast_demote fired -> which artifact was in
    # production? -> what built it? -> who promoted it from where?"
    # Both default to None for back-compat reads.
    source_artifact_lineage: Optional[Dict[str, Any]] = None
    promotion_lineage: Optional[Dict[str, Any]] = None

    def to_row(self) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "generated_at_utc": _now_iso(),
            "lever": self.lever,
            "action": self.action,
            "direction": self.direction,
            "side": self.side,
            "operator": self.operator,
        }
        if self.verdict_snapshot is not None:
            row["verdict_snapshot"] = self.verdict_snapshot
        if self.from_state is not None:
            row["from_state"] = self.from_state
        if self.to_state is not None:
            row["to_state"] = self.to_state
        if self.block_reason:
            row["block_reason"] = self.block_reason
        if self.subprocess_returncode is not None:
            row["subprocess_returncode"] = self.subprocess_returncode
        if self.notes:
            row["notes"] = self.notes
        if self.backup_path:
            row["backup_path"] = self.backup_path
        if self.source_artifact_lineage:
            row["source_artifact_lineage"] = self.source_artifact_lineage
        if self.promotion_lineage:
            row["promotion_lineage"] = self.promotion_lineage
        return row


def write_promotion_event(
    event: PromotionEvent, *, log_path: Optional[Path] = None,
) -> None:
    """Append one event row. Best-effort: a failed write logs to stderr
    but doesn't abort the promotion (the side effect we care about
    already happened on disk)."""
    if log_path is None:
        log_path = _constants.DEFAULT_PROMOTION_EVENTS_LOG
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_row()) + "\n")
    except OSError as exc:
        print(
            f"WARNING failed to append promotion event to {log_path}: {exc!r}",
            file=sys.stderr,
        )


def load_promotion_events(
    log_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Read the append-only promotion events log. Missing/malformed
    lines are skipped silently -- this is research data, not a
    contract; corruption shouldn't break the demote CLI."""
    if log_path is None:
        log_path = _constants.DEFAULT_PROMOTION_EVENTS_LOG
    rows: List[Dict[str, Any]] = []
    if not log_path.exists():
        return rows
    try:
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
    return rows


def latest_promotion_event_for_lever(
    events: List[Dict[str, Any]], lever: str,
    *, side: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Most recent successful promotion event for `lever`. Filters to
    direction=promote (or absent, which legacy-treats as promote) and
    action in {promoted, forced}. Returns None if no such event exists.

    Phase B B2 (2026-05-16) added the optional `side` filter:
      - None (default): no side filter (back-compat behavior)
      - "over"/"under": match rows whose side equals that value OR
        rows whose side is "both" (since "both"-side promotions
        affect every requested side)
      - "both": match only rows whose side is exactly "both"
        (used by callers that want to find ONLY side-symmetric
        events like a Stage-2 promotion)
    Legacy rows without `side` are treated as "both" so they always
    match an "over"/"under" filter.
    """
    candidates: List[Dict[str, Any]] = []
    for r in events:
        if str(r.get("lever") or "") != lever:
            continue
        # Backward-compat: rows without `direction` predate the field
        # and were all promotions.
        direction = str(r.get("direction") or "promote")
        if direction != "promote":
            continue
        if str(r.get("action") or "") not in ("promoted", "forced"):
            continue
        if side is not None:
            row_side = str(r.get("side") or "both")
            if side == "both":
                if row_side != "both":
                    continue
            else:
                # Side-specific filter ("over" or "under"): match same
                # side OR "both" (legacy rows + side-symmetric levers).
                if row_side != side and row_side != "both":
                    continue
        candidates.append(r)
    if not candidates:
        return None
    return max(candidates, key=lambda r: str(r.get("generated_at_utc") or ""))
