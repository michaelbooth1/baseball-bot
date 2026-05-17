"""Module-level constants and shared logger for the MLB Polymarket monitor.

Split out of ``monitor_mlb_polymarket_ou.py`` so trading code, tests, and the
monitor can all import these without pulling in the full orchestrator.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parents[2] / "data" / "polymarket" / "mlb_ou"
DEFAULT_TIMEZONE = "America/Toronto"
DEFAULT_PITCHER_CACHE_PATH = Path(__file__).resolve().parents[2] / "cache" / "pitcher_cache.json"

PITCHER_CACHE_MAX_AGE_HOURS = 24
PITCHER_CACHE_STALE_FALLBACK_MAX_AGE_HOURS = 72
PITCHER_CACHE_MIN_PITCHER_COUNT = 50

DEFAULT_BOOK_FAILURE_RETIRE_STREAK = 6
DEFAULT_BOOK_FAILURE_COOLDOWN_SECS = 120.0
DEFAULT_BOOK_FAILURE_MAX_COOLDOWN_SECS = 1800.0

LOGGER = logging.getLogger("mlb_poly_monitor")

NOISY_LIBRARY_LOGGERS = (
    "urllib3",
    "urllib3.connectionpool",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpx",
    "hpack",
    "hpack.hpack",
    "hpack.table",
    "rlp",
    "rlp.codec",
    "web3",
    "eth_account",
)

POLL_PROGRESS_DEBUG_EVERY_N_CYCLES = 300
SCHEDULE_UNCHANGED_DEBUG_EVERY_N_REFRESHES = 100
SCHEDULE_CHANGED_DEBUG_EVERY_N_REFRESHES = 25
DISCOVERY_NO_NEW_INFO_EVERY_N_CYCLES = 15

MLB_AVG_ERA = 4.20

TEAM_SLUGS: Dict[str, List[str]] = {
    "ARI": ["ari"],
    "ATL": ["atl"],
    "BAL": ["bal"],
    "BOS": ["bos"],
    "CHC": ["chc"],
    "CWS": ["cws", "chw"],
    "CIN": ["cin"],
    "CLE": ["cle"],
    "COL": ["col"],
    "DET": ["det"],
    "HOU": ["hou"],
    "KCR": ["kc", "kcr"],
    "LAA": ["laa", "ana"],
    "LAD": ["lad"],
    "MIA": ["mia", "fla"],
    "MIL": ["mil"],
    "MIN": ["min"],
    "NYM": ["nym"],
    "NYY": ["nyy"],
    "ATH": ["ath", "oak"],
    "PHI": ["phi"],
    "PIT": ["pit"],
    "SDP": ["sd", "sdp"],
    "SFG": ["sf", "sfg"],
    "SEA": ["sea"],
    "STL": ["stl"],
    "TBR": ["tb", "tbr"],
    "TEX": ["tex"],
    "TOR": ["tor"],
    "WSN": ["wsh", "wsn"],
}

LIVE_STATES = {"live"}
PREVIEW_STATES = {"preview", "pre-game", "warmup"}
FINAL_STATES = {"final", "completed early", "completed", "game over"}

OU_LINE_RE = re.compile(
    r"(?:o/u|over\/under|over-under|total(?:s)?(?:\s+runs?)?)\s*([0-9]+(?:\.[0-9])?)",
    re.IGNORECASE,
)


def suppress_noisy_library_loggers() -> None:
    """Suppress low-value third-party DEBUG chatter while preserving warnings."""
    for logger_name in NOISY_LIBRARY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
