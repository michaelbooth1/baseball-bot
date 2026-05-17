"""CLI argument parsing for the MLB Polymarket monitor.

Lifted out of the orchestrator module so ``signal_config`` can import the parser
without dragging in the whole monitor stack.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from monitor_constants import (
    DEFAULT_BOOK_FAILURE_COOLDOWN_SECS,
    DEFAULT_BOOK_FAILURE_MAX_COOLDOWN_SECS,
    DEFAULT_BOOK_FAILURE_RETIRE_STREAK,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_PITCHER_CACHE_PATH,
    DEFAULT_TIMEZONE,
    PITCHER_CACHE_MAX_AGE_HOURS,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Monitor Polymarket MLB O/U orderbooks.")
    p.add_argument("--date", type=str, default="", help="Schedule date (YYYY-MM-DD). Default: today in timezone.")
    p.add_argument("--timezone", type=str, default=DEFAULT_TIMEZONE, help=f"Date timezone (default: {DEFAULT_TIMEZONE}).")
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--poll-interval", type=float, default=2.5, help="Seconds between orderbook polls.")
    p.add_argument("--schedule-refresh-secs", type=float, default=30.0, help="Schedule refresh interval.")
    p.add_argument("--discovery-refresh-secs", type=float, default=120.0, help="Polymarket discovery refresh interval.")
    p.add_argument("--inter-request-delay", type=float, default=0.06, help="Delay between per-token requests in one loop.")
    p.add_argument("--max-workers", type=int, default=20, help="Max parallel CLOB book requests.")
    p.add_argument("--gamma-timeout", type=float, default=4.0)
    p.add_argument("--clob-timeout", type=float, default=3.0)
    p.add_argument("--start-on-preview", action="store_true", help="Start recording in Preview state (otherwise starts at Live).")
    p.add_argument("--once", action="store_true", help="Single schedule+discovery+poll cycle, then exit.")
    p.add_argument("--run-seconds", type=int, default=0, help="Optional max runtime.")
    p.add_argument("--log-level", type=str, default="INFO")
    p.add_argument("--book-failure-retire-streak", type=int, default=DEFAULT_BOOK_FAILURE_RETIRE_STREAK,
                   help="Consecutive retirable book failures before temporary retirement "
                        f"(default: {DEFAULT_BOOK_FAILURE_RETIRE_STREAK}).")
    p.add_argument("--book-failure-cooldown-secs", type=float, default=DEFAULT_BOOK_FAILURE_COOLDOWN_SECS,
                   help="Initial cooldown when a book key is retired (default: "
                        f"{DEFAULT_BOOK_FAILURE_COOLDOWN_SECS}s).")
    p.add_argument("--book-failure-max-cooldown-secs", type=float, default=DEFAULT_BOOK_FAILURE_MAX_COOLDOWN_SECS,
                   help="Maximum cooldown for retired book keys under repeated failures "
                        f"(default: {DEFAULT_BOOK_FAILURE_MAX_COOLDOWN_SECS}s).")
    p.add_argument("--pitcher-cache-path", type=str, default=str(DEFAULT_PITCHER_CACHE_PATH),
                   help=f"Path to pitcher ERA cache JSON. "
                        f"Auto-rebuilt from MLB Stats API if missing or older than "
                        f"{PITCHER_CACHE_MAX_AGE_HOURS}h. (default: {DEFAULT_PITCHER_CACHE_PATH})")
    p.set_defaults(performance_mode=True)
    p.add_argument("--performance-mode", dest="performance_mode", action="store_true",
                   help="Pin process to P-cores and set HIGH process priority (default on; requires psutil). "
                        "Auto-detects i7-12700K layout (20 logical CPUs, P-cores=0-15). "
                        "Use --p-core-affinity for other hardware.")
    p.add_argument("--no-performance-mode", dest="performance_mode", action="store_false",
                   help="Disable CPU affinity / HIGH-priority process tuning.")
    p.add_argument("--p-core-affinity", type=str, default="",
                   help="Comma-separated logical CPU IDs to pin to (e.g. '0,1,2,3,4,5,6,7'). "
                        "Overrides auto-detection. Only used when performance mode is enabled.")
    args = p.parse_args()
    if args.book_failure_retire_streak < 1:
        p.error("--book-failure-retire-streak must be >= 1")
    if args.book_failure_cooldown_secs <= 0:
        p.error("--book-failure-cooldown-secs must be > 0")
    if args.book_failure_max_cooldown_secs <= 0:
        p.error("--book-failure-max-cooldown-secs must be > 0")
    if args.book_failure_max_cooldown_secs < args.book_failure_cooldown_secs:
        p.error("--book-failure-max-cooldown-secs must be >= --book-failure-cooldown-secs")
    return args
