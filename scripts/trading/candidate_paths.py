#!/usr/bin/env python3
"""
candidate_paths.py -- Candidate IDs, file paths, JSONL helpers, outcomes.

This module is intentionally low-level and side-effect-light. It centralizes
the candidate-universe filesystem layout plus JSONL serialization helpers used
by raw candidate rows, compact calibration rows, score-confirmation sidecars,
rollups, and final-score outcome records.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, TYPE_CHECKING

from line_state import _now_iso

if TYPE_CHECKING:
    from signal_engine import SignalEngine

LOGGER = logging.getLogger("signal_engine")


def drop_none_values(value):
    """Return a JSON-safe copy with None fields omitted from nested dicts/lists."""
    if isinstance(value, dict):
        return {
            key: drop_none_values(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [drop_none_values(item) for item in value]
    return value


def count_none_values(value) -> int:
    """Count None values omitted by drop_none_values for storage diagnostics."""
    if value is None:
        return 1
    if isinstance(value, dict):
        return sum(count_none_values(item) for item in value.values())
    if isinstance(value, list):
        return sum(count_none_values(item) for item in value)
    return 0


def jsonl_dumps(row: Dict[str, object]) -> str:
    return json.dumps(row, separators=(",", ":"))


def next_candidate_id(engine: "SignalEngine", game_pk: int, line: str) -> str:
    engine._candidate_seq += 1
    return f"{engine.date_str}_{game_pk}_{line}_{engine._candidate_seq:06d}"


def candidate_log_path(engine: "SignalEngine") -> Path:
    root = engine.trade_args.paper_root / "candidate_universe"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{engine.date_str}_candidates.jsonl"


def candidate_universe_root(engine: "SignalEngine") -> Path:
    trade_args = getattr(engine, "trade_args", None)
    paper_root = getattr(trade_args, "paper_root", None)
    if paper_root is not None:
        root = Path(paper_root) / "candidate_universe"
        root.mkdir(parents=True, exist_ok=True)
        return root
    method = getattr(engine, "_candidate_log_path", None)
    if callable(method):
        return Path(method()).parent
    root = Path(".")
    root.mkdir(parents=True, exist_ok=True)
    return root


def candidate_rollup_path(engine: "SignalEngine") -> Path:
    root = candidate_universe_root(engine)
    return root / f"{engine.date_str}_candidate_rollup.json"


def candidate_calibration_log_path(engine: "SignalEngine") -> Path:
    root = candidate_universe_root(engine)
    return root / f"{engine.date_str}_calibration_opportunities.jsonl"


def score_confirmation_log_path(engine: "SignalEngine") -> Path:
    root = candidate_universe_root(engine)
    return root / f"{engine.date_str}_score_confirmations.jsonl"


def outcome_log_path(engine: "SignalEngine") -> Path:
    root = candidate_universe_root(engine)
    return root / f"{engine.date_str}_outcomes.jsonl"


def path_from_engine(engine: "SignalEngine", method_name: str, fallback) -> Path:
    method = getattr(engine, method_name, None)
    if callable(method):
        return method()
    return fallback(engine)


def write_outcome_record(
    engine: "SignalEngine",
    *,
    game_pk: int,
    line: str,
    final_away: int,
    final_home: int,
) -> None:
    """Append one outcome record per (game, line) when the game goes Final."""
    game = engine.games.get(game_pk)
    final_total = final_away + final_home
    line_val = float(line)
    record = {
        "schema_version": 1,
        "session_date": engine.date_str,
        "mode": engine._candidate_mode,
        "game_pk": game_pk,
        "away_abbrev": game.away_abbrev if game else "",
        "home_abbrev": game.home_abbrev if game else "",
        "line": line,
        "final_away": final_away,
        "final_home": final_home,
        "final_total": final_total,
        "over_hit": bool(final_total > line_val),
        "settled_at": _now_iso(),
    }
    try:
        with open(engine._outcome_log_path(), "a", encoding="utf-8") as f:
            f.write(jsonl_dumps(record) + "\n")
    except Exception as exc:
        LOGGER.warning(
            "Failed to write outcome record game_pk=%d line=%s: %s",
            game_pk, line, exc,
        )
