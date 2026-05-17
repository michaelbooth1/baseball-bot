"""Tests for the Stage-2 promotion stability gate.

Mirrors the calibration stability gate's test pattern. The gate decides
whether the daily Stage-2 staging-vs-production Brier diff has been
consistently in staging's favour long enough to recommend promotion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.analysis.run_daily_refresh import (
    STAGE2_PROMOTION_MIN_CONSECUTIVE,
    STAGE2_PROMOTION_MIN_DELTA,
    STAGE2_PROMOTION_MIN_HISTORY,
    STAGE2_PROMOTION_WINDOW,
    _load_stage2_brier_history,
    _stage2_history_row_date,
    _stage2_promotion_verdict,
    _trailing_stage2_history,
    _write_stage2_brier_history_row,
)


def _row(date: str, delta: float, *, generated_at: str = "") -> dict:
    return {
        "generated_at_utc": generated_at or f"{date}T12:00:00.000000Z",
        "data_max_date": date,
        "production_brier": 0.2284,
        "staging_brier": 0.2284 + delta,
        "delta": delta,
    }


def test_history_row_date_prefers_data_max_date():
    row = {"data_max_date": "2026-05-13", "generated_at_utc": "2026-05-14T05:00:00Z"}
    assert _stage2_history_row_date(row) == "2026-05-13"


def test_history_row_date_falls_back_to_generated_at():
    row = {"data_max_date": None, "generated_at_utc": "2026-05-14T05:00:00Z"}
    assert _stage2_history_row_date(row) == "2026-05-14"


def test_history_row_date_handles_empty():
    assert _stage2_history_row_date({}) == ""


def test_trailing_history_dedupes_same_date_to_latest():
    rows = [
        _row("2026-05-10", -0.001, generated_at="2026-05-10T01:00:00Z"),
        _row("2026-05-10", -0.002, generated_at="2026-05-10T05:00:00Z"),
        _row("2026-05-11", -0.0015),
    ]
    trailing = _trailing_stage2_history(rows, window=7)
    # both 5/10 rows collapse to one date entry
    assert len(trailing) == 2
    assert trailing[-1]["data_max_date"] == "2026-05-11"


def test_trailing_history_excludes_today():
    rows = [
        _row("2026-05-10", -0.001),
        _row("2026-05-11", -0.001),
        _row("2026-05-12", -0.001),
    ]
    trailing = _trailing_stage2_history(rows, window=7, exclude_date="2026-05-12")
    assert [r["data_max_date"] for r in trailing] == ["2026-05-10", "2026-05-11"]


def test_trailing_history_clamps_to_window():
    rows = [_row(f"2026-05-{d:02d}", -0.001) for d in range(1, 12)]
    trailing = _trailing_stage2_history(rows, window=5)
    assert len(trailing) == 5
    assert trailing[0]["data_max_date"] == "2026-05-07"
    assert trailing[-1]["data_max_date"] == "2026-05-11"


def test_verdict_insufficient_when_history_too_short():
    rows = [_row(f"2026-05-{d:02d}", -0.005) for d in range(1, STAGE2_PROMOTION_MIN_HISTORY)]
    verdict = _stage2_promotion_verdict(rows)
    assert verdict["verdict"] == "insufficient_history"
    assert verdict["n_history"] == STAGE2_PROMOTION_MIN_HISTORY - 1


def test_verdict_promote_when_consistently_improving():
    # 7 days, all clearly improving
    rows = [_row(f"2026-05-{d:02d}", -0.005) for d in range(5, 12)]
    verdict = _stage2_promotion_verdict(rows)
    assert verdict["verdict"] == "promote"
    assert verdict["n_improving"] == 7


def test_verdict_hold_when_some_days_dont_meet_threshold():
    # 7 days, only 3 clearly improving (below the 5-of-7 threshold)
    deltas = [-0.005, -0.0001, -0.005, +0.002, -0.005, +0.0005, -0.0001]
    rows = [_row(f"2026-05-{d:02d}", dlt) for d, dlt in zip(range(5, 12), deltas)]
    verdict = _stage2_promotion_verdict(rows)
    assert verdict["verdict"] == "hold"
    assert verdict["n_improving"] == 3


def test_verdict_threshold_is_strict():
    # Exactly at the threshold (delta == -min_delta) qualifies; just above
    # (less negative) does not.
    rows = [
        _row("2026-05-05", -STAGE2_PROMOTION_MIN_DELTA),
        _row("2026-05-06", -STAGE2_PROMOTION_MIN_DELTA),
        _row("2026-05-07", -STAGE2_PROMOTION_MIN_DELTA),
        _row("2026-05-08", -STAGE2_PROMOTION_MIN_DELTA),
        _row("2026-05-09", -STAGE2_PROMOTION_MIN_DELTA),
    ]
    verdict = _stage2_promotion_verdict(rows)
    # 5 days, all exactly at threshold, default min_consecutive=5
    assert verdict["verdict"] == "promote"


def test_verdict_excludes_today_from_history():
    # Today is 5/12 with a marginal improvement; the prior 5 days were
    # uniformly improving. Verdict should be promote (today doesn't dilute).
    rows = [
        _row("2026-05-07", -0.005),
        _row("2026-05-08", -0.005),
        _row("2026-05-09", -0.005),
        _row("2026-05-10", -0.005),
        _row("2026-05-11", -0.005),
        _row("2026-05-12", +0.0005),
    ]
    verdict = _stage2_promotion_verdict(rows, exclude_date="2026-05-12")
    assert verdict["verdict"] == "promote"
    # 5 trailing prior dates, all improving
    assert verdict["n_history"] == 5
    assert verdict["n_improving"] == 5


def test_load_history_returns_empty_for_missing_file(tmp_path: Path):
    rows = _load_stage2_brier_history(tmp_path / "does_not_exist.jsonl")
    assert rows == []


def test_load_history_skips_malformed_lines(tmp_path: Path):
    p = tmp_path / "history.jsonl"
    p.write_text(
        '{"generated_at_utc": "2026-05-10T00:00:00Z", "data_max_date": "2026-05-10", "delta": -0.005}\n'
        'not a json line\n'
        '{"generated_at_utc": "2026-05-11T00:00:00Z", "data_max_date": "2026-05-11", "delta": -0.005}\n',
        encoding="utf-8",
    )
    rows = _load_stage2_brier_history(p)
    assert len(rows) == 2
    assert rows[0]["data_max_date"] == "2026-05-10"
    assert rows[1]["data_max_date"] == "2026-05-11"


def test_write_history_round_trip(tmp_path: Path):
    p = tmp_path / "history.jsonl"
    _write_stage2_brier_history_row(
        p,
        production_brier=0.2284,
        staging_brier=0.2229,
        delta=-0.0055,
        data_max_date="2026-05-13",
        generated_at_utc="2026-05-14T00:15:49.282529Z",
    )
    _write_stage2_brier_history_row(
        p,
        production_brier=0.2284,
        staging_brier=0.2230,
        delta=-0.0054,
        data_max_date="2026-05-14",
        generated_at_utc="2026-05-15T00:15:49.282529Z",
    )
    rows = _load_stage2_brier_history(p)
    assert len(rows) == 2
    assert rows[0]["data_max_date"] == "2026-05-13"
    assert rows[1]["delta"] == pytest.approx(-0.0054)


def test_promotion_constants_align_with_calibration_gate():
    # Sanity: the gate uses the same shape as the calibration stability
    # gate. If these constants change, this test should be updated, not
    # silently passed.
    assert STAGE2_PROMOTION_WINDOW == 7
    assert STAGE2_PROMOTION_MIN_HISTORY == 5
    assert STAGE2_PROMOTION_MIN_CONSECUTIVE == 5
    assert STAGE2_PROMOTION_MIN_DELTA == 0.001
