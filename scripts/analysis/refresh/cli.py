"""Refresh CLI + orchestration entry point.

run_startup_refresh, parse_args, and main were the original module's
public-bottom; the orchestration logic moved here unchanged.
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .config import (
    DEFAULT_CANDIDATE_DIR,
    DEFAULT_LOG_DIR,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_PITCHER_CACHE_PATH,
    DEFAULT_SESSIONS_DIR,
    DEFAULT_STADIUM_WEATHER_METADATA_PATH,
    DEFAULT_WEATHER_CACHE_DIR,
    LOGGER,
    RefreshConfig,
    RefreshStepResult,
)
from .execution import _run_step
from .helpers import _now_iso, _valid_date
from .manifest import _logs_dir_bytes, _phase6_reminder, _write_manifest
from .rollup import _run_refresh_health_rollup
from .session_discovery import discover_session_dates, latest_refreshable_date
from .steps import build_refresh_steps


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
