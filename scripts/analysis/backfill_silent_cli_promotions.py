"""One-shot: backfill promotion_events.jsonl with the 2026-05-22
calibrator-enforce and scoped-Alt-A-enforce promotions.

Why this exists
---------------
Both flips were CLI-flag levers (`--prob-calibration-mode enforce`
and `--stage1-alt-a-scope-mode enforce`), which means `promote.py`
was NOT involved in shipping them. `promote.py` only writes events
for file-swap levers (stage2, stage3-v2) and the file-mutation
CLI levers it actually owns. Result: a major behavioral promotion
(calibrator now modifies live FV; alt-empirical FV now used for
non-inning-8 candidates) landed on 2026-05-22 with zero entries in
`promotion_events.jsonl`.

This script appends two backfill stub events so:
  - daemon_retrospective.py's staleness check sees them
  - calibration_health._attribute_alert_to_promotions can attribute
    future cohort-ROI drift alerts to "calibrator promoted N days ago"
  - any future audit reading the log knows when the flip happened

Idempotent: re-running does not duplicate the entries (matches on
the unique `backfill_id` field).

Schema notes
------------
Rows are direct dicts matching the PromotionEvent.to_row() shape,
PLUS extra fields (`backfilled_at_utc`, `backfilled_by`,
`backfilled_source`, `backfill_id`) so the rows are clearly post-hoc
inserts, not real-time promote.py output. Readers ignore unknown
fields.

Usage: `python scripts/analysis/backfill_silent_cli_promotions.py`
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_LOG_PATH = (
    PROJECT_DIR / "data" / "analysis_output" / "promotion_events.jsonl"
)


# Each backfill entry has a stable `backfill_id` so re-running the
# script is idempotent. The id is the (lever, event_date) pair so
# adding more backfills later for OTHER dates won't collide.
BACKFILL_ENTRIES: List[Dict[str, Any]] = [
    {
        "backfill_id": "cli_flag_promote:prob_calibration:2026-05-22",
        # The 5/20 paper session had prob_calibration_mode=shadow
        # (cal_applied=0, cal_shadow_scored=1964). The 5/22 session
        # had prob_calibration_mode=enforce (cal_applied=2055). So the
        # flip happened between 5/20 EOD and 5/22 bot start. Stamping
        # 5/22 because that's the first session with enforce active.
        "generated_at_utc": "2026-05-22T18:50:00Z",
        "lever": "prob_calibration",
        "action": "promoted",
        "direction": "promote",
        "side": "both",
        "operator": "backfill_audit_2026-05-23",
        "from_state": {"mode": "shadow"},
        "to_state": {"mode": "enforce", "enforce_min_raw": 0.9},
        "notes": (
            "Backfill of CLI-flag promotion. Operator flipped "
            "--prob-calibration-mode from shadow to enforce on or "
            "around 2026-05-22 (first session with "
            "prob_calibration_applied > 0: 2055/3422 scored). "
            "Recommendation came from 2026-05-22 daily audit: 5/20 "
            "calibrator-enforce counterfactual would have saved "
            "$+25.21 (would_block 14/17 = 82%, blocked WR 64%). "
            "CLI-flag levers don't auto-write to "
            "promotion_events.jsonl, so this stub is added "
            "retroactively for staleness / drift-attribution "
            "purposes. See 2026-05-23 audit."
        ),
    },
    {
        "backfill_id": "cli_flag_promote:stage1_alt_a_scope:2026-05-22",
        "generated_at_utc": "2026-05-22T18:50:00Z",
        "lever": "stage1_alt_a_scope",
        "action": "promoted",
        "direction": "promote",
        "side": "over",  # alt-A only affects over-side FV chain
        "operator": "backfill_audit_2026-05-23",
        "from_state": {"mode": "shadow"},
        "to_state": {
            "mode": "enforce",
            "default_action": "apply",
            "rules": ["inning_gte_8_regression: hold_poisson"],
        },
        "notes": (
            "Backfill of CLI-flag promotion. Operator flipped "
            "--stage1-alt-a-scope-mode from shadow to enforce on or "
            "around 2026-05-22 (1,426 candidates with "
            "stage1_alt_a_scope_decision=applied + 37 with "
            "held_poisson_enforce in 5/22 candidate JSONL; 0 in 5/20 "
            "and 5/21 because pre-ship / late-start). The 5/22 6-bet "
            "set used empirical FV for 4 trades and held Poisson for "
            "2 (both inning 8, hold-poisson rule fired). 4W/2L day. "
            "Recommendation came from 2026-05-21 audit: Alt-A reduces "
            "aggregate bias by 5.9pp but regresses inning>=8 cohort "
            "(-23.8pp). CLI-flag levers don't auto-write to "
            "promotion_events.jsonl, so this stub is added "
            "retroactively for staleness / drift-attribution "
            "purposes. See 2026-05-23 audit."
        ),
    },
]


def _load_existing(path: Path) -> List[Dict[str, Any]]:
    """Read existing promotion_events.jsonl entries (or empty list)."""
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _existing_backfill_ids(events: List[Dict[str, Any]]) -> set:
    return {
        str(e.get("backfill_id")) for e in events
        if e.get("backfill_id") is not None
    }


def backfill(log_path: Path = DEFAULT_LOG_PATH) -> Dict[str, Any]:
    # 2026-05-23 (followup): assert each backfill entry's lever is
    # registered in promote.KNOWN_LEVERS. Catches typos and forces
    # any new CLI-flag lever to be declared in the central registry
    # before stub-events for it land in the log.
    try:
        from promote import KNOWN_LEVERS as _KNOWN_LEVERS  # type: ignore[import-not-found]
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from promote import KNOWN_LEVERS as _KNOWN_LEVERS  # type: ignore[import-not-found]
    for entry in BACKFILL_ENTRIES:
        lever = entry.get("lever")
        if lever not in _KNOWN_LEVERS:
            raise ValueError(
                f"Backfill entry lever {lever!r} not in promote.KNOWN_LEVERS "
                f"({sorted(_KNOWN_LEVERS)}). Register it in promote.py first."
            )

    existing = _load_existing(log_path)
    existing_ids = _existing_backfill_ids(existing)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    appended: List[str] = []
    skipped: List[str] = []
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        for entry in BACKFILL_ENTRIES:
            bid = entry["backfill_id"]
            if bid in existing_ids:
                skipped.append(bid)
                continue
            # Stamp the back-fill metadata before write so it's clear
            # in the log that this is a post-hoc entry, not a
            # real-time promote.py output.
            row = dict(entry)
            row["backfilled_at_utc"] = now_iso
            row["backfilled_by"] = "scripts/analysis/backfill_silent_cli_promotions.py"
            row["backfilled_source"] = "2026-05-23 daily audit"
            f.write(json.dumps(row) + "\n")
            appended.append(bid)
    return {
        "log_path": str(log_path),
        "appended": appended,
        "skipped_existing": skipped,
        "total_in_log_after": len(existing) + len(appended),
    }


def main() -> int:
    result = backfill()
    print(f"Backfill complete. Log: {result['log_path']}")
    if result["appended"]:
        print(f"  Appended ({len(result['appended'])}):")
        for bid in result["appended"]:
            print(f"    + {bid}")
    if result["skipped_existing"]:
        print(f"  Skipped (already present, idempotent):")
        for bid in result["skipped_existing"]:
            print(f"    = {bid}")
    print(f"  Total rows in log: {result['total_in_log_after']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
