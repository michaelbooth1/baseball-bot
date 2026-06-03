"""Gate-threshold promote + demote handlers.

`promote gate-threshold <gate> <value>` writes a new per-gate threshold
to the live overrides file, gated on the walk-forward certification's
RETUNE / RETIRE verdict for that gate. `demote gate-threshold <gate>`
removes the override key (or replaces with --to-value when set).
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
from .demotion import gate_threshold_demotion_verdict
from .events import (
    PromotionEvent,
    _resolve_operator,
    load_promotion_events,
    write_promotion_event,
)
from .gate_helpers import _gate_cli_flag, _parse_gate_value
from .output import _print_block, _print_checklist, _print_header


def cmd_gate_threshold(args: argparse.Namespace) -> int:
    _print_header(f"Promote gate-threshold change: {args.gate_name} -> {args.new_value}")
    operator = _resolve_operator(args.operator)
    payload, err = _safe_load_json(args.walk_forward_cert_path)
    if err or not isinstance(payload, dict):
        print(
            f"\nERROR could not read walk-forward certification at "
            f"{args.walk_forward_cert_path}: {err}",
            file=sys.stderr,
        )
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="blocked",
                operator=operator,
                block_reason=f"walk-forward cert unreadable: {err}",
            ),
            log_path=args.event_log_path,
        )
        return 2

    gates = payload.get("gates") or []
    gate_entry = next(
        (g for g in gates if (g.get("name") or "") == args.gate_name), None
    )
    if gate_entry is None:
        msg = f"gate '{args.gate_name}' not found in walk-forward certification"
        print(f"\nERROR {msg}", file=sys.stderr)
        print(
            "  Available gates: "
            + ", ".join(g.get("name") or "?" for g in gates)
        )
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="blocked",
                operator=operator,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 2

    gate_verdict = gate_entry.get("verdict") or {}
    verdict_label = str(gate_verdict.get("verdict") or "").upper()
    current_threshold = gate_entry.get("current_threshold")
    recommended = gate_verdict.get("recommended_threshold")
    readiness = (payload.get("readiness") or {}).get("label", "<unknown>")
    _print_block("readiness", readiness)
    _print_block("gate verdict", verdict_label)
    _print_block("current threshold", current_threshold)
    _print_block("recommended threshold", recommended)
    _print_block("reason", (gate_verdict.get("reason") or "").strip()[:200])

    if verdict_label not in ("RETUNE", "RETIRE") and not args.force:
        msg = (
            f"gate verdict is '{verdict_label}', not 'RETUNE' / 'RETIRE'; "
            "refusing to recommend change (use --force to override)"
        )
        print(f"\nBLOCKED {msg}")
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="blocked",
                operator=operator,
                verdict_snapshot={
                    "gate_name": args.gate_name,
                    "gate_verdict": gate_verdict,
                    "current_threshold": current_threshold,
                    "readiness": readiness,
                },
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 1

    # Map gate name to the live-engine CLI flag. Centralizing here so the
    # promote CLI is the one place a future agent maintains the binding.
    flag = _gate_cli_flag(args.gate_name)
    if flag is None:
        msg = (
            f"gate '{args.gate_name}' has no documented CLI flag mapping; "
            "extend `_gate_cli_flag` in promote.py before retuning."
        )
        print(f"\nERROR {msg}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="blocked",
                operator=operator,
                verdict_snapshot={"gate_name": args.gate_name, "gate_verdict": gate_verdict},
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 2

    print(f"\nRECOMMENDED CLI CHANGE")
    print(f"  Update the live-engine flag:")
    print(f"      {flag} {args.new_value}")
    print(f"  (replacing the prior value; current threshold is {current_threshold})")
    print()
    print(f"  Note: this promote ALSO writes gate_thresholds.{args.gate_name}={args.new_value}")
    print(f"  to {args.live_overrides_path}; live engine reads it on startup.")

    if args.dry_run:
        print("\nDRY-RUN no overrides written, no event recorded.")
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="dry_run",
                operator=operator,
                verdict_snapshot={
                    "gate_name": args.gate_name,
                    "gate_verdict": gate_verdict,
                    "readiness": readiness,
                },
                from_state={"threshold": current_threshold},
                to_state={"threshold": args.new_value, "cli_flag": flag},
            ),
            log_path=args.event_log_path,
        )
        return 0

    backup_path: Optional[Path] = None
    try:
        typed_value = _parse_gate_value(args.gate_name, args.new_value)
        backup_path, _payload = _live_overrides_set(
            path=args.live_overrides_path,
            operator=operator,
            gate_thresholds={args.gate_name: typed_value},
        )
    except (OSError, ValueError) as exc:
        msg = f"failed to write overrides file: {exc!r}"
        print(f"\nERROR {msg}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="blocked",
                operator=operator,
                verdict_snapshot={
                    "gate_name": args.gate_name,
                    "gate_verdict": gate_verdict,
                    "readiness": readiness,
                },
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 2

    write_promotion_event(
        PromotionEvent(
            lever="gate_threshold",
            side=getattr(args, "side", "over"),
            action="forced" if (verdict_label not in ("RETUNE", "RETIRE") and args.force) else "promoted",
            operator=operator,
            verdict_snapshot={
                "gate_name": args.gate_name,
                "gate_verdict": gate_verdict,
                "readiness": readiness,
            },
            from_state={"threshold": current_threshold},
            to_state={
                "threshold": args.new_value,
                "cli_flag": flag,
                "overrides_path": str(args.live_overrides_path),
            },
            backup_path=str(backup_path) if backup_path else None,
            source_artifact_lineage=_capture_artifact_lineage(args.walk_forward_cert_path),
            promotion_lineage=_compute_promotion_lineage(),
            notes="overrides file mutated; restart live engine to pick up",
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            f"Restart `live_engine.py` so it re-reads {args.live_overrides_path}.",
            f"Next live session will enforce {args.gate_name} at {args.new_value}.",
            "Re-check walk-forward certification after ~7 sessions: the gate's blocked-cohort "
            "ROI should converge as the new threshold takes effect.",
            f"Audit trail: see {args.event_log_path} for this promotion's row.",
        ]
    )
    return 0


def cmd_demote_gate_threshold(args: argparse.Namespace) -> int:
    _print_header(f"Demote gate-threshold: revert {args.gate_name}")
    operator = _resolve_operator(args.operator)
    events = load_promotion_events(args.event_log_path)
    verdict = gate_threshold_demotion_verdict(
        events=events, sessions_dir=args.sessions_dir,
    )
    _print_demotion_verdict_block(verdict)

    # The lever-level promotion event isn't gate-name-specific (we log
    # all gate-threshold changes under lever="gate_threshold"); the
    # promotion event's from_state.threshold IS the value to revert to.
    pe = verdict.get("promotion_event") or {}
    from_state = pe.get("from_state") or {}
    prior_threshold = from_state.get("threshold")
    if prior_threshold is None and not args.force:
        msg = "promotion event has no prior threshold to revert to (--force to specify --to-value)"
        print(f"\nBLOCKED {msg}")
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="blocked",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 1

    gate = _demote_verdict_gate(
        verdict=verdict, args=args, operator=operator, lever="gate_threshold",
    )
    if gate is not None:
        return gate

    flag = _gate_cli_flag(args.gate_name)
    if flag is None:
        msg = (
            f"gate '{args.gate_name}' has no CLI flag mapping; "
            "extend `_gate_cli_flag` in promote.py before reverting."
        )
        print(f"\nERROR {msg}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
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

    revert_value = args.to_value if args.to_value is not None else prior_threshold
    print(f"\nRECOMMENDED CLI CHANGE")
    print(f"  Update the live-engine flag back to:")
    print(f"      {flag} {revert_value}")
    print(f"  (replacing the post-promotion value)")
    print()
    print(f"  Note: this demote ALSO removes gate_thresholds.{args.gate_name}")
    print(f"  from {args.live_overrides_path}. With `--to-value` set, the override")
    print(f"  is instead replaced with the explicit revert value.")

    if args.dry_run:
        print("\nDRY-RUN no overrides changed, no event recorded.")
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
                side=getattr(args, "side", "over"),
                action="dry_run",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                from_state={"gate_name": args.gate_name},
                to_state={"threshold": revert_value, "cli_flag": flag},
            ),
            log_path=args.event_log_path,
        )
        return 0

    backup_path: Optional[Path] = None
    try:
        if args.to_value is not None:
            # Operator-specified revert value: write it as the new override.
            typed_value = _parse_gate_value(args.gate_name, args.to_value)
            backup_path, _payload = _live_overrides_set(
                path=args.live_overrides_path,
                operator=operator,
                gate_thresholds={args.gate_name: typed_value},
            )
        else:
            # Default revert: drop the override key so the engine falls
            # back to argparse defaults (or the prior_threshold the
            # operator's saved command supplies).
            backup_path, _payload = _live_overrides_remove(
                path=args.live_overrides_path,
                operator=operator,
                gate_threshold_keys=[args.gate_name],
            )
    except (OSError, ValueError) as exc:
        msg = f"failed to update overrides file: {exc!r}"
        print(f"\nERROR {msg}", file=sys.stderr)
        write_promotion_event(
            PromotionEvent(
                lever="gate_threshold",
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
            lever="gate_threshold",
            side=getattr(args, "side", "over"),
            action="forced" if (verdict.get("verdict") != "demote" and args.force) else "demoted",
            direction="demote",
            operator=operator,
            verdict_snapshot=verdict,
            from_state={"gate_name": args.gate_name},
            to_state={
                "threshold": revert_value,
                "cli_flag": flag,
                "overrides_path": str(args.live_overrides_path),
            },
            backup_path=str(backup_path) if backup_path else None,
            notes="overrides file mutated; restart live engine to pick up",
        ),
        log_path=args.event_log_path,
    )
    _print_checklist(
        [
            f"Restart `live_engine.py` so it re-reads {args.live_overrides_path}.",
            f"Next live session will enforce {args.gate_name} at {revert_value}.",
            f"Audit trail: see {args.event_log_path} for this demotion's row.",
        ]
    )
    return 0
