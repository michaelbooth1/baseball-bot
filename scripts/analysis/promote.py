#!/usr/bin/env python3
"""Unified promotion CLI for the four manual self-improvement levers.

The daily refresh's stability gates compute "PROMOTION READY" verdicts
on Stage-2 (Brier diff stability), Stage-3 v2 (research-vs-prod beta
drift), stake-scaling (high-vs-low cohort) and per-gate threshold
(walk-forward certification's KEEP/RETUNE/RETIRE). Each subcommand:

  1. Reads the relevant verdict file built by `run_daily_refresh.py`.
  2. Refuses to promote unless the verdict says go (or `--force`).
  3. Performs the swap (file copy / subprocess / runtime override).
  4. Writes a row to `data/analysis_output/promotion_events.jsonl`.
  5. Prints a next-action checklist for the operator.

Closes the gap between "system says go" and "system actually goes"
without removing human consent.

2026-06-01 refactor: implementation moved to ``scripts.analysis.promotion``
as a package (constants / events / output / backups / demotion /
fast_demotion / stage1_verdict / gate_helpers / demote_helpers /
cmd_* / cli). This file is a back-compat shim that re-exports every
previously-public name so existing callers (the daemon, retrospective,
human_review/core_health, tests) keep working without import changes.
It also stays a real `.py` file with a working `if __name__ ==
"__main__"` so `subprocess.run([python, "scripts/analysis/promote.py",
...])` from auto_promote_demote_daemon.py keeps working.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Direct `python scripts/analysis/promote.py status` invocation (the way
# auto_promote_demote_daemon.py launches us via subprocess) needs the
# project root on sys.path before we can import the `scripts` package.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# `subprocess` is re-exported as `promote.subprocess` so existing tests
# that patch `promote.subprocess.run` (to mock the Stage-3 v2 promote
# script invocation) keep working. Both this name and the import inside
# scripts/analysis/promotion/cmd_stage3_v2.py refer to the same stdlib
# module object, so monkeypatching attributes on either side affects
# the actual call.
import subprocess  # noqa: F401, E402

# Public constants + dataclasses + entry points.
from scripts.analysis.promotion import (  # noqa: F401
    BACKUP_ARCHIVE_KEEP,
    DEFAULT_LIVE_ENGINE_OVERRIDES_PATH,
    DEFAULT_PROMOTE_TEAM_OFFENSE_SCRIPT,
    DEFAULT_PROMOTION_EVENTS_LOG,
    DEFAULT_SESSIONS_DIR,
    DEFAULT_STAGE1_CACHE_PATH,
    DEFAULT_STAGE1_STAGING_PATH,
    DEFAULT_STAGE2_BRIER_HISTORY_PATH,
    DEFAULT_STAGE2_CACHE_PATH,
    DEFAULT_STAGE2_STAGING_PATH,
    DEFAULT_STAGE3_V2_DRIFT_HISTORY_PATH,
    DEFAULT_STAGE3_V2_PROD_WEIGHTS_PATH,
    DEFAULT_STAGE3_V2_RESEARCH_FIT_PATH,
    DEFAULT_STAKE_SCALING_REPORT_PATH,
    DEFAULT_WALK_FORWARD_CERT_PATH,
    DEMOTE_MIN_FILLED_PER_WINDOW,
    DEMOTE_PRE_POST_WINDOW_DAYS,
    DEMOTE_ROI_REGRESSION_THRESHOLD,
    FAST_DEMOTE_GRACE_DAYS,
    FAST_DEMOTE_MIN_POST_FILLS,
    FAST_DEMOTE_Z,
    KNOWN_LEVERS,
    PROJECT_DIR,
    PROMOTION_ATTRIBUTION_WINDOW_DAYS,
    PromotionEvent,
    cmd_demote_gate_threshold,
    cmd_demote_stage1,
    cmd_demote_stage2,
    cmd_demote_stage3_v2,
    cmd_demote_stake_scaling,
    cmd_gate_threshold,
    cmd_stage1,
    cmd_stage2,
    cmd_stage3_v2,
    cmd_stake_scaling,
    cmd_status,
    gate_threshold_demotion_verdict,
    gate_threshold_fast_demote_verdict,
    latest_promotion_event_for_lever,
    load_promotion_events,
    main,
    parse_args,
    stage1_demotion_verdict,
    stage1_fast_demote_verdict,
    stage2_demotion_verdict,
    stage2_fast_demote_verdict,
    stage3_v2_demotion_verdict,
    stage3_v2_fast_demote_verdict,
    stake_scaling_demotion_verdict,
    stake_scaling_fast_demote_verdict,
    write_promotion_event,
)

# Private helpers that tests reach into directly. Re-exported because
# `from promotion import *` skips underscore-prefixed names.
from scripts.analysis.promotion import (  # noqa: F401
    _GATE_CLI_FLAGS,
    _GATE_VALUE_TYPE,
    _archive_dir_for,
    _atomic_copy,
    _backup_path,
    _backup_prior_production,
    _capture_artifact_lineage,
    _compute_promotion_lineage,
    _demote_verdict_gate,
    _demotion_verdict_from_summaries,
    _fast_wilson_demote_from_post_bets,
    _filled_bets_in_window,
    _gate_cli_flag,
    _gc_archive_backups,
    _list_archive_backups,
    _list_session_dates_in_window,
    _now_iso,
    _parse_gate_value,
    _parse_iso_date,
    _per_lever_demotion_verdict,
    _per_lever_fast_demote_verdict,
    _print_block,
    _print_checklist,
    _print_demotion_verdict_block,
    _print_header,
    _read_lineage_built_at,
    _resolve_operator,
    _rotate_existing_backup_to_archive,
    _shift_date,
    _stage1_promotion_verdict,
    _stake_scaling_bet_filter,
    _summarize_filled_bets,
    _wilson_upper_bound,
)


# Re-export run_daily_refresh helpers that the daemon + retrospective
# scripts access as `promote.X` (back-compat: the original promote.py
# imported these at module scope, making them attribute-accessible on
# `import promote`).
from scripts.analysis.run_daily_refresh import (  # noqa: F401, E402
    _load_stage2_brier_history,
    _load_stage3_v2_drift_history,
    _safe_load_json,
    _stage2_promotion_verdict,
    _stage3_v2_promotion_verdict,
)


if __name__ == "__main__":
    raise SystemExit(main())
