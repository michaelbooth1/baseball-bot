"""Step cluster: preflight + scrape + caches + weather + early enrichment.

Everything that runs BEFORE we know there's a max_date (i.e.,
independent of any completed session). Plus settlement_truth +
daily-review steps which kick off the session-data half.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from .. import config as _config
from ..config import (
    RefreshConfig,
    RefreshStep,
    STAGE1_HISTORY_FULL_SEASONS,
    StalenessCheck,
)
from ..helpers import (
    _first_of_prior_month,
    _last_of_month,
    _python,
    _script,
    _shift_days,
    _stage1_expected_season_window,
)
from ..session_discovery import (
    daily_review_dates_needing_refresh,
    discover_paper_only_review_targets,
)


def build_preflight_and_caches_steps(
    config: RefreshConfig,
    session_dates: Sequence[str],
    max_date: Optional[str],
) -> List[RefreshStep]:
    steps: List[RefreshStep] = []

    if config.run_preflight_secrets:
        steps.append(
            RefreshStep(
                name="preflight_env_secrets",
                description="Verify .env presence and POLY_PRIVATE_KEY (warning unless require_poly_private_key).",
                command=[],
                kind="inline",
            )
        )

    if config.refresh_recent_games:
        end_date = _shift_days(config.active_date, -1)
        start_date = _shift_days(config.active_date, -int(config.recent_games_lookback_days))
        if start_date > end_date:
            start_date = end_date
        steps.append(
            RefreshStep(
                name="scrape_recent_games",
                description=f"Backfill completed MLB games {start_date} -> {end_date} (skip-existing).",
                command=[
                    _python(),
                    _script("scripts/scraping/scrape_mlb_history.py"),
                    "--start-date", start_date,
                    "--end-date", end_date,
                    "--game-types", "R",
                ],
            )
        )

    if config.refresh_stage1_cache:
        stage1_min_season, stage1_max_season = _stage1_expected_season_window(config.active_date)
        steps.append(
            RefreshStep(
                name="stage1_ou_cache",
                description=(
                    "Rebuild Stage-1 O/U probability cache from the "
                    f"{STAGE1_HISTORY_FULL_SEASONS} completed regular seasons "
                    f"{stage1_min_season}-{stage1_max_season} -> STAGING path. "
                    "stage1_cache_promote runs immediately after with a sanity guard. "
                    "Staleness-checked against data/games/regular/ dir mtimes; "
                    "skips when no new game files have arrived since the staging "
                    "artifact was last written. Saves ~10 min per refresh on no-data days."
                ),
                command=[
                    _python(),
                    _script("cache/build_mlb_ou_cache.py"),
                    "--season-type",
                    "regular",
                    "--min-season",
                    str(stage1_min_season),
                    "--max-season",
                    str(stage1_max_season),
                    "--out",
                    str(_config.PROJECT_DIR / "cache" / "mlb_ou_cache.staging.json"),
                ],
                staleness_check=StalenessCheck(
                    output_path=_config.PROJECT_DIR / "cache" / "mlb_ou_cache.staging.json",
                    input_paths=(),
                    input_dir_mtime_roots=(
                        _config.PROJECT_DIR / "data" / "games" / "regular",
                    ),
                ),
            )
        )
        steps.append(
            RefreshStep(
                name="stage1_cache_promote",
                kind="inline",
                description=(
                    "Promote staging Stage-1 cache to production after a "
                    "sanity guard (game-count floor + coverage window). "
                    "Refuses to promote if staging looks like a partial scrape."
                ),
                command=[],
            )
        )
        steps.append(
            RefreshStep(
                name="stage1_ou_cache_alt_a",
                description=(
                    "Rebuild Stage-1 O/U cache in Alt-A mode "
                    "(empirical-when-available) to a SEPARATE staging "
                    "path. No auto-promote. Same history window + "
                    "staleness check as the production Stage-1 step."
                ),
                command=[
                    _python(),
                    _script("cache/build_mlb_ou_cache.py"),
                    "--season-type",
                    "regular",
                    "--min-season",
                    str(stage1_min_season),
                    "--max-season",
                    str(stage1_max_season),
                    "--smoothing-mode",
                    "empirical_when_available",
                    "--out",
                    str(_config.PROJECT_DIR / "cache" / "mlb_ou_cache_alt_a.staging.json"),
                ],
                staleness_check=StalenessCheck(
                    output_path=_config.PROJECT_DIR / "cache" / "mlb_ou_cache_alt_a.staging.json",
                    input_paths=(),
                    input_dir_mtime_roots=(
                        _config.PROJECT_DIR / "data" / "games" / "regular",
                    ),
                ),
            )
        )
        steps.append(
            RefreshStep(
                name="stage1_ou_cache_nb",
                description=(
                    "Hygiene #3 (2026-06-11): rebuild Stage-1 O/U cache "
                    "in negative-binomial mode to a SEPARATE staging "
                    "path. Per-phase NB dispersion (method of moments) "
                    "replaces the thin-tailed Poisson; first build "
                    "closed 93% of the +10.7pp poisson-vs-empirical "
                    "gap at FV>=0.85. Consumed by the O_nb_stage1 fleet "
                    "arm via --cache-path. No auto-promote; promote.py "
                    "stage1 is the manual gate after fleet evidence."
                ),
                command=[
                    _python(),
                    _script("cache/build_mlb_ou_cache.py"),
                    "--season-type",
                    "regular",
                    "--min-season",
                    str(stage1_min_season),
                    "--max-season",
                    str(stage1_max_season),
                    "--smoothing-mode",
                    "negative_binomial",
                    "--out",
                    str(_config.PROJECT_DIR / "cache" / "mlb_ou_cache_nb.staging.json"),
                ],
                staleness_check=StalenessCheck(
                    output_path=_config.PROJECT_DIR / "cache" / "mlb_ou_cache_nb.staging.json",
                    input_paths=(),
                    input_dir_mtime_roots=(
                        _config.PROJECT_DIR / "data" / "games" / "regular",
                    ),
                ),
            )
        )

    if config.refresh_active_schedule:
        prior_month_start = _first_of_prior_month(config.active_date)
        month_end = _last_of_month(config.active_date)
        steps.append(
            RefreshStep(
                name="scrape_active_schedule",
                description=(
                    f"Refresh MLB schedule {prior_month_start} -> "
                    f"{month_end} (prior + active month; no game "
                    "downloads). Game files are pulled by "
                    "scrape_recent_games above."
                ),
                command=[
                    _python(),
                    _script("scripts/scraping/scrape_mlb_history.py"),
                    "--start-date", prior_month_start,
                    "--end-date", month_end,
                    "--game-types", "R",
                    "--dry-run",
                ],
            )
        )

    if config.refresh_weather_cache:
        steps.append(
            RefreshStep(
                name="game_weather_cache",
                description="Refresh local stadium weather cache for active schedule date.",
                command=[
                    _python(),
                    _script("scripts/analysis/refresh_game_weather.py"),
                    "--date",
                    config.active_date,
                    "--provider",
                    str(config.weather_provider),
                    "--metadata-path",
                    str(config.weather_metadata_path),
                    "--cache-dir",
                    str(config.weather_cache_dir),
                    "--timeout",
                    f"{float(config.weather_timeout):g}",
                ],
            )
        )
        steps.append(
            RefreshStep(
                name="game_meta_cache",
                description="Refresh per-game home-plate umpire / officials cache for active schedule date.",
                command=[
                    _python(),
                    _script("scripts/analysis/refresh_game_meta.py"),
                    "--date",
                    config.active_date,
                    "--timeout",
                    f"{float(config.weather_timeout):g}",
                ],
            )
        )

    if config.refresh_pitcher_cache:
        season = config.active_date[:4]
        steps.append(
            RefreshStep(
                name="pitcher_cache",
                description="Refresh current-season pitcher ERA cache.",
                command=[
                    _python(),
                    _script("scripts/analysis/build_pitcher_cache.py"),
                    "--season",
                    season,
                    "--cache-path",
                    str(config.pitcher_cache_path),
                ],
            )
        )

    if config.refresh_team_game_log:
        steps.append(
            RefreshStep(
                name="team_game_log",
                description="Rebuild per-team RPG cache (Stage-3 input) from scraped MLB games.",
                command=[
                    _python(),
                    _script("scripts/analysis/build_team_game_log.py"),
                    "--output", str(config.team_game_log_path),
                ],
            )
        )

    if config.refresh_park_hr_factors:
        steps.append(
            RefreshStep(
                name="park_hr_factors",
                description="Rebuild per-(park, season) HR factor cache (Stage-2 hr_factor family input).",
                command=[
                    _python(),
                    _script("scripts/analysis/build_park_hr_factors.py"),
                    "--output", str(config.park_hr_factors_path),
                ],
            )
        )

    if config.run_preflight_artifacts:
        steps.append(
            RefreshStep(
                name="preflight_artifacts",
                description="Validate Stage-1/2/3 caches load and Stage-3 has active-season coverage.",
                command=[],
                kind="inline",
            )
        )

    return steps


def build_settlement_and_review_steps(
    config: RefreshConfig,
    session_dates: Sequence[str],
    max_date: str,
) -> List[RefreshStep]:
    """The two early per-session-data steps: settlement_truth_verification +
    daily_human_review:DATE (one per stale date) + daily_human_review_paper:DATE
    for paper-only days. Caller must guarantee max_date is non-empty."""
    steps: List[RefreshStep] = []

    steps.append(
        RefreshStep(
            name="settlement_truth_verification",
            description=(
                "Cross-check settled bets against MLB ground truth "
                "(home_runs + away_runs from the live-feed JSON). "
                "Surfaces resolution_mismatch / stale_filled / "
                "missing_mlb_data / game_not_final_yet diagnostics. "
                "Phase C v2 inventory integrity depends on this."
            ),
            command=[
                _python(),
                _script("scripts/analysis/verify_settlement_truth.py"),
                "--mode", "live",
                "--today", max_date,
            ],
        )
    )

    if config.refresh_daily_reviews:
        review_dir = _config.PROJECT_DIR / "data" / "analysis_output" / "daily_human_review"
        for date_str in daily_review_dates_needing_refresh(
            session_dates,
            max_date=max_date,
            sessions_dir=config.sessions_dir,
            candidate_dir=config.candidate_dir,
            log_dir=config.log_dir,
            review_dir=review_dir,
        ):
            steps.append(
                RefreshStep(
                    name=f"daily_human_review:{date_str}",
                    description="Refresh compact daily human-review JSON/Markdown.",
                    command=[
                        _python(),
                        _script("scripts/analysis/build_daily_human_review_report.py"),
                        "--session-date",
                        date_str,
                    ],
                )
            )
        # 2026-05-31: paper-only days (no live session) also get a
        # daily_human_review built so the rich cohort/calibration
        # health blocks aren't dark on paper-only days.
        for date_str, paper_sessions_dir, paper_candidate_dir in (
            discover_paper_only_review_targets(
                live_session_dates=session_dates,
                max_date=max_date,
                review_dir=review_dir,
                log_dir=config.log_dir,
            )
        ):
            steps.append(
                RefreshStep(
                    name=f"daily_human_review_paper:{date_str}",
                    description=(
                        "Refresh compact daily human-review JSON/Markdown "
                        "for a paper-only day (no live session)."
                    ),
                    command=[
                        _python(),
                        _script("scripts/analysis/build_daily_human_review_report.py"),
                        "--session-date",
                        date_str,
                        "--sessions-dir",
                        str(paper_sessions_dir),
                        "--candidate-dir",
                        str(paper_candidate_dir),
                    ],
                )
            )

    return steps
