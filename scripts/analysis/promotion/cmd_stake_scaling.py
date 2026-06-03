"""Stake-scaling promote + demote handlers.

`promote stake-scaling` writes `calibrated_stake_scale_mode=enforce`
to the live overrides file + prints the recommended CLI change.
`demote stake-scaling` removes the override + prints the revert CLI.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from scripts.analysis.run_daily_refresh import _safe_load_json
from scripts.trading.live_engine_overrides import (
    remove_override as _live_overrides_remove,
    set_override as _live_overrides_set,
)

from .backups import _capture_artifact_lineage, _compute_promotion_lineage
from .demote_helpers import _demote_verdict_gate, _print_demotion_verdict_block
from .demotion import stake_scaling_demotion_verdict
from .events import (
    PromotionEvent,
    _resolve_operator,
    load_promotion_events,
    write_promotion_event,
)
from .output import _print_block, _print_checklist, _print_header


def cmd_stake_scaling(args: argparse.Namespace) -> int:
    _print_header("Promote stake-scaling shadow -> enforce")
    operator = _resolve_operator(args.operator)
    payload, err = _safe_load_json(args.stake_scaling_report_path)
    if err or not isinstance(payload, dict):
        print(
            f"\nERROR could not read stake-scaling report at "
            f"{args.stake_scaling_report_path}: {err}",
            file=sys.stderr,
        )
        write_promotion_event(
            PromotionEvent(
                lever="stake_scaling",
                side=getattr(args, "side", "over"),
                action="blocked",
                operator=operator,
                block_reason=f"verdict report unreadable: {err}",
            ),
            log_path=args.event_log_path,
        )
        return 2

    verdict_label = str(payload.get("verdict") or "<missing>")
    _print_block("verdict", verdict_label)
    _print_block("reason", payload.get("verdict_reason", ""))
    _print_block(
        "sessions",
        f"{payload.get('n_sessions', 0)}/{(payload.get('thresholds') or {}).get('min_sessions', 30)}",
    )

    if verdict_label != "promote" and not args.force:
        msg = (
            f"verdict is '{verdict_label}', not 'promote'; "
            "refusing to recommend enforce (use --force to override)"
        )
        print(f"\nBLOCKED {msg}")
        write_promotion_event(
            PromotionEvent(
                lever="stake_scaling",
                side=getattr(args, "side", "over"),
                action="blocked",
                operator=operator,
                verdict_snapshot=payload,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 1

    cli_change = "--calibrated-stake-scale-mode enforce"
    print(f"\nRECOMMENDED CLI CHANGE")
    print(f"  Replace `--calibrated-stake-scale-mode shadow` with:")
    print(f"      {cli_change}")
    print()
    print(f"  Note: this promote ALSO writes `calibrated_stake_scale_mode=enforce` to")
    print(f"  {args.live_overrides_path}, which the live engine reads on startup.")
    print(f"  Explicit CLI flag still wins if you pass one.")

    if args.dry_run:
        print("\nDRY-RUN no overrides written, no event recorded.")
        write_promotion_event(
            PromotionEvent(
                lever="stake_scaling",
                side=getattr(args, "side", "over"),
                action="dry_run",
                operator=operator,
                verdict_snapshot=payload,
                from_state={"calibrated_stake_scale_mode": "shadow"},
                to_state={"calibrated_stake_scale_mode": "enforce"},
            ),
            log_path=args.event_log_path,
        )
        return 0

    backup_path: Optional[Path] = None
    try:
        backup_path, _payload = _live_overrides_set(
            path=args.live_overrides_path,
            operator=operator,
            top_level={"calibrated_stake_scale_mode": "enforce"},
        )
    except (OSError, ValueError) as exc:
        msg = f"failed to write overrides file: {exc!r}"
        print(f"\nERROR {msg}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="stake_scaling",
                side=getattr(args, "side", "over"),
                action="blocked",
                operator=operator,
                verdict_snapshot=payload,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 2

    write_promotion_event(
        PromotionEvent(
            lever="stake_scaling",
            side=getattr(args, "side", "over"),
            action="forced" if (verdict_label != "promote" and args.force) else "promoted",
            operator=operator,
            verdict_snapshot=payload,
            from_state={"calibrated_stake_scale_mode": "shadow"},
            to_state={
                "calibrated_stake_scale_mode": "enforce",
                "overrides_path": str(args.live_overrides_path),
            },
            backup_path=str(backup_path) if backup_path else None,
            source_artifact_lineage=_capture_artifact_lineage(args.stake_scaling_report_path),
            promotion_lineage=_compute_promotion_lineage(),
            notes="overrides file mutated; restart live engine to pick up",
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            f"Restart `live_engine.py` so it re-reads {args.live_overrides_path}.",
            "Next live session will multiply base stake by `calibrated_stake_multiplier` "
            "(was logged-only in shadow).",
            "Continue watching `analyze_stake_scaling_promotion`'s verdict daily; "
            "if the high-vs-low gap collapses, run `promote.py demote stake-scaling`.",
            f"Audit trail: see {args.event_log_path} for this promotion's row.",
        ]
    )
    return 0


def cmd_demote_stake_scaling(args: argparse.Namespace) -> int:
    _print_header("Demote stake-scaling: enforce -> shadow")
    operator = _resolve_operator(args.operator)
    events = load_promotion_events(args.event_log_path)
    verdict = stake_scaling_demotion_verdict(
        events=events, sessions_dir=args.sessions_dir,
    )
    _print_demotion_verdict_block(verdict)

    gate = _demote_verdict_gate(
        verdict=verdict, args=args, operator=operator, lever="stake_scaling",
    )
    if gate is not None:
        return gate

    cli_change = "--calibrated-stake-scale-mode shadow"
    print(f"\nRECOMMENDED CLI CHANGE")
    print(f"  Replace `--calibrated-stake-scale-mode enforce` with:")
    print(f"      {cli_change}")
    print(f"  In the live trader command line.")
    print()
    print(f"  Note: this demote ALSO removes `calibrated_stake_scale_mode` from")
    print(f"  {args.live_overrides_path} (or restores from backup if one exists).")

    if args.dry_run:
        print("\nDRY-RUN no overrides changed, no event recorded.")
        write_promotion_event(
            PromotionEvent(
                lever="stake_scaling",
                side=getattr(args, "side", "over"),
                action="dry_run",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                from_state={"calibrated_stake_scale_mode": "enforce"},
                to_state={"calibrated_stake_scale_mode": "shadow"},
            ),
            log_path=args.event_log_path,
        )
        return 0

    # Demote: drop the override key. Restore from backup if one exists
    # (a prior promote->demote->repromote cycle may have stacked
    # backups; remove_override captures the current state into a fresh
    # backup so a subsequent re-promote can also be reverted).
    backup_path: Optional[Path] = None
    try:
        backup_path, _payload = _live_overrides_remove(
            path=args.live_overrides_path,
            operator=operator,
            top_level_keys=["calibrated_stake_scale_mode"],
        )
    except OSError as exc:
        msg = f"failed to update overrides file: {exc!r}"
        print(f"\nERROR {msg}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="stake_scaling",
                side=getattr(args, "side", "over"),
                action="blocked",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 2

    write_promotion_event(
        PromotionEvent(
            lever="stake_scaling",
            side=getattr(args, "side", "over"),
            action="forced" if (verdict.get("verdict") != "demote" and args.force) else "demoted",
            direction="demote",
            operator=operator,
            verdict_snapshot=verdict,
            from_state={"calibrated_stake_scale_mode": "enforce"},
            to_state={
                "calibrated_stake_scale_mode": "shadow",
                "overrides_path": str(args.live_overrides_path),
            },
            backup_path=str(backup_path) if backup_path else None,
            notes="overrides key removed; restart live engine to pick up",
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            f"Restart `live_engine.py` so it re-reads {args.live_overrides_path}.",
            "Multiplier will continue to be computed and logged but won't change stake.",
            "Continue watching `analyze_stake_scaling_promotion`'s verdict daily.",
            f"Audit trail: see {args.event_log_path} for this demotion's row.",
        ]
    )
    return 0
