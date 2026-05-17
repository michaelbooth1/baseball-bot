#!/usr/bin/env python3
"""
runtime_log_rollups.py -- compact DEBUG rollups for repeated runtime messages.

This module is observability-only. It replaces high-volume per-tick DEBUG
messages with compact periodic/final summaries while preserving the dimensions
needed for audits: game, line, inning/state, source key, and one example line.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Any, Dict, Tuple


LOGGER = logging.getLogger("signal_engine")


def ensure_runtime_log_rollup_state(engine: Any) -> None:
    if not hasattr(engine, "_runtime_debug_rollup_counts"):
        engine._runtime_debug_rollup_counts = Counter()
    if not hasattr(engine, "_runtime_debug_rollup_examples"):
        engine._runtime_debug_rollup_examples = {}
    if not hasattr(engine, "_last_runtime_debug_rollup_log_ts"):
        engine._last_runtime_debug_rollup_log_ts = 0.0


def _fmt_message(message: str, args: Tuple[object, ...]) -> str:
    try:
        return message % args
    except Exception:
        return message


def record_runtime_debug_rollup(
    engine: Any,
    *,
    kind: str,
    game_pk: Any,
    game_label: str,
    line: Any,
    inning: Any,
    state: str,
    source_key: Any,
    message: str,
    args: Tuple[object, ...] = (),
) -> None:
    """Record one repeated DEBUG event without emitting a log line."""
    ensure_runtime_log_rollup_state(engine)
    key = (
        str(kind),
        str(game_pk),
        str(game_label),
        str(line),
        str(inning),
        str(state),
        str(source_key or "unknown"),
    )
    engine._runtime_debug_rollup_counts[key] += 1
    if key not in engine._runtime_debug_rollup_examples:
        engine._runtime_debug_rollup_examples[key] = {
            "kind": str(kind),
            "game_pk": str(game_pk),
            "game": str(game_label),
            "line": str(line),
            "inning": str(inning),
            "state": str(state),
            "source_key": str(source_key or "unknown"),
            "example": _fmt_message(message, args),
        }


def log_runtime_debug_rollups(
    engine: Any,
    *,
    force: bool = False,
    interval_secs: float = 1800.0,
    top_n: int = 50,
) -> None:
    """Emit and clear pending DEBUG rollups when due or forced."""
    ensure_runtime_log_rollup_state(engine)
    now = time.time()
    if (
        not force
        and (now - float(getattr(engine, "_last_runtime_debug_rollup_log_ts", 0.0) or 0.0))
        < interval_secs
    ):
        return

    counts: Counter = getattr(engine, "_runtime_debug_rollup_counts", Counter())
    if not counts:
        engine._last_runtime_debug_rollup_log_ts = now
        return

    examples: Dict[Tuple[str, ...], Dict[str, str]] = getattr(
        engine, "_runtime_debug_rollup_examples", {}
    )
    total_events = sum(counts.values())
    by_kind = Counter()
    for key, count in counts.items():
        by_kind[key[0]] += count

    LOGGER.debug(
        "runtime debug rollup: events=%d unique_keys=%d by_kind=%s",
        total_events,
        len(counts),
        dict(sorted(by_kind.items())),
    )
    for key, count in counts.most_common(top_n):
        example = examples.get(key, {})
        LOGGER.debug(
            "runtime debug rollup detail kind=%s count=%d game=%s line=%s "
            "inning=%s state=%s source_key=%s example=%s",
            example.get("kind", key[0]),
            count,
            example.get("game", key[2]),
            example.get("line", key[3]),
            example.get("inning", key[4]),
            example.get("state", key[5]),
            example.get("source_key", key[6]),
            example.get("example", ""),
        )
    hidden = len(counts) - int(top_n)
    if hidden > 0:
        LOGGER.debug("runtime debug rollup detail omitted_keys=%d", hidden)

    engine._runtime_debug_rollup_counts = Counter()
    engine._runtime_debug_rollup_examples = {}
    engine._last_runtime_debug_rollup_log_ts = now
