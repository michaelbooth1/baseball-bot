"""Shared helpers for the 4 cmd_demote_* commands.

Verdict-block printing + the common "refuse unless verdict=='demote'
(or --force)" gate. Both consume a verdict dict + an argparse Namespace.
"""
from __future__ import annotations

import argparse
from typing import Any, Dict, Optional

from .events import PromotionEvent, write_promotion_event
from .output import _print_block


def _print_demotion_verdict_block(verdict: Dict[str, Any]) -> None:
    _print_block("verdict", verdict["verdict"])
    pre = verdict.get("pre_window") or {}
    post = verdict.get("post_window") or {}
    if pre:
        _print_block(
            "pre window",
            f"n={pre.get('n_filled', 0)}  W/L={pre.get('wins', 0)}/{pre.get('losses', 0)}  "
            f"ROI={(pre.get('roi') or 0) * 100:+.1f}%  "
            f"profit=${pre.get('total_profit', 0):+.2f}",
        )
    if post:
        _print_block(
            "post window",
            f"n={post.get('n_filled', 0)}  W/L={post.get('wins', 0)}/{post.get('losses', 0)}  "
            f"ROI={(post.get('roi') or 0) * 100:+.1f}%  "
            f"profit=${post.get('total_profit', 0):+.2f}",
        )
    if verdict.get("roi_delta") is not None:
        _print_block("ROI delta", f"{verdict['roi_delta'] * 100:+.1f}pp (threshold: <= {verdict['regression_threshold'] * 100:+.0f}pp)")
    pe = verdict.get("promotion_event") or {}
    if pe:
        _print_block("promoted at", pe.get("generated_at_utc"))
        _print_block("promoted by", pe.get("operator"))


def _demote_verdict_gate(
    *,
    verdict: Dict[str, Any],
    args: argparse.Namespace,
    operator: str,
    lever: str,
) -> Optional[int]:
    """Common gate logic: refuse unless verdict=='demote' (or --force).
    Returns an exit code if the call should abort; None if it should
    proceed. Writes the appropriate audit row in either case."""
    label = verdict.get("verdict")
    if label == "no_promotion_to_demote" and not args.force:
        msg = "no recent promotion to demote (audit log has no promote/forced event for this lever)"
        print(f"\nBLOCKED {msg}")
        write_promotion_event(
            PromotionEvent(
                lever=lever,
                action="blocked",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 1
    # Either "demote" (windowed verdict) or "fast_demote" (Wilson UB)
    # is sufficient to proceed. The two checks are independent; firing
    # either means we have actionable evidence the policy is failing.
    if label not in {"demote", "fast_demote"} and not args.force:
        msg = (
            f"verdict is '{label}', not 'demote'/'fast_demote'; "
            "refusing to demote (use --force to override)"
        )
        print(f"\nBLOCKED {msg}")
        write_promotion_event(
            PromotionEvent(
                lever=lever,
                action="blocked",
                direction="demote",
                operator=operator,
                verdict_snapshot=verdict,
                block_reason=msg,
            ),
            log_path=args.event_log_path,
        )
        return 1
    return None
