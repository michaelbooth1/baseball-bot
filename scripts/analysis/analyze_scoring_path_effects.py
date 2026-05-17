#!/usr/bin/env python3
"""
Analyze whether prior scoring path predicts future MLB scoring.

This research artifact tests whether two games with the same current score and
inning should receive different fair values based on how the score arrived
(early burst vs steady scoring). It reads local StatsAPI game feeds only and
does not affect live trading logic.

Outputs:
  data/analysis_output/scoring_path_effects/
    scoring_path_summary.json
    scoring_path_report.md
    exact_2_2_end4_by_path.csv
    residual_path_buckets.csv
    regression_coefficients.csv
    predictive_lift.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

try:
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
    from sklearn.metrics import (
        brier_score_loss,
        log_loss,
        mean_absolute_error,
        mean_squared_error,
        roc_auc_score,
    )
except Exception:  # pragma: no cover - environment guard
    HistGradientBoostingClassifier = None  # type: ignore
    HistGradientBoostingRegressor = None  # type: ignore
    brier_score_loss = log_loss = mean_absolute_error = mean_squared_error = roc_auc_score = None  # type: ignore

try:  # CLI execution from repo root.
    from cache.build_mlb_ou_cache import (
        final_score_from_game,
        is_final_game,
        line_to_threshold,
        official_date_from_game,
    )
except ImportError:  # pragma: no cover - package import fallback
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from cache.build_mlb_ou_cache import (  # type: ignore
        final_score_from_game,
        is_final_game,
        line_to_threshold,
        official_date_from_game,
    )


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_GAMES_ROOT = PROJECT_DIR / "data" / "games" / "regular"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "scoring_path_effects"
DEFAULT_LINES = "7.5,8.5,9.5"
EPS = 1e-12


@dataclass(frozen=True)
class GamePath:
    game_pk: int
    official_date: str
    season: int
    game_type: str
    scheduled_innings: int
    away_final: int
    home_final: int
    inning_away_runs: Tuple[int, ...]
    inning_home_runs: Tuple[int, ...]
    path: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    if value is None:
        return None
    try:
        value = float(value)
        if not math.isfinite(value):
            return None
        return round(value, digits)
    except Exception:
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def _parse_lines(raw: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for part in str(raw or "").split(","):
        line = part.strip()
        if not line:
            continue
        out[line] = int(line_to_threshold(line))
    if not out:
        raise ValueError("--lines must include at least one O/U line")
    return out


def _game_date_from_path(path: Path) -> str:
    parts = path.parts
    try:
        idx = parts.index("regular")
        return f"{int(parts[idx + 1]):04d}-{int(parts[idx + 2]):02d}-{int(parts[idx + 3]):02d}"
    except Exception:
        return ""


def _season_from_path(path: Path) -> Optional[int]:
    parts = path.parts
    try:
        idx = parts.index("regular")
        return int(parts[idx + 1])
    except Exception:
        return None


def _extract_game_path(path: Path, allowed_game_types: set[str]) -> Optional[GamePath]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    game_pk = data.get("gamePk")
    if not isinstance(game_pk, int):
        return None

    game_data = data.get("gameData", {}) or {}
    game_type = str((game_data.get("game", {}) or {}).get("type") or "").upper()
    if allowed_game_types and game_type not in allowed_game_types:
        return None
    if not is_final_game(data):
        return None

    final_score = final_score_from_game(data)
    if final_score is None:
        return None
    away_final, home_final = final_score

    official_date = official_date_from_game(data) or _game_date_from_path(path)
    if len(official_date) < 10:
        return None

    linescore = (data.get("liveData", {}) or {}).get("linescore", {}) or {}
    innings = linescore.get("innings") or []
    if not isinstance(innings, list) or not innings:
        return None

    scheduled_innings = _safe_int(linescore.get("scheduledInnings"), 9)
    away_runs: List[int] = []
    home_runs: List[int] = []
    for inning in sorted(innings, key=lambda row: _safe_int((row or {}).get("num"), 999)):
        if not isinstance(inning, dict):
            continue
        num = _safe_int(inning.get("num"), 0)
        if num <= 0 or num > 9:
            continue
        away = inning.get("away", {}) or {}
        home = inning.get("home", {}) or {}
        away_runs.append(_safe_int(away.get("runs"), 0))
        home_runs.append(_safe_int(home.get("runs"), 0))

    if not away_runs or len(away_runs) != len(home_runs):
        return None

    return GamePath(
        game_pk=game_pk,
        official_date=official_date[:10],
        season=int(official_date[:4]),
        game_type=game_type,
        scheduled_innings=scheduled_innings,
        away_final=int(away_final),
        home_final=int(home_final),
        inning_away_runs=tuple(away_runs),
        inning_home_runs=tuple(home_runs),
        path=str(path),
    )


def load_games(
    games_root: Path,
    *,
    min_season: int,
    max_season: int,
    allowed_game_types: set[str],
    require_scheduled_innings: int,
) -> Tuple[List[GamePath], Dict[str, Any]]:
    files = sorted(games_root.rglob("*.json"))
    deduped: Dict[int, GamePath] = {}
    parsed = 0
    duplicate_files = 0
    skipped_short = 0

    for path in files:
        path_season = _season_from_path(path)
        if path_season is not None and (path_season < min_season or path_season > max_season):
            continue
        game = _extract_game_path(path, allowed_game_types)
        if game is None:
            continue
        parsed += 1
        if game.season < min_season or game.season > max_season:
            continue
        if require_scheduled_innings and game.scheduled_innings < require_scheduled_innings:
            skipped_short += 1
            continue
        if game.game_pk in deduped:
            duplicate_files += 1
            current = deduped[game.game_pk]
            if _game_date_from_path(Path(game.path)) == game.official_date:
                deduped[game.game_pk] = game
            elif _game_date_from_path(Path(current.path)) != current.official_date:
                deduped[game.game_pk] = min([current, game], key=lambda row: row.path)
            continue
        deduped[game.game_pk] = game

    games = sorted(deduped.values(), key=lambda row: (row.official_date, row.game_pk))
    meta = {
        "games_root": str(games_root),
        "files_seen": len(files),
        "parsed_final_games": parsed,
        "duplicate_game_files_skipped": duplicate_files,
        "short_scheduled_games_skipped": skipped_short,
        "deduped_games": len(games),
        "min_season": min_season,
        "max_season": max_season,
        "require_scheduled_innings": require_scheduled_innings,
    }
    return games, meta


def _entropy_norm(values: Sequence[int]) -> Optional[float]:
    total = sum(values)
    if total <= 0 or len(values) <= 1:
        return None
    parts = [v / total for v in values if v > 0]
    entropy = -sum(p * math.log(p) for p in parts)
    return entropy / math.log(len(values))


def _herfindahl(values: Sequence[int]) -> Optional[float]:
    total = sum(values)
    if total <= 0:
        return None
    return sum((v / total) ** 2 for v in values if v > 0)


def _trailing_count(values: Sequence[int], *, positive: bool) -> int:
    count = 0
    for value in reversed(values):
        if positive and value > 0:
            count += 1
        elif not positive and value == 0:
            count += 1
        else:
            break
    return count


def _weighted_mean_index(values: Sequence[int]) -> Optional[float]:
    total = sum(values)
    if total <= 0:
        return None
    return sum((idx + 1) * value for idx, value in enumerate(values)) / total


def _linear_slope(values: Sequence[int]) -> Optional[float]:
    if len(values) < 2:
        return None
    xs = np.arange(1, len(values) + 1, dtype=float)
    ys = np.array(values, dtype=float)
    x_mean = float(xs.mean())
    y_mean = float(ys.mean())
    denom = float(((xs - x_mean) ** 2).sum())
    if denom <= 0:
        return None
    return float(((xs - x_mean) * (ys - y_mean)).sum() / denom)


def _bucket_scoreless_streak(value: int) -> str:
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    if value == 2:
        return "2"
    return "3+"


def _bucket_rate(value: Optional[float]) -> str:
    if value is None:
        return "none"
    if value < 0.25:
        return "<25%"
    if value < 0.50:
        return "25-50%"
    if value < 0.75:
        return "50-75%"
    return "75%+"


def _bucket_share(value: Optional[float]) -> str:
    if value is None:
        return "none"
    if value < 0.40:
        return "<40%"
    if value < 0.60:
        return "40-60%"
    if value < 0.80:
        return "60-80%"
    return "80%+"


def build_observations(
    games: Sequence[GamePath],
    *,
    min_inning: int,
    max_inning: int,
    lines: Mapping[str, int],
) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for game in games:
        inning_totals = [a + h for a, h in zip(game.inning_away_runs, game.inning_home_runs)]
        half_totals: List[int] = []
        for a, h in zip(game.inning_away_runs, game.inning_home_runs):
            half_totals.extend([a, h])
        final_total = game.away_final + game.home_final

        for inning in range(min_inning, max_inning + 1):
            if inning <= 0 or inning > len(inning_totals):
                continue
            away_score = sum(game.inning_away_runs[:inning])
            home_score = sum(game.inning_home_runs[:inning])
            current_total = away_score + home_score
            remaining_runs = final_total - current_total
            inning_path = list(inning_totals[:inning])
            half_path = list(half_totals[: inning * 2])
            scoring_innings = sum(1 for value in inning_path if value > 0)
            scoring_halves = sum(1 for value in half_path if value > 0)
            max_inning_runs = max(inning_path) if inning_path else 0
            max_half_runs = max(half_path) if half_path else 0
            first_half_cut = max(1, math.ceil(inning / 2))
            recent2_runs = sum(inning_path[-2:])
            recent3_runs = sum(inning_path[-3:])
            last_score_inning = max((idx + 1 for idx, value in enumerate(inning_path) if value > 0), default=0)
            age_since_score = inning - last_score_inning if last_score_inning else inning
            weighted_run_inning = _weighted_mean_index(inning_path)
            weighted_half_index = _weighted_mean_index(half_path)

            row: Dict[str, Any] = {
                "game_pk": game.game_pk,
                "official_date": game.official_date,
                "season": game.season,
                "inning": inning,
                "away_score": away_score,
                "home_score": home_score,
                "score_diff": home_score - away_score,
                "abs_score_diff": abs(home_score - away_score),
                "current_total": current_total,
                "final_total": final_total,
                "remaining_runs": remaining_runs,
                "state_id": f"{inning}:{away_score}:{home_score}",
                "total_state_id": f"{inning}:{current_total}:{home_score - away_score}",
                "inning_run_path": "-".join(str(v) for v in inning_path),
                "half_run_path": "-".join(str(v) for v in half_path),
                "scoring_innings": scoring_innings,
                "scoring_inning_rate": scoring_innings / inning if inning else None,
                "scoreless_innings": inning - scoring_innings,
                "scoring_halves": scoring_halves,
                "scoring_half_rate": scoring_halves / (inning * 2) if inning else None,
                "max_inning_runs": max_inning_runs,
                "max_half_runs": max_half_runs,
                "burst_share": max_inning_runs / current_total if current_total > 0 else None,
                "half_burst_share": max_half_runs / current_total if current_total > 0 else None,
                "inning_herfindahl": _herfindahl(inning_path),
                "half_herfindahl": _herfindahl(half_path),
                "inning_entropy_norm": _entropy_norm(inning_path),
                "half_entropy_norm": _entropy_norm(half_path),
                "last_inning_runs": inning_path[-1] if inning_path else 0,
                "last2_inning_runs": recent2_runs,
                "last3_inning_runs": recent3_runs,
                "recent2_run_share": recent2_runs / current_total if current_total > 0 else None,
                "recent3_run_share": recent3_runs / current_total if current_total > 0 else None,
                "front_half_run_share": (
                    sum(inning_path[:first_half_cut]) / current_total if current_total > 0 else None
                ),
                "scoreless_streak": _trailing_count(inning_path, positive=False),
                "scoring_streak": _trailing_count(inning_path, positive=True),
                "age_since_score": age_since_score,
                "weighted_run_inning": weighted_run_inning,
                "weighted_run_inning_norm": weighted_run_inning / inning if weighted_run_inning is not None else None,
                "weighted_half_index_norm": (
                    weighted_half_index / (inning * 2) if weighted_half_index is not None and inning else None
                ),
                "inning_run_slope": _linear_slope(inning_path),
                "scoring_rate_bucket": _bucket_rate(scoring_innings / inning if inning else None),
                "burst_share_bucket": _bucket_share(max_inning_runs / current_total if current_total > 0 else None),
                "scoreless_streak_bucket": _bucket_scoreless_streak(_trailing_count(inning_path, positive=False)),
            }
            for line, threshold in lines.items():
                safe_line = line.replace(".", "p")
                row[f"over_{safe_line}"] = int(final_total >= threshold)
                row[f"needed_remaining_{safe_line}"] = max(0, threshold - current_total)
                row[f"line_{safe_line}_already_decided"] = int(current_total >= threshold)
            rows.append(row)

    return pd.DataFrame(rows)


def _summarize_numeric(values: Sequence[float]) -> Dict[str, Any]:
    arr = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not arr:
        return {"n": 0}
    series = pd.Series(arr)
    return {
        "n": int(series.count()),
        "mean": _round(float(series.mean())),
        "std": _round(float(series.std(ddof=1))) if len(series) > 1 else None,
        "p25": _round(float(series.quantile(0.25))),
        "p50": _round(float(series.quantile(0.50))),
        "p75": _round(float(series.quantile(0.75))),
    }


def exact_2_2_end4_tables(df: pd.DataFrame, lines: Mapping[str, int]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    exact = df[(df["inning"] == 4) & (df["away_score"] == 2) & (df["home_score"] == 2)].copy()
    if exact.empty:
        return pd.DataFrame(), pd.DataFrame()
    agg_dict: Dict[str, Tuple[str, str]] = {
        "n": ("remaining_runs", "count"),
        "mean_remaining_runs": ("remaining_runs", "mean"),
        "median_remaining_runs": ("remaining_runs", "median"),
    }
    for line in lines:
        key = f"over_{line.replace('.', 'p')}"
        agg_dict[f"over_{line}_rate"] = (key, "mean")
    by_scoring = (
        exact.groupby(["scoring_innings", "burst_share_bucket", "scoreless_streak_bucket"], dropna=False)
        .agg(**agg_dict)
        .reset_index()
        .sort_values(["scoring_innings", "burst_share_bucket"])
    )
    by_path = (
        exact.groupby(["inning_run_path"], dropna=False)
        .agg(**agg_dict)
        .reset_index()
        .sort_values(["n", "mean_remaining_runs"], ascending=[False, False])
    )
    return by_scoring, by_path


def residual_path_buckets(df: pd.DataFrame) -> pd.DataFrame:
    work = df[(df["current_total"] > 0) & (df["inning"] >= 2)].copy()
    if work.empty:
        return pd.DataFrame()
    state_counts = work.groupby("state_id")["remaining_runs"].transform("count")
    work = work[state_counts >= 30].copy()
    if work.empty:
        return pd.DataFrame()
    state_mean = work.groupby("state_id")["remaining_runs"].transform("mean")
    work["remaining_run_residual_vs_exact_score_state"] = work["remaining_runs"] - state_mean

    bucket_specs = [
        ("scoring_rate_bucket", "scoring_inning_rate"),
        ("burst_share_bucket", "burst_share"),
        ("scoreless_streak_bucket", "scoreless_streak"),
    ]
    rows: List[Dict[str, Any]] = []
    for bucket_col, value_col in bucket_specs:
        for bucket, group in work.groupby(bucket_col, dropna=False):
            rows.append(
                {
                    "bucket_type": bucket_col,
                    "bucket": str(bucket),
                    "n": int(len(group)),
                    "mean_value": _round(float(group[value_col].mean())) if value_col in group else None,
                    "mean_remaining_runs": _round(float(group["remaining_runs"].mean())),
                    "mean_residual_remaining_runs": _round(
                        float(group["remaining_run_residual_vs_exact_score_state"].mean())
                    ),
                    "median_residual_remaining_runs": _round(
                        float(group["remaining_run_residual_vs_exact_score_state"].median())
                    ),
                    "states_covered": int(group["state_id"].nunique()),
                }
            )
    return pd.DataFrame(rows).sort_values(["bucket_type", "bucket"])


PATH_FEATURES = [
    "scoring_inning_rate",
    "burst_share",
    "recent2_run_share",
    "scoreless_streak",
    "weighted_run_inning_norm",
    "inning_run_slope",
]


def _two_way_residualize(values: pd.Series, frame: pd.DataFrame, groups: Sequence[str], *, iterations: int = 5) -> pd.Series:
    """Fast fixed-effect residualization for research diagnostics.

    This is a Frisch-Waugh style approximation for additive fixed effects. It
    avoids slow formula regressions over ~170k historical inning-state rows and
    is more exact to the question we care about than broad inning/score controls:
    does the path explain variation left after same-score-state and season means?
    """

    residual = pd.to_numeric(values, errors="coerce").astype(float)
    residual = residual - float(residual.mean())
    for _ in range(max(1, iterations)):
        for group_col in groups:
            residual = residual - residual.groupby(frame[group_col], observed=True).transform("mean")
        residual = residual - float(residual.mean())
    return residual


def _normal_two_sided_pvalue(t_stat: Optional[float]) -> Optional[float]:
    if t_stat is None or not math.isfinite(float(t_stat)):
        return None
    return math.erfc(abs(float(t_stat)) / math.sqrt(2.0))


def _fixed_effect_single_feature(
    frame: pd.DataFrame,
    *,
    target_col: str,
    feature_col: str,
    groups: Sequence[str],
) -> Dict[str, Any]:
    cols = [target_col, feature_col, "game_pk", *groups]
    work = frame.loc[:, cols].dropna().copy()
    if len(work) < 20:
        raise ValueError("not enough rows")
    y = _two_way_residualize(work[target_col], work, groups)
    x = _two_way_residualize(work[feature_col], work, groups)
    x_values = x.to_numpy(dtype=float)
    y_values = y.to_numpy(dtype=float)
    xtx = float(np.dot(x_values, x_values))
    if xtx <= EPS:
        raise ValueError("feature has no residual variation after fixed effects")
    beta = float(np.dot(x_values, y_values) / xtx)
    err = y_values - beta * x_values

    # Cluster-robust one-regressor sandwich variance by game.
    cluster_scores = pd.Series(x_values * err).groupby(work["game_pk"].to_numpy()).sum().to_numpy(dtype=float)
    meat = float(np.dot(cluster_scores, cluster_scores))
    n = len(work)
    g = len(cluster_scores)
    correction = (g / (g - 1)) * ((n - 1) / max(1, n - 1)) if g > 1 else 1.0
    variance = correction * meat / (xtx * xtx)
    se = math.sqrt(max(0.0, variance)) if math.isfinite(variance) else None
    t_stat = beta / se if se and se > 0 else None
    y_hat = beta * x_values
    sst = float(np.dot(y_values, y_values))
    sse = float(np.dot(y_values - y_hat, y_values - y_hat))
    return {
        "n": int(n),
        "clusters": int(g),
        "coef": _round(beta),
        "std_err_clustered": _round(se),
        "t_stat_clustered": _round(t_stat),
        "p_value_clustered_normal": _round(_normal_two_sided_pvalue(t_stat)),
        "partial_r2": _round(1.0 - sse / sst if sst > EPS else None),
        "fixed_effects": "+".join(groups),
    }


def run_regressions(df: pd.DataFrame, over_line: str) -> pd.DataFrame:
    work = df[(df["current_total"] > 0) & (df["inning"] >= 2)].copy()
    for feature in PATH_FEATURES:
        work[feature] = pd.to_numeric(work[feature], errors="coerce").fillna(0.0)

    rows: List[Dict[str, Any]] = []
    for feature in PATH_FEATURES:
        try:
            result = _fixed_effect_single_feature(
                work,
                target_col="remaining_runs",
                feature_col=feature,
                groups=("state_id", "season"),
            )
            rows.append(
                {
                    "model": "fe_ols_remaining_runs",
                    "target": "remaining_runs",
                    "feature": feature,
                    **result,
                }
            )
        except Exception as exc:
            rows.append(
                {"model": "fe_ols_remaining_runs", "target": "remaining_runs", "feature": feature, "error": str(exc)}
            )

    safe_line = over_line.replace(".", "p")
    target = f"over_{safe_line}"
    decided = f"line_{safe_line}_already_decided"
    if target in work.columns and decided in work.columns:
        logit_work = work[work[decided] == 0].copy()
        if logit_work[target].nunique() == 2 and len(logit_work) >= 100:
            for feature in PATH_FEATURES:
                try:
                    result = _fixed_effect_single_feature(
                        logit_work,
                        target_col=target,
                        feature_col=feature,
                        groups=("state_id", "season"),
                    )
                    rows.append(
                        {
                            "model": "fe_linear_probability_over",
                            "target": target,
                            "feature": feature,
                            **result,
                        }
                    )
                except Exception as exc:
                    rows.append(
                        {"model": "fe_linear_probability_over", "target": target, "feature": feature, "error": str(exc)}
                    )
    return pd.DataFrame(rows)


def _regression_frame(
    df: pd.DataFrame,
    *,
    feature_names: Sequence[str],
    target: str,
    train_end_season: int,
    test_start_season: int,
) -> Tuple[pd.DataFrame, pd.Series, pd.DataFrame, pd.Series]:
    cols = list(feature_names)
    work = df.dropna(subset=[target]).copy()
    for col in cols:
        work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0.0)
    train = work[work["season"] <= train_end_season].copy()
    test = work[work["season"] >= test_start_season].copy()
    return train[cols], train[target], test[cols], test[target]


def run_predictive_lift(
    df: pd.DataFrame,
    *,
    over_line: str,
    train_end_season: int,
    test_start_season: int,
) -> pd.DataFrame:
    if HistGradientBoostingRegressor is None or HistGradientBoostingClassifier is None:
        return pd.DataFrame()

    base_features = ["inning", "away_score", "home_score", "current_total", "score_diff", "abs_score_diff"]
    path_features = base_features + PATH_FEATURES + [
        "scoring_half_rate",
        "half_burst_share",
        "half_entropy_norm",
        "recent3_run_share",
        "age_since_score",
        "weighted_half_index_norm",
    ]
    rows: List[Dict[str, Any]] = []

    for label, features in [("current_state_only", base_features), ("current_state_plus_path", path_features)]:
        x_train, y_train, x_test, y_test = _regression_frame(
            df,
            feature_names=features,
            target="remaining_runs",
            train_end_season=train_end_season,
            test_start_season=test_start_season,
        )
        if len(x_train) >= 100 and len(x_test) >= 100:
            model = HistGradientBoostingRegressor(
                max_iter=250,
                learning_rate=0.05,
                l2_regularization=0.05,
                random_state=42,
            )
            model.fit(x_train, y_train)
            pred = np.maximum(0.0, model.predict(x_test))
            rows.append(
                {
                    "task": "remaining_runs_regression",
                    "model": label,
                    "train_rows": int(len(x_train)),
                    "test_rows": int(len(x_test)),
                    "train_end_season": train_end_season,
                    "test_start_season": test_start_season,
                    "rmse": _round(math.sqrt(float(mean_squared_error(y_test, pred)))),
                    "mae": _round(float(mean_absolute_error(y_test, pred))),
                    "mean_pred": _round(float(np.mean(pred))),
                    "mean_actual": _round(float(np.mean(y_test))),
                }
            )

    safe_line = over_line.replace(".", "p")
    target = f"over_{safe_line}"
    decided = f"line_{safe_line}_already_decided"
    class_work = df[df[decided] == 0].copy() if decided in df.columns else df.copy()
    for label, features in [("current_state_only", base_features), ("current_state_plus_path", path_features)]:
        x_train, y_train, x_test, y_test = _regression_frame(
            class_work,
            feature_names=features,
            target=target,
            train_end_season=train_end_season,
            test_start_season=test_start_season,
        )
        if len(x_train) >= 100 and len(x_test) >= 100 and y_train.nunique() == 2 and y_test.nunique() == 2:
            model = HistGradientBoostingClassifier(
                max_iter=250,
                learning_rate=0.05,
                l2_regularization=0.05,
                random_state=42,
            )
            model.fit(x_train, y_train.astype(int))
            pred = np.clip(model.predict_proba(x_test)[:, 1], 1e-6, 1 - 1e-6)
            rows.append(
                {
                    "task": f"over_{over_line}_classification",
                    "model": label,
                    "train_rows": int(len(x_train)),
                    "test_rows": int(len(x_test)),
                    "train_end_season": train_end_season,
                    "test_start_season": test_start_season,
                    "brier": _round(float(brier_score_loss(y_test.astype(int), pred))),
                    "logloss": _round(float(log_loss(y_test.astype(int), pred))),
                    "auc": _round(float(roc_auc_score(y_test.astype(int), pred))),
                    "mean_pred": _round(float(np.mean(pred))),
                    "mean_actual": _round(float(np.mean(y_test))),
                }
            )
    return pd.DataFrame(rows)


def _lift_summary(lift_df: pd.DataFrame) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if lift_df.empty:
        return rows
    for task, group in lift_df.groupby("task"):
        base = group[group["model"] == "current_state_only"]
        path = group[group["model"] == "current_state_plus_path"]
        if base.empty or path.empty:
            continue
        b = base.iloc[0].to_dict()
        p = path.iloc[0].to_dict()
        row: Dict[str, Any] = {"task": task}
        for metric in ["rmse", "mae", "brier", "logloss"]:
            if metric in b and metric in p and pd.notna(b.get(metric)) and pd.notna(p.get(metric)):
                row[f"{metric}_base"] = _round(float(b[metric]))
                row[f"{metric}_path"] = _round(float(p[metric]))
                row[f"{metric}_improvement"] = _round(float(b[metric]) - float(p[metric]))
                row[f"{metric}_pct_improvement"] = _round(
                    (float(b[metric]) - float(p[metric])) / float(b[metric]) if float(b[metric]) else None
                )
        for metric in ["auc"]:
            if metric in b and metric in p and pd.notna(b.get(metric)) and pd.notna(p.get(metric)):
                row[f"{metric}_base"] = _round(float(b[metric]))
                row[f"{metric}_path"] = _round(float(p[metric]))
                row[f"{metric}_improvement"] = _round(float(p[metric]) - float(b[metric]))
        rows.append(row)
    return rows


def _write_csv(path: Path, rows: Iterable[Mapping[str, Any]] | pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(rows, pd.DataFrame):
        rows.to_csv(path, index=False)
        return
    rows_list = list(rows)
    if not rows_list:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: List[str] = []
    for row in rows_list:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_list)


def _md_table(df: pd.DataFrame, columns: Sequence[str], *, max_rows: int = 20) -> List[str]:
    if df.empty:
        return ["No rows."]
    view = df.loc[:, [col for col in columns if col in df.columns]].head(max_rows).copy()
    for col in view.columns:
        if pd.api.types.is_float_dtype(view[col]):
            view[col] = view[col].map(lambda v: "" if pd.isna(v) else f"{float(v):.4f}")
    lines = ["| " + " | ".join(view.columns) + " |"]
    lines.append("| " + " | ".join(["---"] * len(view.columns)) + " |")
    for _, row in view.iterrows():
        lines.append("| " + " | ".join(str(row[col]) for col in view.columns) + " |")
    return lines


def build_report(
    *,
    summary: Mapping[str, Any],
    exact_by_scoring: pd.DataFrame,
    exact_by_path: pd.DataFrame,
    residuals: pd.DataFrame,
    regressions: pd.DataFrame,
    lift: pd.DataFrame,
) -> str:
    lines: List[str] = []
    lines.append("# Scoring Path Effects Report")
    lines.append("")
    lines.append(f"Generated: {summary.get('generated_at_utc')}")
    lines.append("")
    lines.append("## Scope")
    lines.append("")
    lines.append(
        f"Loaded {summary.get('games', {}).get('deduped_games')} deduped full-length regular-season games "
        f"from {summary.get('args', {}).get('min_season')} to {summary.get('args', {}).get('max_season')}, "
        f"creating {summary.get('observations', {}).get('rows')} end-of-full-inning observations."
    )
    lines.append("")
    lines.append("## Current FV Coverage")
    lines.append("")
    lines.append(
        "Current Stage-1 fair value keys current score, inning/half/outs/bases, and line, "
        "then layers run environment/team/weather adjustments. It does not key whether prior "
        "runs arrived in one early burst or across multiple innings."
    )
    lines.append("")
    lines.append("## Exact Example: 2-2 After 4 Innings")
    lines.append("")
    lines.extend(
        _md_table(
            exact_by_scoring,
            [
                "scoring_innings",
                "burst_share_bucket",
                "scoreless_streak_bucket",
                "n",
                "mean_remaining_runs",
                "over_7.5_rate",
                "over_8.5_rate",
                "over_9.5_rate",
            ],
            max_rows=30,
        )
    )
    lines.append("")
    lines.append("Most common exact inning-run paths for 2-2 after 4:")
    lines.append("")
    lines.extend(
        _md_table(
            exact_by_path,
            ["inning_run_path", "n", "mean_remaining_runs", "over_7.5_rate", "over_8.5_rate", "over_9.5_rate"],
            max_rows=12,
        )
    )
    lines.append("")
    lines.append("## Same-State Residual Buckets")
    lines.append("")
    lines.append(
        "Residuals subtract the mean future runs for the exact same end-inning score state "
        "`inning:away_score:home_score` using only states with at least 30 examples."
    )
    lines.append("")
    lines.extend(
        _md_table(
            residuals,
            [
                "bucket_type",
                "bucket",
                "n",
                "mean_value",
                "mean_remaining_runs",
                "mean_residual_remaining_runs",
                "states_covered",
            ],
            max_rows=40,
        )
    )
    lines.append("")
    lines.append("## Controlled Regression")
    lines.append("")
    lines.append(
        "Each path feature is tested one-at-a-time after residualizing both feature and target "
        "against exact same-score state (`inning:away_score:home_score`) and season. "
        "Standard errors are clustered by game."
    )
    lines.append("")
    lines.extend(
        _md_table(
            regressions,
            [
                "model",
                "target",
                "feature",
                "n",
                "coef",
                "std_err_clustered",
                "p_value_clustered_normal",
                "partial_r2",
                "fixed_effects",
            ],
            max_rows=40,
        )
    )
    lines.append("")
    lines.append("## Chronological Predictive Lift")
    lines.append("")
    lines.append("Train through the configured train-end season and test on later seasons.")
    lines.append("")
    lines.extend(
        _md_table(
            lift,
            [
                "task",
                "model",
                "train_rows",
                "test_rows",
                "rmse",
                "mae",
                "brier",
                "logloss",
                "auc",
                "mean_pred",
                "mean_actual",
            ],
            max_rows=20,
        )
    )
    lines.append("")
    lines.append("## Read")
    lines.append("")
    for item in summary.get("read", []):
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def analyze(args: argparse.Namespace) -> Dict[str, Any]:
    lines = _parse_lines(args.lines)
    allowed_game_types = {part.strip().upper() for part in str(args.game_types).split(",") if part.strip()}
    games, game_meta = load_games(
        args.games_root,
        min_season=args.min_season,
        max_season=args.max_season,
        allowed_game_types=allowed_game_types,
        require_scheduled_innings=args.require_scheduled_innings,
    )
    df = build_observations(games, min_inning=args.min_inning, max_inning=args.max_inning, lines=lines)
    if df.empty:
        raise RuntimeError("No observations built from historical game data.")

    exact_by_scoring, exact_by_path = exact_2_2_end4_tables(df, lines)
    residuals = residual_path_buckets(df)
    regressions = run_regressions(df, args.primary_over_line)
    lift = run_predictive_lift(
        df,
        over_line=args.primary_over_line,
        train_end_season=args.train_end_season,
        test_start_season=args.test_start_season,
    )

    lift_summary = _lift_summary(lift)
    path_rows = lift[lift["model"] == "current_state_plus_path"] if not lift.empty else pd.DataFrame()
    base_rows = lift[lift["model"] == "current_state_only"] if not lift.empty else pd.DataFrame()

    exact_summary: Dict[str, Any] = {"rows": int(len(df[(df["inning"] == 4) & (df["away_score"] == 2) & (df["home_score"] == 2)]))}
    if not exact_by_scoring.empty:
        exact_summary["by_scoring_innings"] = exact_by_scoring.to_dict(orient="records")

    read: List[str] = []
    if not exact_by_scoring.empty:
        ex = exact_by_scoring.copy()
        ex["_rem_x_n"] = ex["mean_remaining_runs"] * ex["n"]
        by_scoring = (
            ex.groupby("scoring_innings", dropna=False)
            .agg(n=("n", "sum"), rem=("_rem_x_n", "sum"))
            .reset_index()
            .sort_values("scoring_innings")
        )
        by_scoring["mean_remaining_runs"] = by_scoring["rem"] / by_scoring["n"]
        low = by_scoring.iloc[0]
        high = by_scoring.iloc[-1]
        read.append(
            "In the exact 2-2 after 4th-inning state, the aggregate bucket with "
            f"{int(high['scoring_innings'])} scoring innings averaged "
            f"{_round(float(high['mean_remaining_runs']))} remaining runs (n={int(high['n'])}) versus "
            f"{_round(float(low['mean_remaining_runs']))} (n={int(low['n'])}) for the one-scoring-inning bucket."
        )
    if not residuals.empty:
        rate_rows = residuals[residuals["bucket_type"] == "scoring_rate_bucket"]
        if not rate_rows.empty:
            best = rate_rows.sort_values("mean_residual_remaining_runs", ascending=False).iloc[0]
            worst = rate_rows.sort_values("mean_residual_remaining_runs", ascending=True).iloc[0]
            read.append(
                "After subtracting exact same-score-state means, scoring-rate buckets still move "
                f"future runs from {worst['mean_residual_remaining_runs']} to "
                f"{best['mean_residual_remaining_runs']} runs."
            )
    if lift_summary:
        for row in lift_summary:
            if row.get("task") == "remaining_runs_regression" and row.get("rmse_improvement") is not None:
                read.append(
                    "Chronological prediction shows scoring-path features change remaining-runs RMSE by "
                    f"{row.get('rmse_improvement')} ({row.get('rmse_pct_improvement')} of baseline)."
                )
            if row.get("task") == f"over_{args.primary_over_line}_classification" and row.get("brier_improvement") is not None:
                read.append(
                    f"For Over {args.primary_over_line}, scoring-path features change out-of-sample Brier by "
                    f"{row.get('brier_improvement')} ({row.get('brier_pct_improvement')} of baseline)."
                )
    if not read:
        read.append("No strong scoring-path result was detected with the current configuration.")

    summary: Dict[str, Any] = {
        "generated_at_utc": _now_iso(),
        "args": {
            "games_root": str(args.games_root),
            "min_season": args.min_season,
            "max_season": args.max_season,
            "min_inning": args.min_inning,
            "max_inning": args.max_inning,
            "lines": list(lines),
            "primary_over_line": args.primary_over_line,
            "train_end_season": args.train_end_season,
            "test_start_season": args.test_start_season,
            "require_scheduled_innings": args.require_scheduled_innings,
        },
        "games": game_meta,
        "observations": {
            "rows": int(len(df)),
            "min_date": str(df["official_date"].min()),
            "max_date": str(df["official_date"].max()),
            "seasons": [int(x) for x in sorted(df["season"].unique())],
            "current_total_positive_rows": int((df["current_total"] > 0).sum()),
        },
        "exact_2_2_end4": exact_summary,
        "remaining_runs": _summarize_numeric(df["remaining_runs"].tolist()),
        "predictive_lift_summary": lift_summary,
        "read": read,
    }

    output_root = args.output_root
    output_root.mkdir(parents=True, exist_ok=True)
    _write_csv(output_root / "exact_2_2_end4_by_scoring.csv", exact_by_scoring)
    _write_csv(output_root / "exact_2_2_end4_by_path.csv", exact_by_path)
    _write_csv(output_root / "residual_path_buckets.csv", residuals)
    _write_csv(output_root / "regression_coefficients.csv", regressions)
    _write_csv(output_root / "predictive_lift.csv", lift)
    (output_root / "scoring_path_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    report = build_report(
        summary=summary,
        exact_by_scoring=exact_by_scoring,
        exact_by_path=exact_by_path,
        residuals=residuals,
        regressions=regressions,
        lift=lift,
    )
    (output_root / "scoring_path_report.md").write_text(report, encoding="utf-8")
    return summary


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Analyze historical MLB scoring-path effects.")
    p.add_argument("--games-root", type=Path, default=DEFAULT_GAMES_ROOT)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--min-season", type=int, default=2016)
    p.add_argument("--max-season", type=int, default=2025)
    p.add_argument("--game-types", type=str, default="R")
    p.add_argument("--require-scheduled-innings", type=int, default=9)
    p.add_argument("--min-inning", type=int, default=1)
    p.add_argument("--max-inning", type=int, default=8)
    p.add_argument("--lines", type=str, default=DEFAULT_LINES)
    p.add_argument("--primary-over-line", type=str, default="8.5")
    p.add_argument("--train-end-season", type=int, default=2022)
    p.add_argument("--test-start-season", type=int, default=2023)
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.primary_over_line not in _parse_lines(args.lines):
        raise SystemExit("--primary-over-line must be included in --lines")
    summary = analyze(args)
    print(json.dumps({
        "status": "ok",
        "output_root": str(args.output_root),
        "observations": summary.get("observations", {}).get("rows"),
        "games": summary.get("games", {}).get("deduped_games"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
