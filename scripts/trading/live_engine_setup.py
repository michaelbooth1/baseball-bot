"""live_engine_setup.py -- Logging, log rotation, and startup-refresh glue.

Extracted from `live_engine.py` (2026-05-12) to keep the engine file under
the LLM-friendly 1200-line threshold. Three concerns live here:

  1. **Library log suppression** -- ``urllib3`` / ``httpcore`` / ``hpack``
     etc default to DEBUG which floods our session logs. We pin them to
     WARNING so the file is readable.

  2. **File log rotation** -- once a day starts a new file, the previous
     day gets gzipped; archives past the retention window get deleted.
     Without this, ``logs/real-logs/`` grew to ~470 MB after 19 days.

  3. **Startup analysis refresh** -- before the engine boots, run
     ``run_daily_refresh.py`` to rebuild every per-day analysis artifact
     (calibration, drift alerts, walk-forward, etc). This is the canonical
     "no manual reruns" entry point that the README's operational
     guidance points operators at.

`main()` (the CLI entry point) stays in ``live_engine.py`` and calls into
the helpers below.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta
from pathlib import Path

# Library loggers that need WARNING-level pinning to keep session logs readable.
NOISY_LIBRARY_LOGGERS = (
    "urllib3",
    "urllib3.connectionpool",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpx",
    "hpack",
    "hpack.hpack",
    "hpack.table",
)

# Log rotation policy. The TTL is per-file so this is essentially a sliding
# 60-day window of compressed daily logs.
LOG_ROTATION_RETENTION_DAYS = 60
LOG_ROTATION_GZIP_AFTER_DAYS = 1

LOGGER = logging.getLogger("live_engine")


def suppress_noisy_library_loggers() -> None:
    """Keep raw runtime logs focused on project decisions, not HTTP traces."""
    for logger_name in NOISY_LIBRARY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def rotate_old_log_files(
    log_dir: Path,
    today_date_str: str,
    *,
    gzip_after_days: int = LOG_ROTATION_GZIP_AFTER_DAYS,
    retention_days: int = LOG_ROTATION_RETENTION_DAYS,
) -> None:
    """Gzip yesterday-and-older `.log` files; delete `.log.gz` past retention.

    One log file per session date already gives day-based segmentation; this
    just compresses the closed days and prunes the oldest. Best-effort: any
    failure logs a warning and continues so a stuck file never blocks startup.
    Skips today's file so an in-progress run never has its handle yanked.
    """
    import gzip
    import os
    import shutil

    if not log_dir.exists():
        return
    cutoff_gzip = datetime.now() - timedelta(days=gzip_after_days)
    cutoff_delete = datetime.now() - timedelta(days=retention_days)

    for path in sorted(log_dir.iterdir()):
        try:
            if not path.is_file():
                continue
            if path.name == f"{today_date_str}.log":
                continue
            if path.suffix == ".log":
                if datetime.fromtimestamp(path.stat().st_mtime) > cutoff_gzip:
                    continue
                gz_path = path.with_suffix(".log.gz")
                if gz_path.exists():
                    # Concurrent rotation already produced the gz; just drop the raw.
                    path.unlink()
                    continue
                with open(path, "rb") as src, gzip.open(gz_path, "wb", compresslevel=6) as dst:
                    shutil.copyfileobj(src, dst)
                # Preserve original mtime so retention math stays meaningful.
                mtime = path.stat().st_mtime
                os.utime(gz_path, (mtime, mtime))
                path.unlink()
            elif path.name.endswith(".log.gz"):
                if datetime.fromtimestamp(path.stat().st_mtime) < cutoff_delete:
                    path.unlink()
        except Exception as exc:
            LOGGER.warning("Log rotation skipped %s: %s", path.name, exc)


def setup_logging(log_dir: Path, date_str: str, terminal_level: str = "INFO") -> Path:
    """Configure logging: project DEBUG to file, INFO to terminal.

    The file handler remains DEBUG for our own diagnostics, but external HTTP
    and crypto libraries are raised to WARNING. Recent runs showed urllib3 /
    httpcore / hpack DEBUG traces dominating raw logs while adding little audit
    value; warnings and errors from those libraries still surface.

    On startup, gzips closed-day `.log` files and deletes archives past the
    retention window so `logs/real-logs/` does not grow unbounded (was ~470 MB
    after 19 days uncompressed; target ~50-80 MB after gzip + 60d retention).
    """
    import sys

    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{date_str}.log"

    rotate_old_log_files(log_dir, date_str)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    suppress_noisy_library_loggers()

    # File handler (DEBUG level)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(fh)

    # Terminal handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(getattr(logging, terminal_level.upper(), logging.INFO))
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(ch)

    return log_path


def run_startup_refresh(
    *,
    date_str: str,
    live_args: argparse.Namespace,
    trade_args: argparse.Namespace,
    monitor_args: argparse.Namespace,
) -> None:
    """Refresh completed-session analysis artifacts before runtime loads models."""
    from live_engine_cli import (
        DEFAULT_DAILY_BUDGET,
        DEFAULT_LIVE_STAKE,
        DEFAULT_PER_GAME_BUDGET_FRACTION,
        DEFAULT_STARTUP_REFRESH_ENABLED,
        DEFAULT_STARTUP_WEATHER_PROVIDER,
        DEFAULT_STARTUP_WEATHER_TIMEOUT_SECS,
        PROJECT_DIR,
    )
    from run_daily_refresh import (
        RefreshConfig as _StartupRefreshConfig,
        run_startup_refresh as _run_startup_refresh_impl,
    )

    if not getattr(live_args, "startup_refresh", DEFAULT_STARTUP_REFRESH_ENABLED):
        LOGGER.info("Startup artifact refresh disabled by --no-startup-refresh.")
        return

    pitcher_cache_path = Path(
        getattr(monitor_args, "pitcher_cache_path", str(PROJECT_DIR / "cache" / "pitcher_cache.json"))
    )
    config = _StartupRefreshConfig(
        active_date=date_str,
        max_date=getattr(live_args, "startup_refresh_max_date", "") or "",
        include_run_date=bool(getattr(live_args, "startup_refresh_include_run_date", False)),
        strict=bool(getattr(live_args, "startup_refresh_strict", False)),
        refresh_pitcher_cache=not bool(getattr(live_args, "startup_refresh_skip_pitcher_cache", False)),
        refresh_weather_cache=not bool(getattr(live_args, "startup_refresh_skip_weather_cache", False)),
        refresh_daily_reviews=not bool(getattr(live_args, "startup_refresh_skip_daily_reviews", False)),
        run_walk_forward=not bool(getattr(live_args, "startup_refresh_skip_walk_forward", False)),
        refresh_recent_games=not bool(getattr(live_args, "startup_refresh_skip_recent_games_scrape", False)),
        recent_games_lookback_days=int(getattr(live_args, "startup_refresh_recent_games_lookback_days", 7) or 7),
        refresh_active_schedule=not bool(getattr(live_args, "startup_refresh_skip_active_schedule_scrape", False)),
        refresh_stage1_cache=not bool(getattr(live_args, "startup_refresh_skip_stage1_cache", False)),
        refresh_team_game_log=not bool(getattr(live_args, "startup_refresh_skip_team_game_log", False)),
        refresh_park_hr_factors=not bool(getattr(live_args, "startup_refresh_skip_park_hr_factors", False)),
        run_preflight_secrets=not bool(getattr(live_args, "startup_refresh_skip_preflight_secrets", False)),
        run_preflight_artifacts=not bool(getattr(live_args, "startup_refresh_skip_preflight_artifacts", False)),
        require_poly_private_key=bool(getattr(live_args, "startup_refresh_require_poly_private_key", False)),
        pitcher_cache_path=pitcher_cache_path,
        weather_provider=str(getattr(live_args, "startup_weather_provider", DEFAULT_STARTUP_WEATHER_PROVIDER)),
        weather_timeout=float(
            getattr(live_args, "startup_weather_timeout", DEFAULT_STARTUP_WEATHER_TIMEOUT_SECS)
            or DEFAULT_STARTUP_WEATHER_TIMEOUT_SECS
        ),
        stake=float(getattr(trade_args, "stake", DEFAULT_LIVE_STAKE) or DEFAULT_LIVE_STAKE),
        daily_budget=float(getattr(live_args, "daily_budget", DEFAULT_DAILY_BUDGET) or DEFAULT_DAILY_BUDGET),
        per_game_budget_fraction=float(
            getattr(live_args, "per_game_budget_fraction", DEFAULT_PER_GAME_BUDGET_FRACTION)
            or DEFAULT_PER_GAME_BUDGET_FRACTION
        ),
    )

    try:
        payload = _run_startup_refresh_impl(config)
    except Exception:
        if bool(getattr(live_args, "startup_refresh_strict", False)):
            LOGGER.exception("Startup artifact refresh failed in strict mode; aborting live startup.")
            raise
        LOGGER.exception("Startup artifact refresh failed; continuing because strict mode is off.")
        return

    LOGGER.info(
        "Startup artifact refresh complete: max_refresh_date=%s steps_ok=%s steps_failed=%s manifest=%s",
        payload.get("max_refresh_date") or "none",
        payload.get("steps_ok"),
        payload.get("steps_failed"),
        payload.get("manifest_path"),
    )
    if payload.get("steps_failed"):
        LOGGER.warning("Startup artifact refresh had failures; inspect manifest before trusting refreshed analysis.")
