#!/usr/bin/env python3
"""
Refresh canonical post-session artifacts for the live trading research loop.

This is the startup-safe orchestration layer. It rebuilds data and reports that
should reflect all completed live sessions before a new run starts:

- pitcher cache, so starter-quality inputs are current before runtime loads
- game weather cache, so local stadium weather is captured before runtime starts
- Stage-1 O/U cache, so the historical base absorbs newly completed games
- daily human-review reports for stale or missing completed sessions
- candidate universe, calibration-opportunity, model-maturity, unified signals, and leakage-aware training tables
- fair-value stage ablation, so FV stack damage/improvement is visible daily
- FV trust/shrinkage experiment, so support-weighted market anchoring is visible daily
- inferred Stage-1 empirical audit, so Poisson-vs-empirical overconfidence is visible daily
- execution diagnostics, queue-aware replay, and state-value reports
- score-event and no-score drift walk-forward research outputs

It intentionally does not retrain live decision artifacts such as probability
calibration or EV-policy model JSONs. Those artifacts can affect live behavior
when enabled, so promotion/retraining remains an explicit research step.
"""

from __future__ import annotations

import argparse
import calendar
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SESSIONS_DIR = PROJECT_DIR / "data" / "live_trading" / "sessions"
DEFAULT_CANDIDATE_DIR = PROJECT_DIR / "data" / "live_trading" / "candidate_universe"
DEFAULT_LOG_DIR = PROJECT_DIR / "logs" / "real-logs"
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
    # New (2026-05-08): startup is now the canonical daily refresh.
    refresh_recent_games: bool = True
    # Bumped from 7 -> 45 on 2026-05-17 after Active #12 settlement-
    # truth verification found 22 missing MLB game JSONs (24.7% of
    # filled bets) clustered in a 17-30-day-old window. The 7d
    # lookback was catching only the most recent week, while late
    # settlements + month-boundary games rolled off into a permanent
    # data gap. 45d covers the longest tail of "bet still settling"
    # plus a buffer for rescheduled games. Scraper is idempotent +
    # skip-existing, so the cost after a one-time backfill is minimal.
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
    # Stability-gate history files. Defaults point at the canonical repo
    # paths; tests override to tmp_path to avoid polluting production
    # history (one row per refresh, append-only).
    stage2_brier_history_path: Path = field(
        default_factory=lambda: DEFAULT_STAGE2_BRIER_HISTORY_PATH
    )
    stage3_v2_drift_history_path: Path = field(
        default_factory=lambda: DEFAULT_STAGE3_V2_DRIFT_HISTORY_PATH
    )
    plan_only: bool = False
    # Bypass staleness checks on retrain steps. Default False -- heavy
    # retrains (Stage-2, Stage-3, walk-forward) skip when their inputs
    # haven't changed since the previous successful run.
    force_retrain: bool = False
    # Auto-promote/demote daemon mode. Default "preview": daemon reads
    # verdicts + cooldown but takes no action. "act" actually invokes
    # promote.py. "off" skips the daemon step entirely. Operator should
    # leave this at "preview" until they've reviewed daemon output for a
    # few sessions, then flip to "act" to fully close the self-improving
    # loop.
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_date(date_str: str) -> bool:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except Exception:
        return False


def _stage1_expected_season_window(active_date: str) -> Tuple[int, int]:
    active_year = int(str(active_date)[:4])
    return active_year - STAGE1_HISTORY_FULL_SEASONS, active_year - 1


def _script(path: str) -> str:
    return str(PROJECT_DIR / path)


def _python() -> str:
    return sys.executable or "python"


def _parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d")


def _shift_days(date_str: str, days: int) -> str:
    return (_parse_date(date_str) + timedelta(days=days)).strftime("%Y-%m-%d")


def _first_of_month(date_str: str) -> str:
    d = _parse_date(date_str)
    return d.replace(day=1).strftime("%Y-%m-%d")


def _last_of_month(date_str: str) -> str:
    d = _parse_date(date_str)
    last_day = calendar.monthrange(d.year, d.month)[1]
    return d.replace(day=last_day).strftime("%Y-%m-%d")


def _first_of_prior_month(date_str: str) -> str:
    """First day of the calendar month before `date_str`. Wraps to
    December of the prior year when called in January. Used by the
    schedule refresh to cover the active month PLUS the prior month
    so late-added games near month boundaries don't fall through the
    cracks (see Active #12 root-cause analysis 2026-05-17)."""
    d = _parse_date(date_str)
    if d.month == 1:
        prior_year, prior_month = d.year - 1, 12
    else:
        prior_year, prior_month = d.year, d.month - 1
    return date(prior_year, prior_month, 1).strftime("%Y-%m-%d")


def discover_session_dates(sessions_dir: Path = DEFAULT_SESSIONS_DIR) -> List[str]:
    """Return sorted YYYY-MM-DD dates with live session JSON files."""
    if not sessions_dir.exists():
        return []
    dates: List[str] = []
    for path in sessions_dir.glob("*_session.json"):
        match = SESSION_RE.match(path.name)
        if match:
            dates.append(match.group("date"))
    return sorted(set(dates))


def latest_refreshable_date(
    session_dates: Sequence[str],
    *,
    active_date: str,
    max_date: str = "",
    include_run_date: bool = False,
) -> Optional[str]:
    """Pick the newest completed session date to fold into canonical outputs."""
    if not session_dates:
        return None
    if max_date:
        candidates = [d for d in session_dates if d <= max_date]
    elif include_run_date:
        candidates = [d for d in session_dates if d <= active_date]
    else:
        candidates = [d for d in session_dates if d < active_date]
    return max(candidates) if candidates else None


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _daily_review_is_current(
    *,
    date_str: str,
    sessions_dir: Path,
    candidate_dir: Path,
    log_dir: Path,
    review_dir: Path,
) -> bool:
    outputs = [
        review_dir / f"{date_str}_human_review.json",
        review_dir / f"{date_str}_human_review.md",
    ]
    output_mtimes = [_mtime(path) for path in outputs]
    if any(value is None for value in output_mtimes):
        return False

    sources = [
        sessions_dir / f"{date_str}_session.json",
        candidate_dir / f"{date_str}_candidate_rollup.json",
        log_dir / f"{date_str}.log",
    ]
    source_mtimes = [value for value in (_mtime(path) for path in sources) if value is not None]
    if not source_mtimes:
        return False
    return min(output_mtimes) >= max(source_mtimes)


def daily_review_dates_needing_refresh(
    session_dates: Iterable[str],
    *,
    max_date: str,
    sessions_dir: Path,
    candidate_dir: Path,
    log_dir: Path,
    review_dir: Path,
) -> List[str]:
    dates = [d for d in session_dates if d <= max_date]
    out: List[str] = []
    for date_str in dates:
        if not _daily_review_is_current(
            date_str=date_str,
            sessions_dir=sessions_dir,
            candidate_dir=candidate_dir,
            log_dir=log_dir,
            review_dir=review_dir,
        ):
            out.append(date_str)
    return out


# ---------------------------------------------------------------------------
# Inline preflight handlers. Cheap, in-process checks that surface broken
# state (missing .env, unreadable caches, stale Stage-3 corpus) up front so
# failures don't appear at first-tick of the live engine.
# ---------------------------------------------------------------------------

InlineHandler = Callable[[RefreshConfig], Tuple[bool, str]]
INLINE_HANDLERS: Dict[str, InlineHandler] = {}


def _inline(name: str) -> Callable[[InlineHandler], InlineHandler]:
    def deco(fn: InlineHandler) -> InlineHandler:
        INLINE_HANDLERS[name] = fn
        return fn
    return deco


def _read_env_file(env_path: Path) -> Dict[str, str]:
    if not env_path.exists():
        return {}
    out: Dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


@_inline("preflight_env_secrets")
def _handle_preflight_env_secrets(config: RefreshConfig) -> Tuple[bool, str]:
    env_path = config.env_path
    notes: List[str] = [f"env_path={env_path}"]
    if not env_path.exists():
        notes.append("WARNING: .env file not found (paper mode unaffected; live mode requires POLY_PRIVATE_KEY).")
        if config.require_poly_private_key:
            return False, "\n".join(notes)
        return True, "\n".join(notes)
    env = _read_env_file(env_path)
    poly_key = env.get("POLY_PRIVATE_KEY", "")
    if not poly_key:
        notes.append("WARNING: POLY_PRIVATE_KEY not set in .env (live trading will fail).")
        if config.require_poly_private_key:
            return False, "\n".join(notes)
        return True, "\n".join(notes)
    # Don't log the key value. Just confirm length looks plausible.
    notes.append(f"POLY_PRIVATE_KEY present (length={len(poly_key)})")
    return True, "\n".join(notes)


def _safe_load_json(path: Path) -> Tuple[Optional[object], Optional[str]]:
    if not path.exists():
        return None, f"missing: {path}"
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except Exception as exc:
        return None, f"unreadable: {path} ({exc})"


def _team_game_log_health(payload: object, active_year: str) -> Tuple[bool, str]:
    """Verify team_game_log has games for the active season for ~all teams."""
    if not isinstance(payload, dict):
        return False, "team_game_log payload is not a dict"
    games = payload.get("games") or []
    if not isinstance(games, list):
        return False, "team_game_log.games is not a list"
    teams_with_active_season: Dict[str, int] = {}
    teams_total: set = set()
    for g in games:
        if not isinstance(g, dict):
            continue
        date_str = str(g.get("date", ""))
        away = str(g.get("away", "") or "")
        home = str(g.get("home", "") or "")
        if away:
            teams_total.add(away)
        if home:
            teams_total.add(home)
        if date_str.startswith(active_year + "-"):
            if away:
                teams_with_active_season[away] = teams_with_active_season.get(away, 0) + 1
            if home:
                teams_with_active_season[home] = teams_with_active_season.get(home, 0) + 1
    if not teams_total:
        return False, "team_game_log has zero teams"
    coverage = len(teams_with_active_season) / max(len(teams_total), 1)
    if coverage < 0.8:
        return False, (
            f"only {len(teams_with_active_season)}/{len(teams_total)} teams have {active_year} games "
            f"(coverage {coverage:.0%}; Stage-3 season_to_date will be unreliable). "
            f"Run scrape_recent_games to backfill."
        )
    avg = sum(teams_with_active_season.values()) / max(len(teams_with_active_season), 1)
    return True, (
        f"team_game_log: {len(teams_total)} teams, "
        f"{len(teams_with_active_season)} with {active_year} games (avg {avg:.0f}/team)"
    )


def _stage1_cache_health(payload: object, active_date: str) -> Tuple[bool, str]:
    """Warn when Stage-1 cache does not cover the expected production history window."""
    if not isinstance(payload, dict):
        return False, "Stage-1 cache payload is not a dict"
    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        return True, "WARNING Stage-1 coverage metadata missing; rebuild with current build_mlb_ou_cache.py"

    expected_start, expected_end = _stage1_expected_season_window(active_date)
    expected = [str(year) for year in range(expected_start, expected_end + 1)]
    games_by_season = meta.get("games_by_season") or {}
    seasons = [str(season) for season in (meta.get("seasons") or [])]
    missing = [season for season in expected if str(season) not in games_by_season and str(season) not in seasons]
    history_start = str(meta.get("history_start_date") or "")
    history_end = str(meta.get("history_end_date") or "")
    total_games = meta.get("total_games", meta.get("games_loaded"))
    duplicate_skipped = meta.get("duplicate_game_files_skipped")
    if missing:
        return True, (
            "WARNING Stage-1 coverage: expected prior "
            f"{STAGE1_HISTORY_FULL_SEASONS} full seasons {expected_start}-{expected_end}, "
            f"missing metadata for {', '.join(missing)}. "
            "Rebuild with --min-season/--max-season after historical scrape."
        )
    if history_start and history_start > f"{expected_start}-12-31":
        return True, (
            f"WARNING Stage-1 coverage starts at {history_start}, later than expected season {expected_start}."
        )
    if history_end and history_end < f"{expected_end}-01-01":
        return True, (
            f"WARNING Stage-1 coverage ends at {history_end}, earlier than expected season {expected_end}."
        )
    return True, (
        f"ok Stage-1 production coverage: seasons {expected_start}-{expected_end}, "
        f"total_games={total_games}, history={history_start or 'unknown'}->{history_end or 'unknown'}, "
        f"duplicate_files_skipped={duplicate_skipped if duplicate_skipped is not None else 'unknown'}"
    )


@_inline("preflight_artifacts")
def _handle_preflight_artifacts(config: RefreshConfig) -> Tuple[bool, str]:
    notes: List[str] = []
    ok = True

    payload, err = _safe_load_json(config.mlb_ou_cache_path)
    if err:
        ok = False
        notes.append(f"FAIL Stage-1 (mlb_ou_cache.json): {err}")
    else:
        if isinstance(payload, dict) and not payload:
            ok = False
            notes.append("FAIL Stage-1 (mlb_ou_cache.json): empty dict")
        else:
            notes.append("ok Stage-1 (mlb_ou_cache.json) loaded")
            sub_ok, sub_msg = _stage1_cache_health(payload, config.active_date)
            if not sub_ok:
                ok = False
            notes.append(sub_msg)

    payload, err = _safe_load_json(config.stage2_cache_path)
    if err:
        ok = False
        notes.append(f"FAIL Stage-2 (mlb_stage2_run_env.json): {err}")
    else:
        notes.append("ok Stage-2 (mlb_stage2_run_env.json) loaded")

    payload, err = _safe_load_json(config.team_game_log_path)
    if err:
        ok = False
        notes.append(f"FAIL Stage-3 (team_game_log.json): {err}")
    else:
        active_year = config.active_date[:4]
        sub_ok, sub_msg = _team_game_log_health(payload, active_year)
        if not sub_ok:
            ok = False
            notes.append(f"FAIL Stage-3: {sub_msg}")
        else:
            notes.append(f"ok Stage-3: {sub_msg}")

    payload, err = _safe_load_json(config.pitcher_cache_path)
    if err:
        # Pitcher cache failures are warnings — Gate 8i is bypassable.
        notes.append(f"WARNING Pitcher cache: {err} (Gate 8i will be inactive).")
    else:
        if isinstance(payload, dict):
            n_pitchers = len(payload.get("pitchers", payload)) if isinstance(payload, dict) else 0
            notes.append(f"ok Pitcher cache ({n_pitchers} entries)")
        else:
            notes.append("ok Pitcher cache loaded")

    payload, err = _safe_load_json(config.park_hr_factors_path)
    if err:
        # Stage-2 hr_factor family degrades to UNKNOWN_BUCKET when this file
        # is missing -- non-fatal (other Stage-2 families still apply).
        notes.append(f"WARNING Park HR factors: {err} (Stage-2 hr_factor family will be inactive).")
    elif isinstance(payload, dict):
        by_park = payload.get("by_park") or {}
        active_year = config.active_date[:4]
        with_active = sum(1 for entries in by_park.values()
                          if isinstance(entries, dict) and active_year in entries)
        if not by_park:
            notes.append("WARNING Park HR factors: empty by_park (hr_factor family inactive).")
        elif with_active == 0:
            notes.append(
                f"WARNING Park HR factors: no entries for {active_year} "
                f"(hr_factor will use most-recent-year fallback)."
            )
        else:
            notes.append(f"ok Park HR factors: {len(by_park)} parks, {with_active} with {active_year} entries")

    return ok, "\n".join(notes)


# ---------------------------------------------------------------------------
# Stage-2 staging-vs-production comparison + stale-artifact alerts.
# ---------------------------------------------------------------------------
#
# We auto-rebuild Stage-2 every refresh but write to a STAGING path so the
# live cache (`cache/mlb_stage2_run_env.json`) is never silently swapped.
# This handler diffs the two so any meaningful retrain shift surfaces as a
# refresh-time alert; promotion is a deliberate human action.
#
# Also flags any model artifact that has aged past STALE_AGE_DAYS so the
# operator knows when to schedule a refit, instead of discovering it via a
# silent calibration-drift alert weeks later.

STALE_MODEL_AGE_DAYS = 30
STAGE2_BRIER_DRIFT_THRESHOLD = 0.001  # 0.1pp Brier change is meaningful at our scale

# Stage-2 promotion stability gate (mirrors the calibration stability gate).
# We log staging-vs-production Brier each refresh into a per-row history file.
# Only emit a "PROMOTION READY" line when staging beats production by at least
# STAGE2_PROMOTION_MIN_DELTA on at least STAGE2_PROMOTION_MIN_CONSECUTIVE of
# the trailing STAGE2_PROMOTION_WINDOW distinct dates. This stops single-day
# noise from looking like a green light.
DEFAULT_STAGE2_BRIER_HISTORY_PATH = (
    PROJECT_DIR / "data" / "analysis_output" / "calibration" / "stage2_brier_history.jsonl"
)
STAGE2_PROMOTION_WINDOW = 7
STAGE2_PROMOTION_MIN_HISTORY = 5
STAGE2_PROMOTION_MIN_CONSECUTIVE = 5
STAGE2_PROMOTION_MIN_DELTA = 0.001  # staging must beat prod by >= this each day

# Stage-3 v2 promotion stability gate. Compares the daily research fit
# (phase4_models.json -> model_3_blend) against the currently-promoted
# production weights (cache/team_offense_v2_weights.json) or compiled-in
# defaults if no promotion has happened yet. Drift score = max absolute
# delta across the three coefficients (prior_season, season_to_date,
# momentum_10). Same stability-gate pattern as Stage-2.
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
# Material drift threshold: ~10% relative change at typical 0.15 magnitude.
# Anything smaller is fit noise; larger means the research fit has actually
# moved away from the live model.
STAGE3_V2_PROMOTION_DRIFT_THRESHOLD = 0.015

# Verdict-stability gate (shipped 2026-05-16). Mirrors the calibration
# method-stability gate -- after the primary n_drifting count-based
# verdict fires today, recompute the verdict on each of the prior
# distinct dates and override today to the modal. Suppresses
# 5-of-7-boundary flaps (one day's max_abs_delta crossing the threshold
# can swing the count between 4 and 5). The primary gate stays the
# stability primitive; the modal is the second-layer guard.
STAGE3_V2_VERDICT_STABILITY_WINDOW = 7
STAGE3_V2_VERDICT_STABILITY_MIN_HISTORY = 5


def _artifact_age_days(path: Path) -> Optional[float]:
    try:
        if not path.exists():
            return None
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        return round((datetime.now() - mtime).total_seconds() / 86400.0, 2)
    except OSError:
        return None


def _stage2_validation_brier(payload: object) -> Optional[float]:
    """Pull the validation Brier from a Stage-2 model payload, if present.

    Canonical schema (build_mlb_stage2_run_env.py):
        payload["validation_metrics"][<line>]["stage2_brier"]
    Older / hand-built payloads may use a flat "validation_brier" key or
    a per-line "lines"/"by_line" dict; those legacy shapes are kept as
    fallbacks so a hand-rolled fixture still works.
    """
    if not isinstance(payload, dict):
        return None
    # Canonical: average stage2_brier across the validation_metrics dict.
    vm = payload.get("validation_metrics")
    if isinstance(vm, dict):
        scores: List[float] = []
        for entry in vm.values():
            if not isinstance(entry, dict):
                continue
            for key in ("stage2_brier", "validation_brier", "val_brier", "brier"):
                v = entry.get(key)
                if isinstance(v, (int, float)):
                    scores.append(float(v))
                    break
        if scores:
            return sum(scores) / len(scores)
    # Legacy flat key.
    for key in ("validation_brier", "val_brier", "brier"):
        val = payload.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    summary = payload.get("summary") or {}
    if isinstance(summary, dict):
        for key in ("validation_brier", "val_brier", "brier"):
            val = summary.get(key)
            if isinstance(val, (int, float)):
                return float(val)
    # Legacy per-line shape.
    lines = payload.get("lines") or payload.get("by_line") or {}
    if isinstance(lines, dict):
        scores = []
        for entry in lines.values():
            if not isinstance(entry, dict):
                continue
            for key in ("validation_brier", "val_brier", "brier"):
                v = entry.get(key)
                if isinstance(v, (int, float)):
                    scores.append(float(v))
                    break
        if scores:
            return sum(scores) / len(scores)
    return None


def _load_stage2_brier_history(path: Path) -> List[Dict[str, object]]:
    """Read prior staging-vs-prod Brier observations. Missing/malformed lines
    are skipped silently -- this is a research-output history, not a
    contract; corruption should not break the refresh.
    """
    rows: List[Dict[str, object]] = []
    if not path.exists():
        return rows
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rows.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def _stage2_history_row_date(row: Dict[str, object]) -> str:
    d = row.get("data_max_date")
    if d:
        return str(d)[:10]
    g = row.get("generated_at_utc") or ""
    return str(g)[:10] if g else ""


def _trailing_stage2_history(
    history_rows: List[Dict[str, object]],
    *,
    window: int,
    exclude_date: Optional[str] = None,
) -> List[Dict[str, object]]:
    """Return the last `window` distinct-date Stage-2 observations, oldest
    first. Same-date rows dedupe to the latest entry."""
    by_date: Dict[str, Dict[str, object]] = {}
    for row in history_rows:
        d = _stage2_history_row_date(row)
        if not d:
            continue
        if exclude_date and d == exclude_date:
            continue
        by_date[d] = row
    if not by_date:
        return []
    ordered = sorted(by_date.items(), key=lambda kv: kv[0])
    return [v for _, v in ordered[-window:]]


def _stage2_promotion_verdict(
    history_rows: List[Dict[str, object]],
    *,
    window: int = STAGE2_PROMOTION_WINDOW,
    min_history: int = STAGE2_PROMOTION_MIN_HISTORY,
    min_consecutive: int = STAGE2_PROMOTION_MIN_CONSECUTIVE,
    min_delta: float = STAGE2_PROMOTION_MIN_DELTA,
    exclude_date: Optional[str] = None,
) -> Dict[str, object]:
    """Decide whether staging has consistently beaten production.

    Returns a dict with `verdict` in {"insufficient_history", "hold",
    "promote"} plus diagnostic counts so the alert line can explain itself.
    """
    trailing = _trailing_stage2_history(
        history_rows, window=window, exclude_date=exclude_date
    )
    n_history = len(trailing)
    if n_history < min_history:
        return {
            "verdict": "insufficient_history",
            "n_history": n_history,
            "n_history_required": min_history,
            "n_improving": 0,
            "n_consecutive_required": min_consecutive,
            "min_delta": min_delta,
        }
    n_improving = 0
    for row in trailing:
        delta = row.get("delta")
        if isinstance(delta, (int, float)) and float(delta) <= -min_delta:
            n_improving += 1
    if n_improving >= min_consecutive:
        verdict = "promote"
    else:
        verdict = "hold"
    return {
        "verdict": verdict,
        "n_history": n_history,
        "n_history_required": min_history,
        "n_improving": n_improving,
        "n_consecutive_required": min_consecutive,
        "min_delta": min_delta,
    }


def _write_stage2_brier_history_row(
    path: Path,
    *,
    production_brier: Optional[float],
    staging_brier: Optional[float],
    delta: Optional[float],
    data_max_date: Optional[str],
    generated_at_utc: str,
) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "generated_at_utc": generated_at_utc,
            "data_max_date": data_max_date,
            "production_brier": production_brier,
            "staging_brier": staging_brier,
            "delta": delta,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as exc:
        LOGGER.warning(
            "Failed to append stage2 brier history row to %s: %s. "
            "Promotion stability gate has nothing to read on the next refresh.",
            path, exc,
        )


def _stage1_total_games(payload: object) -> Optional[int]:
    """Read total_games out of the Stage-1 cache meta block (canonical
    schema: payload["meta"]["total_games"], fallback "games_loaded")."""
    if not isinstance(payload, dict):
        return None
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None
    for key in ("total_games", "games_loaded"):
        v = meta.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return None


def _stage1_promotion_guard(
    staging_payload: object,
    production_payload: Optional[object],
    *,
    active_date: str,
    min_games_ratio: float = STAGE1_PROMOTE_MIN_GAMES_RATIO,
) -> Tuple[bool, str]:
    """Decide whether the staging Stage-1 cache is safe to promote.

    Returns (ok_to_promote, reason). Conservative: any structural problem
    blocks promotion. The legitimate first-run case (production missing)
    is allowed as long as the staging cache passes its own coverage check.
    """
    if not isinstance(staging_payload, dict):
        return False, "staging payload is not a dict"
    staging_games = _stage1_total_games(staging_payload)
    if staging_games is None or staging_games <= 0:
        return False, "staging cache has no total_games metadata; refusing to promote"

    coverage_ok, coverage_note = _stage1_cache_health(staging_payload, active_date)
    if not coverage_ok or coverage_note.startswith("WARNING"):
        return False, f"staging coverage check rejected: {coverage_note}"

    if production_payload is None or not isinstance(production_payload, dict):
        return True, (
            f"production cache missing; promoting staging "
            f"(games={staging_games}). First-run case."
        )
    prod_games = _stage1_total_games(production_payload)
    if prod_games is None or prod_games <= 0:
        # Production exists but is malformed; staging is at least readable.
        return True, (
            f"production cache present but missing total_games; promoting staging "
            f"(games={staging_games}) to recover."
        )
    floor = int(prod_games * min_games_ratio)
    if staging_games < floor:
        return False, (
            f"staging has {staging_games} games vs production {prod_games} "
            f"(< {min_games_ratio:.0%} floor of {floor}); refusing to promote. "
            "Likely a partial scrape; investigate before retrying."
        )
    return True, (
        f"sanity guard passed: staging {staging_games} games >= "
        f"{min_games_ratio:.0%} of production {prod_games}."
    )


@_inline("stage1_cache_promote")
def _handle_stage1_cache_promote(config: RefreshConfig) -> Tuple[bool, str]:
    """Promote the staging Stage-1 cache to production after a sanity check.

    Stage-1 is a deterministic empirical lookup, not learned weights, so
    the right default is auto-promote; the guard's only job is to refuse
    when the staging cache looks broken (partial scrape, corrupt file,
    season window narrowed). Never fails the refresh; descriptive only.
    """
    notes: List[str] = []
    staging_path = DEFAULT_MLB_OU_CACHE_STAGING_PATH
    prod_path = config.mlb_ou_cache_path

    if not staging_path.exists():
        notes.append(
            f"Stage-1 staging cache missing at {staging_path.name} (rebuild step skipped?). "
            "Production cache untouched."
        )
        return True, "\n".join(notes)

    staging_payload, staging_err = _safe_load_json(staging_path)
    if staging_err:
        notes.append(
            f"ALERT Stage-1 staging cache unreadable ({staging_err}); refusing to promote."
        )
        return True, "\n".join(notes)
    prod_payload, _ = _safe_load_json(prod_path)
    promote_ok, reason = _stage1_promotion_guard(
        staging_payload, prod_payload, active_date=config.active_date
    )
    if not promote_ok:
        notes.append(
            f"ALERT Stage-1 promotion BLOCKED: {reason} "
            f"Production cache at {prod_path.name} kept; "
            f"inspect {staging_path.name} before next refresh."
        )
        return True, "\n".join(notes)
    # Promote: atomic on-disk swap (write-temp + replace) so a crash
    # mid-promotion can't leave a half-written production file.
    try:
        prod_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = prod_path.with_suffix(prod_path.suffix + ".promote_tmp")
        # On Windows os.replace handles cross-file atomic move.
        tmp_path.write_bytes(staging_path.read_bytes())
        os.replace(tmp_path, prod_path)
    except OSError as exc:
        notes.append(
            f"ALERT Stage-1 promotion FAILED during file swap: {exc!r}. "
            f"Production cache at {prod_path.name} may be in inconsistent state."
        )
        return True, "\n".join(notes)
    notes.append(
        f"ok Stage-1 promoted: {staging_path.name} -> {prod_path.name}. {reason}"
    )
    return True, "\n".join(notes)


def _extract_stage3_v2_research_betas(payload: object) -> Optional[Dict[str, float]]:
    """Pull the three coefficients out of phase4_models.json.

    Schema: payload["models"]["model_3_blend"]["beta_prior" | "beta_season" |
    "beta_momentum"]. Older keys ("model_3", "Model 3") are accepted for
    forward compat with promote_team_offense_v2.py's lookup.
    """
    if not isinstance(payload, dict):
        return None
    models = payload.get("models")
    if not isinstance(models, dict):
        return None
    fit = None
    for key in ("model_3_blend", "model_3", "Model 3", "model3", "phase4_model3"):
        if key in models and isinstance(models[key], dict):
            fit = models[key]
            break
    if fit is None:
        return None
    out: Dict[str, float] = {}
    for src_key, dest_key in (
        ("beta_prior", "prior_season"),
        ("prior_season", "prior_season"),
        ("beta_prior_season", "prior_season"),
        ("beta_season", "season_to_date"),
        ("season_to_date", "season_to_date"),
        ("beta_season_to_date", "season_to_date"),
        ("beta_momentum", "momentum_10"),
        ("momentum_10", "momentum_10"),
        ("beta_momentum_10", "momentum_10"),
    ):
        if src_key in fit and isinstance(fit[src_key], (int, float)):
            out.setdefault(dest_key, float(fit[src_key]))
    if set(out) != {"prior_season", "season_to_date", "momentum_10"}:
        return None
    return out


def _extract_stage3_v2_active_betas(prod_payload: object) -> Tuple[Dict[str, float], str]:
    """Return (betas_in_use, source_label).

    Prefers the production weights JSON when it exists and is well-formed;
    falls back to the compiled-in defaults so the comparison still works
    on first promotion.
    """
    if isinstance(prod_payload, dict):
        betas = prod_payload.get("betas")
        if isinstance(betas, dict):
            try:
                return (
                    {
                        "prior_season": float(betas["prior_season"]),
                        "season_to_date": float(betas["season_to_date"]),
                        "momentum_10": float(betas["momentum_10"]),
                    },
                    "production_weights_file",
                )
            except (KeyError, TypeError, ValueError):
                pass
    return dict(STAGE3_V2_COMPILED_DEFAULTS), "compiled_defaults"


def _stage3_v2_max_abs_delta(
    research: Dict[str, float], active: Dict[str, float]
) -> float:
    return max(abs(research[k] - active[k]) for k in ("prior_season", "season_to_date", "momentum_10"))


def _load_stage3_v2_drift_history(path: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    if not path.exists():
        return rows
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rows.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def _trailing_stage3_v2_history(
    history_rows: List[Dict[str, object]],
    *,
    window: int,
    exclude_date: Optional[str] = None,
) -> List[Dict[str, object]]:
    """Same shape as Stage-2's trailing helper: dedupe same-date rows to
    the latest entry, return last `window` distinct dates oldest-first."""
    by_date: Dict[str, Dict[str, object]] = {}
    for row in history_rows:
        d = _stage2_history_row_date(row)  # same date-key fallback semantics
        if not d:
            continue
        if exclude_date and d == exclude_date:
            continue
        by_date[d] = row
    if not by_date:
        return []
    ordered = sorted(by_date.items(), key=lambda kv: kv[0])
    return [v for _, v in ordered[-window:]]


def _stage3_v2_primary_verdict(
    history_rows: List[Dict[str, object]],
    *,
    window: int,
    min_history: int,
    min_consecutive: int,
    drift_threshold: float,
    exclude_date: Optional[str],
) -> Dict[str, object]:
    """Primary count-based gate. Returns a verdict dict in the same shape
    `_stage3_v2_promotion_verdict` historically returned (insufficient_history
    / hold / promote). Pulled out so the verdict-stability gate can replay
    it against per-date slices of history."""
    trailing = _trailing_stage3_v2_history(
        history_rows, window=window, exclude_date=exclude_date
    )
    n_history = len(trailing)
    if n_history < min_history:
        return {
            "verdict": "insufficient_history",
            "n_history": n_history,
            "n_history_required": min_history,
            "n_drifting": 0,
            "n_consecutive_required": min_consecutive,
            "drift_threshold": drift_threshold,
        }
    n_drifting = 0
    for row in trailing:
        d = row.get("max_abs_delta")
        if isinstance(d, (int, float)) and float(d) >= drift_threshold:
            n_drifting += 1
    if n_drifting >= min_consecutive:
        verdict = "promote"
    else:
        verdict = "hold"
    return {
        "verdict": verdict,
        "n_history": n_history,
        "n_history_required": min_history,
        "n_drifting": n_drifting,
        "n_consecutive_required": min_consecutive,
        "drift_threshold": drift_threshold,
    }


def _stage3_v2_distinct_history_dates(
    history_rows: List[Dict[str, object]],
    *,
    exclude_date: Optional[str] = None,
) -> List[str]:
    """Sorted unique dates present in history (ignoring `exclude_date`)."""
    seen: set = set()
    for row in history_rows:
        d = _stage2_history_row_date(row)
        if d and d != exclude_date:
            seen.add(d)
    return sorted(seen)


def _stage3_v2_verdict_stability_gate(
    history_rows: List[Dict[str, object]],
    pre_override_verdict: str,
    *,
    stability_window: int,
    stability_min_history: int,
    primary_window: int,
    primary_min_history: int,
    primary_min_consecutive: int,
    primary_drift_threshold: float,
    exclude_date: Optional[str],
) -> Tuple[str, Dict[str, object]]:
    """Second-layer modal check on the verdict itself. Replays the primary
    count-based verdict on each prior distinct date (with history sliced
    to <= that date) to build a trailing verdict history, then takes the
    modal of the last `stability_window` distinct dates. If today's
    verdict differs from an unambiguous modal AND we have at least
    `stability_min_history` computable prior dates, override to the modal.

    Returns (final_verdict, audit). Audit shape mirrors the calibration
    stability-gate audit so the daily review block can render either
    one with the same template.
    """
    audit: Dict[str, object] = {
        "verdict_stability_gate_enabled": True,
        "verdict_stability_window": stability_window,
        "verdict_stability_min_history": stability_min_history,
        "verdict_stability_history": [],
        "verdict_stability_modal": None,
        "verdict_stability_gate_applied": False,
        "pre_override_verdict": pre_override_verdict,
    }
    prior_dates = _stage3_v2_distinct_history_dates(
        history_rows, exclude_date=exclude_date,
    )
    if not prior_dates:
        return pre_override_verdict, audit
    # Replay the primary verdict for each prior date in the window.
    trailing_dates = prior_dates[-stability_window:]
    trailing_verdicts: List[str] = []
    for date_anchor in trailing_dates:
        # `exclude_date=date_anchor+1d` would be the cleanest semantics
        # but we only have a date list, not a calendar. Easiest: pass
        # the slice helper an artificial exclude_date that's later than
        # `date_anchor`. Use a sentinel "z" greater than any 4-digit
        # year prefix to avoid excluding any of the prior dates.
        sliced = [
            r for r in history_rows
            if _stage2_history_row_date(r) and _stage2_history_row_date(r) <= date_anchor
        ]
        v = _stage3_v2_primary_verdict(
            sliced,
            window=primary_window,
            min_history=primary_min_history,
            min_consecutive=primary_min_consecutive,
            drift_threshold=primary_drift_threshold,
            exclude_date=None,
        )
        trailing_verdicts.append(str(v.get("verdict") or ""))
    audit["verdict_stability_history"] = trailing_verdicts
    # Filter "insufficient_history" rows out of the modal -- those days
    # aren't a real signal one way or the other.
    voting = [v for v in trailing_verdicts if v in ("promote", "hold")]
    if len(voting) < stability_min_history:
        return pre_override_verdict, audit
    counts: Dict[str, int] = {}
    for v in voting:
        counts[v] = counts.get(v, 0) + 1
    if not counts:
        return pre_override_verdict, audit
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    modal, modal_count = top[0]
    if len(top) > 1 and top[1][1] == modal_count:
        # Ambiguous modal (tie) -- don't override on an arbitrary tie-break.
        return pre_override_verdict, audit
    audit["verdict_stability_modal"] = modal
    if pre_override_verdict in ("promote", "hold") and modal != pre_override_verdict:
        audit["verdict_stability_gate_applied"] = True
        return modal, audit
    return pre_override_verdict, audit


def _stage3_v2_promotion_verdict(
    history_rows: List[Dict[str, object]],
    *,
    window: int = STAGE3_V2_PROMOTION_WINDOW,
    min_history: int = STAGE3_V2_PROMOTION_MIN_HISTORY,
    min_consecutive: int = STAGE3_V2_PROMOTION_MIN_CONSECUTIVE,
    drift_threshold: float = STAGE3_V2_PROMOTION_DRIFT_THRESHOLD,
    exclude_date: Optional[str] = None,
    stability_gate_enabled: bool = True,
    stability_window: int = STAGE3_V2_VERDICT_STABILITY_WINDOW,
    stability_min_history: int = STAGE3_V2_VERDICT_STABILITY_MIN_HISTORY,
) -> Dict[str, object]:
    """Stage-3 v2 promotion verdict with two-layer stability:
      - Primary: n_drifting >= min_consecutive of trailing `window` dates.
      - Secondary (when `stability_gate_enabled`): modal of the trailing
        `stability_window` per-date primary verdicts. If today differs
        from an unambiguous modal, override to the modal.

    The primary gate is the data-signal stability primitive (does the
    underlying drift hold?). The secondary gate prevents the 5-of-7
    boundary flap (one day's max_abs_delta crossing 0.015 swings the
    primary verdict promote <-> hold; the modal smooths that out).

    Disable with `stability_gate_enabled=False` to backfill or debug.
    """
    primary = _stage3_v2_primary_verdict(
        history_rows,
        window=window,
        min_history=min_history,
        min_consecutive=min_consecutive,
        drift_threshold=drift_threshold,
        exclude_date=exclude_date,
    )
    primary_verdict_label = str(primary.get("verdict") or "")
    if not stability_gate_enabled:
        primary["verdict_stability_gate_enabled"] = False
        return primary
    final_verdict, stability_audit = _stage3_v2_verdict_stability_gate(
        history_rows,
        primary_verdict_label,
        stability_window=stability_window,
        stability_min_history=stability_min_history,
        primary_window=window,
        primary_min_history=min_history,
        primary_min_consecutive=min_consecutive,
        primary_drift_threshold=drift_threshold,
        exclude_date=exclude_date,
    )
    primary["verdict"] = final_verdict
    primary.update(stability_audit)
    return primary


def _write_stage3_v2_drift_history_row(
    path: Path,
    *,
    research_betas: Dict[str, float],
    active_betas: Dict[str, float],
    active_source: str,
    max_abs_delta: float,
    data_max_date: Optional[str],
    generated_at_utc: str,
) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "generated_at_utc": generated_at_utc,
            "data_max_date": data_max_date,
            "research_betas": research_betas,
            "active_betas": active_betas,
            "active_source": active_source,
            "max_abs_delta": max_abs_delta,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as exc:
        LOGGER.warning(
            "Failed to append stage3-v2 drift history row to %s: %s. "
            "Promotion stability gate has nothing to read on the next refresh.",
            path, exc,
        )


@_inline("stage3_v2_promotion_check")
def _handle_stage3_v2_promotion_check(config: RefreshConfig) -> Tuple[bool, str]:
    """Detect when the daily Stage-3 v2 research fit has materially drifted
    from the currently-active production weights, with a stability gate so
    single-day fit noise doesn't fire a promotion alert.

    Never fails the refresh; descriptive only.
    """
    notes: List[str] = []
    research_path = DEFAULT_STAGE3_V2_RESEARCH_FIT_PATH
    prod_path = DEFAULT_STAGE3_V2_PROD_WEIGHTS_PATH
    history_path = config.stage3_v2_drift_history_path

    research_payload, research_err = _safe_load_json(research_path)
    if research_err:
        notes.append(
            f"Stage-3 v2 promotion check skipped: research fit unreadable ({research_err})."
        )
        return True, "\n".join(notes)
    research_betas = _extract_stage3_v2_research_betas(research_payload)
    if research_betas is None:
        notes.append(
            f"Stage-3 v2 promotion check skipped: could not extract model_3_blend "
            f"betas from {research_path.name}."
        )
        return True, "\n".join(notes)

    prod_payload, _ = _safe_load_json(prod_path)
    active_betas, active_source = _extract_stage3_v2_active_betas(prod_payload)
    max_abs_delta = _stage3_v2_max_abs_delta(research_betas, active_betas)

    notes.append(
        f"Stage-3 v2: research vs {active_source} max|delta|={max_abs_delta:.4f} "
        f"(prior {research_betas['prior_season']:+.4f} vs {active_betas['prior_season']:+.4f}, "
        f"season {research_betas['season_to_date']:+.4f} vs {active_betas['season_to_date']:+.4f}, "
        f"momentum {research_betas['momentum_10']:+.4f} vs {active_betas['momentum_10']:+.4f})."
    )

    today_date_key = (config.max_date or config.active_date or "")[:10] or None
    _write_stage3_v2_drift_history_row(
        history_path,
        research_betas=research_betas,
        active_betas=active_betas,
        active_source=active_source,
        max_abs_delta=max_abs_delta,
        data_max_date=today_date_key,
        generated_at_utc=_now_iso(),
    )
    history_rows = _load_stage3_v2_drift_history(history_path)
    verdict = _stage3_v2_promotion_verdict(
        history_rows, exclude_date=today_date_key
    )
    v_label = verdict["verdict"]
    if v_label == "promote":
        notes.append(
            f"ALERT Stage-3 v2 PROMOTION READY: max|delta| >= "
            f"{verdict['drift_threshold']:.4f} on {verdict['n_drifting']}/"
            f"{verdict['n_history']} of the last {STAGE3_V2_PROMOTION_WINDOW} "
            f"distinct dates (threshold {verdict['n_consecutive_required']}). "
            f"Promote with: python scripts/analysis/promote_team_offense_v2.py."
        )
    elif v_label == "hold":
        notes.append(
            f"ok Stage-3 v2 promotion stability gate: hold "
            f"({verdict['n_drifting']}/{verdict['n_history']} drifting days, "
            f"need {verdict['n_consecutive_required']})."
        )
    else:  # insufficient_history
        notes.append(
            f"ok Stage-3 v2 promotion stability gate: building history "
            f"({verdict['n_history']}/{verdict['n_history_required']} distinct prior dates)."
        )
    if active_source == "compiled_defaults":
        notes.append(
            "note Stage-3 v2 production weights file missing -- runtime is using "
            "compiled-in defaults. Drift is measured against those defaults until "
            "promote_team_offense_v2.py runs at least once."
        )
    return True, "\n".join(notes)


@_inline("model_freshness_health")
def _handle_model_freshness_health(config: RefreshConfig) -> Tuple[bool, str]:
    """Compare staging vs production Stage-2 + age-check model artifacts.

    Returns (ok, notes). Never fails the refresh -- this is descriptive
    only. The point is to surface promotion candidates and stale
    artifacts so they're visible at the end of every refresh.
    """
    notes: List[str] = []

    # Stage-2 staging vs production comparison.
    prod_path = config.stage2_cache_path
    staging_path = PROJECT_DIR / "cache" / "mlb_stage2_run_env.staging.json"
    if staging_path.exists():
        prod_payload, prod_err = _safe_load_json(prod_path)
        stg_payload, stg_err = _safe_load_json(staging_path)
        if prod_err or stg_err:
            notes.append(
                f"Stage-2 comparison skipped: prod_err={prod_err} staging_err={stg_err}"
            )
        else:
            prod_brier = _stage2_validation_brier(prod_payload)
            stg_brier = _stage2_validation_brier(stg_payload)
            if prod_brier is None or stg_brier is None:
                notes.append(
                    f"Stage-2 comparison: validation Brier not found on one side "
                    f"(prod={prod_brier} staging={stg_brier})."
                )
            else:
                delta = stg_brier - prod_brier
                if abs(delta) >= STAGE2_BRIER_DRIFT_THRESHOLD:
                    direction = "IMPROVES" if delta < 0 else "REGRESSES"
                    notes.append(
                        f"ALERT Stage-2 staging {direction} validation Brier by "
                        f"{abs(delta):.4f} ({stg_brier:.4f} vs {prod_brier:.4f}). "
                        f"Promote with: copy {staging_path.name} -> {prod_path.name}."
                    )
                else:
                    notes.append(
                        f"ok Stage-2 staging matches production within tolerance "
                        f"(staging Brier {stg_brier:.4f} vs prod {prod_brier:.4f})."
                    )
                # Append today's observation to history, then check the
                # multi-day promotion stability gate. The single-day diff
                # alert above is raw signal; this verdict is the actionable
                # "promote ready" gate that resists single-day noise.
                history_path = config.stage2_brier_history_path
                today_date_key = (config.max_date or config.active_date or "")[:10] or None
                _write_stage2_brier_history_row(
                    history_path,
                    production_brier=prod_brier,
                    staging_brier=stg_brier,
                    delta=delta,
                    data_max_date=today_date_key,
                    generated_at_utc=_now_iso(),
                )
                history_rows = _load_stage2_brier_history(history_path)
                verdict = _stage2_promotion_verdict(
                    history_rows,
                    exclude_date=today_date_key,
                )
                v_label = verdict["verdict"]
                if v_label == "promote":
                    notes.append(
                        f"ALERT Stage-2 PROMOTION READY: staging beat production by "
                        f">= {verdict['min_delta']:.4f} on "
                        f"{verdict['n_improving']}/{verdict['n_history']} of the last "
                        f"{STAGE2_PROMOTION_WINDOW} distinct dates "
                        f"(threshold {verdict['n_consecutive_required']}). "
                        f"Promote with: copy {staging_path.name} -> {prod_path.name}."
                    )
                elif v_label == "hold":
                    notes.append(
                        f"ok Stage-2 promotion stability gate: hold "
                        f"({verdict['n_improving']}/{verdict['n_history']} improving days, "
                        f"need {verdict['n_consecutive_required']})."
                    )
                else:  # insufficient_history
                    notes.append(
                        f"ok Stage-2 promotion stability gate: building history "
                        f"({verdict['n_history']}/{verdict['n_history_required']} "
                        f"distinct prior dates)."
                    )
    else:
        notes.append("Stage-2 staging artifact not present (retrain step skipped?).")

    # Stale-artifact age checks.
    age_targets = [
        ("Stage-2 production cache", config.stage2_cache_path),
        ("Stage-1 OU cache", config.mlb_ou_cache_path),
        ("EV-policy report",
         PROJECT_DIR / "data" / "analysis_output" / "ev_policy" / "ev_policy_report.json"),
        ("EV-policy win model",
         PROJECT_DIR / "data" / "analysis_output" / "model_baselines" / "signal_win_model.json"),
        ("EV-policy fill model",
         PROJECT_DIR / "data" / "analysis_output" / "model_baselines" / "execution_fill_model.json"),
        ("Calibration artifact",
         PROJECT_DIR / "data" / "analysis_output" / "calibration" / "signal_win_calibration.json"),
        ("Learned execution policy report",
         PROJECT_DIR / "data" / "analysis_output" / "execution_policy_prototype" / "learned_execution_policy_report.json"),
        ("Stage-3 v2 fit (phase4_models.json)",
         PROJECT_DIR / "data" / "analysis_output" / "team_offense_calibration" / "phase4_models.json"),
    ]
    for label, path in age_targets:
        age = _artifact_age_days(path)
        if age is None:
            notes.append(f"WARNING {label} missing at {path.name}")
            continue
        if age > STALE_MODEL_AGE_DAYS:
            notes.append(
                f"ALERT {label} is {age:.1f}d old (> {STALE_MODEL_AGE_DAYS}d). "
                "Check the corresponding rebuild step ran successfully."
            )
        else:
            notes.append(f"ok {label}: {age:.1f}d old")

    return True, "\n".join(notes)


def build_refresh_steps(config: RefreshConfig, session_dates: Sequence[str], max_date: Optional[str]) -> List[RefreshStep]:
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
        # Backfill recently-completed games. End date is yesterday so we never
        # download in-progress games for the active date.
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
                    str(PROJECT_DIR / "cache" / "mlb_ou_cache.staging.json"),
                ],
                staleness_check=StalenessCheck(
                    output_path=PROJECT_DIR / "cache" / "mlb_ou_cache.staging.json",
                    input_paths=(),
                    input_dir_mtime_roots=(
                        PROJECT_DIR / "data" / "games" / "regular",
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
        # Active #8 Alt-A staging cache (2026-05-17). Same history window
        # as the production builder, but `--smoothing-mode
        # empirical_when_available` materializes the runtime's on-the-fly
        # Alt-A shadow (today's shadow-override report: -6pp aggregate
        # bias on 30d) as a real cache file. Writes to a SEPARATE staging
        # path; NEVER auto-promoted. Operator runs `promote.py stage1
        # --source cache/mlb_ou_cache_alt_a.staging.json` after the
        # paper-mode validation period clears its bar.
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
                    str(PROJECT_DIR / "cache" / "mlb_ou_cache_alt_a.staging.json"),
                ],
                staleness_check=StalenessCheck(
                    output_path=PROJECT_DIR / "cache" / "mlb_ou_cache_alt_a.staging.json",
                    input_paths=(),
                    input_dir_mtime_roots=(
                        PROJECT_DIR / "data" / "games" / "regular",
                    ),
                ),
            )
        )

    if config.refresh_active_schedule:
        # Schedule-only refresh covering PRIOR MONTH + ACTIVE MONTH so
        # late-added or rescheduled games near the month boundary are
        # picked up before they roll out of the trailing-window game
        # scrape above. --dry-run writes the schedule file(s) without
        # downloading in-progress feeds. The trailing-window
        # `scrape_recent_games` step downloads the game files for any
        # game_pks that newly appear in the refreshed schedules.
        #
        # Before 2026-05-17: this step only covered the ACTIVE month,
        # which meant a game scheduled for Apr 25 but added to MLB's
        # API only on Apr 26 was never re-scraped (the May refresh
        # didn't go back to April). Active #12 settlement-truth
        # verification found 22 such missing game JSONs (24.7% of
        # filled bets) before this fix.
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
        # Tier-2 (2026-05-29): per-game umpire/officials cache. Gated on the
        # same flag as weather (both are active-date network enrichments;
        # --startup-refresh-skip-weather-cache skips both). Fail-open.
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
        # Explicit Stage-3 input rebuild. Without this, TeamOffenseModel.load()
        # rebuilds lazily on first-tick (1-day staleness), which can hang
        # startup or surface failures inside the live engine.
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
        # Stage-2 hr_factor family input. Per-(park, season) HR rate vs league
        # mean, shrunk toward 1.0 for low-N parks. Drifts as the season fills
        # in, so a daily rebuild keeps the most-recent-year fallback honest.
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

    if not max_date:
        return steps

    # Settlement-truth verification (Active #12, 2026-05-17).
    # Cross-checks every settled bet across all sessions against the
    # MLB Stats API ground truth. Refreshed BEFORE the per-date
    # daily-review steps below so the review's settlement_truth_health
    # block reads a fresh artifact. Read-only: no live state mutation.
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
        review_dir = PROJECT_DIR / "data" / "analysis_output" / "daily_human_review"
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

    strict_flag = ["--strict"] if config.strict else []
    max_date_args = ["--max-date", max_date]

    steps.extend(
        [
            RefreshStep(
                name="analysis_safe_trade_table",
                description=(
                    "Rebuild canonical analysis-safe trade table from session "
                    "JSONs plus deduped live/master ledgers; excludes "
                    "order_status=error attempts by default."
                ),
                command=[
                    _python(),
                    _script("scripts/analysis/build_analysis_safe_trade_table.py"),
                    "--max-date",
                    max_date,
                    *strict_flag,
                ],
            ),
            RefreshStep(
                name="candidate_universe_table",
                description="Rebuild decision-level candidate table.",
                command=[
                    _python(),
                    _script("scripts/analysis/build_candidate_universe_table.py"),
                    "--mode",
                    "live",
                    *max_date_args,
                    *strict_flag,
                ],
            ),
            RefreshStep(
                name="calibration_opportunity_training",
                description="Rebuild model-bearing calibration-opportunity training table.",
                command=[
                    _python(),
                    _script("scripts/analysis/build_calibration_opportunity_training_table.py"),
                    "--mode",
                    "live",
                    *max_date_args,
                    *strict_flag,
                ],
            ),
            # Calibrate steps moved DOWN to after concept_drift_report
            # (2026-05-20 audit fix: previously the calibrators ran here
            # at refresh start, recording the PRIOR-DAY concept_drift hash;
            # then concept_drift_report rebuilt later in the same refresh,
            # leaving the calibrator's lineage hash stale -- 2 of 7
            # cross-artifact alerts every day. The reorder ensures the
            # calibrator records the FRESH concept_drift hash.)
            RefreshStep(
                name="model_maturity_report",
                description="Rebuild model family maturity/readiness report.",
                command=[
                    _python(),
                    _script("scripts/analysis/build_model_maturity_report.py"),
                    "--mode",
                    "live",
                    *max_date_args,
                ],
            ),
            RefreshStep(
                name="fair_value_stage_ablation",
                description="Rebuild FV stage ablation report (market, inference, Stage-2, Stage-3, final FV).",
                command=[
                    _python(),
                    _script("scripts/analysis/fair_value_stage_ablation_report.py"),
                    "--mode",
                    "live",
                    *max_date_args,
                ],
            ),
            RefreshStep(
                name="fv_gap_decomposition",
                description="Rebuild FV gap decomposition report (market/no-vig vs Poisson, empirical, Stage-2/3, final FV).",
                command=[
                    _python(),
                    _script("scripts/analysis/build_fv_gap_decomposition_report.py"),
                    "--mode",
                    "live",
                    *max_date_args,
                ],
            ),
            RefreshStep(
                name="fv_trust_shrinkage",
                description="Rebuild FV trust/shrinkage experiment (support-weighted market anchoring).",
                command=[
                    _python(),
                    _script("scripts/analysis/build_fv_trust_shrinkage_experiment.py"),
                    "--mode",
                    "live",
                    *max_date_args,
                ],
            ),
            RefreshStep(
                name="calibration_market_anchored_alpha",
                description="Train family-separated market-anchored alpha research models from calibration opportunities.",
                command=[
                    _python(),
                    _script("scripts/analysis/train_calibration_market_anchored_alpha.py"),
                    "--mode",
                    "live",
                    "--artifact-purpose",
                    "runtime-refit",
                    *max_date_args,
                ],
            ),
            RefreshStep(
                name="stage1_inferred_empirical_audit",
                description="Rebuild score-event Stage-1 Poisson-vs-empirical inferred-state audit.",
                command=[
                    _python(),
                    _script("scripts/analysis/audit_stage1_inferred_empirical.py"),
                    "--mode",
                    "live",
                    *max_date_args,
                ],
            ),
            RefreshStep(
                name="unified_signals",
                description=(
                    "Rebuild canonical event-level signal table. "
                    "Mode=both folds paper sessions in alongside live "
                    "(2026-05-19 fix discovered during paper-trading "
                    "audit; previously hardcoded live-only, which "
                    "blocked the paper-mode runway from feeding "
                    "loss-attribution + shadow-override + training "
                    "table). Mode tag on each row preserves the "
                    "live/paper distinction for downstream consumers "
                    "that need to filter (any metric using "
                    "realized P&L or fill behavior)."
                ),
                command=[
                    _python(),
                    _script("scripts/analysis/build_unified_signal_table.py"),
                    "--mode",
                    "both",
                    *max_date_args,
                    *strict_flag,
                ],
            ),
            RefreshStep(
                name="concept_drift_report",
                description=(
                    "Leading-indicator drift detection: PSI/TVD on the "
                    "model's input features (weather, Stage-2/3 deltas, "
                    "base FV, stadium mix) over a trailing 7d window vs "
                    "the prior 30d. Catches shifts in the inputs the live "
                    "model consumes BEFORE calibration error / cohort "
                    "losses materialize."
                ),
                command=[
                    _python(),
                    _script("scripts/analysis/build_concept_drift_report.py"),
                    "--active-date", config.active_date,
                ],
                staleness_check=StalenessCheck(
                    output_path=PROJECT_DIR / "data" / "analysis_output"
                    / "concept_drift" / "concept_drift_report.json",
                    input_paths=(
                        PROJECT_DIR / "data" / "analysis_output"
                        / "unified_signals" / "signals_master.jsonl",
                    ),
                ),
            ),
            # Calibrators MUST run AFTER concept_drift_report so their
            # lineage hashes reference the freshly-rebuilt drift report
            # (the calibrator records concept_drift_report_path in
            # input_paths -- see calibrate_signal_probabilities.py:1561).
            # Pre-2026-05-20: these ran early and recorded yesterday's
            # drift hash, producing daily cross-artifact stale alerts.
            RefreshStep(
                name="calibrate_signal_probabilities",
                description=(
                    "Refit fair_value probability calibration (per-family Platt/"
                    "isotonic) from the calibration-opportunity training table."
                ),
                command=[
                    _python(),
                    _script("scripts/analysis/calibrate_signal_probabilities.py"),
                    "--input-path",
                    str(
                        PROJECT_DIR
                        / "data"
                        / "analysis_output"
                        / "calibration_opportunity_training"
                        / "calibration_opportunity_training_table.jsonl"
                    ),
                    "--input-kind",
                    "auto",
                    "--family-mode",
                    "separate",
                    "--artifact-purpose",
                    "runtime-refit",
                    "--mode",
                    "live",
                    *max_date_args,
                    *strict_flag,
                ],
            ),
            RefreshStep(
                name="calibrate_signal_probabilities_under",
                description=(
                    "Phase A2 (2026-05-16): refit the UNDER-side fair_value "
                    "probability calibration with flipped labels + raw probs. "
                    "Same per-family Platt/isotonic machinery as Over; "
                    "separate artifact (signal_win_calibration_under.json) "
                    "and separate stability-gate selection history. UNDER "
                    "calibration stays offline / shadow until Phase B/C "
                    "wire it into the live engine."
                ),
                command=[
                    _python(),
                    _script("scripts/analysis/calibrate_signal_probabilities.py"),
                    "--side",
                    "under",
                    "--input-path",
                    str(
                        PROJECT_DIR
                        / "data"
                        / "analysis_output"
                        / "calibration_opportunity_training"
                        / "calibration_opportunity_training_table.jsonl"
                    ),
                    "--input-kind",
                    "auto",
                    "--family-mode",
                    "separate",
                    "--artifact-purpose",
                    "runtime-refit",
                    "--mode",
                    "live",
                    *max_date_args,
                    *strict_flag,
                ],
            ),
            RefreshStep(
                name="drift_in_drift_report",
                description=(
                    "Slow-creep drift: linear-trend fit on the trailing 30d "
                    "of psi_history.jsonl, projected 30d forward. Catches "
                    "features that drift <0.25 PSI per day but accumulate "
                    "past the major threshold over weeks -- a failure mode "
                    "the day-vs-baseline concept_drift_report can't see."
                ),
                command=[
                    _python(),
                    _script("scripts/analysis/build_drift_in_drift_report.py"),
                    "--active-date", config.active_date,
                ],
                staleness_check=StalenessCheck(
                    output_path=PROJECT_DIR / "data" / "analysis_output"
                    / "concept_drift" / "drift_in_drift_report.json",
                    input_paths=(
                        PROJECT_DIR / "data" / "analysis_output"
                        / "concept_drift" / "psi_history.jsonl",
                    ),
                ),
            ),
            RefreshStep(
                name="signal_training_table",
                description=(
                    "Rebuild leakage-aware training table. Mode=both "
                    "pairs with the unified_signals --mode both change "
                    "(2026-05-19) so paper bets carrying Alt-A shadow "
                    "fields reach loss-attribution + shadow-override "
                    "reports. Safe because both reports use the `won` "
                    "boolean (counterfactual: did the over/under hit), "
                    "which is identical for paper and live bets -- "
                    "paper's 100% taker assumption only distorts "
                    "realized_profit/realized_executed, which these "
                    "reports do not read. Other refresh steps that "
                    "DO read fill behavior (clv_report, "
                    "execution_diagnostics, ev_policy_backtest, "
                    "queue_aware_execution_replay) stay --mode live "
                    "intentionally."
                ),
                command=[
                    _python(),
                    _script("scripts/analysis/build_signal_training_table.py"),
                    "--mode",
                    "both",
                    *max_date_args,
                    *strict_flag,
                ],
            ),
            RefreshStep(
                name="clv_report",
                description=(
                    "Rebuild closing/late-price value diagnostics: entry "
                    "price vs late captured mid, grouped by family/gate/bucket "
                    "and compared with realized ROI."
                ),
                command=[
                    _python(),
                    _script("scripts/analysis/build_clv_report.py"),
                    "--mode",
                    "live",
                    *max_date_args,
                    *strict_flag,
                ],
                staleness_check=StalenessCheck(
                    output_path=PROJECT_DIR / "data" / "analysis_output" / "clv" / "clv_summary.json",
                    input_paths=(
                        PROJECT_DIR / "data" / "analysis_output" / "unified_signals" / "signals_master.jsonl",
                        PROJECT_DIR / "data" / "analysis_output" / "unified_signals" / "signal_book_snapshots.jsonl",
                        PROJECT_DIR / "data" / "analysis_output" / "analysis_safe_trades" / "analysis_safe_trades.jsonl",
                        PROJECT_DIR / "data" / "analysis_output" / "calibration_opportunity_training" / "calibration_opportunity_training_table.jsonl",
                    ),
                ),
            ),
            RefreshStep(
                name="fv_disagreement_quality",
                description=(
                    "Rebuild FV-vs-market disagreement quality diagnostics: "
                    "when raw FV disagrees with market, report calibration "
                    "gain, CLV, ROI, support/trust, and family bucket ranks."
                ),
                command=[
                    _python(),
                    _script("scripts/analysis/build_fv_disagreement_quality_report.py"),
                    "--mode",
                    "live",
                    *max_date_args,
                    *strict_flag,
                ],
                staleness_check=StalenessCheck(
                    output_path=PROJECT_DIR
                    / "data" / "analysis_output"
                    / "fv_disagreement_quality"
                    / "fv_disagreement_quality_summary.json",
                    input_paths=(
                        PROJECT_DIR
                        / "data" / "analysis_output"
                        / "calibration_opportunity_training"
                        / "calibration_opportunity_training_table.jsonl",
                        PROJECT_DIR / "data" / "analysis_output" / "clv" / "clv_rows.jsonl",
                    ),
                ),
            ),
            RefreshStep(
                name="calibration_edge_shaving",
                description=(
                    "Quantify how much edge the current probability calibrator "
                    "shaves off post-structural score-event candidates and "
                    "whether the shrinkage is justified by realized win rates. "
                    "Emits a recommended --prob-calibration-enforce-min-raw "
                    "(0.90->0.95 as of first run); feeds the manual lever "
                    "decision + the L_enforce_min_raw_095 paper A/B."
                ),
                command=[
                    _python(),
                    _script("scripts/analysis/analyze_calibration_edge_shaving.py"),
                ],
                staleness_check=StalenessCheck(
                    output_path=PROJECT_DIR
                    / "data" / "analysis_output"
                    / "calibration_edge_shaving"
                    / "calibration_edge_shaving.json",
                    input_paths=(
                        PROJECT_DIR
                        / "data" / "analysis_output"
                        / "calibration_opportunity_training"
                        / "by_family"
                        / "calibration_opportunity_training_table_score_event_transition.jsonl",
                        PROJECT_DIR
                        / "data" / "analysis_output"
                        / "calibration"
                        / "signal_win_calibration.json",
                    ),
                ),
            ),
            RefreshStep(
                name="train_baseline_models",
                description=(
                    "Rebuild EV-policy win + fill baseline models from the "
                    "leakage-aware training table. Consumed by EV-policy "
                    "shadow scoring at next live-engine startup."
                ),
                command=[
                    _python(),
                    _script("scripts/analysis/train_baseline_models.py"),
                    *strict_flag,
                ],
                staleness_check=StalenessCheck(
                    output_path=PROJECT_DIR / "data" / "analysis_output"
                    / "model_baselines" / "signal_win_model.json",
                    input_paths=(
                        PROJECT_DIR / "data" / "analysis_output"
                        / "training_tables" / "signal_training_table.jsonl",
                    ),
                ),
            ),
            RefreshStep(
                name="ev_policy_backtest",
                description=(
                    "Rebuild EV-policy backtest report. Produces "
                    "ev_policy_report.json that runtime EV scoring reads."
                ),
                command=[
                    _python(),
                    _script("scripts/analysis/backtest_ev_policy.py"),
                    "--policy-mode",
                    "live",
                    "--artifact-purpose",
                    "runtime-refit",
                    *strict_flag,
                ],
                staleness_check=StalenessCheck(
                    output_path=PROJECT_DIR / "data" / "analysis_output"
                    / "ev_policy" / "ev_policy_report.json",
                    input_paths=(
                        PROJECT_DIR / "data" / "analysis_output"
                        / "training_tables" / "signal_training_table.jsonl",
                        PROJECT_DIR / "data" / "analysis_output"
                        / "model_baselines" / "signal_win_model.json",
                        PROJECT_DIR / "data" / "analysis_output"
                        / "model_baselines" / "execution_fill_model.json",
                    ),
                ),
            ),
            RefreshStep(
                name="stage2_run_env_retrain_staging",
                description=(
                    "Refit Stage-2 run-env model on the latest game corpus and "
                    "write to a STAGING path (cache/mlb_stage2_run_env.staging.json). "
                    "The comparison step downstream alerts when the staged model "
                    "would change Brier; the production cache is NEVER overwritten "
                    "by this step."
                ),
                command=[
                    _python(),
                    _script("cache/build_mlb_stage2_run_env.py"),
                    "--out",
                    str(PROJECT_DIR / "cache" / "mlb_stage2_run_env.staging.json"),
                ],
                # Stage-2 is the biggest single-step cost (956s on the
                # 2026-05-12 run). Skip if no new game files have arrived
                # since the staging artifact was last written.
                staleness_check=StalenessCheck(
                    output_path=PROJECT_DIR / "cache" / "mlb_stage2_run_env.staging.json",
                    input_paths=(
                        PROJECT_DIR / "cache" / "mlb_ou_cache.json",
                        PROJECT_DIR / "cache" / "park_hr_factors.json",
                    ),
                    input_dir_mtime_roots=(
                        PROJECT_DIR / "data" / "games" / "regular",
                    ),
                ),
            ),
            RefreshStep(
                name="stage3_team_offense_features",
                description="Rebuild leakage-free team-offense feature matrix (Stage-3 v2 input).",
                command=[
                    _python(),
                    _script("scripts/analysis/build_team_offense_features.py"),
                ],
                staleness_check=StalenessCheck(
                    output_path=PROJECT_DIR / "data" / "analysis_output"
                    / "team_offense_calibration" / "team_features.jsonl",
                    input_paths=(
                        PROJECT_DIR / "cache" / "team_game_log.json",
                    ),
                ),
            ),
            RefreshStep(
                name="stage3_team_offense_calibration_table",
                description="Rebuild Stage-3 v2 calibration table (per (game, half, line) rows + features).",
                command=[
                    _python(),
                    _script("scripts/analysis/build_team_offense_calibration_table.py"),
                ],
                staleness_check=StalenessCheck(
                    output_path=PROJECT_DIR / "data" / "analysis_output"
                    / "team_offense_calibration" / "team_offense_calibration_table.jsonl",
                    input_paths=(
                        PROJECT_DIR / "cache" / "mlb_ou_cache.json",
                        PROJECT_DIR / "data" / "analysis_output"
                        / "team_offense_calibration" / "team_features.jsonl",
                    ),
                    input_dir_mtime_roots=(
                        PROJECT_DIR / "data" / "games" / "regular",
                    ),
                ),
            ),
            RefreshStep(
                name="stage3_team_offense_v2_fit",
                description=(
                    "Refit Stage-3 v2 team-offense weights. Output goes to "
                    "data/analysis_output/team_offense_calibration/phase4_models.json "
                    "(research path; production weights are compiled into "
                    "team_offense_model.py and require an explicit promotion)."
                ),
                command=[
                    _python(),
                    _script("scripts/analysis/calibrate_team_offense_v2.py"),
                ],
                staleness_check=StalenessCheck(
                    output_path=PROJECT_DIR / "data" / "analysis_output"
                    / "team_offense_calibration" / "phase4_models.json",
                    input_paths=(
                        PROJECT_DIR / "data" / "analysis_output"
                        / "team_offense_calibration" / "team_offense_calibration_table.jsonl",
                        PROJECT_DIR / "data" / "analysis_output"
                        / "team_offense_calibration" / "team_features.jsonl",
                    ),
                ),
            ),
            RefreshStep(
                name="model_freshness_health",
                kind="inline",
                description=(
                    "Compare Stage-2 staging vs production cache and surface "
                    "any meaningful drift; flag stale model artifacts."
                ),
                command=[],
            ),
            RefreshStep(
                name="stage3_v2_promotion_check",
                kind="inline",
                description=(
                    "Diff today's Stage-3 v2 research fit (model_3_blend in "
                    "phase4_models.json) against the active production "
                    "weights or compiled-in defaults. Stability gate prevents "
                    "single-day fit noise from firing a promotion alert."
                ),
                command=[],
            ),
            RefreshStep(
                name="execution_diagnostics",
                description="Rebuild trade execution diagnostics.",
                command=[
                    _python(),
                    _script("scripts/analysis/build_execution_diagnostics_report.py"),
                    "--mode",
                    "live",
                    *max_date_args,
                    "--no-console-report",
                    *strict_flag,
                ],
            ),
            RefreshStep(
                name="queue_aware_execution_replay",
                description="Rebuild queue-aware execution replay.",
                command=[
                    _python(),
                    _script("scripts/analysis/build_queue_aware_execution_replay.py"),
                    "--mode",
                    "live",
                    *max_date_args,
                    *strict_flag,
                ],
            ),
            RefreshStep(
                name="learn_execution_policy",
                description=(
                    "Rebuild offline learned-execution-policy prototype "
                    "from the queue-aware replay."
                ),
                command=[
                    _python(),
                    _script("scripts/analysis/learn_execution_policy.py"),
                ],
            ),
            RefreshStep(
                name="state_value_transition_report",
                description="Rebuild state-value transition diagnostics.",
                command=[
                    _python(),
                    _script("scripts/analysis/build_state_value_transition_report.py"),
                    "--mode",
                    "live",
                    *max_date_args,
                ],
            ),
            RefreshStep(
                name="under_state_value_transition_report",
                description=(
                    "Phase A3 (2026-05-16): UNDER-side state-value transition "
                    "diagnostics. Flips outcome (under_hit = not over_hit), "
                    "computes under-side ROI from under_best_ask, inverts "
                    "regime classifiers (negative current-state edge is "
                    "positive for Under). Pure offline; no live trading "
                    "behavior change until Phase C."
                ),
                command=[
                    _python(),
                    _script(
                        "scripts/analysis/build_under_state_value_transition_report.py"
                    ),
                    "--mode",
                    "live",
                    *max_date_args,
                ],
            ),
            RefreshStep(
                name="under_candidate_universe",
                description=(
                    "Phase B A5 prereq (2026-05-16): synthesize UNDER "
                    "candidate-universe rows from OVER. For each OVER "
                    "candidate with under_pair_available=True AND a "
                    "computed fair_value_raw, emit a `<date>_under_"
                    "candidates.jsonl` sibling with flipped FV (UNDER "
                    "calibrator applied when loaded; fallback_flip from "
                    "OVER calibrated value otherwise), under_best_ask as "
                    "decision_ask, decision='shadow_under'. Downstream "
                    "consumers: B1 side-aware drift alerts, B3 per-side "
                    "session reporting, future side-aware walk-forward."
                ),
                command=[
                    _python(),
                    _script(
                        "scripts/analysis/build_under_candidate_universe.py"
                    ),
                    "--mode",
                    "live",
                ],
            ),
            RefreshStep(
                name="no_score_drift_policy",
                description="Rebuild no-score drift policy evaluator.",
                command=[
                    _python(),
                    _script("scripts/analysis/evaluate_no_score_drift_policy.py"),
                    "--mode",
                    "live",
                    *max_date_args,
                ],
            ),
            RefreshStep(
                name="no_score_drift_paper_ledger",
                description="Rebuild no-score drift paper policy ledger.",
                command=[
                    _python(),
                    _script("scripts/analysis/build_no_score_drift_paper_ledger.py"),
                    "--mode",
                    "live",
                    *max_date_args,
                    "--stake",
                    f"{config.stake:g}",
                    "--daily-budget",
                    f"{config.daily_budget:g}",
                    "--per-game-budget-fraction",
                    f"{config.per_game_budget_fraction:g}",
                    *strict_flag,
                ],
            ),
        ]
    )

    if config.run_walk_forward:
        steps.extend(
            [
                RefreshStep(
                    name="walk_forward_score_event",
                    description="Refresh score-event walk-forward research output.",
                    command=[
                        _python(),
                        _script("scripts/analysis/walk_forward_runner.py"),
                        "--mode",
                        "live",
                        "--end-date",
                        max_date,
                        *strict_flag,
                    ],
                    staleness_check=StalenessCheck(
                        output_path=PROJECT_DIR / "data" / "analysis_output"
                        / "walk_forward" / "summary.json",
                        input_paths=(
                            PROJECT_DIR / "data" / "analysis_output"
                            / "training_tables" / "signal_training_table.jsonl",
                        ),
                    ),
                ),
                RefreshStep(
                    name="walk_forward_no_score_drift",
                    description="Refresh no-score drift walk-forward research output.",
                    command=[
                        _python(),
                        _script("scripts/analysis/no_score_drift_walk_forward.py"),
                        "--mode",
                        "live",
                        *max_date_args,
                        "--end-date",
                        max_date,
                        *strict_flag,
                    ],
                ),
                RefreshStep(
                    name="walk_forward_market_anchored_alpha",
                    description=(
                        "Refresh family-separated market-anchored alpha "
                        "walk-forward research output with ask/no-vig "
                        "baselines and clustered policy-P&L intervals."
                    ),
                    command=[
                        _python(),
                        _script("scripts/analysis/calibration_market_anchored_alpha_walk_forward.py"),
                        "--mode",
                        "live",
                        *max_date_args,
                        "--end-date",
                        max_date,
                        *strict_flag,
                    ],
                    staleness_check=StalenessCheck(
                        output_path=PROJECT_DIR / "data" / "analysis_output"
                        / "calibration_market_anchored_alpha_walk_forward" / "summary.json",
                        input_paths=(
                            PROJECT_DIR / "data" / "analysis_output"
                            / "calibration_opportunity_training"
                            / "calibration_opportunity_training_table.jsonl",
                        ),
                    ),
                ),
                RefreshStep(
                    name="under_walk_forward",
                    description=(
                        "Phase A4 (2026-05-16): UNDER walk-forward "
                        "(signal_win only, flipped labels). No "
                        "execution_fill: no UNDER orders have ever "
                        "been posted, so no fill history to learn "
                        "from. Sibling to walk_forward_score_event."
                    ),
                    command=[
                        _python(),
                        _script("scripts/analysis/under_walk_forward_runner.py"),
                        "--mode",
                        "live",
                        "--end-date",
                        max_date,
                        *strict_flag,
                    ],
                    staleness_check=StalenessCheck(
                        output_path=PROJECT_DIR / "data" / "analysis_output"
                        / "under_walk_forward" / "summary.json",
                        input_paths=(
                            PROJECT_DIR / "data" / "analysis_output"
                            / "training_tables" / "signal_training_table.jsonl",
                        ),
                    ),
                ),
                RefreshStep(
                    name="walk_forward_fv_disagreement_quality",
                    description=(
                        "Refresh FV-vs-market disagreement bucket "
                        "walk-forward validation: train/validation-selected "
                        "trust buckets applied out of sample."
                    ),
                    command=[
                        _python(),
                        _script("scripts/analysis/fv_disagreement_quality_walk_forward.py"),
                        "--mode",
                        "live",
                        *max_date_args,
                        "--end-date",
                        max_date,
                        *strict_flag,
                    ],
                    staleness_check=StalenessCheck(
                        output_path=PROJECT_DIR / "data" / "analysis_output"
                        / "fv_disagreement_quality_walk_forward" / "summary.json",
                        input_paths=(
                            PROJECT_DIR
                            / "data" / "analysis_output"
                            / "calibration_opportunity_training"
                            / "calibration_opportunity_training_table.jsonl",
                            PROJECT_DIR / "data" / "analysis_output" / "clv" / "clv_rows.jsonl",
                        ),
                    ),
                ),
            ]
        )

    # Stake-scaling promotion analyzer. Reads filled+settled bets across
    # all session JSONs, buckets them by calibrated_stake_multiplier, and
    # emits a need_more_data / hold / promote verdict for Active #6 part 2.
    # Read-only over data/live_trading/sessions/.
    steps.append(RefreshStep(
        name="stake_scaling_promotion_analyzer",
        description=(
            "Rebuild the Active #6 stake-scaling promotion analyzer from "
            "all session JSONs that carry calibrated_stake_multiplier."
        ),
        command=[
            _python(),
            _script("scripts/analysis/analyze_stake_scaling_promotion.py"),
        ],
    ))

    # Walk-forward certification report (Active #1). Reads the
    # signal_training_table the prior step refreshed and produces the
    # per-cohort + per-gate scorecard the operator needs to read on
    # post-TR20+TR21 day 30. Refreshes daily so the report shape and
    # auto-degrading verdicts are proven before the data threshold lands;
    # on day 30 we just re-read the same path and the verdicts upgrade
    # from PRELIMINARY to READY.
    steps.append(RefreshStep(
        name="walk_forward_certification",
        description=(
            "Rebuild the Active #1 walk-forward certification report "
            "(per-cohort + per-gate scorecard with READY/PRELIMINARY/"
            "INSUFFICIENT verdict) from signal_training_table.jsonl."
        ),
        command=[
            _python(),
            _script("scripts/analysis/build_walk_forward_certification.py"),
        ],
    ))

    # Active #8 prep (2026-05-17): Stage-1 shadow override report.
    # Replays two candidate Stage-1 fixes (empirical-when-available +
    # block-deep-fallback) against actual training-table outcomes and
    # surfaces "if we'd shipped this alt, the trailing-30d bias would
    # have been X% instead of Y%". The shadow-first evidence path
    # that precedes the eventual Active #8 runtime change.
    steps.append(RefreshStep(
        name="stage1_shadow_override_report",
        description=(
            "Replay two candidate Stage-1 fixes (empirical-when-"
            "available + block-deep-fallback) against actual bet "
            "outcomes; surface counterfactual bias deltas + P&L "
            "delta + recommendation verdicts so Active #8's runtime "
            "change has shadow evidence before promotion."
        ),
        command=[
            _python(),
            _script(
                "scripts/analysis/build_stage1_shadow_override_report.py"
            ),
        ],
    ))

    # Active #10 follow-up (2026-05-17): Stage-1 cell-conditional loss
    # attribution. Drills the standard loss attribution into Stage-1's
    # INTERNAL cohort dimensions (fallback level, line fallback mode,
    # used_fallback, sample size, Poisson-vs-empirical gap) so the
    # operator can see WHICH Stage-1 cells own the bias before
    # Active #8 rebuilds the cache. Reads signal_training_table.jsonl;
    # output under data/analysis_output/stage1_cell_loss_attribution/.
    steps.append(RefreshStep(
        name="stage1_cell_loss_attribution",
        description=(
            "Drill today's Active #10 finding (Stage-1 owns the "
            "aggregate bias) into Stage-1-internal cohort cuts: "
            "fallback level, line fallback mode, used_fallback, "
            "cell sample size, Poisson-vs-empirical gap. Surfaces "
            "the cohort culprits that narrow Active #8's retrain "
            "surface from 'rebuild Stage-1' to 'fix THIS Stage-1 "
            "cohort.'"
        ),
        command=[
            _python(),
            _script(
                "scripts/analysis/build_stage1_cell_loss_attribution.py"
            ),
        ],
    ))

    # Active #10 (2026-05-17): bet-level loss attribution report.
    # Decomposes each filled+settled bet's calibrated FV via the
    # logit-additive chain (p0 -> p1 -> p2 -> p3) into per-stage
    # probability contributions, then aggregates across the trailing
    # window to surface which stage owns the largest share of the
    # aggregate bias (mean_p3 - mean_won). Pure offline analysis;
    # reads signal_training_table.jsonl, writes under
    # data/analysis_output/loss_attribution/.
    steps.append(RefreshStep(
        name="loss_attribution_report",
        description=(
            "Rebuild the Active #10 bet-level loss attribution "
            "report -- per-stage decomposition of each filled+settled "
            "bet's FV chain into Stage-1 / Stage-2 / Stage-3 / "
            "calibration contributions, plus aggregate culprit "
            "ranking by share of bias direction."
        ),
        command=[
            _python(),
            _script("scripts/analysis/build_loss_attribution_report.py"),
        ],
    ))

    # Active #11 (2026-05-17): gate counterfactual report. Reuses the
    # cert's GATE_DEFS + _sweep_one to compute realized-$ counterfactual
    # deltas for each (gate, alt_threshold, time_window). Surfaces a
    # `top_recommendations` list ranked by trailing-30d $ saved so the
    # operator can see "if I had tightened X to Y last week, I would
    # have saved $Z." Pure offline analysis; reads
    # signal_training_table.jsonl, writes under
    # data/analysis_output/gate_counterfactual/.
    steps.append(RefreshStep(
        name="gate_counterfactual_report",
        description=(
            "Rebuild the Active #11 gate counterfactual report -- "
            "for each enforced gate, each sweep threshold, each "
            "time window (all / trailing_30d / trailing_7d), compute "
            "the realized-$ counterfactual P&L delta vs. current and "
            "rank the top tightening recommendations."
        ),
        command=[
            _python(),
            _script("scripts/analysis/build_gate_counterfactual_report.py"),
        ],
    ))

    steps.append(RefreshStep(
        name="over_gate_ev_audit",
        description=(
            "Per-OVER-gate counterfactual EV audit (2026-05-28). Unlike "
            "walk_forward_certification / gate_counterfactual (which run on "
            "the ~227 FILLED bets and can't see pre-FV gates that block 0 "
            "filled bets), this reads candidate-universe SKIP rows -- the "
            "bets each gate actually blocked -- dedupes to unique game "
            "states, joins over_hit outcomes, and computes each blocked "
            "cohort's taker ROI vs breakeven (Wilson-bounded) -> per-gate "
            "+EV / marginal / -EV verdict. Keeps the case for re-enabling "
            "gate_extreme_edge / gate_stage2_suppression (disabled "
            "2026-05-28) fresh as live data accumulates. Outputs to "
            "data/analysis_output/over_gate_ev_audit/."
        ),
        command=[
            _python(),
            _script("scripts/analysis/audit_over_gate_ev.py"),
        ],
    ))

    steps.append(RefreshStep(
        name="feed_enrichment",
        description=(
            "Tier-3 offline feed enrichment (2026-05-29). Joins each "
            "model-bearing candidate to its scraped MLB live-feed JSON by ts "
            "and reconstructs decision-time pitch count, times-through-order, "
            "bullpen depth, handedness matchup, catcher, and velocity/exit-velo "
            "trends. No live polling -- reads data/games we already scrape; "
            "backfills history. Outputs data/analysis_output/feed_enrichment/ "
            "keyed by candidate_id for join into calibration / walk-forward / "
            "the gate-EV audit."
        ),
        command=[
            _python(),
            _script("scripts/analysis/build_feed_enrichment.py"),
        ],
    ))

    steps.append(RefreshStep(
        name="quote_engine_shadow_report",
        description=(
            "Phase C shadow (2026-05-17): summarise the two-sided "
            "quote engine's shadow ledger. Reads per-date "
            "quote_engine_shadow/<date>_quotes.jsonl files written "
            "by the live engine running --quote-engine-mode shadow. "
            "Pure observability; no order placement. Outputs to "
            "data/analysis_output/quote_engine_shadow/."
        ),
        command=[
            _python(),
            _script("scripts/analysis/build_quote_engine_shadow_report.py"),
        ],
    ))

    steps.append(RefreshStep(
        name="under_walk_forward_certification",
        description=(
            "Phase A4 (2026-05-16): UNDER walk-forward certification "
            "report. Mirrors the Active #1 cert but for the UNDER side "
            "with flipped outcome, under-side ROI math, and no "
            "per-gate scorecard (no UNDER gates are enforced today)."
        ),
        command=[
            _python(),
            _script("scripts/analysis/build_under_walk_forward_certification.py"),
        ],
    ))

    # Penultimate step: weekly drift rollup HTML. Reads the per-date
    # human-review JSONs we just refreshed and bundles a 7-day trend page
    # so the operator can spot drift over a longer horizon than a single
    # day's review. Read-only, fail-open.
    steps.append(RefreshStep(
        name="weekly_drift_rollup",
        description=(
            "Render the trailing 7-day drift / health HTML rollup from the "
            "per-date daily-review JSONs."
        ),
        command=[
            _python(),
            _script("scripts/analysis/build_weekly_drift_rollup.py"),
        ],
    ))

    # Auto-promotion / auto-demotion daemon. Reads today's verdicts +
    # the promotion_events log; for file-swap levers (stage2, stage3-v2)
    # invokes promote.py when a verdict says go AND the lever isn't in
    # cooldown. Ships --mode preview by DEFAULT (logs decisions, takes
    # no action). Operator opts into --mode act after reviewing preview
    # output for a few sessions. CLI-flag levers (stake-scaling,
    # gate-threshold) get notes only -- daemon doesn't actuate those.
    steps.append(RefreshStep(
        name="auto_promote_demote_daemon",
        description=(
            "Auto promote/demote daemon (default: preview mode). Reads "
            "stability-gate verdicts + promote_events log; for stage2 and "
            "stage3-v2 invokes promote.py when verdict says go AND "
            "cooldown has elapsed. Skip-decisions logged to stdout, "
            "actions logged to promote_events.jsonl with operator=auto_daemon. "
            "Switch to --auto-daemon-mode act after reviewing preview output."
        ),
        command=[
            _python(),
            _script("scripts/analysis/auto_promote_demote_daemon.py"),
            "--mode", config.auto_daemon_mode,
            "--cooldown-days", str(config.auto_daemon_cooldown_days),
            "--active-date", config.active_date,
        ],
    ))

    # Daemon retrospective. Replays the daemon's promote-decision logic
    # against the per-date history snapshots (stage2_brier_history,
    # stage3_v2_drift_history) and the audit log; classifies each
    # (date, lever) into MATCH / DAEMON_ONLY / OPERATOR_ONLY /
    # DAEMON_DISAGREED / BOTH_NO_ACTION. Produces the evidence operators
    # use to gain confidence before flipping --auto-daemon-mode from
    # preview to act. Cheap to refresh -- pure history-file reads + math.
    steps.append(RefreshStep(
        name="daemon_retrospective",
        description=(
            "Replay daemon decisions against per-date history snapshots; "
            "report per-day MATCH/DAEMON_ONLY/OPERATOR_ONLY/DISAGREE "
            "agreement vs the audit log. Builds operator confidence to "
            "flip --auto-daemon-mode preview -> act."
        ),
        command=[
            _python(),
            _script("scripts/analysis/daemon_retrospective.py"),
            "--cooldown-days", str(config.auto_daemon_cooldown_days),
        ],
    ))

    # Penultimate integrity guard: canonical artifact lineage + freshness.
    # This catches drift such as a calibration table refreshed through a newer
    # date than the runtime calibration artifact.
    steps.append(RefreshStep(
        name="artifact_lineage_freshness",
        description=(
            "Build canonical artifact lineage/freshness report with input "
            "hashes, mtimes, row/family counts, and downstream staleness flags."
        ),
        command=[
            _python(),
            _script("scripts/analysis/build_artifact_lineage_freshness_report.py"),
        ],
    ))

    # Final step: roll up drift alerts, step failures, and stale-artifact
    # warnings into one operator-readable block. Runs unconditionally.
    steps.append(RefreshStep(
        name="refresh_health_rollup",
        kind="inline",
        description=(
            "End-of-refresh health rollup. Reads the latest daily review + "
            "walk-forward summary + per-step results and prints one "
            "consolidated 'is the project healthy?' summary."
        ),
        command=[],
    ))

    return steps


def _output_tail(output: str, max_chars: int = 4000) -> str:
    output = output.strip()
    if len(output) <= max_chars:
        return output
    return output[-max_chars:]


def _run_inline_step(step: RefreshStep, config: RefreshConfig) -> RefreshStepResult:
    handler = INLINE_HANDLERS.get(step.name)
    started = time.monotonic()
    if handler is None:
        elapsed = time.monotonic() - started
        return RefreshStepResult(
            name=step.name,
            command=[],
            returncode=1,
            elapsed_secs=round(elapsed, 3),
            status="failed",
            output_tail=f"no inline handler registered for {step.name!r}",
        )
    try:
        ok, output = handler(config)
        rc = 0 if ok else 1
    except Exception as exc:
        ok = False
        rc = 1
        output = f"inline handler raised: {exc!r}"
    elapsed = time.monotonic() - started
    return RefreshStepResult(
        name=step.name,
        command=[],
        returncode=rc,
        elapsed_secs=round(elapsed, 3),
        status="ok" if ok else "failed",
        output_tail=_output_tail(output or ""),
    )


def _run_refresh_health_rollup(
    step: RefreshStep,
    config: RefreshConfig,
    prior_results: List[RefreshStepResult],
) -> RefreshStepResult:
    """Build the end-of-refresh operator summary from accumulated state.

    Reads: (a) the step results so far, (b) the latest daily human-review
    JSON if the daily_human_review step ran, (c) the walk-forward
    summary if walk_forward_score_event ran, (d) the model_freshness_health
    notes from earlier in this same refresh.

    Output is descriptive only and never fails the refresh.
    """
    started = time.monotonic()
    lines: List[str] = []
    try:
        steps_total = len(prior_results)
        steps_ok = sum(1 for r in prior_results if r.status == "ok")
        steps_failed = [r for r in prior_results if r.status == "failed"]
        lines.append(
            f"Step roll-up: {steps_ok}/{steps_total} ok"
            + (f", {len(steps_failed)} failed ({', '.join(r.name for r in steps_failed)})"
               if steps_failed else "")
        )

        # Pull alert counts from the latest daily human-review JSON.
        review_dir = PROJECT_DIR / "data" / "analysis_output" / "daily_human_review"
        latest_review_path: Optional[Path] = None
        if review_dir.exists():
            review_files = sorted(review_dir.glob("*_human_review.json"))
            if review_files:
                latest_review_path = review_files[-1]
        if latest_review_path is not None:
            try:
                review = json.loads(latest_review_path.read_text(encoding="utf-8"))
            except Exception:
                review = {}
            review_date = review.get("session_date") or latest_review_path.stem.split("_")[0]
            alert_counts = {
                "calibration": len((review.get("calibration_health") or {}).get("alerts") or []),
                "fill_rate":   len((review.get("fill_rate_health") or {}).get("alerts") or []),
                "signal_qual": len((review.get("signal_quality_health") or {}).get("alerts") or []),
                "regime_mix":  len((review.get("regime_mix_health") or {}).get("alerts") or []),
                "reconciler":  len((review.get("reconciler_summary") or {}).get("alerts") or []),
            }
            total = sum(alert_counts.values())
            lines.append(
                f"Latest daily review ({review_date}): {total} active drift alerts "
                + "(" + ", ".join(f"{k}={v}" for k, v in alert_counts.items()) + ")"
            )
        else:
            lines.append("No daily human-review JSON found yet.")

        # Pull walk-forward summary if available.
        wf_summary_path = (
            PROJECT_DIR / "data" / "analysis_output" / "walk_forward" / "summary.json"
        )
        if wf_summary_path.exists():
            try:
                wf = json.loads(wf_summary_path.read_text(encoding="utf-8"))
            except Exception:
                wf = {}
            base = wf.get("baseline_live_engine_results") or {}
            lines.append(
                "Walk-forward: "
                f"{wf.get('n_windows_completed', 0)}/{wf.get('n_windows_planned', 0)} windows, "
                f"baseline cumulative profit ${base.get('cumulative_baseline_realized_profit', 0):+.2f}, "
                f"max DD ${base.get('max_baseline_drawdown_across_test_windows', 0):+.2f}"
            )

        # Forward the model-freshness handler's tail if it ran (descriptive
        # only -- already logged at step time, but worth surfacing in the
        # rollup so operators don't have to scroll back).
        for r in prior_results:
            if r.name == "model_freshness_health" and r.output_tail:
                first_alerts = [
                    ln for ln in r.output_tail.splitlines()
                    if ln.strip().startswith(("ALERT", "WARNING"))
                ]
                if first_alerts:
                    lines.append("Model freshness alerts:")
                    lines.extend(f"  - {ln.strip()}" for ln in first_alerts[:10])
                else:
                    lines.append("Model freshness: no alerts.")
                break

        # Same treatment for the Stage-3 v2 promotion-readiness check.
        for r in prior_results:
            if r.name == "stage3_v2_promotion_check" and r.output_tail:
                first_alerts = [
                    ln for ln in r.output_tail.splitlines()
                    if ln.strip().startswith(("ALERT", "WARNING"))
                ]
                if first_alerts:
                    lines.append("Stage-3 v2 promotion alerts:")
                    lines.extend(f"  - {ln.strip()}" for ln in first_alerts[:5])
                else:
                    lines.append("Stage-3 v2 promotion: no alerts.")
                break

        # Auto-daemon decisions. The daemon's stdout is its summary --
        # surface the header line + any actionable lever lines so the
        # operator sees what the daemon decided (preview / act / off).
        for r in prior_results:
            if r.name == "auto_promote_demote_daemon" and r.output_tail:
                tail_lines = r.output_tail.splitlines()
                # First line is "auto_daemon mode=... cooldown_days=... active_date=..."
                if tail_lines:
                    lines.append(f"Auto-daemon: {tail_lines[0]}")
                actionable = [
                    ln for ln in tail_lines
                    if ln.strip().startswith("ALERT")
                ]
                if actionable:
                    lines.extend(f"  - {ln.strip()}" for ln in actionable[:8])
                break

        # Stake-scaling promotion verdict (Active #6 part 2). The analyzer
        # writes a need_more_data / hold / promote verdict per refresh.
        # Surface it here so a `promote` verdict can never sit unread.
        stake_scaling_path = (
            PROJECT_DIR / "data" / "analysis_output"
            / "stake_scaling_analysis" / "stake_scaling_analysis.json"
        )
        if stake_scaling_path.exists():
            try:
                ss = json.loads(stake_scaling_path.read_text(encoding="utf-8"))
            except Exception:
                ss = {}
            verdict = str(ss.get("verdict") or "unknown")
            n_sessions = ss.get("n_sessions", 0)
            min_sessions = (ss.get("thresholds") or {}).get("min_sessions", 30)
            n_filled = ss.get("n_filled_bets", 0)
            prefix = "ALERT " if verdict == "promote" else ""
            lines.append(
                f"{prefix}Stake scaling: verdict={verdict} "
                f"({n_sessions}/{min_sessions} sessions, {n_filled} filled bets)"
            )
        else:
            lines.append("Stake scaling report not present (analyzer didn't run).")

        # Walk-forward certification scorecard (Active #1). Readiness
        # label + per-gate KEEP/RETUNE/RETIRE counts. Detail bullets only
        # for verdicts that need operator action (RETUNE/RETIRE).
        wfc_path = (
            PROJECT_DIR / "data" / "analysis_output"
            / "walk_forward_certification" / "walk_forward_certification.json"
        )
        if wfc_path.exists():
            try:
                wfc = json.loads(wfc_path.read_text(encoding="utf-8"))
            except Exception:
                wfc = {}
            readiness = wfc.get("readiness") or {}
            label = str(readiness.get("label") or "unknown")
            gate_entries = wfc.get("gates") or []
            gate_counts = {"KEEP": 0, "RETUNE": 0, "RETIRE": 0}
            actionable: List[str] = []
            for entry in gate_entries:
                v = (entry.get("verdict") or {})
                vname = str(v.get("verdict") or "").upper()
                gate_counts[vname] = gate_counts.get(vname, 0) + 1
                if vname in ("RETUNE", "RETIRE"):
                    actionable.append(
                        f"  - {entry.get('name')} -> {vname}"
                        f" (recommended_threshold={v.get('recommended_threshold')}):"
                        f" {(v.get('reason') or '').strip()}"
                    )
            needs_action = gate_counts.get("RETUNE", 0) + gate_counts.get("RETIRE", 0) > 0
            prefix = "ALERT " if needs_action else ""
            lines.append(
                f"{prefix}Walk-forward certification: {label} "
                f"({readiness.get('n_filled', 0)} filled / {readiness.get('n_dates', 0)} dates); "
                f"gates: {gate_counts.get('KEEP', 0)} KEEP, "
                f"{gate_counts.get('RETUNE', 0)} RETUNE, "
                f"{gate_counts.get('RETIRE', 0)} RETIRE"
            )
            lines.extend(actionable[:10])
        else:
            lines.append("Walk-forward certification not present (builder didn't run).")

        lineage_path = (
            PROJECT_DIR / "data" / "analysis_output"
            / "artifact_lineage_freshness" / "artifact_lineage_freshness_report.json"
        )
        if lineage_path.exists():
            try:
                lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
            except Exception:
                lineage = {}
            summary = lineage.get("summary") or {}
            status = str(lineage.get("status") or "unknown")
            prefix = "ALERT " if status == "error" else ("WARNING " if status == "warning" else "")
            lines.append(
                f"{prefix}Artifact lineage freshness: status={status}, "
                f"{summary.get('ok', 0)} ok / {summary.get('warning', 0)} warning / "
                f"{summary.get('error', 0)} error, "
                f"stale_mtime={summary.get('stale_by_mtime', 0)}, "
                f"stale_max_date={summary.get('stale_by_max_date', 0)}"
            )
            actionable = [
                art for art in (lineage.get("artifacts") or [])
                if ((art.get("health") or {}).get("status") in {"warning", "error"})
            ]
            for art in actionable[:5]:
                health = art.get("health") or {}
                tags = list(health.get("errors") or []) + list(health.get("warnings") or [])
                lines.append(f"  - {art.get('name')}: {', '.join(tags[:4])}")
        else:
            lines.append("Artifact lineage freshness report not present (builder didn't run).")

        # Doc-freshness gauge (Phase 3b, 2026-05-25). Surfaces stale
        # AGENT_CONTEXT.md files + tests/AGENT_CONTEXT.md test-count
        # drift via the shared helper. Best-effort, never raises.
        try:
            from doc_freshness import render_summary_lines as _doc_freshness_lines  # type: ignore
        except Exception:
            try:
                from scripts.analysis.doc_freshness import (  # type: ignore
                    render_summary_lines as _doc_freshness_lines,
                )
            except Exception as exc:
                lines.append(f"Doc freshness check unavailable: {exc!r}")
                _doc_freshness_lines = None  # type: ignore
        if _doc_freshness_lines is not None:
            try:
                lines.extend(_doc_freshness_lines())
            except Exception as exc:
                lines.append(f"Doc freshness check raised (non-fatal): {exc!r}")
    except Exception as exc:
        lines.append(f"Health rollup encountered an error (non-fatal): {exc!r}")

    elapsed = time.monotonic() - started
    return RefreshStepResult(
        name=step.name,
        command=[],
        returncode=0,
        elapsed_secs=round(elapsed, 3),
        status="ok",
        output_tail=_output_tail("\n".join(lines)),
    )


def _max_mtime(paths: Iterable[Path]) -> Optional[float]:
    """Return the max mtime across the given paths, or None if all missing."""
    out: Optional[float] = None
    for p in paths:
        try:
            if not p.exists():
                continue
            mt = p.stat().st_mtime
        except OSError:
            continue
        if out is None or mt > out:
            out = mt
    return out


def _max_dir_mtime(roots: Iterable[Path]) -> Optional[float]:
    """Cheaply scan ``roots`` recursively, returning the latest directory mtime.

    Used for huge corpora (e.g. ``data/games/regular/<year>/<month>/<day>``)
    where leaf-file globbing is expensive. Directory mtime updates whenever
    a file is added or removed, which is the change we care about.
    """
    out: Optional[float] = None
    for root in roots:
        if not root.exists():
            continue
        try:
            stack = [root]
            while stack:
                d = stack.pop()
                try:
                    mt = d.stat().st_mtime
                except OSError:
                    continue
                if out is None or mt > out:
                    out = mt
                try:
                    for child in d.iterdir():
                        if child.is_dir():
                            stack.append(child)
                except OSError:
                    continue
        except OSError:
            continue
    return out


def _is_step_fresh(check: StalenessCheck) -> Tuple[bool, str]:
    """Return (fresh, note). Step is fresh when output mtime >= every input mtime."""
    if not check.output_path.exists():
        return False, f"output {check.output_path.name} missing"
    try:
        output_mtime = check.output_path.stat().st_mtime
    except OSError as exc:
        return False, f"output stat failed: {exc}"

    input_mtime = _max_mtime(check.input_paths)
    dir_mtime = _max_dir_mtime(check.input_dir_mtime_roots)
    candidates = [m for m in (input_mtime, dir_mtime) if m is not None]
    if not candidates:
        # No reachable inputs -- can't compare, run the step to be safe.
        return False, "no inputs reachable; running to be safe"
    newest_input = max(candidates)
    if output_mtime >= newest_input:
        delta = output_mtime - newest_input
        return True, f"output is newer than newest input by {delta:.0f}s"
    delta = newest_input - output_mtime
    return False, f"output is {delta:.0f}s older than newest input"


def _run_step(step: RefreshStep, config: RefreshConfig) -> RefreshStepResult:
    if step.kind == "inline":
        return _run_inline_step(step, config)

    # Skip-if-fresh check: only for subprocess steps with a staleness policy.
    if step.staleness_check is not None and not config.force_retrain:
        fresh, note = _is_step_fresh(step.staleness_check)
        if fresh:
            return RefreshStepResult(
                name=step.name,
                command=step.command,
                returncode=0,
                elapsed_secs=0.0,
                status="skipped_fresh",
                output_tail=f"skip: {note}",
            )

    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    paths = [str(PROJECT_DIR)]
    if existing_pythonpath:
        paths.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(paths)

    started = time.monotonic()
    proc = subprocess.run(
        step.command,
        cwd=str(PROJECT_DIR),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    elapsed = time.monotonic() - started
    status = "ok" if proc.returncode == 0 else "failed"
    return RefreshStepResult(
        name=step.name,
        command=step.command,
        returncode=proc.returncode,
        elapsed_secs=round(elapsed, 3),
        status=status,
        output_tail=_output_tail(proc.stdout or ""),
    )


def _logs_dir_bytes(log_dir: Path) -> int:
    if not log_dir.exists():
        return 0
    total = 0
    for path in log_dir.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _phase6_reminder(active_date: str) -> Optional[str]:
    if not _valid_date(active_date):
        return None
    if active_date < PHASE6_GATE_RECALIBRATION_DUE_DATE:
        return None
    return (
        f"Phase 6 gate recalibration is due (active_date {active_date} >= "
        f"{PHASE6_GATE_RECALIBRATION_DUE_DATE}). Re-tune TR19 extreme_edge_max "
        "against post-TR20 v2 Stage-3 edge distribution. See "
        "model_improvements/handover_2026_05_07.txt."
    )


def _write_manifest(config: RefreshConfig, payload: Dict[str, object]) -> Path:
    config.output_root.mkdir(parents=True, exist_ok=True)
    date_part = config.active_date or datetime.now().strftime("%Y-%m-%d")
    suffix = "startup_refresh_plan" if config.plan_only else "startup_refresh"
    path = config.output_root / f"{date_part}_{suffix}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def run_startup_refresh(config: RefreshConfig) -> Dict[str, object]:
    if not _valid_date(config.active_date):
        raise ValueError(f"active_date must be YYYY-MM-DD, got {config.active_date!r}")
    if config.max_date and not _valid_date(config.max_date):
        raise ValueError(f"max_date must be YYYY-MM-DD, got {config.max_date!r}")

    session_dates = discover_session_dates(config.sessions_dir)
    max_date = latest_refreshable_date(
        session_dates,
        active_date=config.active_date,
        max_date=config.max_date,
        include_run_date=config.include_run_date,
    )
    steps = build_refresh_steps(config, session_dates, max_date)

    LOGGER.info(
        "Startup refresh plan: active_date=%s max_refresh_date=%s steps=%d strict=%s",
        config.active_date,
        max_date or "none",
        len(steps),
        bool(config.strict),
    )

    results: List[RefreshStepResult] = []
    if config.plan_only:
        results = [
            RefreshStepResult(
                name=step.name,
                command=step.command,
                returncode=None,
                elapsed_secs=0.0,
                status="planned",
            )
            for step in steps
        ]
    else:
        for step in steps:
            LOGGER.info("Startup refresh step started: %s", step.name)
            # Special-case: the health-rollup step needs visibility into the
            # results accumulated so far, which generic inline handlers don't
            # have. Build it inline from `results` instead of dispatching.
            if step.name == "refresh_health_rollup":
                result = _run_refresh_health_rollup(step, config, results)
            else:
                result = _run_step(step, config)
            results.append(result)
            if result.status in ("ok", "skipped_fresh"):
                if result.status == "skipped_fresh":
                    LOGGER.info(
                        "Startup refresh step skipped (fresh): %s -- %s",
                        result.name,
                        result.output_tail or "no detail",
                    )
                else:
                    LOGGER.info(
                        "Startup refresh step ok: %s (%.1fs)",
                        result.name,
                        result.elapsed_secs,
                    )
                if result.output_tail:
                    LOGGER.debug("Startup refresh output tail for %s:\n%s", result.name, result.output_tail)
                # Health-rollup is the operator-facing summary: tail it at
                # INFO so it always appears in the live-engine log without
                # opening the manifest.
                if step.name == "refresh_health_rollup" and result.output_tail:
                    LOGGER.info(
                        "Startup refresh health summary:\n%s", result.output_tail
                    )
            else:
                LOGGER.warning(
                    "Startup refresh step failed: %s rc=%s (%.1fs)\n%s",
                    result.name,
                    result.returncode,
                    result.elapsed_secs,
                    result.output_tail,
                )
                if config.strict:
                    break

    result_dicts = [asdict(result) for result in results]
    failures = [result for result in result_dicts if result.get("status") == "failed"]
    steps_ok = sum(1 for result in result_dicts if result.get("status") == "ok")
    steps_skipped_fresh = sum(
        1 for result in result_dicts if result.get("status") == "skipped_fresh"
    )
    steps_planned_status = sum(1 for result in result_dicts if result.get("status") == "planned")
    failed_names = [str(result.get("name")) for result in failures]
    notes: List[str] = [
        "Refresh retrains runtime decision artifacts in-band as of 2026-05-12: probability calibration, EV-policy table, Stage-2 staging, and Stage-3 v2 research fits all run daily. Only the Stage-3 v2 production promotion (promote_team_offense_v2.py) remains a manual step.",
        "Default max_refresh_date excludes the active run date to avoid training on an in-progress session.",
        "Weather refresh writes canonical Weather v2 inputs for live Stage-2 FV; missing provider data degrades to unknown weather buckets.",
        "Stage-2 staging cache (mlb_stage2_run_env.staging.json) is rewritten daily; production cache (mlb_stage2_run_env.json) is swapped manually after reviewing model_freshness_health Brier diff.",
        "Startup is the canonical daily refresh: 45-step base pipeline (plus one daily_human_review step per stale completed session) that scrapes yesterday's games, refreshes today's schedule, rebuilds Stage-1/2/3 inputs, retrains decision artifacts, runs preflight cache checks, audits artifact lineage/freshness, and finishes with refresh_health_rollup. Skip flags exist for partial runs; --force-retrain bypasses StalenessCheck.",
        "Model maturity report is descriptive only; it marks artifacts not_enough_data until family-specific sample and class-balance minimums are met.",
    ]
    phase6_msg = _phase6_reminder(config.active_date)
    if phase6_msg:
        notes.append(phase6_msg)

    summary_status = "ok" if not failures else "failed"
    # In plan-only mode no steps actually executed, so lead with the planned
    # count instead of the misleading "0/N steps ok" framing.
    if config.plan_only and steps_ok == 0 and not failures:
        summary_line = f"plan-only: {steps_planned_status}/{len(steps)} steps planned"
    else:
        summary_line = (
            f"{steps_ok}/{len(steps)} steps ok"
            + (f", {steps_skipped_fresh} skipped-fresh" if steps_skipped_fresh else "")
            + (f", {len(failures)} failed ({', '.join(failed_names)})" if failures else "")
            + (f", {steps_planned_status} planned" if steps_planned_status else "")
        )

    payload: Dict[str, object] = {
        "schema_version": 2,
        "generated_at_utc": _now_iso(),
        "summary": summary_line,
        "summary_status": summary_status,
        "manifest_kind": "plan" if config.plan_only else "run",
        "active_date": config.active_date,
        "max_refresh_date": max_date,
        "include_run_date": config.include_run_date,
        "strict": config.strict,
        "plan_only": config.plan_only,
        "session_dates_seen": session_dates,
        "steps_planned": len(steps),
        "steps_ok": steps_ok,
        "steps_failed": len(failures),
        "failed_step_names": failed_names,
        "logs_dir_bytes": _logs_dir_bytes(config.log_dir),
        "phase6_reminder": phase6_msg or "",
        "steps": result_dicts,
        "notes": notes,
    }
    manifest_path = _write_manifest(config, payload)
    payload["manifest_path"] = str(manifest_path)

    if failures and config.strict:
        names = ", ".join(str(result.get("name")) for result in failures)
        raise RuntimeError(f"Startup refresh failed in strict mode: {names}. See {manifest_path}")

    return payload


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Refresh live post-session analysis artifacts.")
    p.add_argument("--active-date", type=str, default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--max-date", type=str, default="", help="Override max completed session date.")
    p.add_argument(
        "--include-run-date",
        action="store_true",
        help="Include active_date in refresh. Use only after that session is complete.",
    )
    p.add_argument("--strict", action="store_true", help="Abort on first failed refresh step.")
    p.add_argument("--skip-pitcher-cache", action="store_true")
    p.add_argument("--skip-weather-cache", action="store_true")
    p.add_argument("--skip-daily-reviews", action="store_true")
    p.add_argument("--skip-walk-forward", action="store_true")
    p.add_argument("--skip-recent-games-scrape", action="store_true",
                   help="Skip scraping yesterday's completed games (Stage-3 input).")
    p.add_argument("--recent-games-lookback-days", type=int, default=7,
                   help="How many days back to backfill in scrape_recent_games (default: 7).")
    p.add_argument("--skip-active-schedule-scrape", action="store_true",
                   help="Skip refreshing today's MLB schedule.")
    p.add_argument("--skip-stage1-cache", action="store_true",
                   help="Skip rebuilding cache/mlb_ou_cache.json during startup refresh.")
    p.add_argument("--skip-team-game-log", action="store_true",
                   help="Skip explicit Stage-3 team_game_log rebuild (lazy rebuild remains).")
    p.add_argument("--skip-park-hr-factors", action="store_true",
                   help="Skip Stage-2 hr_factor input rebuild (cache/park_hr_factors.json).")
    p.add_argument("--skip-preflight-secrets", action="store_true",
                   help="Skip .env / POLY_PRIVATE_KEY preflight check.")
    p.add_argument("--skip-preflight-artifacts", action="store_true",
                   help="Skip Stage-1/2/3 cache load preflight check.")
    p.add_argument("--require-poly-private-key", action="store_true",
                   help="Treat missing POLY_PRIVATE_KEY as a hard preflight failure (live mode only).")
    p.add_argument("--pitcher-cache-path", type=Path, default=DEFAULT_PITCHER_CACHE_PATH)
    p.add_argument("--weather-metadata-path", type=Path, default=DEFAULT_STADIUM_WEATHER_METADATA_PATH)
    p.add_argument("--weather-cache-dir", type=Path, default=DEFAULT_WEATHER_CACHE_DIR)
    p.add_argument("--weather-provider", choices=["open-meteo", "none"], default="open-meteo")
    p.add_argument("--weather-timeout", type=float, default=8.0)
    p.add_argument("--stake", type=float, default=10.0)
    p.add_argument("--daily-budget", type=float, default=80.0)
    p.add_argument("--per-game-budget-fraction", type=float, default=0.40)
    p.add_argument("--sessions-dir", type=Path, default=DEFAULT_SESSIONS_DIR)
    p.add_argument("--candidate-dir", type=Path, default=DEFAULT_CANDIDATE_DIR)
    p.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--plan-only", action="store_true")
    p.add_argument(
        "--force-retrain",
        action="store_true",
        help=(
            "Bypass per-step staleness checks. Without this, heavy retrains "
            "(Stage-2, Stage-3, walk-forward, etc.) skip when their input "
            "files haven't changed since the previous output. Use to force "
            "a full rebuild."
        ),
    )
    p.add_argument(
        "--auto-daemon-mode",
        choices=("preview", "act", "off"),
        default="preview",
        help=(
            "Auto promote/demote daemon mode. preview (default) logs "
            "decisions but takes no action; act invokes promote.py for "
            "actionable verdicts; off skips the daemon step entirely. "
            "Operator should review preview output for several sessions "
            "before flipping to act."
        ),
    )
    p.add_argument(
        "--auto-daemon-cooldown-days",
        type=int,
        default=14,
        help=(
            "Days the daemon waits between consecutive actions on the same "
            "lever. Matches the demotion-verdict pre/post window so the "
            "demote signal can gather evidence before another action."
        ),
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    config = RefreshConfig(
        active_date=args.active_date,
        max_date=args.max_date,
        include_run_date=bool(args.include_run_date),
        strict=bool(args.strict),
        refresh_pitcher_cache=not bool(args.skip_pitcher_cache),
        refresh_weather_cache=not bool(args.skip_weather_cache),
        refresh_daily_reviews=not bool(args.skip_daily_reviews),
        run_walk_forward=not bool(args.skip_walk_forward),
        refresh_recent_games=not bool(args.skip_recent_games_scrape),
        recent_games_lookback_days=int(args.recent_games_lookback_days),
        refresh_active_schedule=not bool(args.skip_active_schedule_scrape),
        refresh_stage1_cache=not bool(args.skip_stage1_cache),
        refresh_team_game_log=not bool(args.skip_team_game_log),
        refresh_park_hr_factors=not bool(args.skip_park_hr_factors),
        run_preflight_secrets=not bool(args.skip_preflight_secrets),
        run_preflight_artifacts=not bool(args.skip_preflight_artifacts),
        require_poly_private_key=bool(args.require_poly_private_key),
        pitcher_cache_path=args.pitcher_cache_path,
        weather_metadata_path=args.weather_metadata_path,
        weather_cache_dir=args.weather_cache_dir,
        weather_provider=args.weather_provider,
        weather_timeout=float(args.weather_timeout),
        stake=args.stake,
        daily_budget=args.daily_budget,
        per_game_budget_fraction=args.per_game_budget_fraction,
        sessions_dir=args.sessions_dir,
        candidate_dir=args.candidate_dir,
        log_dir=args.log_dir,
        output_root=args.output_root,
        plan_only=bool(args.plan_only),
        force_retrain=bool(args.force_retrain),
        auto_daemon_mode=str(args.auto_daemon_mode),
        auto_daemon_cooldown_days=int(args.auto_daemon_cooldown_days),
    )
    payload = run_startup_refresh(config)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
