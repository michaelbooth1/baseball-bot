#!/usr/bin/env python3
"""
Refresh canonical post-session artifacts for the live trading research loop.

This is the startup-safe orchestration layer. It rebuilds data and reports that
should reflect all completed live sessions before a new run starts.

2026-05-31 refactor: implementation moved to ``scripts.analysis.refresh`` as a
package (config / helpers / session_discovery / preflight / promotion_stage*
/ model_freshness / steps / execution / manifest / rollup / cli). This file
is a back-compat shim that re-exports every previously-public name so existing
callers (signal_engine, live_engine, promote, dump_refresh_steps, tests) keep
working without import changes.

It intentionally does not retrain live decision artifacts such as probability
calibration or EV-policy model JSONs. Those artifacts can affect live behavior
when enabled, so promotion/retraining remains an explicit research step.
"""
from __future__ import annotations

# Public constants + dataclasses + entry points.
from scripts.analysis.refresh import (  # noqa: F401
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
    INLINE_HANDLERS,
    LOGGER,
    PHASE6_GATE_RECALIBRATION_DUE_DATE,
    PROJECT_DIR,
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
    RefreshConfig,
    RefreshStep,
    RefreshStepResult,
    StalenessCheck,
    build_refresh_steps,
    daily_review_dates_needing_refresh,
    discover_paper_only_review_targets,
    discover_session_dates,
    latest_refreshable_date,
    main,
    parse_args,
    run_startup_refresh,
)

# Private helpers that tests reach into directly. Re-exported because
# `from refresh import *` skips underscore-prefixed names.
from scripts.analysis.refresh import (  # noqa: F401
    _artifact_age_days,
    _daily_review_is_current,
    _extract_stage3_v2_active_betas,
    _extract_stage3_v2_research_betas,
    _first_of_month,
    _first_of_prior_month,
    _handle_model_freshness_health,
    _handle_preflight_artifacts,
    _handle_preflight_env_secrets,
    _handle_stage1_cache_promote,
    _handle_stage3_v2_promotion_check,
    _inline,
    _is_step_fresh,
    _last_of_month,
    _load_stage2_brier_history,
    _load_stage3_v2_drift_history,
    _logs_dir_bytes,
    _max_dir_mtime,
    _max_mtime,
    _mtime,
    _now_iso,
    _output_tail,
    _parse_date,
    _phase6_reminder,
    _python,
    _read_env_file,
    _run_inline_step,
    _run_refresh_health_rollup,
    _run_step,
    _safe_load_json,
    _script,
    _shift_days,
    _stage1_cache_health,
    _stage1_expected_season_window,
    _stage1_promotion_guard,
    _stage1_total_games,
    _stage2_history_row_date,
    _stage2_promotion_verdict,
    _stage2_validation_brier,
    _stage3_v2_distinct_history_dates,
    _stage3_v2_max_abs_delta,
    _stage3_v2_primary_verdict,
    _stage3_v2_promotion_verdict,
    _stage3_v2_verdict_stability_gate,
    _team_game_log_health,
    _trailing_stage2_history,
    _trailing_stage3_v2_history,
    _valid_date,
    _write_manifest,
    _write_stage2_brier_history_row,
    _write_stage3_v2_drift_history_row,
)


if __name__ == "__main__":
    raise SystemExit(main())
