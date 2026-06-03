"""Stage-3 v2 promote + demote handlers.

`promote stage3-v2` subprocess-invokes promote_team_offense_v2.py to
swap the production weights, gated on the drift-stability verdict.
`demote stage3-v2` restores prior weights from the backup or falls back
to compiled defaults if no backup exists.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Optional

from scripts.analysis.run_daily_refresh import (
    _extract_stage3_v2_active_betas,
    _extract_stage3_v2_research_betas,
    _load_stage3_v2_drift_history,
    _safe_load_json,
    _stage3_v2_max_abs_delta,
    _stage3_v2_promotion_verdict,
)

from . import constants as _constants
from .backups import (
    _atomic_copy,
    _backup_path,
    _backup_prior_production,
    _capture_artifact_lineage,
    _compute_promotion_lineage,
)
from .demote_helpers import _demote_verdict_gate, _print_demotion_verdict_block
from .demotion import stage3_v2_demotion_verdict
from .events import (
    PromotionEvent,
    _resolve_operator,
    load_promotion_events,
    write_promotion_event,
)
from .output import _print_block, _print_checklist, _print_header


def cmd_stage3_v2(args: argparse.Namespace) -> int:
    _print_header("Promote Stage-3 v2 research fit -> production weights")
    operator = _resolve_operator(args.operator)
    history = _load_stage3_v2_drift_history(args.stage3_v2_drift_history_path)
    verdict = _stage3_v2_promotion_verdict(history)
    _print_block("verdict", verdict["verdict"])
    _print_block(
        "drifting days",
        f"{verdict['n_drifting']}/{verdict['n_history']} (need {verdict['n_consecutive_required']})",
    )

    research_payload, _ = _safe_load_json(args.stage3_v2_research_fit_path)
    research_betas = _extract_stage3_v2_research_betas(research_payload)
    prod_payload, _ = _safe_load_json(args.stage3_v2_prod_weights_path)
    active_betas, active_source = _extract_stage3_v2_active_betas(prod_payload)
    if research_betas is not None:
        max_delta = _stage3_v2_max_abs_delta(research_betas, active_betas)
        _print_block("max |delta|", f"{max_delta:.4f}")
        _print_block(
            "research betas",
            ", ".join(f"{k}={v:+.4f}" for k, v in sorted(research_betas.items())),
        )
        _print_block(
            f"active betas ({active_source})",
            ", ".join(f"{k}={v:+.4f}" for k, v in sorted(active_betas.items())),
        )
    else:
        _print_block("research betas", "<unreadable>")

    if research_betas is None:
        print(
            f"\nERROR could not extract Stage-3 v2 betas from "
            f"{args.stage3_v2_research_fit_path}",
            file=sys.stderr,
        )
        write_promotion_event(
            PromotionEvent(
                lever="stage3_v2",
                action="blocked",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason="could not extract research betas",
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
                lever="stage3_v2",
                action="blocked",
                direction="promote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 1

    if args.dry_run:
        print(
            f"\nDRY-RUN would invoke "
            f"`python {args.promote_team_offense_script} --source-artifact {args.stage3_v2_research_fit_path}`"
            f"\n  prior production (if present) would be backed up to "
            f"{_backup_path(args.stage3_v2_prod_weights_path).name} first"
        )
        write_promotion_event(
            PromotionEvent(
                lever="stage3_v2",
                action="dry_run",
                direction="promote",
                operator=operator,
                verdict_snapshot=verdict,
                from_state={"active_betas": active_betas, "active_source": active_source},
                to_state={"research_betas": research_betas},
            ),
            log_path=args.event_log_path,
        )
        return 0

    # Back up prior production weights (if any) before subprocess overwrites
    # them. None when this is the first promotion (compiled-defaults case);
    # demote stage3-v2 will then revert by deleting the new production file.
    try:
        backup = _backup_prior_production(args.stage3_v2_prod_weights_path)
    except OSError as exc:
        print(f"\nERROR backup failed: {exc!r}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="stage3_v2",
                action="blocked",
                direction="promote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=f"backup failed: {exc!r}",
            ),
            log_path=args.event_log_path,
        )
        return 3

    cmd = [
        sys.executable,
        str(args.promote_team_offense_script),
        "--source-artifact",
        str(args.stage3_v2_research_fit_path),
        "--output-path",
        str(args.stage3_v2_prod_weights_path),
    ]
    print(f"\nInvoking {' '.join(cmd)}")
    proc = subprocess.run(
        cmd,
        cwd=str(_constants.PROJECT_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if proc.stdout:
        print(proc.stdout)
    if proc.returncode != 0:
        print(
            f"\nERROR promote_team_offense_v2.py exited {proc.returncode}",
            file=sys.stderr,
        )
        write_promotion_event(
            PromotionEvent(
                lever="stage3_v2",
                action="blocked",
                direction="promote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=f"subprocess exit {proc.returncode}",
                subprocess_returncode=proc.returncode,
                backup_path=str(backup) if backup else None,
            ),
            log_path=args.event_log_path,
        )
        return proc.returncode

    print(
        f"\nPROMOTED Stage-3 v2: production weights updated"
        + (f"\n  prior production backed up to {backup.name}" if backup else "\n  (no prior production -- was using compiled defaults)")
    )
    write_promotion_event(
        PromotionEvent(
            lever="stage3_v2",
            action="forced" if (verdict["verdict"] != "promote" and args.force) else "promoted",
            direction="promote",
            operator=operator,
            verdict_snapshot=verdict,
            from_state={"active_betas": active_betas, "active_source": active_source},
            to_state={"research_betas": research_betas},
            subprocess_returncode=proc.returncode,
            backup_path=str(backup) if backup else None,
            source_artifact_lineage=_capture_artifact_lineage(args.stage3_v2_research_fit_path),
            promotion_lineage=_compute_promotion_lineage(),
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            "Restart `live_engine.py` (or wait for next session) so TeamOffenseModel reloads.",
            "Verify the startup log line: 'TeamOffenseModel loaded ... betas=(prior=... season=... mom10=...)' matches the new betas.",
            "Tomorrow's Stage-3 v2 promotion-readiness verdict should drop to 'insufficient_history' (history dedupes by date; the swap resets the drift baseline).",
            f"Audit trail: see {args.event_log_path} for this promotion's row.",
        ]
    )
    return 0


def cmd_demote_stage3_v2(args: argparse.Namespace) -> int:
    _print_header("Demote Stage-3 v2: restore prior production weights from backup")
    operator = _resolve_operator(args.operator)
    events = load_promotion_events(args.event_log_path)
    verdict = stage3_v2_demotion_verdict(
        events=events, sessions_dir=args.sessions_dir,
    )
    _print_demotion_verdict_block(verdict)

    gate = _demote_verdict_gate(
        verdict=verdict, args=args, operator=operator, lever="stage3_v2",
    )
    if gate is not None:
        return gate

    pe = verdict.get("promotion_event") or {}
    backup_str = pe.get("backup_path")
    backup_path: Optional[Path] = Path(backup_str) if backup_str else None
    prod_path = args.stage3_v2_prod_weights_path

    if args.dry_run:
        if backup_path:
            print(f"\nDRY-RUN would restore {backup_path.name} -> {prod_path.name}")
        else:
            print(
                f"\nDRY-RUN would delete {prod_path.name} "
                "(no prior backup; runtime would fall back to compiled defaults)"
            )
        write_promotion_event(
            PromotionEvent(
                lever="stage3_v2",
                action="dry_run",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                from_state={"production_path": str(prod_path)},
                to_state={"restored_from": str(backup_path) if backup_path else "compiled_defaults"},
            ),
            log_path=args.event_log_path,
        )
        return 0

    try:
        if backup_path and backup_path.exists():
            _atomic_copy(backup_path, prod_path)
            print(f"\nDEMOTED Stage-3 v2: restored {backup_path.name} -> {prod_path.name}")
        else:
            if prod_path.exists():
                prod_path.unlink()
            print(
                f"\nDEMOTED Stage-3 v2: removed {prod_path.name} "
                "(no backup found; runtime falls back to compiled defaults)"
            )
    except OSError as exc:
        print(f"\nERROR demote file action failed: {exc!r}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="stage3_v2",
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
            lever="stage3_v2",
            action="forced" if (verdict.get("verdict") != "demote" and args.force) else "demoted",
            direction="demote",
            operator=operator,
            verdict_snapshot=verdict,
            from_state={"production_path": str(prod_path)},
            to_state={"restored_from": str(backup_path) if backup_path else "compiled_defaults"},
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            "Restart `live_engine.py` so TeamOffenseModel re-loads (will use restored weights, or compiled defaults if no backup).",
            "Verify startup log line: 'TeamOffenseModel loaded ... betas=...' matches expected.",
            f"Audit trail: see {args.event_log_path} for this demotion's row.",
        ]
    )
    return 0
