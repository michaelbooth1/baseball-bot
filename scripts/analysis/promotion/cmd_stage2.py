"""Stage-2 promote + demote handlers.

`promote stage2` atomic-copies the staging Stage-2 run-env cache to
production, gated on the Brier-stability verdict from the daily
refresh's history. `demote stage2` restores the prior production
cache from the backup written at promote time.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from scripts.analysis.run_daily_refresh import (
    _load_stage2_brier_history,
    _safe_load_json,
    _stage2_promotion_verdict,
    _stage2_validation_brier,
)

from .backups import (
    _atomic_copy,
    _backup_path,
    _backup_prior_production,
    _capture_artifact_lineage,
    _compute_promotion_lineage,
)
from .demote_helpers import _demote_verdict_gate, _print_demotion_verdict_block
from .demotion import stage2_demotion_verdict
from .events import (
    PromotionEvent,
    _resolve_operator,
    load_promotion_events,
    write_promotion_event,
)
from .output import _print_block, _print_checklist, _print_header


def cmd_stage2(args: argparse.Namespace) -> int:
    _print_header("Promote Stage-2 staging -> production")
    operator = _resolve_operator(args.operator)
    history = _load_stage2_brier_history(args.stage2_brier_history_path)
    verdict = _stage2_promotion_verdict(history)
    _print_block("verdict", verdict["verdict"])
    _print_block(
        "improving days",
        f"{verdict['n_improving']}/{verdict['n_history']} (need {verdict['n_consecutive_required']})",
    )

    # Read current Brier values for the audit trail (not for decision logic --
    # the verdict above is authoritative). We snapshot prod + staging so the
    # event log says exactly what we swapped from / to.
    prod_payload, _ = _safe_load_json(args.stage2_cache_path)
    stg_payload, _ = _safe_load_json(args.stage2_staging_path)
    prod_brier = _stage2_validation_brier(prod_payload)
    stg_brier = _stage2_validation_brier(stg_payload)
    _print_block("staging brier", f"{stg_brier:.4f}" if stg_brier is not None else "<unreadable>")
    _print_block("production brier", f"{prod_brier:.4f}" if prod_brier is not None else "<unreadable>")

    if not args.stage2_staging_path.exists():
        print(
            f"\nERROR Stage-2 staging cache missing at {args.stage2_staging_path}",
            file=sys.stderr,
        )
        write_promotion_event(
            PromotionEvent(
                lever="stage2",
                action="blocked",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason="staging cache missing",
            ),
            log_path=args.event_log_path,
        )
        return 2

    if verdict["verdict"] != "promote" and not args.force:
        msg = (
            f"verdict is '{verdict['verdict']}', not 'promote'; "
            "refusing to promote (use --force to override)"
        )
        print(f"\nBLOCKED {msg}")
        write_promotion_event(
            PromotionEvent(
                lever="stage2",
                action="blocked",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 1

    if args.dry_run:
        print(
            f"\nDRY-RUN would atomically copy "
            f"{args.stage2_staging_path.name} -> {args.stage2_cache_path.name} "
            f"(backing up prior production to {_backup_path(args.stage2_cache_path).name} first)"
        )
        write_promotion_event(
            PromotionEvent(
                lever="stage2",
                action="dry_run",
                direction="promote",
                operator=operator,
                verdict_snapshot=verdict,
                from_state={"production_brier": prod_brier},
                to_state={"staging_brier": stg_brier},
            ),
            log_path=args.event_log_path,
        )
        return 0

    # Back up the current production file BEFORE the swap so a future
    # `demote stage2` can restore it. None means there was no prior
    # production file -- shouldn't happen at this point but handled
    # defensively (demote would revert by deleting the new production).
    try:
        backup = _backup_prior_production(args.stage2_cache_path)
    except OSError as exc:
        print(f"\nERROR backup failed: {exc!r}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="stage2",
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
        _atomic_copy(args.stage2_staging_path, args.stage2_cache_path)
    except OSError as exc:
        print(f"\nERROR file swap failed: {exc!r}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="stage2",
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
        f"\nPROMOTED Stage-2: {args.stage2_staging_path.name} -> {args.stage2_cache_path.name}"
        + (f"\n  prior production backed up to {backup.name}" if backup else "")
    )
    write_promotion_event(
        PromotionEvent(
            lever="stage2",
            action="forced" if (verdict["verdict"] != "promote" and args.force) else "promoted",
            direction="promote",
            operator=operator,
            verdict_snapshot=verdict,
            from_state={"production_brier": prod_brier},
            to_state={"staging_brier": stg_brier},
            backup_path=str(backup) if backup else None,
            # Active #16: source lineage from the staging artifact +
            # fresh promotion-time lineage. Lets fast_demote investigations
            # answer "which Stage-2 was promoted, from which git_sha?"
            source_artifact_lineage=_capture_artifact_lineage(args.stage2_staging_path),
            promotion_lineage=_compute_promotion_lineage(),
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            "Restart `live_engine.py` (or wait for next session) so the new Stage-2 cache loads.",
            "Verify by checking the next refresh's `model_freshness_health` "
            "block: prod and staging Brier should now be ~equal.",
            f"If outcomes regress, run `promote.py demote stage2` to restore from "
            f"{backup.name if backup else '<no backup>'}.",
            f"Audit trail: see {args.event_log_path} for this promotion's row.",
        ]
    )
    return 0


def cmd_demote_stage2(args: argparse.Namespace) -> int:
    _print_header("Demote Stage-2: restore prior production cache from backup")
    operator = _resolve_operator(args.operator)
    events = load_promotion_events(args.event_log_path)
    verdict = stage2_demotion_verdict(
        events=events, sessions_dir=args.sessions_dir,
    )
    _print_demotion_verdict_block(verdict)

    gate = _demote_verdict_gate(
        verdict=verdict, args=args, operator=operator, lever="stage2",
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
                f"{backup_path.name} -> {args.stage2_cache_path.name}"
            )
        else:
            print(
                f"\nDRY-RUN would delete {args.stage2_cache_path.name} "
                "(no prior backup; runtime would fall back to load default)"
            )
        write_promotion_event(
            PromotionEvent(
                lever="stage2",
                action="dry_run",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                from_state={"production_path": str(args.stage2_cache_path)},
                to_state={"restored_from": str(backup_path) if backup_path else None},
            ),
            log_path=args.event_log_path,
        )
        return 0

    # Restore: atomic copy backup -> production. If backup is missing
    # (first-promotion case or backup file lost), the safe action is to
    # delete the current production file so the next refresh's staging
    # rebuild + sanity guard re-promotes from scratch.
    try:
        if backup_path and backup_path.exists():
            _atomic_copy(backup_path, args.stage2_cache_path)
            print(f"\nDEMOTED Stage-2: restored {backup_path.name} -> {args.stage2_cache_path.name}")
        else:
            if args.stage2_cache_path.exists():
                args.stage2_cache_path.unlink()
            print(
                f"\nDEMOTED Stage-2: removed {args.stage2_cache_path.name} "
                "(no backup found; next refresh's stage1_cache_promote will rebuild)"
            )
    except OSError as exc:
        print(f"\nERROR demote file action failed: {exc!r}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="stage2",
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
            lever="stage2",
            action="forced" if (verdict.get("verdict") != "demote" and args.force) else "demoted",
            direction="demote",
            operator=operator,
            verdict_snapshot=verdict,
            from_state={"production_path": str(args.stage2_cache_path)},
            to_state={"restored_from": str(backup_path) if backup_path else None},
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            "Restart `live_engine.py` (or wait for next session) so the restored Stage-2 cache loads.",
            "If outcomes recover, the demote was correct. If not, investigate further upstream.",
            f"Audit trail: see {args.event_log_path} for this demotion's row.",
        ]
    )
    return 0
