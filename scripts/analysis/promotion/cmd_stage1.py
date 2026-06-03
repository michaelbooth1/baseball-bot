"""Stage-1 cache promote + demote handlers.

`promote stage1` atomically swaps a Stage-1 cache source file ->
production with backup + audit + lineage. `demote stage1` restores
the prior production cache from backup (or deletes if no backup
existed, letting the next refresh's stage1_ou_cache step rebuild).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from .backups import (
    _atomic_copy,
    _backup_path,
    _backup_prior_production,
    _capture_artifact_lineage,
    _compute_promotion_lineage,
)
from .demote_helpers import _demote_verdict_gate, _print_demotion_verdict_block
from .events import (
    PromotionEvent,
    _resolve_operator,
    load_promotion_events,
    write_promotion_event,
)
from .output import _print_block, _print_checklist, _print_header
from .stage1_verdict import _stage1_promotion_verdict, stage1_demotion_verdict


def cmd_stage1(args: argparse.Namespace) -> int:
    _print_header("Promote Stage-1 cache: source -> production")
    operator = _resolve_operator(args.operator)
    verdict = _stage1_promotion_verdict(
        source_path=args.stage1_source_path,
        production_path=args.stage1_cache_path,
    )
    _print_block("verdict", verdict["verdict"])
    _print_block("source path", args.stage1_source_path.name)
    _print_block("production path", args.stage1_cache_path.name)
    if verdict.get("source_built_at_utc"):
        _print_block("source built_at_utc", verdict["source_built_at_utc"])
    if verdict.get("production_built_at_utc"):
        _print_block(
            "production built_at_utc", verdict["production_built_at_utc"],
        )
    if verdict.get("verdict_reason"):
        _print_block("reason", verdict["verdict_reason"])

    if verdict["verdict"] != "promote" and not args.force:
        msg = (
            f"verdict is '{verdict['verdict']}', not 'promote'; "
            "refusing to promote (use --force to override)"
        )
        print(f"\nBLOCKED {msg}")
        write_promotion_event(
            PromotionEvent(
                lever="stage1",
                action="blocked",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 1

    if args.dry_run:
        backup_target = _backup_path(args.stage1_cache_path)
        if args.stage1_cache_path.exists():
            print(
                f"\nDRY-RUN would atomically copy "
                f"{args.stage1_source_path.name} -> "
                f"{args.stage1_cache_path.name} (backing up prior production "
                f"to {backup_target.name} first)"
            )
        else:
            print(
                f"\nDRY-RUN would atomically copy "
                f"{args.stage1_source_path.name} -> "
                f"{args.stage1_cache_path.name} (no prior production; no "
                "backup)"
            )
        write_promotion_event(
            PromotionEvent(
                lever="stage1",
                action="dry_run",
                direction="promote",
                operator=operator,
                verdict_snapshot=verdict,
                from_state={
                    "production_built_at_utc": verdict.get(
                        "production_built_at_utc",
                    ),
                },
                to_state={
                    "source_built_at_utc": verdict.get(
                        "source_built_at_utc",
                    ),
                },
            ),
            log_path=args.event_log_path,
        )
        return 0

    # Backup current production (if any) before the atomic swap.
    backup: Optional[Path] = None
    if args.stage1_cache_path.exists():
        try:
            backup = _backup_prior_production(args.stage1_cache_path)
        except OSError as exc:
            print(f"\nERROR backup failed: {exc!r}", file=sys.stderr)
            write_promotion_event(
                PromotionEvent(
                    lever="stage1",
                    action="blocked",
                    direction="promote",
                    operator=operator,
                    verdict_snapshot=verdict,
                    block_reason=f"backup failed: {exc!r}",
                ),
                log_path=args.event_log_path,
            )
            return 3

    try:
        _atomic_copy(args.stage1_source_path, args.stage1_cache_path)
    except OSError as exc:
        print(f"\nERROR file swap failed: {exc!r}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="stage1",
                action="blocked",
                direction="promote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=f"file swap failed: {exc!r}",
                backup_path=str(backup) if backup else None,
            ),
            log_path=args.event_log_path,
        )
        return 3

    print(
        f"\nPROMOTED Stage-1: {args.stage1_source_path.name} -> "
        f"{args.stage1_cache_path.name}"
        + (f"\n  prior production backed up to {backup.name}" if backup else "")
    )
    write_promotion_event(
        PromotionEvent(
            lever="stage1",
            action="forced" if (
                verdict["verdict"] != "promote" and args.force
            ) else "promoted",
            direction="promote",
            operator=operator,
            verdict_snapshot=verdict,
            from_state={
                "production_built_at_utc": verdict.get(
                    "production_built_at_utc",
                ),
            },
            to_state={
                "source_built_at_utc": verdict.get(
                    "source_built_at_utc",
                ),
            },
            backup_path=str(backup) if backup else None,
            # Source-artifact lineage from the Stage-1 cache being
            # promoted; promotion-time lineage so fast_demote
            # investigations answer "which Stage-1 was live + by
            # what code at promote time".
            source_artifact_lineage=_capture_artifact_lineage(
                args.stage1_source_path,
            ),
            promotion_lineage=_compute_promotion_lineage(),
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            "Restart `live_engine.py` (or wait for next session) so the new "
            "Stage-1 cache loads. The startup INFO log will surface "
            "`Artifact lineage: stage1_cache built=...` to confirm.",
            "Watch the next refresh's `cohort_calibration_health` + "
            "`loss_attribution_health` blocks for bias reduction.",
            "Active #13 fast Wilson-UB demote check is now armed on "
            "stage1 -- a bad promotion will be flagged within ~5-6 days.",
            f"Audit trail: see {args.event_log_path} for this promotion's row.",
            f"If outcomes regress, run `promote.py demote stage1` to restore "
            f"from {backup.name if backup else '<no backup>'}.",
        ]
    )
    return 0


def cmd_demote_stage1(args: argparse.Namespace) -> int:
    _print_header("Demote Stage-1: restore prior production cache from backup")
    operator = _resolve_operator(args.operator)
    events = load_promotion_events(args.event_log_path)
    verdict = stage1_demotion_verdict(
        events=events, sessions_dir=args.sessions_dir,
    )
    _print_demotion_verdict_block(verdict)

    gate = _demote_verdict_gate(
        verdict=verdict, args=args, operator=operator, lever="stage1",
    )
    if gate is not None:
        return gate

    pe = verdict.get("promotion_event") or {}
    backup_str = pe.get("backup_path")
    backup_path: Optional[Path] = Path(backup_str) if backup_str else None

    if args.dry_run:
        if backup_path:
            print(
                f"\nDRY-RUN would atomically restore "
                f"{backup_path.name} -> {args.stage1_cache_path.name}"
            )
        else:
            print(
                f"\nDRY-RUN would delete {args.stage1_cache_path.name} "
                "(no prior backup; the next refresh's stage1_ou_cache "
                "step will rebuild from data/games/)"
            )
        write_promotion_event(
            PromotionEvent(
                lever="stage1",
                action="dry_run",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                from_state={"production_path": str(args.stage1_cache_path)},
                to_state={
                    "restored_from": str(backup_path) if backup_path else None,
                },
            ),
            log_path=args.event_log_path,
        )
        return 0

    try:
        if backup_path and backup_path.exists():
            _atomic_copy(backup_path, args.stage1_cache_path)
            print(
                f"\nDEMOTED Stage-1: restored {backup_path.name} -> "
                f"{args.stage1_cache_path.name}"
            )
        else:
            if args.stage1_cache_path.exists():
                args.stage1_cache_path.unlink()
            print(
                f"\nDEMOTED Stage-1: removed {args.stage1_cache_path.name} "
                "(no backup found; next refresh's stage1_ou_cache "
                "will rebuild)"
            )
    except OSError as exc:
        print(f"\nERROR demote file action failed: {exc!r}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="stage1",
                action="blocked",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=f"file action failed: {exc!r}",
            ),
            log_path=args.event_log_path,
        )
        return 3

    write_promotion_event(
        PromotionEvent(
            lever="stage1",
            action="forced" if (
                verdict.get("verdict") != "demote" and args.force
            ) else "demoted",
            direction="demote",
            operator=operator,
            verdict_snapshot=verdict,
            from_state={"production_path": str(args.stage1_cache_path)},
            to_state={
                "restored_from": str(backup_path) if backup_path else None,
            },
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            "Restart `live_engine.py` (or wait for next session) so the "
            "restored Stage-1 cache loads.",
            "If outcomes recover, the demote was correct. If not, "
            "the regression source was downstream of Stage-1 -- "
            "investigate Stage-2 / calibration / gates next.",
            f"Audit trail: see {args.event_log_path} for this demotion's row.",
        ]
    )
    return 0
