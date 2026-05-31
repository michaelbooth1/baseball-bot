"""Refresh configuration: dataclasses, default paths, threshold constants.

Extracted from run_daily_refresh.py during the 2026-05-31 refactor.
All previously-public names are re-exported from
scripts.analysis.run_daily_refresh for back-compat.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_SESSIONS_DIR = PROJECT_DIR / "data" / "live_trading" / "sessions"
DEFAULT_CANDIDATE_DIR = PROJECT_DIR / "data" / "live_trading" / "candidate_universe"
DEFAULT_LOG_DIR = PROJECT_DIR / "logs" / "real-logs"

# 2026-05-31: paper-only daily_human_review support. When a date has
# only paper session(s) (no live), the daily-review step builds
# against one of these paper roots so the operator still gets the
# rich health-block JSON instead of just session-only fallback.
DEFAULT_PAPER_REVIEW_ROOTS: Tuple[Tuple[Path, Path], ...] = (
    (
        PROJECT_DIR / "data" / "paper_trading" / "sessions",
        PROJECT_DIR / "data" / "paper_trading" / "candidate_universe",
    ),
    (
        PROJECT_DIR / "data" / "paper_A_current" / "sessions",
        PROJECT_DIR / "data" / "paper_A_current" / "candidate_universe",
    ),
)
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "startup_refresh"
DEFAULT_PITCHER_CACHE_PATH = PROJECT_DIR / "cache" / "pitcher_cache.json"
DEFAULT_STADIUM_WEATHER_METADATA_PATH = PROJECT_DIR / "data" / "reference" / "mlb_stadium_weather_metadata.json"
DEFAULT_WEATHER_CACHE_DIR = PROJECT_DIR / "cache" / "weather"
DEFAULT_TEAM_GAME_LOG_PATH = PROJECT_DIR / "cache" / "team_game_log.json"
DEFAULT_MLB_OU_CACHE_PATH = PROJECT_DIR / "cache" / "mlb_ou_cache.json"
DEFAULT_MLB_OU_CACHE_STAGING_PATH = PROJECT_DIR / "cache" / "mlb_ou_cache.staging.json"
# Auto-promotion guard: staging must be at least this fraction of production's
# game count. Catches a corrupted/partial scrape; legitimate growth always passes.
STAGE1_PROMOTE_MIN_GAMES_RATIO = 0.99
DEFAULT_STAGE2_CACHE_PATH = PROJECT_DIR / "cache" / "mlb_stage2_run_env.json"
DEFAULT_PARK_HR_FACTORS_PATH = PROJECT_DIR / "cache" / "park_hr_factors.json"
DEFAULT_ENV_PATH = PROJECT_DIR / ".env"

# 30-day post-TR20 window: TR20 deployed 2026-05-07. After this date, the
# pre-TR20 gate calibration audit (see model_improvements/handover_2026_05_07.txt
# section "Phase 6") is due so TR19 extreme_edge_max=0.22 can be re-tuned for the
# v2 Stage-3 edge distribution.
PHASE6_GATE_RECALIBRATION_DUE_DATE = "2026-06-07"
STAGE1_PRODUCTION_FULL_SEASONS = 5
STAGE1_LEAD_CANDIDATE_FULL_SEASONS = 4
STAGE1_RESEARCH_MAX_FULL_SEASONS = 10
STAGE1_HISTORY_FULL_SEASONS = STAGE1_PRODUCTION_FULL_SEASONS

# Stage-2 promotion stability gate.
DEFAULT_STAGE2_BRIER_HISTORY_PATH = (
    PROJECT_DIR / "data" / "analysis_output" / "calibration" / "stage2_brier_history.jsonl"
)
STALE_MODEL_AGE_DAYS = 30
STAGE2_BRIER_DRIFT_THRESHOLD = 0.001  # 0.1pp Brier change is meaningful at our scale
STAGE2_PROMOTION_WINDOW = 7
STAGE2_PROMOTION_MIN_HISTORY = 5
STAGE2_PROMOTION_MIN_CONSECUTIVE = 5
STAGE2_PROMOTION_MIN_DELTA = 0.001  # staging must beat prod by >= this each day

# Stage-3 v2 promotion stability gate.
DEFAULT_STAGE3_V2_DRIFT_HISTORY_PATH = (
    PROJECT_DIR / "data" / "analysis_output" / "calibration" / "stage3_v2_drift_history.jsonl"
)
DEFAULT_STAGE3_V2_PROD_WEIGHTS_PATH = PROJECT_DIR / "cache" / "team_offense_v2_weights.json"
DEFAULT_STAGE3_V2_RESEARCH_FIT_PATH = (
    PROJECT_DIR / "data" / "analysis_output" / "team_offense_calibration" / "phase4_models.json"
)
# Compiled-in defaults from team_offense_model.py (must stay in sync). Used
# as the comparison baseline when the production weights file is missing.
STAGE3_V2_COMPILED_DEFAULTS = {
    "prior_season": -0.1514,
    "season_to_date": +0.1407,
    "momentum_10": +0.1503,
}
STAGE3_V2_PROMOTION_WINDOW = 7
STAGE3_V2_PROMOTION_MIN_HISTORY = 5
STAGE3_V2_PROMOTION_MIN_CONSECUTIVE = 5
STAGE3_V2_PROMOTION_DRIFT_THRESHOLD = 0.015

# Verdict-stability gate (shipped 2026-05-16).
STAGE3_V2_VERDICT_STABILITY_WINDOW = 7
STAGE3_V2_VERDICT_STABILITY_MIN_HISTORY = 5

SESSION_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})_session\.json$")
LOGGER = logging.getLogger("daily_refresh")


@dataclass(frozen=True)
class RefreshConfig:
    active_date: str
    max_date: str = ""
    include_run_date: bool = False
    strict: bool = False
    refresh_pitcher_cache: bool = True
    refresh_weather_cache: bool = True
    refresh_daily_reviews: bool = True
    run_walk_forward: bool = True
    refresh_recent_games: bool = True
    recent_games_lookback_days: int = 45
    refresh_active_schedule: bool = True
    refresh_stage1_cache: bool = True
    refresh_team_game_log: bool = True
    refresh_park_hr_factors: bool = True
    run_preflight_secrets: bool = True
    run_preflight_artifacts: bool = True
    require_poly_private_key: bool = False
    pitcher_cache_path: Path = DEFAULT_PITCHER_CACHE_PATH
    weather_metadata_path: Path = DEFAULT_STADIUM_WEATHER_METADATA_PATH
    weather_cache_dir: Path = DEFAULT_WEATHER_CACHE_DIR
    weather_provider: str = "open-meteo"
    weather_timeout: float = 8.0
    stake: float = 10.0
    daily_budget: float = 80.0
    per_game_budget_fraction: float = 0.40
    sessions_dir: Path = DEFAULT_SESSIONS_DIR
    candidate_dir: Path = DEFAULT_CANDIDATE_DIR
    log_dir: Path = DEFAULT_LOG_DIR
    output_root: Path = DEFAULT_OUTPUT_ROOT
    team_game_log_path: Path = DEFAULT_TEAM_GAME_LOG_PATH
    mlb_ou_cache_path: Path = DEFAULT_MLB_OU_CACHE_PATH
    stage2_cache_path: Path = DEFAULT_STAGE2_CACHE_PATH
    park_hr_factors_path: Path = DEFAULT_PARK_HR_FACTORS_PATH
    env_path: Path = DEFAULT_ENV_PATH
    stage2_brier_history_path: Path = field(
        default_factory=lambda: DEFAULT_STAGE2_BRIER_HISTORY_PATH
    )
    stage3_v2_drift_history_path: Path = field(
        default_factory=lambda: DEFAULT_STAGE3_V2_DRIFT_HISTORY_PATH
    )
    plan_only: bool = False
    force_retrain: bool = False
    auto_daemon_mode: str = "preview"
    auto_daemon_cooldown_days: int = 14


@dataclass(frozen=True)
class StalenessCheck:
    """Skip-if-fresh policy for an expensive subprocess step.

    The step is skipped (status="skipped_fresh") iff ``output_path`` exists
    AND its mtime is >= the mtime of every file matched by ``input_paths``
    and ``input_globs``. ``--force-retrain`` (RefreshConfig.force_retrain)
    bypasses the check.

    ``input_globs`` is a list of (root_dir, glob_pattern) pairs. Globbing
    only matches files whose mtime check is required; for huge corpora
    (data/games/*) we scan parent directories' mtimes instead of leaf
    files, which is much cheaper and still catches add/remove.
    """
    output_path: Path
    input_paths: Tuple[Path, ...] = field(default_factory=tuple)
    input_dir_mtime_roots: Tuple[Path, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RefreshStep:
    name: str
    command: List[str] = field(default_factory=list)
    description: str = ""
    # "subprocess" runs `command` via subprocess.
    # "inline" dispatches to INLINE_HANDLERS by step name (preflight checks).
    kind: str = "subprocess"
    # Optional skip-if-fresh policy (only applies to subprocess steps).
    staleness_check: Optional[StalenessCheck] = None


@dataclass
class RefreshStepResult:
    name: str
    command: List[str]
    returncode: Optional[int]
    elapsed_secs: float
    status: str
    output_tail: str = ""
