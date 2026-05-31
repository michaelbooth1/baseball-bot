"""Daily refresh orchestration package.

Split out of scripts/analysis/run_daily_refresh.py during the 2026-05-31
refactor. The legacy entry point continues to re-export every public
name from this package so existing imports keep working.

Module layout:
- config.py            dataclasses + DEFAULT_* paths + PROMOTION_* constants
- helpers.py           date / path helpers
- session_discovery.py session globbing + daily-review staleness
- preflight.py         INLINE_HANDLERS registry + preflight_env_secrets + preflight_artifacts
- promotion_stage1.py  stage1_cache_promote inline handler + guard
- promotion_stage2.py  Stage-2 staging vs prod Brier helpers + verdict
- promotion_stage3_v2.py Stage-3 v2 drift handler + verdict + stability gate
- model_freshness.py   model_freshness_health inline handler
- steps/               build_refresh_steps split into topic clusters
- execution.py         _run_step / _is_step_fresh / _output_tail
- manifest.py          _write_manifest + _phase6_reminder + _logs_dir_bytes
- rollup.py            _run_refresh_health_rollup (end-of-refresh summary)
- cli.py               run_startup_refresh + parse_args + main
"""
from __future__ import annotations

# Import sub-modules in an order that guarantees @_inline handlers
# register BEFORE any caller looks up INLINE_HANDLERS.
from . import preflight as _preflight  # noqa: F401  (registers preflight_env_secrets + preflight_artifacts)
from . import promotion_stage1 as _promotion_stage1  # noqa: F401  (registers stage1_cache_promote)
from . import promotion_stage3_v2 as _promotion_stage3_v2  # noqa: F401  (registers stage3_v2_promotion_check)
from . import model_freshness as _model_freshness  # noqa: F401  (registers model_freshness_health)

# Public re-exports. Tests + production scripts (signal_engine.py,
# live_engine.py, promote.py, dump_refresh_steps.py) import these
# names from `scripts.analysis.run_daily_refresh`, which now itself
# re-exports from here.
from .config import (
    DEFAULT_CANDIDATE_DIR,
    DEFAULT_ENV_PATH,
    DEFAULT_LOG_DIR,
    DEFAULT_MLB_OU_CACHE_PATH,
    DEFAULT_MLB_OU_CACHE_STAGING_PATH,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_PAPER_REVIEW_ROOTS,
    DEFAULT_PARK_HR_FACTORS_PATH,
    DEFAULT_PITCHER_CACHE_PATH,
    DEFAULT_SESSIONS_DIR,
    DEFAULT_STADIUM_WEATHER_METADATA_PATH,
    DEFAULT_STAGE2_BRIER_HISTORY_PATH,
    DEFAULT_STAGE2_CACHE_PATH,
    DEFAULT_STAGE3_V2_DRIFT_HISTORY_PATH,
    DEFAULT_STAGE3_V2_PROD_WEIGHTS_PATH,
    DEFAULT_STAGE3_V2_RESEARCH_FIT_PATH,
    DEFAULT_TEAM_GAME_LOG_PATH,
    DEFAULT_WEATHER_CACHE_DIR,
    LOGGER,
    PHASE6_GATE_RECALIBRATION_DUE_DATE,
    PROJECT_DIR,
    RefreshConfig,
    RefreshStep,
    RefreshStepResult,
    SESSION_RE,
    STAGE1_HISTORY_FULL_SEASONS,
    STAGE1_LEAD_CANDIDATE_FULL_SEASONS,
    STAGE1_PRODUCTION_FULL_SEASONS,
    STAGE1_PROMOTE_MIN_GAMES_RATIO,
    STAGE1_RESEARCH_MAX_FULL_SEASONS,
    STAGE2_BRIER_DRIFT_THRESHOLD,
    STAGE2_PROMOTION_MIN_CONSECUTIVE,
    STAGE2_PROMOTION_MIN_DELTA,
    STAGE2_PROMOTION_MIN_HISTORY,
    STAGE2_PROMOTION_WINDOW,
    STAGE3_V2_COMPILED_DEFAULTS,
    STAGE3_V2_PROMOTION_DRIFT_THRESHOLD,
    STAGE3_V2_PROMOTION_MIN_CONSECUTIVE,
    STAGE3_V2_PROMOTION_MIN_HISTORY,
    STAGE3_V2_PROMOTION_WINDOW,
    STAGE3_V2_VERDICT_STABILITY_MIN_HISTORY,
    STAGE3_V2_VERDICT_STABILITY_WINDOW,
    STALE_MODEL_AGE_DAYS,
    StalenessCheck,
)
from .helpers import (
    _first_of_month,
    _first_of_prior_month,
    _last_of_month,
    _mtime,
    _now_iso,
    _parse_date,
    _python,
    _script,
    _shift_days,
    _stage1_expected_season_window,
    _valid_date,
)
from .session_discovery import (
    _daily_review_is_current,
    daily_review_dates_needing_refresh,
    discover_paper_only_review_targets,
    discover_session_dates,
    latest_refreshable_date,
)
from .preflight import (
    INLINE_HANDLERS,
    _handle_preflight_artifacts,
    _handle_preflight_env_secrets,
    _inline,
    _read_env_file,
    _safe_load_json,
    _stage1_cache_health,
    _team_game_log_health,
)
from .promotion_stage1 import (
    _handle_stage1_cache_promote,
    _stage1_promotion_guard,
    _stage1_total_games,
)
from .promotion_stage2 import (
    _artifact_age_days,
    _load_stage2_brier_history,
    _stage2_history_row_date,
    _stage2_promotion_verdict,
    _stage2_validation_brier,
    _trailing_stage2_history,
    _write_stage2_brier_history_row,
)
from .promotion_stage3_v2 import (
    _extract_stage3_v2_active_betas,
    _extract_stage3_v2_research_betas,
    _handle_stage3_v2_promotion_check,
    _load_stage3_v2_drift_history,
    _stage3_v2_distinct_history_dates,
    _stage3_v2_max_abs_delta,
    _stage3_v2_primary_verdict,
    _stage3_v2_promotion_verdict,
    _stage3_v2_verdict_stability_gate,
    _trailing_stage3_v2_history,
    _write_stage3_v2_drift_history_row,
)
from .model_freshness import _handle_model_freshness_health
from .execution import (
    _is_step_fresh,
    _max_dir_mtime,
    _max_mtime,
    _output_tail,
    _run_inline_step,
    _run_step,
)
from .manifest import _logs_dir_bytes, _phase6_reminder, _write_manifest
from .rollup import _run_refresh_health_rollup
from .steps import build_refresh_steps
from .cli import main, parse_args, run_startup_refresh
