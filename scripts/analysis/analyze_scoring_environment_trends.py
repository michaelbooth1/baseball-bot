#!/usr/bin/env python3
"""
Analyze MLB scoring-environment drift from local game feeds.

This is a research artifact for Stage-1 fair-value weighting. It reads only
local StatsAPI game JSON files, deduplicates repeated schedule-date files by
gamePk, and emits season/month/line scoring diagnostics plus weighting
backtests for candidate historical-season weighting schemes.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:  # CLI execution from repo root.
    from cache.build_mlb_ou_cache import (
        final_score_from_game,
        official_date_from_game,
        is_final_game,
        line_to_threshold,
    )
except ImportError:  # Package import in tests.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from cache.build_mlb_ou_cache import (  # type: ignore
        final_score_from_game,
        official_date_from_game,
        is_final_game,
        line_to_threshold,
    )


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_GAMES_ROOT = PROJECT_DIR / "data" / "games" / "regular"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "scoring_trends"
DEFAULT_LINES = "6.5,7.5,8.5,9.5,10.5,11.5"
EPS = 1e-9


@dataclass(frozen=True)
class GameRecord:
    game_pk: int
    official_date: str
    season: int
    month: int
    away_runs: int
    home_runs: int
    total_runs: int
    venue_id: str
    venue_name: str
    game_type: str
    path: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_date(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    if value is None:
        return None
    try:
        if not math.isfinite(float(value)):
            return None
        return round(float(value), digits)
    except Exception:
        return None


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _std(values: Sequence[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    avg = sum(values) / len(values)
    return math.sqrt(sum((v - avg) ** 2 for v in values) / (len(values) - 1))


def _quantile(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def _pct(numer: int, denom: int) -> Optional[float]:
    return numer / denom if denom else None


def _game_date_from_path(path: Path) -> str:
    parts = path.parts
    try:
        idx = parts.index("regular")
        return f"{int(parts[idx + 1]):04d}-{int(parts[idx + 2]):02d}-{int(parts[idx + 3]):02d}"
    except Exception:
        return ""


def _extract_game(path: Path, allowed_game_types: set[str]) -> Optional[GameRecord]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    game_pk = data.get("gamePk")
    if not isinstance(game_pk, int):
        return None
    gd = data.get("gameData", {}) or {}
    game_type = str((gd.get("game", {}) or {}).get("type") or "").upper()
    if allowed_game_types and game_type not in allowed_game_types:
        return None
    if not is_final_game(data):
        return None
    score = final_score_from_game(data)
    if score is None:
        return None
    official_date = official_date_from_game(data) or _game_date_from_path(path)
    if len(official_date) < 10:
        return None
    away_runs, home_runs = score
    venue = gd.get("venue", {}) or {}
    venue_id = str(venue.get("id") or "")
    venue_name = str(venue.get("name") or "").strip() or "unknown"
    season = int(official_date[:4])
    month = int(official_date[5:7])
    return GameRecord(
        game_pk=game_pk,
        official_date=official_date[:10],
        season=season,
        month=month,
        away_runs=int(away_runs),
        home_runs=int(home_runs),
        total_runs=int(away_runs + home_runs),
        venue_id=venue_id,
        venue_name=venue_name,
        game_type=game_type,
        path=str(path),
    )


def load_games(
    games_root: Path,
    *,
    min_season: int,
    max_season: int,
    allowed_game_types: set[str],
    as_of_date: str = "",
) -> Tuple[List[GameRecord], Dict[str, Any]]:
    files = sorted(games_root.rglob("*.json"))
    as_of = _parse_date(as_of_date) if as_of_date else None
    deduped: Dict[int, GameRecord] = {}
    duplicate_files = 0
    parsed = 0
    for path in files:
        record = _extract_game(path, allowed_game_types)
        if record is None:
            continue
        parsed += 1
        if record.season < min_season or record.season > max_season:
            continue
        if as_of and _parse_date(record.official_date) > as_of:
            continue
        if record.game_pk in deduped:
            duplicate_files += 1
            # Prefer the feed whose schedule path matches officialDate. This
            # avoids postponed-game duplicates landing under the old date.
            current = deduped[record.game_pk]
            if _game_date_from_path(Path(record.path)) == record.official_date:
                deduped[record.game_pk] = record
            elif _game_date_from_path(Path(current.path)) != current.official_date:
                deduped[record.game_pk] = min([current, record], key=lambda r: r.path)
            continue
        deduped[record.game_pk] = record
    out = sorted(deduped.values(), key=lambda r: (r.official_date, r.game_pk))
    meta = {
        "files_seen": len(files),
        "parsed_final_games": parsed,
        "deduped_games": len(out),
        "duplicate_game_files_skipped": duplicate_files,
        "games_root": str(games_root),
        "min_season": min_season,
        "max_season": max_season,
        "as_of_date": as_of_date,
    }
    return out, meta


def _season_row(
    season: int,
    games: Sequence[GameRecord],
    *,
    lines: Sequence[str],
    park_adjusted_rpg: Optional[float] = None,
) -> Dict[str, Any]:
    totals = [float(g.total_runs) for g in games]
    home = [float(g.home_runs) for g in games]
    away = [float(g.away_runs) for g in games]
    row: Dict[str, Any] = {
        "season": season,
        "games": len(games),
        "first_game_date": min((g.official_date for g in games), default=""),
        "last_game_date": max((g.official_date for g in games), default=""),
        "runs_per_game": _round(_mean(totals)),
        "park_adjusted_runs_per_game": _round(park_adjusted_rpg),
        "median_total": _round(float(median(totals)) if totals else None),
        "std_total": _round(_std(totals)),
        "p25_total": _round(_quantile(totals, 0.25)),
        "p75_total": _round(_quantile(totals, 0.75)),
        "away_runs_per_game": _round(_mean(away)),
        "home_runs_per_game": _round(_mean(home)),
        "home_minus_away_rpg": _round((_mean(home) or 0.0) - (_mean(away) or 0.0) if games else None),
        "pct_12_plus_runs": _round(_pct(sum(1 for v in totals if v >= 12), len(totals))),
        "pct_6_or_fewer_runs": _round(_pct(sum(1 for v in totals if v <= 6), len(totals))),
    }
    for line in lines:
        threshold = line_to_threshold(line)
        row[f"over_{line}_rate"] = _round(_pct(sum(1 for v in totals if v >= threshold), len(totals)))
    return row


def build_season_stats(games: Sequence[GameRecord], lines: Sequence[str]) -> List[Dict[str, Any]]:
    by_season: Dict[int, List[GameRecord]] = defaultdict(list)
    for game in games:
        by_season[game.season].append(game)
    adjusted = park_adjusted_season_rpg(games)
    return [
        _season_row(season, by_season[season], lines=lines, park_adjusted_rpg=adjusted.get(season))
        for season in sorted(by_season)
    ]


def build_month_stats(games: Sequence[GameRecord], lines: Sequence[str]) -> List[Dict[str, Any]]:
    by_key: Dict[Tuple[int, int], List[GameRecord]] = defaultdict(list)
    for game in games:
        by_key[(game.season, game.month)].append(game)
    rows: List[Dict[str, Any]] = []
    for (season, month), group in sorted(by_key.items()):
        totals = [float(g.total_runs) for g in group]
        row: Dict[str, Any] = {
            "season": season,
            "month": month,
            "games": len(group),
            "runs_per_game": _round(_mean(totals)),
            "median_total": _round(float(median(totals)) if totals else None),
            "std_total": _round(_std(totals)),
        }
        for line in lines:
            threshold = line_to_threshold(line)
            row[f"over_{line}_rate"] = _round(_pct(sum(1 for v in totals if v >= threshold), len(totals)))
        rows.append(row)
    return rows


def build_line_rates_by_season(games: Sequence[GameRecord], lines: Sequence[str]) -> List[Dict[str, Any]]:
    by_season: Dict[int, List[GameRecord]] = defaultdict(list)
    for game in games:
        by_season[game.season].append(game)
    rows: List[Dict[str, Any]] = []
    for season, group in sorted(by_season.items()):
        totals = [g.total_runs for g in group]
        for line in lines:
            threshold = line_to_threshold(line)
            rows.append(
                {
                    "season": season,
                    "line": line,
                    "games": len(group),
                    "over_rate": _round(_pct(sum(1 for v in totals if v >= threshold), len(totals))),
                    "under_or_push_rate": _round(_pct(sum(1 for v in totals if v < threshold), len(totals))),
                }
            )
    return rows


def park_adjusted_season_rpg(games: Sequence[GameRecord], *, shrink_n: float = 120.0, iterations: int = 12) -> Dict[int, float]:
    if not games:
        return {}
    overall = sum(g.total_runs for g in games) / len(games)
    seasons = sorted({g.season for g in games})
    venues = sorted({g.venue_id or g.venue_name for g in games})
    season_eff = {season: 0.0 for season in seasons}
    venue_eff = {venue: 0.0 for venue in venues}
    by_season: Dict[int, List[GameRecord]] = defaultdict(list)
    by_venue: Dict[str, List[GameRecord]] = defaultdict(list)
    for game in games:
        by_season[game.season].append(game)
        by_venue[game.venue_id or game.venue_name].append(game)

    for _ in range(iterations):
        for season, group in by_season.items():
            season_eff[season] = sum(
                g.total_runs - overall - venue_eff[g.venue_id or g.venue_name]
                for g in group
            ) / len(group)
        center = sum(season_eff[g.season] for g in games) / len(games)
        for season in seasons:
            season_eff[season] -= center

        for venue, group in by_venue.items():
            raw = sum(g.total_runs - overall - season_eff[g.season] for g in group) / len(group)
            venue_eff[venue] = raw * (len(group) / (len(group) + shrink_n))
        center = sum(venue_eff[g.venue_id or g.venue_name] for g in games) / len(games)
        for venue in venues:
            venue_eff[venue] -= center

    return {season: overall + season_eff[season] for season in seasons}


def _weighted_average(values: Mapping[int, float], weights: Mapping[int, float]) -> Optional[float]:
    denom = sum(weights.get(k, 0.0) for k in values)
    if denom <= 0:
        return None
    return sum(values[k] * weights.get(k, 0.0) for k in values) / denom


def _exp_weights(seasons: Sequence[int], *, as_of_season: int, half_life: float) -> Dict[int, float]:
    raw = {s: 0.5 ** ((as_of_season - s) / half_life) for s in seasons}
    total = sum(raw.values())
    return {s: v / total for s, v in raw.items()} if total > 0 else {}


def _linear_trend_predict(values: Mapping[int, float], target_season: int) -> Optional[float]:
    if len(values) < 3:
        return _mean(list(values.values()))
    xs = [float(s) for s in sorted(values)]
    ys = [float(values[int(s)]) for s in xs]
    xbar = sum(xs) / len(xs)
    ybar = sum(ys) / len(ys)
    denom = sum((x - xbar) ** 2 for x in xs)
    if denom <= 0:
        return ybar
    slope = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denom
    intercept = ybar - slope * xbar
    return intercept + slope * target_season


def _rpg_by_season(games: Sequence[GameRecord], *, full_season_min_games: int) -> Dict[int, float]:
    by_season: Dict[int, List[GameRecord]] = defaultdict(list)
    for game in games:
        by_season[game.season].append(game)
    return {
        season: sum(g.total_runs for g in group) / len(group)
        for season, group in by_season.items()
        if len(group) >= full_season_min_games
    }


def backtest_weighting_schemes(
    games: Sequence[GameRecord],
    *,
    min_train_seasons: int = 3,
    full_season_min_games: int = 1000,
) -> List[Dict[str, Any]]:
    values = _rpg_by_season(games, full_season_min_games=full_season_min_games)
    seasons = sorted(values)
    schemes = ["all_prior_uniform", "recent3_uniform", "recent5_uniform", "exp_half_life_2", "exp_half_life_4", "linear_trend"]
    predictions: Dict[str, List[Tuple[int, float, float]]] = {scheme: [] for scheme in schemes}
    for target in seasons:
        prior = [s for s in seasons if s < target]
        if len(prior) < min_train_seasons:
            continue
        actual = values[target]
        prior_values = {s: values[s] for s in prior}
        candidates: Dict[str, Optional[float]] = {
            "all_prior_uniform": _mean(list(prior_values.values())),
            "recent3_uniform": _mean([values[s] for s in prior[-3:]]),
            "recent5_uniform": _mean([values[s] for s in prior[-5:]]),
            "exp_half_life_2": _weighted_average(prior_values, _exp_weights(prior, as_of_season=target, half_life=2.0)),
            "exp_half_life_4": _weighted_average(prior_values, _exp_weights(prior, as_of_season=target, half_life=4.0)),
            "linear_trend": _linear_trend_predict(prior_values, target),
        }
        for scheme, pred in candidates.items():
            if pred is not None:
                predictions[scheme].append((target, pred, actual))

    rows: List[Dict[str, Any]] = []
    for scheme, vals in predictions.items():
        errors = [pred - actual for _season, pred, actual in vals]
        abs_errors = [abs(e) for e in errors]
        sq_errors = [e * e for e in errors]
        rows.append(
            {
                "scheme": scheme,
                "test_seasons": len(vals),
                "first_test_season": min((v[0] for v in vals), default=None),
                "last_test_season": max((v[0] for v in vals), default=None),
                "mae_rpg": _round(_mean(abs_errors)),
                "rmse_rpg": _round(math.sqrt(_mean(sq_errors) or 0.0) if sq_errors else None),
                "bias_rpg": _round(_mean(errors)),
            }
        )
    return sorted(rows, key=lambda r: (r.get("mae_rpg") is None, r.get("mae_rpg") or 999.0))


def project_current_season(
    games: Sequence[GameRecord],
    *,
    projection_season: int,
    history_start_season: int,
    history_end_season: int,
    prior_strength_games: float = 600.0,
) -> Dict[str, Any]:
    current = [g for g in games if g.season == projection_season]
    full_history = [g for g in games if history_start_season <= g.season <= history_end_season]
    history_rpg = _rpg_by_season(full_history, full_season_min_games=1000)
    trend_prior = _linear_trend_predict(history_rpg, projection_season)
    if not current:
        return {
            "projection_season": projection_season,
            "status": "no_current_games",
            "trend_prior_rpg": _round(trend_prior),
        }

    latest = max(g.official_date for g in current)
    cutoff_mmdd = latest[5:10]
    current_ytd = sum(g.total_runs for g in current) / len(current)
    ratios: List[float] = []
    deltas: List[float] = []
    for season in sorted(history_rpg):
        hist_games = [g for g in full_history if g.season == season]
        same_period = [g for g in hist_games if g.official_date[5:10] <= cutoff_mmdd]
        if len(same_period) < 100:
            continue
        ytd = sum(g.total_runs for g in same_period) / len(same_period)
        full = history_rpg[season]
        if ytd > 0:
            ratios.append(full / ytd)
        deltas.append(full - ytd)
    avg_ratio = _mean(ratios)
    avg_delta = _mean(deltas)
    ratio_projection = current_ytd * avg_ratio if avg_ratio is not None else None
    delta_projection = current_ytd + avg_delta if avg_delta is not None else None
    ytd_adjusted = _mean([v for v in (ratio_projection, delta_projection) if v is not None])
    if ytd_adjusted is None:
        ytd_adjusted = current_ytd
    if trend_prior is None:
        blended = ytd_adjusted
    else:
        w = len(current) / (len(current) + prior_strength_games)
        blended = w * ytd_adjusted + (1.0 - w) * trend_prior
    history_values = list(history_rpg.values())
    return {
        "projection_season": projection_season,
        "status": "ok",
        "games_ytd": len(current),
        "latest_game_date": latest,
        "cutoff_mmdd": cutoff_mmdd,
        "current_ytd_rpg": _round(current_ytd),
        "historical_same_date_full_to_ytd_ratio": _round(avg_ratio),
        "historical_same_date_full_minus_ytd_delta": _round(avg_delta),
        "ytd_adjusted_projection_rpg": _round(ytd_adjusted),
        "trend_prior_rpg": _round(trend_prior),
        "blended_projection_rpg": _round(blended),
        "projection_vs_10y_mean": _round(blended - (_mean(history_values) or blended)),
        "projection_index_vs_10y": _round(blended / (_mean(history_values) or blended)),
        "same_date_history_seasons": len(deltas),
    }


def recommended_season_weights(
    season_stats: Sequence[Mapping[str, Any]],
    *,
    target_rpg: Optional[float],
    history_start_season: int,
    history_end_season: int,
    half_life: float = 3.0,
    similarity_scale_rpg: float = 0.35,
) -> List[Dict[str, Any]]:
    rows = [
        row for row in season_stats
        if history_start_season <= int(row["season"]) <= history_end_season and row.get("runs_per_game") is not None
    ]
    if not rows:
        return []
    if target_rpg is None:
        target_rpg = float(rows[-1]["runs_per_game"])
    max_season = max(int(row["season"]) for row in rows)
    raw_weights: Dict[int, float] = {}
    for row in rows:
        season = int(row["season"])
        rpg = float(row["runs_per_game"])
        recency = 0.5 ** ((max_season - season) / half_life)
        similarity = math.exp(-abs(rpg - target_rpg) / max(similarity_scale_rpg, EPS))
        raw_weights[season] = recency * similarity
    total = sum(raw_weights.values())
    out: List[Dict[str, Any]] = []
    for row in rows:
        season = int(row["season"])
        rpg = float(row["runs_per_game"])
        out.append(
            {
                "season": season,
                "runs_per_game": _round(rpg),
                "target_rpg": _round(target_rpg),
                "rpg_abs_gap_to_target": _round(abs(rpg - target_rpg)),
                "weight": _round(raw_weights[season] / total if total > 0 else 0.0, 8),
            }
        )
    return out


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: List[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _trend_slope(season_stats: Sequence[Mapping[str, Any]], *, start: int, end: int) -> Optional[float]:
    values = {
        int(row["season"]): float(row["runs_per_game"])
        for row in season_stats
        if start <= int(row["season"]) <= end and row.get("runs_per_game") is not None
    }
    if len(values) < 3:
        return None
    xs = [float(s) for s in sorted(values)]
    ys = [values[int(s)] for s in xs]
    xbar = sum(xs) / len(xs)
    ybar = sum(ys) / len(ys)
    denom = sum((x - xbar) ** 2 for x in xs)
    if denom <= 0:
        return None
    return sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denom


def build_report(
    games: Sequence[GameRecord],
    *,
    meta: Mapping[str, Any],
    lines: Sequence[str],
    history_start_season: int,
    history_end_season: int,
    projection_season: int,
) -> Dict[str, Any]:
    season_stats = build_season_stats(games, lines)
    month_stats = build_month_stats(games, lines)
    line_rates = build_line_rates_by_season(games, lines)
    weighting_backtest = backtest_weighting_schemes(games)
    projection = project_current_season(
        games,
        projection_season=projection_season,
        history_start_season=history_start_season,
        history_end_season=history_end_season,
    )
    target = projection.get("blended_projection_rpg") or projection.get("trend_prior_rpg")
    weights = recommended_season_weights(
        season_stats,
        target_rpg=float(target) if target is not None else None,
        history_start_season=history_start_season,
        history_end_season=history_end_season,
    )
    full_history = [
        row for row in season_stats
        if history_start_season <= int(row["season"]) <= history_end_season
    ]
    rpgs = [float(row["runs_per_game"]) for row in full_history if row.get("runs_per_game") is not None]
    latest_full = next((row for row in reversed(full_history) if int(row["season"]) == history_end_season), None)
    ten_year_mean = _mean(rpgs)
    five_year_rows = [row for row in full_history if int(row["season"]) >= history_end_season - 4]
    five_year_mean = _mean([float(row["runs_per_game"]) for row in five_year_rows if row.get("runs_per_game") is not None])
    return {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "input": dict(meta),
        "history_start_season": history_start_season,
        "history_end_season": history_end_season,
        "projection_season": projection_season,
        "summary": {
            "history_games": sum(int(row["games"]) for row in full_history),
            "ten_year_mean_rpg": _round(ten_year_mean),
            "recent_five_year_mean_rpg": _round(five_year_mean),
            "latest_full_season_rpg": _round(float(latest_full["runs_per_game"])) if latest_full else None,
            "latest_full_vs_10y_mean": _round(float(latest_full["runs_per_game"]) - (ten_year_mean or 0.0)) if latest_full and ten_year_mean else None,
            "ten_year_linear_slope_rpg_per_year": _round(_trend_slope(season_stats, start=history_start_season, end=history_end_season)),
            "post_2023_linear_slope_rpg_per_year": _round(_trend_slope(season_stats, start=2023, end=history_end_season)),
        },
        "projection": projection,
        "season_stats": season_stats,
        "monthly_stats": month_stats,
        "line_rates_by_season": line_rates,
        "weighting_backtest": weighting_backtest,
        "recommended_stage1_season_weights": weights,
        "notes": [
            "Recommended weights are research-only and combine recency with similarity to projected run environment.",
            "Projection uses current-season YTD RPG, historical same-date YTD-to-full adjustments, and a trend prior with shrinkage.",
            "Park-adjusted RPG is a simple additive season + shrunken venue fixed-effect diagnostic, not a full causal model.",
        ],
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# MLB Scoring Environment Trends")
    lines.append("")
    lines.append(f"- Generated: `{report.get('generated_at_utc')}`")
    inp = report.get("input", {}) or {}
    lines.append(f"- Games: `{inp.get('deduped_games')}` deduped final games from `{inp.get('games_root')}`")
    lines.append(f"- History window: `{report.get('history_start_season')}-{report.get('history_end_season')}`")
    lines.append(f"- Projection season: `{report.get('projection_season')}`")
    lines.append("")

    summary = report.get("summary", {}) or {}
    lines.append("## Headline")
    lines.append(f"- 10-year mean RPG: `{_round(summary.get('ten_year_mean_rpg'))}`")
    lines.append(f"- Recent 5-year mean RPG: `{_round(summary.get('recent_five_year_mean_rpg'))}`")
    lines.append(f"- Latest full-season RPG: `{_round(summary.get('latest_full_season_rpg'))}`")
    lines.append(f"- 10-year linear slope: `{_round(summary.get('ten_year_linear_slope_rpg_per_year'))}` RPG/year")
    lines.append(f"- Post-2023 slope: `{_round(summary.get('post_2023_linear_slope_rpg_per_year'))}` RPG/year")
    proj = report.get("projection", {}) or {}
    if proj.get("status") == "ok":
        lines.append(
            f"- {proj.get('projection_season')} YTD RPG `{proj.get('current_ytd_rpg')}` "
            f"through `{proj.get('latest_game_date')}`; blended full-season projection "
            f"`{proj.get('blended_projection_rpg')}`."
        )
    else:
        lines.append(f"- Projection status: `{proj.get('status')}`")
    lines.append("")

    lines.append("## Season Scoring")
    lines.append("| Season | Games | RPG | Park-Adj RPG | Median | O7.5 | O8.5 | O9.5 |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in report.get("season_stats", []) or []:
        lines.append(
            "| {season} | {games} | {rpg} | {park} | {med} | {o75} | {o85} | {o95} |".format(
                season=row.get("season"),
                games=row.get("games"),
                rpg=row.get("runs_per_game"),
                park=row.get("park_adjusted_runs_per_game"),
                med=row.get("median_total"),
                o75=row.get("over_7.5_rate", ""),
                o85=row.get("over_8.5_rate", ""),
                o95=row.get("over_9.5_rate", ""),
            )
        )
    lines.append("")

    lines.append("## Weighting Backtest")
    lines.append("| Scheme | Test Seasons | MAE RPG | RMSE RPG | Bias RPG |")
    lines.append("|---|---:|---:|---:|---:|")
    for row in report.get("weighting_backtest", []) or []:
        lines.append(
            f"| {row.get('scheme')} | {row.get('test_seasons')} | {row.get('mae_rpg')} | "
            f"{row.get('rmse_rpg')} | {row.get('bias_rpg')} |"
        )
    lines.append("")

    lines.append("## Proposed Research Weights")
    lines.append("| Season | RPG | Gap To Target | Weight |")
    lines.append("|---:|---:|---:|---:|")
    for row in report.get("recommended_stage1_season_weights", []) or []:
        lines.append(
            f"| {row.get('season')} | {row.get('runs_per_game')} | "
            f"{row.get('rpg_abs_gap_to_target')} | {row.get('weight')} |"
        )
    lines.append("")
    lines.append("These weights are not a live change. They are a candidate prior for future Stage-1 weighted-cache experiments.")
    return "\n".join(lines).rstrip() + "\n"


def write_outputs(report: Mapping[str, Any], output_root: Path) -> Dict[str, str]:
    output_root.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(report, indent=2, sort_keys=True)
    paths = {
        "summary_json": output_root / "scoring_trends_summary.json",
        "report_md": output_root / "scoring_trends_report.md",
        "season_csv": output_root / "season_scoring_trends.csv",
        "monthly_csv": output_root / "monthly_scoring_trends.csv",
        "line_rates_csv": output_root / "line_over_rates_by_season.csv",
        "weighting_backtest_csv": output_root / "season_weighting_backtest.csv",
        "recommended_weights_csv": output_root / "recommended_stage1_season_weights.csv",
    }
    paths["summary_json"].write_text(json_text, encoding="utf-8")
    paths["report_md"].write_text(render_markdown(report), encoding="utf-8")
    _write_csv(paths["season_csv"], report.get("season_stats", []) or [])
    _write_csv(paths["monthly_csv"], report.get("monthly_stats", []) or [])
    _write_csv(paths["line_rates_csv"], report.get("line_rates_by_season", []) or [])
    _write_csv(paths["weighting_backtest_csv"], report.get("weighting_backtest", []) or [])
    _write_csv(paths["recommended_weights_csv"], report.get("recommended_stage1_season_weights", []) or [])
    return {key: str(path) for key, path in paths.items()}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Analyze MLB scoring-environment trends from local game feeds.")
    p.add_argument("--games-root", type=Path, default=DEFAULT_GAMES_ROOT)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--min-season", type=int, default=2016)
    p.add_argument("--max-season", type=int, default=2026)
    p.add_argument("--history-start-season", type=int, default=2016)
    p.add_argument("--history-end-season", type=int, default=2025)
    p.add_argument("--projection-season", type=int, default=2026)
    p.add_argument("--as-of-date", type=str, default="")
    p.add_argument("--game-types", type=str, default="R")
    p.add_argument("--lines", type=str, default=DEFAULT_LINES)
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    allowed_game_types = {x.strip().upper() for x in str(args.game_types or "").split(",") if x.strip()}
    lines = [x.strip() for x in str(args.lines or "").split(",") if x.strip()]
    for line in lines:
        line_to_threshold(line)
    games, meta = load_games(
        args.games_root,
        min_season=int(args.min_season),
        max_season=int(args.max_season),
        allowed_game_types=allowed_game_types,
        as_of_date=str(args.as_of_date or ""),
    )
    report = build_report(
        games,
        meta=meta,
        lines=lines,
        history_start_season=int(args.history_start_season),
        history_end_season=int(args.history_end_season),
        projection_season=int(args.projection_season),
    )
    paths = write_outputs(report, args.output_root)
    print(json.dumps({"paths": paths, "summary": report.get("summary"), "projection": report.get("projection")}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
