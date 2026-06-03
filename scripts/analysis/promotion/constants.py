"""Constants for the promotion CLI package.

Path defaults, lever registry, demote thresholds, and the
PROJECT_DIR anchor. Extracted from promote.py during the 2026-06-01
refactor. All names are re-exported from the package's __init__
and from the scripts.analysis.promote shim.
"""
from __future__ import annotations

import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[3]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


# Re-exported from scripts.analysis.run_daily_refresh for back-compat;
# callers historically imported these from `promote`.
from scripts.analysis.run_daily_refresh import (  # noqa: E402, F401
    DEFAULT_STAGE2_BRIER_HISTORY_PATH,
    DEFAULT_STAGE2_CACHE_PATH,
    DEFAULT_STAGE3_V2_DRIFT_HISTORY_PATH,
    DEFAULT_STAGE3_V2_PROD_WEIGHTS_PATH,
    DEFAULT_STAGE3_V2_RESEARCH_FIT_PATH,
)
from scripts.trading.live_engine_overrides import (  # noqa: E402, F401
    DEFAULT_OVERRIDES_PATH as DEFAULT_LIVE_ENGINE_OVERRIDES_PATH,
)


DEFAULT_STAGE2_STAGING_PATH = PROJECT_DIR / "cache" / "mlb_stage2_run_env.staging.json"

# Stage-1 cache promotion (2026-05-17). The Stage-1 cache builder
# currently writes directly to production (cache/mlb_ou_cache.json) on
# every refresh, so there's no automated staging path the way Stage-2
# has. The `promote stage1` subcommand exists for two reasons:
#   (1) Operator-driven rebuilds (a hand-built Stage-1 cache the
#       operator wants to swap in atomically with backup, audit-logged
#       and lineage-stamped just like Stage-2/Stage-3 promotions).
#   (2) Active #8 prep -- when a future Stage-1 rebuild ships behind
#       a feature flag (Alt A empirical-when-available, or a deeper
#       cache rebuild), the swap needs to flow through the same
#       auditable promote.py path.
# Default `--stage1-source-path` points at a staging file that the
# builder may write to in the future; today it's an opt-in path the
# operator chooses with `--stage1-source-path PATH`.
DEFAULT_STAGE1_CACHE_PATH = PROJECT_DIR / "cache" / "mlb_ou_cache.json"
DEFAULT_STAGE1_STAGING_PATH = PROJECT_DIR / "cache" / "mlb_ou_cache.staging.json"
DEFAULT_PROMOTION_EVENTS_LOG = (
    PROJECT_DIR / "data" / "analysis_output" / "promotion_events.jsonl"
)
DEFAULT_STAKE_SCALING_REPORT_PATH = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "stake_scaling_analysis"
    / "stake_scaling_analysis.json"
)
DEFAULT_WALK_FORWARD_CERT_PATH = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "walk_forward_certification"
    / "walk_forward_certification.json"
)
DEFAULT_PROMOTE_TEAM_OFFENSE_SCRIPT = (
    PROJECT_DIR / "scripts" / "analysis" / "promote_team_offense_v2.py"
)


# 2026-05-23: registry of known lever names. File-swap levers
# (stage2, stage3_v2) and the CLI-mutation levers promote.py owns
# directly (stake_scaling, gate_threshold) plus CLI-flag levers
# operator flips manually with backfill stubs (prob_calibration,
# stage1_alt_a_scope). Add new lever names HERE before referencing
# them in PromotionEvent or backfill scripts -- the backfill script
# asserts membership.
KNOWN_LEVERS = frozenset({
    "stage2",
    "stage3_v2",
    "stake_scaling",
    "gate_threshold",
    "prob_calibration",
    "stage1_alt_a_scope",
})


# Active #14 (2026-05-17): backup retention. Each promotion writes a
# single backup at `<file>.prior_promote.json` (the rolling latest
# that demote restores from). To keep history beyond the most-recent
# promotion, the OLD backup (if any) is rotated into a sibling
# `<file>.prior_promote_archive/` directory under a timestamped
# filename BEFORE the new backup is written. The archive directory
# is then GC'd to the most-recent BACKUP_ARCHIVE_KEEP entries so it
# doesn't grow unbounded across many promotions.
BACKUP_ARCHIVE_KEEP = 5


# Demote infrastructure thresholds.
DEMOTE_PRE_POST_WINDOW_DAYS = 14
FAST_DEMOTE_MIN_POST_FILLS = 20
FAST_DEMOTE_Z = 1.645              # one-sided 95% confidence
FAST_DEMOTE_GRACE_DAYS = 1         # don't fire same-day as promotion
DEMOTE_MIN_FILLED_PER_WINDOW = 10
DEMOTE_ROI_REGRESSION_THRESHOLD = -0.10
DEFAULT_SESSIONS_DIR = PROJECT_DIR / "data" / "live_trading" / "sessions"
# Window for "promotion happened recently" attribution on drift alerts.
PROMOTION_ATTRIBUTION_WINDOW_DAYS = 14
