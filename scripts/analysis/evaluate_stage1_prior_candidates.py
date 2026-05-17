#!/usr/bin/env python3
"""
Screen Stage-1 historical-prior candidates for MLB totals FV.

This script is intentionally a research screen, not a cache promoter. It
generates many candidate season-weight schemes, approximates their effect by
scaling a base cache-swapped FV to the candidate scoring environment, and
reports row-level plus deduped calibration diagnostics.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from scipy.stats import poisson

try:
    from cache.build_mlb_ou_cache import line_to_threshold
    from scripts.analysis import fair_value_stage_ablation_report as fvsa
except ImportError:  # pragma: no cover - CLI fallback
    import sys

    PROJECT_DIR_FALLBACK = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(PROJECT_DIR_FALLBACK))
    from cache.build_mlb_ou_cache import line_to_threshold  # type: ignore
    from scripts.analysis import fair_value_stage_ablation_report as fvsa  # type: ignore


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SCORING_ROOT = PROJECT_DIR / "data" / "analysis_output" / "scoring_trends"
DEFAULT_INPUT_PATH = (
    PROJECT_DIR
    / "data"
    / "analysis_output"
    / "calibration_opportunity_training"
    / "calibration_opportunity_training_table.jsonl"
)
DEFAULT_BASE_CACHE_PATH = PROJECT_DIR / "cache" / "mlb_ou_cache_10y_candidate.json"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "stage1_prior_candidates"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


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


def _read_csv(path: Path) -> List[Dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as f:
        return [dict(row) for row in csv.DictReader(f)]


def load_season_stats(scoring_root: Path, *, history_start: int, history_end: int) -> List[Dict[str, Any]]:
    rows = _read_csv(scoring_root / "season_scoring_trends.csv")
    out: List[Dict[str, Any]] = []
    for row in rows:
        season = int(row["season"])
        if history_start <= season <= history_end:
            out.append(
                {
                    "season": season,
                    "games": int(float(row["games"])),
                    "runs_per_game": float(row["runs_per_game"]),
                }
            )
    if not out:
        raise RuntimeError(f"No season stats loaded from {scoring_root}")
    return sorted(out, key=lambda r: int(r["season"]))


def load_projection_target(scoring_root: Path) -> Optional[float]:
    path = scoring_root / "scoring_trends_summary.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    projection = payload.get("projection", {}) or {}
    return _safe_float(projection.get("blended_projection_rpg"))


def _normalize(weights: Mapping[int, float]) -> Dict[int, float]:
    total = sum(max(0.0, float(v)) for v in weights.values())
    if total <= 0:
        raise ValueError("Cannot normalize zero-weight candidate")
    return {int(k): max(0.0, float(v)) / total for k, v in sorted(weights.items())}


def _game_weighted_allocation(season_stats: Sequence[Mapping[str, Any]], seasons: Sequence[int]) -> Dict[int, float]:
    season_set = set(seasons)
    raw = {
        int(row["season"]): float(row["games"])
        for row in season_stats
        if int(row["season"]) in season_set
    }
    return _normalize(raw)


def _equal_allocation(seasons: Sequence[int]) -> Dict[int, float]:
    return _normalize({int(season): 1.0 for season in seasons})


def _exp_allocation(seasons: Sequence[int], *, max_season: int, half_life: float) -> Dict[int, float]:
    return _normalize({int(season): 0.5 ** ((max_season - int(season)) / half_life) for season in seasons})


def _scoring_env_allocation(
    season_stats: Sequence[Mapping[str, Any]],
    *,
    max_season: int,
    target_rpg: float,
    half_life: float,
    similarity_scale: float,
) -> Dict[int, float]:
    raw: Dict[int, float] = {}
    for row in season_stats:
        season = int(row["season"])
        rpg = float(row["runs_per_game"])
        recency = 0.5 ** ((max_season - season) / half_life)
        similarity = math.exp(-abs(rpg - target_rpg) / max(similarity_scale, 1e-9))
        raw[season] = recency * similarity
    return _normalize(raw)


def _blend(a: Mapping[int, float], b: Mapping[int, float], *, alpha: float) -> Dict[int, float]:
    seasons = sorted(set(a) | set(b))
    return _normalize({season: alpha * a.get(season, 0.0) + (1.0 - alpha) * b.get(season, 0.0) for season in seasons})


def generate_candidates(
    season_stats: Sequence[Mapping[str, Any]],
    *,
    target_rpg: Optional[float],
) -> List[Dict[str, Any]]:
    seasons = [int(row["season"]) for row in season_stats]
    max_season = max(seasons)
    min_season = min(seasons)
    if target_rpg is None:
        target_rpg = float(season_stats[-1]["runs_per_game"])
    candidates: List[Dict[str, Any]] = []

    def add(name: str, family: str, weights: Mapping[int, float], **params: Any) -> Dict[int, float]:
        norm = _normalize(weights)
        candidates.append({"candidate": name, "family": family, "weights": norm, "params": params})
        return norm

    max_years = min(10, len(seasons))
    uniform10 = add(f"rolling{max_years}_game_weighted", "rolling_game_weighted", _game_weighted_allocation(season_stats, seasons), years=max_years)
    for years in range(3, max_years + 1):
        selected = [season for season in range(max_season - years + 1, max_season + 1) if season in seasons]
        add(f"rolling{years}_game_weighted", "rolling_game_weighted", _game_weighted_allocation(season_stats, selected), years=years)
        add(f"rolling{years}_equal_season", "rolling_equal_season", _equal_allocation(selected), years=years)

    for half_life in (1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0):
        add(f"exp_hl_{str(half_life).replace('.', 'p')}", "exp_recency", _exp_allocation(seasons, max_season=max_season, half_life=half_life), half_life=half_life)

    scoring_env_core = None
    for half_life in (1.5, 2.0, 3.0, 4.0, 6.0):
        for scale in (0.20, 0.35, 0.50, 0.75):
            weights = _scoring_env_allocation(
                season_stats,
                max_season=max_season,
                target_rpg=target_rpg,
                half_life=half_life,
                similarity_scale=scale,
            )
            name = f"scoring_env_hl_{str(half_life).replace('.', 'p')}_scale_{str(scale).replace('.', 'p')}"
            add(name, "scoring_env", weights, half_life=half_life, similarity_scale=scale, target_rpg=target_rpg)
            if half_life == 3.0 and abs(scale - 0.35) < 1e-9:
                scoring_env_core = weights

    recent3 = _game_weighted_allocation(season_stats, range(max_season - 2, max_season + 1))
    recent4 = _game_weighted_allocation(season_stats, range(max_season - 3, max_season + 1))
    recent5 = _game_weighted_allocation(season_stats, range(max_season - 4, max_season + 1))
    if scoring_env_core is not None:
        for alpha in (0.25, 0.50, 0.75):
            add(f"blend_recent3_scoring_env_a{int(alpha*100)}", "blend", _blend(recent3, scoring_env_core, alpha=alpha), alpha_recent=alpha)
            add(f"blend_recent4_scoring_env_a{int(alpha*100)}", "blend", _blend(recent4, scoring_env_core, alpha=alpha), alpha_recent=alpha)
            add(f"blend_recent5_scoring_env_a{int(alpha*100)}", "blend", _blend(recent5, scoring_env_core, alpha=alpha), alpha_recent=alpha)
    for alpha in (0.25, 0.50, 0.75):
        add(f"blend_recent3_uniform10_a{int(alpha*100)}", "blend", _blend(recent3, uniform10, alpha=alpha), alpha_recent=alpha)
        add(f"blend_recent5_uniform10_a{int(alpha*100)}", "blend", _blend(recent5, uniform10, alpha=alpha), alpha_recent=alpha)

    # Ensure all historical seasons are represented in exported weights.
    for candidate in candidates:
        weights = candidate["weights"]
        candidate["weights"] = {season: float(weights.get(season, 0.0)) for season in range(min_season, max_season + 1)}
    return candidates


def candidate_target_rpg(candidate: Mapping[str, Any], season_stats: Sequence[Mapping[str, Any]]) -> float:
    rpg_by_season = {int(row["season"]): float(row["runs_per_game"]) for row in season_stats}
    weights = candidate["weights"]
    return sum(float(weights.get(season, 0.0)) * rpg_by_season[season] for season in rpg_by_season)


def _load_rows(path: Path, *, mode: str, min_date: str, max_date: str) -> List[Dict[str, Any]]:
    rows = fvsa.load_rows(path)
    return [
        row for row in fvsa.filter_rows(rows, mode=mode, min_date=min_date, max_date=max_date)
        if fvsa._label(row) is not None
    ]


def _total_after_candidate_state(row: Mapping[str, Any]) -> Optional[int]:
    away = fvsa._safe_int(row.get("away_score_before"))
    home = fvsa._safe_int(row.get("home_score_before"))
    if away is None:
        away = fvsa._safe_int(row.get("current_state_value_away_score"))
    if home is None:
        home = fvsa._safe_int(row.get("current_state_value_home_score"))
    if away is None or home is None:
        return None
    total = away + home
    if fvsa._family(row) == fvsa.SCORE_EVENT_TRANSITION:
        total += max(0, fvsa._safe_int(row.get("inferred_runs")) or 0)
    return total


def _inverse_poisson_lambda(prob: float, needed: int) -> float:
    prob = min(max(float(prob), 1e-6), 1.0 - 1e-6)
    if needed <= 0:
        return 0.0
    lo = 0.0
    hi = 45.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        q = 1.0 - float(poisson.cdf(needed - 1, mid))
        if q < prob:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _scale_probability(row: Mapping[str, Any], base_prob: Optional[float], scale: float) -> Optional[float]:
    if base_prob is None:
        return None
    total = _total_after_candidate_state(row)
    if total is None:
        return None
    try:
        needed = line_to_threshold(str(row.get("line"))) - total
    except Exception:
        return None
    if needed <= 0:
        return 1.0 - 1e-6
    lam = _inverse_poisson_lambda(float(base_prob), needed)
    out = 1.0 - float(poisson.cdf(needed - 1, lam * scale))
    return min(max(out, 1e-6), 1.0 - 1e-6)


def _auc(labels: Sequence[int], probs: Sequence[float]) -> Optional[float]:
    positives = [p for y, p in zip(labels, probs) if y == 1]
    negatives = [p for y, p in zip(labels, probs) if y == 0]
    if not positives or not negatives:
        return None
    wins = 0.0
    total = 0
    for pos in positives:
        for neg in negatives:
            total += 1
            if pos > neg:
                wins += 1.0
            elif pos == neg:
                wins += 0.5
    return wins / total if total else None


def _summarize_predictions(items: Sequence[Mapping[str, Any]], pred_by_id: Mapping[int, Optional[float]]) -> Dict[str, Any]:
    labels: List[int] = []
    probs: List[float] = []
    asks: List[float] = []
    for row in items:
        label = fvsa._label(row)
        pred = pred_by_id.get(id(row))
        ask = fvsa.stage_predictions(row).get("market_ask_baseline")
        if label is None or pred is None or ask is None:
            continue
        labels.append(label)
        probs.append(pred)
        asks.append(ask)
    selected = [(y, a) for y, p, a in zip(labels, probs, asks) if p > a]
    profit = sum((1.0 / ask - 1.0) if y else -1.0 for y, ask in selected)
    brier = sum((p - y) ** 2 for y, p in zip(labels, probs)) / len(labels) if labels else None
    logloss = (
        sum(
            -(
                y * math.log(min(max(p, 1e-6), 1.0 - 1e-6))
                + (1 - y) * math.log(min(max(1.0 - p, 1e-6), 1.0 - 1e-6))
            )
            for y, p in zip(labels, probs)
        )
        / len(labels)
        if labels
        else None
    )
    return {
        "n": len(labels),
        "empirical_rate": _round(sum(labels) / len(labels) if labels else None),
        "avg_prob": _round(sum(probs) / len(probs) if probs else None),
        "brier": _round(brier),
        "logloss": _round(logloss),
        "auc": _round(_auc(labels, probs)),
        "selected_rows": len(selected),
        "selected_win_rate": _round(sum(y for y, _ask in selected) / len(selected) if selected else None),
        "selected_profit_units": _round(profit),
        "selected_roi_per_cost_unit": _round(profit / len(selected) if selected else None),
    }


def _dedupe(rows: Sequence[Dict[str, Any]], key_name: str) -> List[Dict[str, Any]]:
    def game_id(row: Mapping[str, Any]) -> str:
        return str(row.get("game_pk") or row.get("event_slug") or row.get("matchup") or row.get("game_id") or "")

    def key(row: Mapping[str, Any]) -> Tuple[Any, ...]:
        family = fvsa._family(row)
        line = str(row.get("line"))
        if key_name == "game_line_family":
            return family, game_id(row), line
        if key_name == "game_line_score_family":
            return (
                family,
                game_id(row),
                line,
                row.get("away_score_before"),
                row.get("home_score_before"),
                row.get("inferred_runs"),
            )
        return (id(row),)

    out: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for row in rows:
        out.setdefault(key(row), row)
    return list(out.values())


def evaluate_candidates(
    rows: Sequence[Dict[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    *,
    season_stats: Sequence[Mapping[str, Any]],
    base_cache: fvsa.OUCache,
    base_target_rpg: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    base_prob_by_id: Dict[int, Optional[float]] = {}
    for row in rows:
        base_prob_by_id[id(row)] = fvsa.stage_predictions(row, stage1_cache=base_cache).get("final_runtime_fv")

    all_results: List[Dict[str, Any]] = []
    nested: Dict[str, Any] = {}
    for candidate in candidates:
        target = candidate_target_rpg(candidate, season_stats)
        scale = target / base_target_rpg if base_target_rpg > 0 else 1.0
        pred_by_id = {id(row): _scale_probability(row, base_prob_by_id.get(id(row)), scale) for row in rows}
        candidate_rows: Dict[str, Any] = {
            "candidate": candidate["candidate"],
            "family": candidate["family"],
            "target_rpg": _round(target),
            "lambda_scale_vs_base": _round(scale),
            "params": candidate.get("params", {}),
            "weights": candidate.get("weights", {}),
            "metrics": {},
        }
        for dedupe_key in ("row_level", "game_line_family", "game_line_score_family"):
            items = rows if dedupe_key == "row_level" else _dedupe(rows, dedupe_key)
            summary = _summarize_predictions(items, pred_by_id)
            candidate_rows["metrics"][dedupe_key] = {"__all__": summary}
            flat = {
                "candidate": candidate["candidate"],
                "candidate_family": candidate["family"],
                "dedupe": dedupe_key,
                "signal_family": "__all__",
                "target_rpg": _round(target),
                "lambda_scale_vs_base": _round(scale),
                **summary,
            }
            all_results.append(flat)
            for signal_family in sorted({fvsa._family(row) for row in items}):
                subset = [row for row in items if fvsa._family(row) == signal_family]
                fam_summary = _summarize_predictions(subset, pred_by_id)
                candidate_rows["metrics"][dedupe_key][signal_family] = fam_summary
                all_results.append(
                    {
                        "candidate": candidate["candidate"],
                        "candidate_family": candidate["family"],
                        "dedupe": dedupe_key,
                        "signal_family": signal_family,
                        "target_rpg": _round(target),
                        "lambda_scale_vs_base": _round(scale),
                        **fam_summary,
                    }
                )
        nested[str(candidate["candidate"])] = candidate_rows
    return all_results, nested


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


def _write_weight_files(output_root: Path, candidates: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    weights_root = output_root / "weights"
    weights_root.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, str] = {}
    for candidate in candidates:
        path = weights_root / f"{candidate['candidate']}.csv"
        rows = [
            {"season": season, "weight": weight}
            for season, weight in sorted((candidate.get("weights") or {}).items())
        ]
        _write_csv(path, rows)
        paths[str(candidate["candidate"])] = str(path)
    return paths


def render_markdown(report: Mapping[str, Any]) -> str:
    results = report.get("flat_results", []) or []
    row_level = [
        row for row in results
        if row.get("dedupe") == "row_level" and row.get("signal_family") == "__all__"
    ]
    game_line = [
        row for row in results
        if row.get("dedupe") == "game_line_family" and row.get("signal_family") == "__all__"
    ]
    top_row = sorted(row_level, key=lambda r: (r.get("brier") is None, r.get("brier") or 999.0))[:12]
    top_game = sorted(game_line, key=lambda r: (r.get("brier") is None, r.get("brier") or 999.0))[:12]
    lines = ["# Stage-1 Prior Candidate Screen", ""]
    lines.append(f"- Generated: `{report.get('generated_at_utc')}`")
    lines.append(f"- Base cache: `{report.get('base_cache_path')}`")
    lines.append(f"- Candidates: `{report.get('candidate_count')}`")
    lines.append("")
    for title, rows in (("Top Row-Level Brier", top_row), ("Top Deduped Game-Line Brier", top_game)):
        lines.append(f"## {title}")
        lines.append("| Candidate | Family | Target RPG | Scale | Brier | Logloss | AUC | Selected Profit |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
        for row in rows:
            lines.append(
                f"| {row.get('candidate')} | {row.get('candidate_family')} | {row.get('target_rpg')} | "
                f"{row.get('lambda_scale_vs_base')} | {row.get('brier')} | {row.get('logloss')} | "
                f"{row.get('auc')} | {row.get('selected_profit_units')} |"
            )
        lines.append("")
    lines.append("This is a proxy screen based on scaling a base cache-swapped FV. Build full cache artifacts for candidates before promotion.")
    return "\n".join(lines).rstrip() + "\n"


def build_report(args: argparse.Namespace) -> Dict[str, Any]:
    season_stats = load_season_stats(args.scoring_root, history_start=int(args.history_start_season), history_end=int(args.history_end_season))
    target = load_projection_target(args.scoring_root)
    candidates = generate_candidates(season_stats, target_rpg=target)
    rows = _load_rows(args.input_path, mode=str(args.mode or ""), min_date=str(args.min_date or ""), max_date=str(args.max_date or ""))
    base_cache = fvsa.OUCache(args.base_cache_path)
    base_target = candidate_target_rpg(candidates[0], season_stats)
    flat, nested = evaluate_candidates(
        rows,
        candidates,
        season_stats=season_stats,
        base_cache=base_cache,
        base_target_rpg=base_target,
    )
    weight_paths = _write_weight_files(args.output_root, candidates)
    return {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "input_path": str(args.input_path),
        "base_cache_path": str(args.base_cache_path),
        "mode": args.mode,
        "min_date": args.min_date,
        "max_date": args.max_date,
        "history_start_season": int(args.history_start_season),
        "history_end_season": int(args.history_end_season),
        "base_target_rpg": _round(base_target),
        "projection_target_rpg": _round(target),
        "row_count": len(rows),
        "candidate_count": len(candidates),
        "weight_paths": weight_paths,
        "flat_results": flat,
        "candidates": nested,
        "notes": [
            "Proxy probabilities scale the base cache-swapped FV by candidate target RPG via an inverse-Poisson remaining-run approximation.",
            "Use this screen to choose full cache builds; do not promote based on proxy metrics alone.",
            "Deduped game-line and game-line-score metrics are included because candidate rows are clustered.",
        ],
    }


def write_outputs(report: Mapping[str, Any], output_root: Path) -> Dict[str, str]:
    output_root.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary_json": output_root / "stage1_prior_candidate_screen.json",
        "summary_md": output_root / "stage1_prior_candidate_screen.md",
        "flat_csv": output_root / "stage1_prior_candidate_screen.csv",
    }
    paths["summary_json"].write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    paths["summary_md"].write_text(render_markdown(report), encoding="utf-8")
    _write_csv(paths["flat_csv"], report.get("flat_results", []) or [])
    return {key: str(path) for key, path in paths.items()}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Screen Stage-1 prior season-weight candidates.")
    p.add_argument("--scoring-root", type=Path, default=DEFAULT_SCORING_ROOT)
    p.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    p.add_argument("--base-cache-path", type=Path, default=DEFAULT_BASE_CACHE_PATH)
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--mode", type=str, default="live")
    p.add_argument("--min-date", type=str, default="2023-01-01")
    p.add_argument("--max-date", type=str, default="2026-05-09")
    p.add_argument("--history-start-season", type=int, default=2016)
    p.add_argument("--history-end-season", type=int, default=2025)
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    report = build_report(args)
    paths = write_outputs(report, args.output_root)
    print(json.dumps({"paths": paths, "row_count": report.get("row_count"), "candidate_count": report.get("candidate_count")}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
