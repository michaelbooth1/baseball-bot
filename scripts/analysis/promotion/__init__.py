"""Unified promotion CLI package.

Split out of scripts/analysis/promote.py during the 2026-06-01 refactor.
The legacy entry point `scripts/analysis/promote.py` is now a back-compat
shim that re-exports every previously-public name from this package.

Module layout:
- constants.py            DEFAULT_* paths, KNOWN_LEVERS, demote thresholds
- events.py               PromotionEvent dataclass + audit-log helpers
- output.py               _print_header / _print_block / _print_checklist
- backups.py              _atomic_copy + backup archive machinery
- demotion.py             window-based demotion verdicts (4 levers)
- fast_demotion.py        Wilson-UB fast demote verdicts (4 levers)
- stage1_verdict.py       Stage-1 promotion + demote verdicts
- gate_helpers.py         _GATE_CLI_FLAGS map + _gate_cli_flag
- demote_helpers.py       _print_demotion_verdict_block + _demote_verdict_gate
- cmd_status.py           cmd_status (all-verdict summary)
- cmd_stage1.py           cmd_stage1 + cmd_demote_stage1
- cmd_stage2.py           cmd_stage2 + cmd_demote_stage2
- cmd_stage3_v2.py        cmd_stage3_v2 + cmd_demote_stage3_v2
- cmd_stake_scaling.py    cmd_stake_scaling + cmd_demote_stake_scaling
- cmd_gate_threshold.py   cmd_gate_threshold + cmd_demote_gate_threshold
- cli.py                  parse_args + main + dispatch
"""
from __future__ import annotations

# Constants + dataclasses + path defaults.
from .constants import (  # noqa: F401
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
)

# Event log machinery.
from .events import (  # noqa: F401
    PromotionEvent,
    _now_iso,
    _resolve_operator,
    latest_promotion_event_for_lever,
    load_promotion_events,
    write_promotion_event,
)

# Output helpers (tests reach for these in some places).
from .output import _print_block, _print_checklist, _print_header  # noqa: F401

# Atomic copy + backup archive.
from .backups import (  # noqa: F401
    _archive_dir_for,
    _atomic_copy,
    _backup_path,
    _backup_prior_production,
    _capture_artifact_lineage,
    _compute_promotion_lineage,
    _gc_archive_backups,
    _list_archive_backups,
    _rotate_existing_backup_to_archive,
)

# Demotion verdict pipeline (windowed).
from .demotion import (  # noqa: F401
    _demotion_verdict_from_summaries,
    _filled_bets_in_window,
    _list_session_dates_in_window,
    _parse_iso_date,
    _per_lever_demotion_verdict,
    _shift_date,
    _stake_scaling_bet_filter,
    _summarize_filled_bets,
    gate_threshold_demotion_verdict,
    stage2_demotion_verdict,
    stage3_v2_demotion_verdict,
    stake_scaling_demotion_verdict,
)

# Fast Wilson-UB demote pipeline.
from .fast_demotion import (  # noqa: F401
    _fast_wilson_demote_from_post_bets,
    _per_lever_fast_demote_verdict,
    _wilson_upper_bound,
    gate_threshold_fast_demote_verdict,
    stage2_fast_demote_verdict,
    stage3_v2_fast_demote_verdict,
    stake_scaling_fast_demote_verdict,
)

# Stage-1 verdicts (separate file because the gating logic is different).
from .stage1_verdict import (  # noqa: F401
    _read_lineage_built_at,
    _stage1_promotion_verdict,
    stage1_demotion_verdict,
    stage1_fast_demote_verdict,
)

# Gate -> CLI flag map.
from .gate_helpers import (  # noqa: F401
    _GATE_CLI_FLAGS,
    _GATE_VALUE_TYPE,
    _gate_cli_flag,
    _parse_gate_value,
)

# Shared demote helpers.
from .demote_helpers import (  # noqa: F401
    _demote_verdict_gate,
    _print_demotion_verdict_block,
)

# Subcommand handlers.
from .cmd_status import cmd_status  # noqa: F401
from .cmd_stage1 import cmd_demote_stage1, cmd_stage1  # noqa: F401
from .cmd_stage2 import cmd_demote_stage2, cmd_stage2  # noqa: F401
from .cmd_stage3_v2 import cmd_demote_stage3_v2, cmd_stage3_v2  # noqa: F401
from .cmd_stake_scaling import cmd_demote_stake_scaling, cmd_stake_scaling  # noqa: F401
from .cmd_gate_threshold import cmd_demote_gate_threshold, cmd_gate_threshold  # noqa: F401

# CLI entry points.
from .cli import main, parse_args  # noqa: F401
