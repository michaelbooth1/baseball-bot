"""Tests for the Stage-1 cache staging-then-promote guard.

Stage-1 is a deterministic empirical lookup; the right default is to
auto-promote the freshly-built staging cache to production. The guard's
only job is to refuse when the staging cache looks like a partial scrape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.analysis.run_daily_refresh import (
    STAGE1_HISTORY_FULL_SEASONS,
    STAGE1_PROMOTE_MIN_GAMES_RATIO,
    _stage1_promotion_guard,
    _stage1_total_games,
)


# Fixture for an "expected" 2026 production-window cache: 5 prior seasons.
def _well_formed_cache(*, total_games: int, active_year: int = 2026) -> dict:
    end = active_year - 1
    start = end - STAGE1_HISTORY_FULL_SEASONS + 1
    seasons = [str(y) for y in range(start, end + 1)]
    return {
        "meta": {
            "total_games": total_games,
            "history_start_date": f"{start}-01-01",
            "history_end_date": f"{end}-12-31",
            "seasons": seasons,
            "games_by_season": {s: total_games // len(seasons) for s in seasons},
            "duplicate_game_files_skipped": 0,
        },
        # The actual cache body is a state-keyed lookup table; the guard
        # doesn't inspect it, so an empty dict is fine.
        "states": {},
    }


def test_total_games_reads_canonical_field():
    assert _stage1_total_games(_well_formed_cache(total_games=12500)) == 12500


def test_total_games_falls_back_to_games_loaded():
    payload = {"meta": {"games_loaded": 9999}}
    assert _stage1_total_games(payload) == 9999


def test_total_games_returns_none_for_garbage():
    assert _stage1_total_games(None) is None
    assert _stage1_total_games({}) is None
    assert _stage1_total_games({"meta": "not a dict"}) is None
    assert _stage1_total_games({"meta": {"total_games": 0}}) is None


def test_promote_guard_blocks_on_unreadable_staging():
    ok, reason = _stage1_promotion_guard("not a dict", None, active_date="2026-05-14")
    assert not ok
    assert "not a dict" in reason


def test_promote_guard_blocks_when_staging_lacks_total_games():
    ok, reason = _stage1_promotion_guard({"meta": {}}, None, active_date="2026-05-14")
    assert not ok
    assert "total_games" in reason


def test_promote_guard_first_run_passes_when_production_missing():
    staging = _well_formed_cache(total_games=12500)
    ok, reason = _stage1_promotion_guard(staging, None, active_date="2026-05-14")
    assert ok
    assert "First-run" in reason


def test_promote_guard_passes_when_staging_grows():
    prod = _well_formed_cache(total_games=12500)
    staging = _well_formed_cache(total_games=12550)  # +50 games
    ok, reason = _stage1_promotion_guard(staging, prod, active_date="2026-05-14")
    assert ok
    assert "sanity guard passed" in reason


def test_promote_guard_passes_when_staging_equals_production():
    prod = _well_formed_cache(total_games=12500)
    staging = _well_formed_cache(total_games=12500)
    ok, _ = _stage1_promotion_guard(staging, prod, active_date="2026-05-14")
    assert ok


def test_promote_guard_passes_at_exact_floor():
    prod = _well_formed_cache(total_games=10000)
    floor = int(10000 * STAGE1_PROMOTE_MIN_GAMES_RATIO)  # 9900
    staging = _well_formed_cache(total_games=floor)
    ok, _ = _stage1_promotion_guard(staging, prod, active_date="2026-05-14")
    assert ok


def test_promote_guard_blocks_partial_scrape():
    prod = _well_formed_cache(total_games=12500)
    # Half the games -- catastrophic scrape failure
    staging = _well_formed_cache(total_games=6000)
    ok, reason = _stage1_promotion_guard(staging, prod, active_date="2026-05-14")
    assert not ok
    assert "refusing to promote" in reason
    assert "6000" in reason


def test_promote_guard_blocks_when_staging_below_99pct():
    prod = _well_formed_cache(total_games=12500)
    staging = _well_formed_cache(total_games=12000)  # 96% of 12500
    ok, reason = _stage1_promotion_guard(staging, prod, active_date="2026-05-14")
    assert not ok
    assert "12000" in reason and "12500" in reason


def test_promote_guard_recovers_corrupt_production():
    # Production exists but has no total_games metadata; staging is well-formed.
    prod = {"meta": {"history_start_date": "2021-01-01"}}  # no total_games
    staging = _well_formed_cache(total_games=12500)
    ok, reason = _stage1_promotion_guard(staging, prod, active_date="2026-05-14")
    assert ok
    assert "recover" in reason


def test_promote_guard_blocks_on_narrowed_season_window():
    # Staging missing 2 of the expected 5 seasons.
    staging = _well_formed_cache(total_games=12500)
    staging["meta"]["seasons"] = staging["meta"]["seasons"][2:]
    staging["meta"]["games_by_season"] = {
        s: v for s, v in staging["meta"]["games_by_season"].items()
        if s in staging["meta"]["seasons"]
    }
    prod = _well_formed_cache(total_games=12500)
    ok, reason = _stage1_promotion_guard(staging, prod, active_date="2026-05-14")
    assert not ok
    assert "coverage" in reason.lower()


def test_promote_guard_constants_match_design():
    assert STAGE1_PROMOTE_MIN_GAMES_RATIO == 0.99
