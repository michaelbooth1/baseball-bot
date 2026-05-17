#!/usr/bin/env python3
"""
build_team_offense_calibration_table.py -- Phase 1 of team-offense V2 calibration.

Produces a leakage-free per-half-inning residual training table for the
Stage-3 (team offense) calibration work tracked in
`model_improvements/team_offense_v2_plan_2026_05_07.txt`.

For every regular-season game in 2021-2026, replays the score state at the
START of each half-inning (outs=0, bases=0) and emits one row per
(game_pk, inning, half, line) with:

  - Stage-1 empirical Over probability (when cell support is sufficient)
  - Stage-1 Poisson Over probability (always when cell exists)
  - Stage-2 logit-delta from the run-environment model
  - base_fv_stage1_plus_stage2 = sigmoid(logit(stage1) + stage2_delta)
  - over_hit (binary outcome: final_total > line)

Stage-3 is deliberately OFF in this table -- the residual
`over_hit - base_fv_stage1_plus_stage2` is the calibration target Stage-3
should explain.

Schema is intentionally compact -- per-team feature engineering (Phase 2)
joins on (game_pk, date, away/home) downstream. Park / temp / wind
metadata is included so we can sanity-check Stage-2 attribution later.

Output:
  data/analysis_output/team_offense_calibration/training_table.jsonl
  data/analysis_output/team_offense_calibration/manifest.json

Usage:
  python scripts/analysis/build_team_offense_calibration_table.py
  python scripts/analysis/build_team_offense_calibration_table.py --max-files 200
  python scripts/analysis/build_team_offense_calibration_table.py --seasons 2024 2025
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import math
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = PROJECT_DIR / "data"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "data" / "analysis_output" / "team_offense_calibration"
DEFAULT_STAGE1_CACHE = PROJECT_DIR / "cache" / "mlb_ou_cache.json"
DEFAULT_STAGE2_MODEL = PROJECT_DIR / "cache" / "mlb_stage2_run_env.json"
DEFAULT_SEASONS = ["2021", "2022", "2023", "2024", "2025", "2026"]

# Stage-3 V2 will explore lines 5.5 - 11.5; cells generally cover 6.5 - 11.5
# but we emit a row whenever the line is still alive (current_total < line).
SUPPORTED_LINES: Tuple[float, ...] = (5.5, 6.5, 7.5, 8.5, 9.5, 10.5, 11.5)

EPS = 1e-6
LOGGER = logging.getLogger("build_team_offense_calibration_table")

# Add cache/ to sys.path so we can import the runtime applier.
sys.path.insert(0, str(PROJECT_DIR / "cache"))
from stage2_run_env_model import (  # noqa: E402
    Stage2RunEnvModel,
    parse_temp_bin,
    parse_wind_bin,
    UNKNOWN_BUCKET,
)


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------


def _clamp01(p: float) -> float:
    return max(EPS, min(1.0 - EPS, p))


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def line_to_emp_key(line: float) -> str:
    return f"o{int(round(line * 10))}"  # 8.5 -> "o85"


def line_to_po_key(line: float) -> str:
    return f"po{int(round(line * 10))}"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def load_stage1_cache(path: Path) -> Tuple[Dict[str, Any], int]:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    extras_bucket = int(d.get("meta", {}).get("extras_bucket", 10))
    return d.get("cells", {}), extras_bucket


def load_stage2_model(path: Path) -> Stage2RunEnvModel:
    return Stage2RunEnvModel.from_path(path)


def state_key(
    away_score: int,
    home_score: int,
    inning: int,
    half: str,  # 'T' or 'B'
    extras_bucket: int,
) -> str:
    """Match Stage-1 builder's state-key shape: outs=0, bases=0 at half-inning start."""
    inning_bucket = min(inning, extras_bucket)
    return f"{away_score}_{home_score}_{inning_bucket}_{half}_0_0"


# ---------------------------------------------------------------------------
# Game iteration
# ---------------------------------------------------------------------------


def iter_game_files(data_root: Path, seasons: Iterable[str], max_files: Optional[int]) -> Iterable[str]:
    count = 0
    for year in seasons:
        pattern = str(data_root / "games" / "regular" / year / "**" / "*.json")
        for fpath in sorted(glob.glob(pattern, recursive=True)):
            yield fpath
            count += 1
            if max_files is not None and count >= max_files:
                return


def _safe_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def extract_game_meta(game: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return metadata + inning runs arrays, or None if game cannot be used."""
    gd = game.get("gameData", {}) or {}
    status = (gd.get("status", {}) or {}).get("abstractGameState", "")
    if status != "Final":
        return None

    teams = gd.get("teams", {}) or {}
    away_abbrev = (teams.get("away", {}) or {}).get("abbreviation", "")
    home_abbrev = (teams.get("home", {}) or {}).get("abbreviation", "")
    if not (away_abbrev and home_abbrev):
        return None

    date = (gd.get("datetime", {}) or {}).get("officialDate", "")
    if not date:
        return None

    venue = gd.get("venue", {}) or {}
    venue_id = venue.get("id")
    venue_name = venue.get("name") or ""

    weather = gd.get("weather", {}) or {}
    temp_f = _safe_int(weather.get("temp"))
    wind_text = weather.get("wind") or ""
    condition = weather.get("condition") or ""

    ld = game.get("liveData", {}) or {}
    ls = ld.get("linescore", {}) or {}
    innings = ls.get("innings", []) or []
    if not innings:
        return None

    # Per-inning runs; some games have None for an inning (rain, walkoff).
    inning_records: List[Dict[str, Optional[int]]] = []
    for inn in innings:
        away_runs = (inn.get("away", {}) or {}).get("runs")
        home_runs = (inn.get("home", {}) or {}).get("runs")
        inning_records.append({
            "num": inn.get("num"),
            "away": _safe_int(away_runs),
            "home": _safe_int(home_runs),
        })

    final_teams = ls.get("teams", {}) or {}
    final_away = _safe_int((final_teams.get("away", {}) or {}).get("runs"))
    final_home = _safe_int((final_teams.get("home", {}) or {}).get("runs"))
    if final_away is None or final_home is None:
        return None
    final_total = final_away + final_home

    return {
        "game_pk": game.get("gamePk"),
        "season": date[:4],
        "date": date,
        "away_abbrev": away_abbrev,
        "home_abbrev": home_abbrev,
        "venue_id": venue_id,
        "venue_name": venue_name,
        "temp_f": temp_f,
        "wind_text": wind_text,
        "condition": condition,
        "innings": inning_records,
        "final_away": final_away,
        "final_home": final_home,
        "final_total": final_total,
    }


def half_inning_boundaries(meta: Dict[str, Any]) -> Iterable[Tuple[int, str, int, int]]:
    """
    Yield (inning, half, away_score_before, home_score_before) at the START of
    each half-inning -- i.e. *before* any runs in that half are scored. State is
    (outs=0, bases=0) at every yield.

    Skips half-innings the game never reached (walkoff bottom-9 not played, etc.).
    """
    away_score = 0
    home_score = 0
    for inn in meta["innings"]:
        num = inn.get("num")
        if num is None:
            continue

        # Top-of-N starts with current cumulative score.
        yield num, "T", away_score, home_score
        if inn["away"] is None:
            # Top half not played (data anomaly); stop iterating.
            return
        away_score += inn["away"]

        # Bottom-of-N starts after the top half completed.
        if inn["home"] is None:
            # Bottom half not played (walkoff after top, etc.); skip.
            continue
        yield num, "B", away_score, home_score
        home_score += inn["home"]


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------


def build_run_env_context(meta: Dict[str, Any]) -> Dict[str, str]:
    """Build the Stage-2 buckets dict matching `RunEnvContext.buckets()` shape."""
    park_bucket = (
        f"venue_{meta['venue_id']}" if meta.get("venue_id") is not None else UNKNOWN_BUCKET
    )
    temp_bucket = parse_temp_bin(meta.get("temp_f"))
    wind_bucket = parse_wind_bin(meta.get("wind_text"))
    park_wind_bucket = (
        f"{park_bucket}__{wind_bucket}" if park_bucket != UNKNOWN_BUCKET else UNKNOWN_BUCKET
    )
    return {
        "park": park_bucket,
        "temp": temp_bucket,
        "wind": wind_bucket,
        "park_wind": park_wind_bucket,
    }


def stage2_logit_delta(
    stage2: Stage2RunEnvModel,
    line: float,
    buckets: Dict[str, str],
) -> float:
    """Stage-2 logit-delta for a line. Wraps the runtime applier's private method."""
    line_key = f"{line:.1f}"
    return stage2._line_delta(line=line_key, buckets=buckets)  # noqa: SLF001


def build_rows_for_game(
    meta: Dict[str, Any],
    cells: Dict[str, Any],
    extras_bucket: int,
    stage2: Stage2RunEnvModel,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    final_total = meta["final_total"]
    buckets = build_run_env_context(meta)

    for inning, half, away_before, home_before in half_inning_boundaries(meta):
        current_total = away_before + home_before
        key = state_key(away_before, home_before, inning, half, extras_bucket)
        cell = cells.get(key)
        # cell may be None for sparse states (e.g. very high scores in extras).
        # We still emit rows for each line that is still alive so the residual
        # is well-defined; stage1 fields will be null when no cell exists.

        for line in SUPPORTED_LINES:
            if current_total >= line + 0.5:
                # Over already locked in (final_total >= line is guaranteed
                # since current >= line+0.5 means int >= line+1 >= ceil(line)+1).
                # Skip; not a prediction problem.
                continue

            emp_key = line_to_emp_key(line)
            po_key = line_to_po_key(line)

            stage1_emp = None
            stage1_po = None
            if cell is not None:
                ev = cell.get(emp_key)
                pv = cell.get(po_key)
                if isinstance(ev, (int, float)):
                    stage1_emp = float(ev)
                if isinstance(pv, (int, float)):
                    stage1_po = float(pv)

            # Pick best Stage-1: empirical when present, Poisson otherwise.
            stage1_pick = stage1_emp if stage1_emp is not None else stage1_po
            stage1_source = "emp" if stage1_emp is not None else ("po" if stage1_po is not None else None)

            if stage1_pick is None:
                # Line not priced by Stage-1 at this state -- not a calibration
                # target. Skip rather than write a null row.
                continue
            stage2_delta = stage2_logit_delta(stage2, line, buckets)
            stage1_plus_stage2 = _sigmoid(_logit(_clamp01(stage1_pick)) + stage2_delta)

            over_hit = 1 if final_total > line else 0  # standard total-runs rule

            rows.append({
                "game_pk": meta["game_pk"],
                "season": meta["season"],
                "date": meta["date"],
                "away": meta["away_abbrev"],
                "home": meta["home_abbrev"],
                "venue_id": meta["venue_id"],
                "venue_name": meta["venue_name"],
                "temp_f": meta["temp_f"],
                "wind_text": meta["wind_text"],
                "condition": meta["condition"],
                "park_bucket": buckets["park"],
                "temp_bucket": buckets["temp"],
                "wind_bucket": buckets["wind"],
                "inning": inning,
                "half": half,
                "away_score_before": away_before,
                "home_score_before": home_before,
                "current_total": current_total,
                "line": line,
                "stage1_emp": stage1_emp,
                "stage1_po": stage1_po,
                "stage1_pick": stage1_pick,
                "stage1_source": stage1_source,
                "stage2_delta": stage2_delta,
                "base_fv_stage1_plus_stage2": stage1_plus_stage2,
                "final_away": meta["final_away"],
                "final_home": meta["final_home"],
                "final_total": final_total,
                "over_hit": over_hit,
            })

    return rows


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    p.add_argument("--seasons", nargs="+", default=DEFAULT_SEASONS)
    p.add_argument("--stage1-cache", type=Path, default=DEFAULT_STAGE1_CACHE)
    p.add_argument("--stage2-model", type=Path, default=DEFAULT_STAGE2_MODEL)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--max-files", type=int, default=None,
                   help="Cap on number of game files (smoke test).")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    LOGGER.info("Loading Stage-1 cache from %s", args.stage1_cache)
    cells, extras_bucket = load_stage1_cache(args.stage1_cache)
    LOGGER.info("  %d cells, extras_bucket=%d", len(cells), extras_bucket)

    LOGGER.info("Loading Stage-2 model from %s", args.stage2_model)
    stage2 = load_stage2_model(args.stage2_model)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / "training_table.jsonl"
    manifest_path = args.output_dir / "manifest.json"

    started = time.time()
    games_seen = 0
    games_used = 0
    games_skipped = 0
    rows_written = 0
    rows_with_stage1 = 0
    rows_with_stage1_emp = 0
    season_counts: Dict[str, int] = {}
    error_count = 0

    LOGGER.info("Writing rows to %s", output_path)
    with open(output_path, "w", encoding="utf-8") as out:
        for fpath in iter_game_files(args.data_root, args.seasons, args.max_files):
            games_seen += 1
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    game = json.load(f)
            except Exception as exc:
                error_count += 1
                LOGGER.debug("Skip %s: %s", fpath, exc)
                continue

            meta = extract_game_meta(game)
            if meta is None:
                games_skipped += 1
                continue

            try:
                rows = build_rows_for_game(meta, cells, extras_bucket, stage2)
            except Exception as exc:
                error_count += 1
                LOGGER.warning("Row-build failure for %s: %s", fpath, exc)
                continue

            if not rows:
                games_skipped += 1
                continue
            games_used += 1
            season_counts[meta["season"]] = season_counts.get(meta["season"], 0) + 1

            for r in rows:
                if r["stage1_pick"] is not None:
                    rows_with_stage1 += 1
                if r["stage1_emp"] is not None:
                    rows_with_stage1_emp += 1
                out.write(json.dumps(r, separators=(",", ":")))
                out.write("\n")
                rows_written += 1

            if games_seen % 1000 == 0:
                elapsed = time.time() - started
                LOGGER.info(
                    "  progress: %d games seen, %d used, %d rows, %.0fs",
                    games_seen, games_used, rows_written, elapsed,
                )

    elapsed = time.time() - started
    manifest = {
        "schema_version": 1,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_seconds": round(elapsed, 1),
        "args": {
            "seasons": args.seasons,
            "max_files": args.max_files,
            "stage1_cache": str(args.stage1_cache),
            "stage2_model": str(args.stage2_model),
        },
        "stage1_cells": len(cells),
        "extras_bucket": extras_bucket,
        "supported_lines": list(SUPPORTED_LINES),
        "games_seen": games_seen,
        "games_used": games_used,
        "games_skipped": games_skipped,
        "season_counts": season_counts,
        "rows_written": rows_written,
        "rows_with_stage1": rows_with_stage1,
        "rows_with_stage1_emp": rows_with_stage1_emp,
        "rows_stage1_coverage": round(rows_with_stage1 / max(1, rows_written), 4),
        "rows_stage1_emp_coverage": round(rows_with_stage1_emp / max(1, rows_written), 4),
        "error_count": error_count,
        "output": str(output_path),
        "row_columns": [
            "game_pk", "season", "date", "away", "home", "venue_id", "venue_name",
            "temp_f", "wind_text", "condition", "park_bucket", "temp_bucket", "wind_bucket",
            "inning", "half", "away_score_before", "home_score_before", "current_total",
            "line", "stage1_emp", "stage1_po", "stage1_pick", "stage1_source",
            "stage2_delta", "base_fv_stage1_plus_stage2",
            "final_away", "final_home", "final_total", "over_hit",
        ],
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    LOGGER.info("Done: %d rows written in %.1fs (%.0f rows/s)",
                rows_written, elapsed, rows_written / max(1.0, elapsed))
    LOGGER.info("Manifest: %s", manifest_path)


if __name__ == "__main__":
    main()
