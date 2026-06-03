"""CLI parser + dispatcher for the unified promotion CLI.

Subcommand layout:
  promote.py status
  promote.py stage1          [--dry-run] [--force]
  promote.py stage2          [--dry-run] [--force]
  promote.py stage3-v2       [--dry-run] [--force]
  promote.py stake-scaling   [--dry-run] [--force] [--side over|under|both]
  promote.py gate-threshold <gate> <value> [--dry-run] [--force] [--side ...]
  promote.py demote {stage1|stage2|stage3-v2|stake-scaling|gate-threshold} ...
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import constants as _constants
from .cmd_gate_threshold import cmd_demote_gate_threshold, cmd_gate_threshold
from .cmd_stage1 import cmd_demote_stage1, cmd_stage1
from .cmd_stage2 import cmd_demote_stage2, cmd_stage2
from .cmd_stage3_v2 import cmd_demote_stage3_v2, cmd_stage3_v2
from .cmd_stake_scaling import cmd_demote_stake_scaling, cmd_stake_scaling
from .cmd_status import cmd_status


def _add_common_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dry-run", action="store_true", help="Print planned action without performing it.")
    p.add_argument(
        "--force",
        action="store_true",
        help="Proceed even when the verdict is not 'promote'/'RETUNE'. Logs action as 'forced'.",
    )
    p.add_argument("--operator", type=str, default=None, help="Operator label for the event log (defaults to $USER).")
    p.add_argument(
        "--event-log-path",
        type=Path,
        default=_constants.DEFAULT_PROMOTION_EVENTS_LOG,
        help="Path to promotion events log (append-only JSONL).",
    )


def _add_side_flag(p: argparse.ArgumentParser) -> None:
    """Add --side {over,under,both} for levers whose effect is side-asymmetric.

    Phase B B2 (2026-05-16). stage2 + stage3-v2 are side-symmetric so
    they don't get this flag (the audit row hard-codes side='both').
    stake-scaling and gate-threshold flip the live engine's runtime
    behavior on the Over side today; Phase C will add Under counterparts
    that record side='under'.
    """
    p.add_argument(
        "--side",
        choices=["over", "under", "both"],
        default="over",
        help=(
            "Which side this promotion affects. Defaults to 'over' for "
            "today's levers (the live engine is Over-only). Phase C "
            "introduces UNDER actuation; once those land, operators "
            "pass --side under explicitly. The audit log carries this "
            "field so daemon retrospective + drift attribution can "
            "filter by side."
        ),
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="promote.py",
        description="Unified promotion CLI for the four manual self-improvement levers.",
    )
    sub = p.add_subparsers(dest="lever", required=True)

    # status
    p_status = sub.add_parser("status", help="Read all promotion verdicts and print one summary.")
    p_status.add_argument(
        "--stage1-source-path",
        type=Path,
        default=_constants.DEFAULT_STAGE1_STAGING_PATH,
    )
    p_status.add_argument(
        "--stage1-cache-path",
        type=Path,
        default=_constants.DEFAULT_STAGE1_CACHE_PATH,
    )
    p_status.add_argument(
        "--stage2-brier-history-path",
        type=Path,
        default=_constants.DEFAULT_STAGE2_BRIER_HISTORY_PATH,
    )
    p_status.add_argument(
        "--stage3-v2-drift-history-path",
        type=Path,
        default=_constants.DEFAULT_STAGE3_V2_DRIFT_HISTORY_PATH,
    )
    p_status.add_argument(
        "--stake-scaling-report-path",
        type=Path,
        default=_constants.DEFAULT_STAKE_SCALING_REPORT_PATH,
    )
    p_status.add_argument(
        "--walk-forward-cert-path",
        type=Path,
        default=_constants.DEFAULT_WALK_FORWARD_CERT_PATH,
    )
    # Status also reads the audit log + session bets to compute demotion
    # verdicts (post-promotion outcome regression). Defaults match the
    # demote subcommands so behaviour is consistent across the surface.
    p_status.add_argument(
        "--event-log-path",
        type=Path,
        default=_constants.DEFAULT_PROMOTION_EVENTS_LOG,
    )
    p_status.add_argument(
        "--sessions-dir",
        type=Path,
        default=_constants.DEFAULT_SESSIONS_DIR,
    )
    p_status.set_defaults(handler=cmd_status)

    # stage2
    p_s2 = sub.add_parser("stage2", help="Promote Stage-2 staging cache -> production cache.")
    # stage1
    p_s1 = sub.add_parser(
        "stage1",
        help=(
            "Promote a Stage-1 cache source file -> production with backup. "
            "Verdict gates on source existence + lineage freshness."
        ),
    )
    _add_common_flags(p_s1)
    p_s1.add_argument(
        "--stage1-source-path",
        type=Path,
        default=_constants.DEFAULT_STAGE1_STAGING_PATH,
        help=(
            "Path to the candidate Stage-1 cache to promote. Default: "
            "cache/mlb_ou_cache.staging.json (operator-supplied; the "
            "automated refresh does not write there today)."
        ),
    )
    p_s1.add_argument(
        "--stage1-cache-path",
        type=Path,
        default=_constants.DEFAULT_STAGE1_CACHE_PATH,
        help="Production Stage-1 cache path (the runtime loads this).",
    )
    p_s1.set_defaults(handler=cmd_stage1)

    _add_common_flags(p_s2)
    p_s2.add_argument(
        "--stage2-brier-history-path",
        type=Path,
        default=_constants.DEFAULT_STAGE2_BRIER_HISTORY_PATH,
    )
    p_s2.add_argument(
        "--stage2-staging-path",
        type=Path,
        default=_constants.DEFAULT_STAGE2_STAGING_PATH,
    )
    p_s2.add_argument(
        "--stage2-cache-path",
        type=Path,
        default=_constants.DEFAULT_STAGE2_CACHE_PATH,
    )
    p_s2.set_defaults(handler=cmd_stage2)

    # stage3-v2
    p_s3 = sub.add_parser("stage3-v2", help="Promote Stage-3 v2 research fit -> production weights.")
    _add_common_flags(p_s3)
    p_s3.add_argument(
        "--stage3-v2-drift-history-path",
        type=Path,
        default=_constants.DEFAULT_STAGE3_V2_DRIFT_HISTORY_PATH,
    )
    p_s3.add_argument(
        "--stage3-v2-research-fit-path",
        type=Path,
        default=_constants.DEFAULT_STAGE3_V2_RESEARCH_FIT_PATH,
    )
    p_s3.add_argument(
        "--stage3-v2-prod-weights-path",
        type=Path,
        default=_constants.DEFAULT_STAGE3_V2_PROD_WEIGHTS_PATH,
    )
    p_s3.add_argument(
        "--promote-team-offense-script",
        type=Path,
        default=_constants.DEFAULT_PROMOTE_TEAM_OFFENSE_SCRIPT,
    )
    p_s3.set_defaults(handler=cmd_stage3_v2)

    # stake-scaling
    p_ss = sub.add_parser("stake-scaling", help="Promote stake-scaling shadow -> enforce.")
    _add_common_flags(p_ss)
    _add_side_flag(p_ss)
    p_ss.add_argument(
        "--stake-scaling-report-path",
        type=Path,
        default=_constants.DEFAULT_STAKE_SCALING_REPORT_PATH,
    )
    p_ss.add_argument(
        "--live-overrides-path",
        type=Path,
        default=_constants.DEFAULT_LIVE_ENGINE_OVERRIDES_PATH,
        help="Runtime overrides file the live engine reads on startup.",
    )
    p_ss.set_defaults(handler=cmd_stake_scaling)

    # gate-threshold
    p_gt = sub.add_parser(
        "gate-threshold",
        help="Change a per-gate threshold based on walk-forward certification's RETUNE.",
    )
    _add_common_flags(p_gt)
    _add_side_flag(p_gt)
    p_gt.add_argument("gate_name", help="Gate name (e.g. gate_extreme_edge, gate_min_edge).")
    p_gt.add_argument("new_value", help="New threshold value (passed as-is to the CLI flag).")
    p_gt.add_argument(
        "--walk-forward-cert-path",
        type=Path,
        default=_constants.DEFAULT_WALK_FORWARD_CERT_PATH,
    )
    p_gt.add_argument(
        "--live-overrides-path",
        type=Path,
        default=_constants.DEFAULT_LIVE_ENGINE_OVERRIDES_PATH,
        help="Runtime overrides file the live engine reads on startup.",
    )
    p_gt.set_defaults(handler=cmd_gate_threshold)

    # ---- demote: nested subcommand parser, mirrors promote shape ----
    p_demote = sub.add_parser(
        "demote",
        help="Roll back a prior promotion (mirror of promote subcommands).",
    )
    demote_sub = p_demote.add_subparsers(dest="demote_lever", required=True)

    p_d_s1 = demote_sub.add_parser(
        "stage1",
        help="Restore prior Stage-1 production cache from backup.",
    )
    _add_common_flags(p_d_s1)
    p_d_s1.add_argument(
        "--stage1-cache-path",
        type=Path,
        default=_constants.DEFAULT_STAGE1_CACHE_PATH,
    )
    p_d_s1.add_argument(
        "--sessions-dir",
        type=Path,
        default=_constants.DEFAULT_SESSIONS_DIR,
        help="Live sessions directory (read for pre/post-promotion ROI comparison).",
    )
    p_d_s1.set_defaults(handler=cmd_demote_stage1)

    p_d_s2 = demote_sub.add_parser("stage2", help="Restore prior Stage-2 production cache from backup.")
    _add_common_flags(p_d_s2)
    p_d_s2.add_argument(
        "--stage2-cache-path",
        type=Path,
        default=_constants.DEFAULT_STAGE2_CACHE_PATH,
    )
    p_d_s2.add_argument(
        "--sessions-dir",
        type=Path,
        default=_constants.DEFAULT_SESSIONS_DIR,
        help="Live sessions directory (read for pre/post-promotion ROI comparison).",
    )
    p_d_s2.set_defaults(handler=cmd_demote_stage2)

    p_d_s3 = demote_sub.add_parser("stage3-v2", help="Restore prior Stage-3 v2 production weights from backup.")
    _add_common_flags(p_d_s3)
    p_d_s3.add_argument(
        "--stage3-v2-prod-weights-path",
        type=Path,
        default=_constants.DEFAULT_STAGE3_V2_PROD_WEIGHTS_PATH,
    )
    p_d_s3.add_argument(
        "--sessions-dir",
        type=Path,
        default=_constants.DEFAULT_SESSIONS_DIR,
    )
    p_d_s3.set_defaults(handler=cmd_demote_stage3_v2)

    p_d_ss = demote_sub.add_parser("stake-scaling", help="Demote stake-scaling enforce -> shadow.")
    _add_common_flags(p_d_ss)
    _add_side_flag(p_d_ss)
    p_d_ss.add_argument(
        "--sessions-dir",
        type=Path,
        default=_constants.DEFAULT_SESSIONS_DIR,
    )
    p_d_ss.add_argument(
        "--live-overrides-path",
        type=Path,
        default=_constants.DEFAULT_LIVE_ENGINE_OVERRIDES_PATH,
        help="Runtime overrides file the live engine reads on startup.",
    )
    p_d_ss.set_defaults(handler=cmd_demote_stake_scaling)

    p_d_gt = demote_sub.add_parser("gate-threshold", help="Revert a per-gate threshold change.")
    _add_common_flags(p_d_gt)
    _add_side_flag(p_d_gt)
    p_d_gt.add_argument("gate_name", help="Gate name (e.g. gate_extreme_edge).")
    p_d_gt.add_argument(
        "--to-value", type=str, default=None,
        help="Threshold to revert to. Default: prior_threshold from the promotion event's from_state.",
    )
    p_d_gt.add_argument(
        "--sessions-dir",
        type=Path,
        default=_constants.DEFAULT_SESSIONS_DIR,
    )
    p_d_gt.add_argument(
        "--live-overrides-path",
        type=Path,
        default=_constants.DEFAULT_LIVE_ENGINE_OVERRIDES_PATH,
        help="Runtime overrides file the live engine reads on startup.",
    )
    p_d_gt.set_defaults(handler=cmd_demote_gate_threshold)

    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        print("no handler attached to args", file=sys.stderr)
        return 64
    return int(handler(args) or 0)
