#!/usr/bin/env python3
"""
Build Stage-2 run-environment residual model for MLB O/U probabilities.

Stage-2 is trained as residual calibration on top of Stage-1 cache outputs:
  p_stage2 = sigmoid(logit(p_stage1) + sum_f weight_f * delta_f(bucket_f))

Feature families:
- park
- temp
- wind
- park_wind (interaction)

The builder:
1) streams historical games,
2) aligns per-PA states with Stage-1 cache probabilities,
3) fits shrunken logit-delta tables by family,
4) tunes family weights on holdout years,
5) refits residual tables on all data and writes deployable JSON.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from build_mlb_ou_cache import (
    bases_mask_from_matchup,
    final_score_from_game,
    inning_bucket,
    is_final_game,
    line_to_threshold,
)
from stage2_run_env_model import FAMILY_ORDER, RunEnvContext

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_DIR / "data"
DEFAULT_STAGE1_CACHE = PROJECT_DIR / "cache" / "mlb_ou_cache.json"
DEFAULT_OUT_PATH = PROJECT_DIR / "cache" / "mlb_stage2_run_env.json"

EPS = 1e-6
UNKNOWN_BUCKET = "__UNK__"


@dataclass(frozen=True)
class LineInfo:
    line: str
    threshold: int
    stage1_key: str


def _clamp01(p: float) -> float:
    return max(EPS, min(1.0 - EPS, p))


def _logit(p: float) -> float:
    p = _clamp01(p)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _delta_cap(args: argparse.Namespace, family: str) -> float:
    return float(getattr(args, f"delta_cap_{family}"))


def _min_n(args: argparse.Namespace, family: str) -> int:
    return int(getattr(args, f"min_n_{family}"))


def _prior_n(args: argparse.Namespace, family: str) -> float:
    return float(getattr(args, f"prior_n_{family}"))


def _parse_float_grid(csv_text: str) -> List[float]:
    vals = []
    for tok in csv_text.split(","):
        tok = tok.strip()
        if not tok:
            continue
        vals.append(float(tok))
    if not vals:
        raise ValueError("Weight grid cannot be empty.")
    return sorted(set(vals))


def _parse_year_from_path(path: Path) -> Optional[int]:
    for part in reversed(path.parts):
        if len(part) == 4 and part.isdigit():
            y = int(part)
            if 1900 <= y <= 2100:
                return y
    return None


def _stage1_key_from_line(line: str) -> str:
    return "o" + line.replace(".", "")


def _line_from_stage1_key(key: str) -> Optional[str]:
    if not key.startswith("o"):
        return None
    digits = key[1:]
    if not digits.isdigit():
        return None
    if not digits.endswith("5"):
        return None
    whole = digits[:-1]
    if not whole or not whole.isdigit():
        return None
    return f"{int(whole)}.5"


def _extract_lines(stage1_cache: Mapping[str, object], lines_arg: str) -> List[LineInfo]:
    if lines_arg.strip():
        lines = [x.strip() for x in lines_arg.split(",") if x.strip()]
        return sorted(
            [LineInfo(line=l, threshold=line_to_threshold(l), stage1_key=_stage1_key_from_line(l)) for l in lines],
            key=lambda x: float(x.line),
        )

    meta_lines = (stage1_cache.get("meta", {}) or {}).get("lines", {}) or {}
    parsed: List[LineInfo] = []
    for stage1_key, label in meta_lines.items():
        if not isinstance(label, str):
            continue
        label = label.strip().lower()
        if not label.startswith("over "):
            continue
        line = label.replace("over ", "", 1).strip()
        if not line.endswith(".5"):
            continue
        parsed.append(
            LineInfo(line=line, threshold=line_to_threshold(line), stage1_key=str(stage1_key))
        )
    if parsed:
        return sorted(parsed, key=lambda x: float(x.line))

    # Fallback: infer from first cell keys
    cells = stage1_cache.get("cells", {}) or {}
    for cell in cells.values():
        if not isinstance(cell, dict):
            continue
        out: List[LineInfo] = []
        for k in cell.keys():
            line = _line_from_stage1_key(str(k))
            if not line:
                continue
            out.append(LineInfo(line=line, threshold=line_to_threshold(line), stage1_key=str(k)))
        if out:
            return sorted(out, key=lambda x: float(x.line))
    raise RuntimeError("Unable to infer lines from Stage-1 cache. Pass --lines explicitly.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build MLB Stage-2 run-environment residual model.")
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.add_argument("--season-type", type=str, default="regular")
    p.add_argument("--game-types", type=str, default="R")
    p.add_argument("--stage1-cache", type=Path, default=DEFAULT_STAGE1_CACHE)
    p.add_argument(
        "--lines",
        type=str,
        default="",
        help="Comma-separated O/U lines (optional; defaults to lines found in Stage-1 cache).",
    )
    p.add_argument("--extras-bucket", type=int, default=10)
    p.add_argument("--max-files", type=int, default=0)

    p.add_argument(
        "--train-end-year",
        type=int,
        default=0,
        help="Last season year used for fitting deltas. 0 = auto.",
    )
    p.add_argument(
        "--validation-start-year",
        type=int,
        default=0,
        help="First season year used for validation/tuning. 0 = train_end_year + 1.",
    )

    p.add_argument("--prior-n-park", type=float, default=1500.0)
    p.add_argument("--prior-n-temp", type=float, default=1200.0)
    p.add_argument("--prior-n-wind", type=float, default=1000.0)
    p.add_argument("--prior-n-park_wind", type=float, default=1800.0)
    p.add_argument("--prior-n-density_alt", type=float, default=1500.0)
    p.add_argument("--prior-n-hr_factor", type=float, default=1500.0)

    p.add_argument("--min-n-park", type=int, default=150)
    p.add_argument("--min-n-temp", type=int, default=200)
    p.add_argument("--min-n-wind", type=int, default=150)
    p.add_argument("--min-n-park_wind", type=int, default=90)
    p.add_argument("--min-n-density_alt", type=int, default=200)
    p.add_argument("--min-n-hr_factor", type=int, default=200)

    p.add_argument("--delta-cap-park", type=float, default=0.80)
    p.add_argument("--delta-cap-temp", type=float, default=0.35)
    p.add_argument("--delta-cap-wind", type=float, default=0.35)
    p.add_argument("--delta-cap-park_wind", type=float, default=0.45)
    p.add_argument("--delta-cap-density_alt", type=float, default=0.40)
    p.add_argument("--delta-cap-hr_factor", type=float, default=0.40)
    p.add_argument("--max-total-delta", type=float, default=1.10)

    p.add_argument("--weight-grid-park", type=str, default="0,0.5,0.75,1.0")
    p.add_argument("--weight-grid-temp", type=str, default="0,0.25,0.5")
    p.add_argument("--weight-grid-wind", type=str, default="0,0.25,0.5")
    p.add_argument("--weight-grid-park_wind", type=str, default="0,0.2,0.4")
    p.add_argument("--weight-grid-density_alt", type=str, default="0,0.25,0.5")
    p.add_argument("--weight-grid-hr_factor", type=str, default="0,0.25,0.5")
    p.add_argument(
        "--min-brier-improvement",
        type=float,
        default=0.00005,
        help="Required absolute Brier gain vs baseline to accept non-zero Stage-2 weights.",
    )

    p.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    return p.parse_args()


def _new_stats(lines: List[LineInfo]) -> Dict[str, Dict[str, Dict[str, Dict[str, float]]]]:
    out: Dict[str, Dict[str, Dict[str, Dict[str, float]]]] = {}
    for family in FAMILY_ORDER:
        out[family] = {li.line: {} for li in lines}
    return out


def _update_family_bucket(
    stats: Dict[str, Dict[str, Dict[str, Dict[str, float]]]],
    family: str,
    line: str,
    bucket: str,
    y: int,
    p0: float,
) -> None:
    line_map = stats[family][line]
    row = line_map.get(bucket)
    if row is None:
        row = {"n": 0.0, "hits": 0.0, "raw_sum": 0.0}
        line_map[bucket] = row
    row["n"] += 1.0
    row["hits"] += float(y)
    row["raw_sum"] += float(p0)


def _update_stats(
    stats: Dict[str, Dict[str, Dict[str, Dict[str, float]]]],
    line_infos: List[LineInfo],
    buckets: Mapping[str, str],
    probs: List[float],
    outcomes: List[int],
) -> None:
    for idx, li in enumerate(line_infos):
        p0 = probs[idx]
        y = outcomes[idx]
        for family in FAMILY_ORDER:
            b = str(buckets.get(family, UNKNOWN_BUCKET) or UNKNOWN_BUCKET)
            _update_family_bucket(stats=stats, family=family, line=li.line, bucket=b, y=y, p0=p0)


def _fit_tables(
    stats: Dict[str, Dict[str, Dict[str, Dict[str, float]]]],
    line_infos: List[LineInfo],
    args: argparse.Namespace,
) -> Dict[str, Dict[str, Dict[str, Dict[str, float]]]]:
    tables: Dict[str, Dict[str, Dict[str, Dict[str, float]]]] = {f: {} for f in FAMILY_ORDER}
    for family in FAMILY_ORDER:
        min_n = _min_n(args, family)
        prior_n = _prior_n(args, family)
        cap = _delta_cap(args, family)
        for li in line_infos:
            out_line: Dict[str, Dict[str, float]] = {}
            for bucket, st in stats[family][li.line].items():
                n = int(st["n"])
                if n < min_n:
                    continue
                raw_mean = float(st["raw_sum"]) / n if n else 0.0
                if raw_mean <= EPS or raw_mean >= 1.0 - EPS:
                    continue
                hits = float(st["hits"])
                emp_rate = hits / n
                emp_shrunk = (hits + prior_n * raw_mean) / (n + prior_n)
                delta = _logit(emp_shrunk) - _logit(raw_mean)
                delta = max(-cap, min(cap, delta))
                out_line[bucket] = {
                    "n": n,
                    "raw_mean": round(raw_mean, 6),
                    "emp_rate": round(emp_rate, 6),
                    "emp_shrunk": round(emp_shrunk, 6),
                    "delta": round(delta, 6),
                }
            if out_line:
                tables[family][li.line] = out_line
    return tables


def _eval_combo(
    raw_logits: List[float],
    ys: List[int],
    d_park: List[float],
    d_temp: List[float],
    d_wind: List[float],
    d_park_wind: List[float],
    d_density_alt: List[float],
    d_hr_factor: List[float],
    w_park: float,
    w_temp: float,
    w_wind: float,
    w_park_wind: float,
    w_density_alt: float,
    w_hr_factor: float,
    max_total_delta: float,
) -> tuple[float, float]:
    n = len(ys)
    if n == 0:
        return 0.0, 0.0
    brier = 0.0
    logloss = 0.0
    for i in range(n):
        total_delta = (
            w_park * d_park[i]
            + w_temp * d_temp[i]
            + w_wind * d_wind[i]
            + w_park_wind * d_park_wind[i]
            + w_density_alt * d_density_alt[i]
            + w_hr_factor * d_hr_factor[i]
        )
        total_delta = max(-max_total_delta, min(max_total_delta, total_delta))
        p = _clamp01(_sigmoid(raw_logits[i] + total_delta))
        y = float(ys[i])
        brier += (y - p) * (y - p)
        logloss += -(y * math.log(p) + (1.0 - y) * math.log(1.0 - p))
    return brier / n, logloss / n


def _complexity(*ws: float) -> int:
    return sum(1 for w in ws if w > 0.0)


def _tune_weights(
    train_tables: Dict[str, Dict[str, Dict[str, Dict[str, float]]]],
    val_rows: List[tuple],
    line_infos: List[LineInfo],
    args: argparse.Namespace,
) -> tuple[Dict[str, Dict[str, float]], Dict[str, Dict[str, float]]]:
    weight_grid = {
        "park": _parse_float_grid(args.weight_grid_park),
        "temp": _parse_float_grid(args.weight_grid_temp),
        "wind": _parse_float_grid(args.weight_grid_wind),
        "park_wind": _parse_float_grid(args.weight_grid_park_wind),
        "density_alt": _parse_float_grid(args.weight_grid_density_alt),
        "hr_factor": _parse_float_grid(args.weight_grid_hr_factor),
    }

    # val row shape
    # (park, temp, wind, park_wind, density_alt, hr_factor, probs_by_line, outcomes_by_line)
    weights_by_line: Dict[str, Dict[str, float]] = {}
    metrics_by_line: Dict[str, Dict[str, float]] = {}

    for line_idx, li in enumerate(line_infos):
        line = li.line
        raw_logits: List[float] = []
        ys: List[int] = []
        d_park: List[float] = []
        d_temp: List[float] = []
        d_wind: List[float] = []
        d_park_wind: List[float] = []
        d_density_alt: List[float] = []
        d_hr_factor: List[float] = []

        t_park = (train_tables.get("park", {}) or {}).get(line, {}) or {}
        t_temp = (train_tables.get("temp", {}) or {}).get(line, {}) or {}
        t_wind = (train_tables.get("wind", {}) or {}).get(line, {}) or {}
        t_park_wind = (train_tables.get("park_wind", {}) or {}).get(line, {}) or {}
        t_density_alt = (train_tables.get("density_alt", {}) or {}).get(line, {}) or {}
        t_hr_factor = (train_tables.get("hr_factor", {}) or {}).get(line, {}) or {}

        for row in val_rows:
            p0 = float(row[6][line_idx])
            y = int(row[7][line_idx])
            raw_logits.append(_logit(p0))
            ys.append(y)

            d_park.append(float((t_park.get(row[0]) or {}).get("delta", 0.0)))
            d_temp.append(float((t_temp.get(row[1]) or {}).get("delta", 0.0)))
            d_wind.append(float((t_wind.get(row[2]) or {}).get("delta", 0.0)))
            d_park_wind.append(float((t_park_wind.get(row[3]) or {}).get("delta", 0.0)))
            d_density_alt.append(float((t_density_alt.get(row[4]) or {}).get("delta", 0.0)))
            d_hr_factor.append(float((t_hr_factor.get(row[5]) or {}).get("delta", 0.0)))

        base_brier, base_logloss = _eval_combo(
            raw_logits=raw_logits,
            ys=ys,
            d_park=d_park,
            d_temp=d_temp,
            d_wind=d_wind,
            d_park_wind=d_park_wind,
            d_density_alt=d_density_alt,
            d_hr_factor=d_hr_factor,
            w_park=0.0,
            w_temp=0.0,
            w_wind=0.0,
            w_park_wind=0.0,
            w_density_alt=0.0,
            w_hr_factor=0.0,
            max_total_delta=args.max_total_delta,
        )

        best = {
            "w_park": 0.0,
            "w_temp": 0.0,
            "w_wind": 0.0,
            "w_park_wind": 0.0,
            "w_density_alt": 0.0,
            "w_hr_factor": 0.0,
            "brier": base_brier,
            "logloss": base_logloss,
            "complexity": 0,
        }

        for w_park in weight_grid["park"]:
            for w_temp in weight_grid["temp"]:
                for w_wind in weight_grid["wind"]:
                    for w_park_wind in weight_grid["park_wind"]:
                        if w_park == 0.0 and w_park_wind > 0.0:
                            continue
                        for w_density_alt in weight_grid["density_alt"]:
                            for w_hr_factor in weight_grid["hr_factor"]:
                                cand_brier, cand_logloss = _eval_combo(
                                    raw_logits=raw_logits,
                                    ys=ys,
                                    d_park=d_park,
                                    d_temp=d_temp,
                                    d_wind=d_wind,
                                    d_park_wind=d_park_wind,
                                    d_density_alt=d_density_alt,
                                    d_hr_factor=d_hr_factor,
                                    w_park=w_park,
                                    w_temp=w_temp,
                                    w_wind=w_wind,
                                    w_park_wind=w_park_wind,
                                    w_density_alt=w_density_alt,
                                    w_hr_factor=w_hr_factor,
                                    max_total_delta=args.max_total_delta,
                                )
                                cand_complexity = _complexity(
                                    w_park, w_temp, w_wind, w_park_wind,
                                    w_density_alt, w_hr_factor,
                                )

                                better = cand_brier < best["brier"] - 1e-12
                                tie_brier = abs(cand_brier - best["brier"]) <= 1e-12
                                better_tie = tie_brier and (
                                    cand_logloss < best["logloss"] - 1e-12
                                    or (
                                        abs(cand_logloss - best["logloss"]) <= 1e-12
                                        and cand_complexity < best["complexity"]
                                    )
                                )
                                if better or better_tie:
                                    best = {
                                        "w_park": float(w_park),
                                        "w_temp": float(w_temp),
                                        "w_wind": float(w_wind),
                                        "w_park_wind": float(w_park_wind),
                                        "w_density_alt": float(w_density_alt),
                                        "w_hr_factor": float(w_hr_factor),
                                        "brier": cand_brier,
                                        "logloss": cand_logloss,
                                        "complexity": cand_complexity,
                                    }

        abs_gain = base_brier - best["brier"]
        if abs_gain < args.min_brier_improvement:
            best = {
                "w_park": 0.0,
                "w_temp": 0.0,
                "w_wind": 0.0,
                "w_park_wind": 0.0,
                "w_density_alt": 0.0,
                "w_hr_factor": 0.0,
                "brier": base_brier,
                "logloss": base_logloss,
                "complexity": 0,
            }

        weights_by_line[line] = {
            "park": round(best["w_park"], 4),
            "temp": round(best["w_temp"], 4),
            "wind": round(best["w_wind"], 4),
            "park_wind": round(best["w_park_wind"], 4),
            "density_alt": round(best["w_density_alt"], 4),
            "hr_factor": round(best["w_hr_factor"], 4),
        }
        rel_gain = ((base_brier - best["brier"]) / base_brier * 100.0) if base_brier > 0 else 0.0
        metrics_by_line[line] = {
            "validation_rows": len(ys),
            "baseline_brier": round(base_brier, 6),
            "stage2_brier": round(best["brier"], 6),
            "baseline_logloss": round(base_logloss, 6),
            "stage2_logloss": round(best["logloss"], 6),
            "brier_gain_abs": round(base_brier - best["brier"], 6),
            "brier_gain_pct": round(rel_gain, 4),
        }
    return weights_by_line, metrics_by_line


def _extract_game_with_context(
    path: Path,
    allowed_game_types: set[str],
    extras_bucket: int,
) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    game_type = str(data.get("gameData", {}).get("game", {}).get("type", "") or "").upper()
    if allowed_game_types and game_type not in allowed_game_types:
        return None
    if not is_final_game(data):
        return None

    score = final_score_from_game(data)
    if score is None:
        return None
    final_total = int(score[0] + score[1])

    all_plays = data.get("liveData", {}).get("plays", {}).get("allPlays", []) or []
    if not all_plays:
        return None

    game_pk = data.get("gamePk")
    if not isinstance(game_pk, int):
        return None

    date_txt = str((data.get("gameData", {}).get("datetime", {}) or {}).get("dateTime", "") or "")
    year: Optional[int] = None
    if len(date_txt) >= 4 and date_txt[:4].isdigit():
        year = int(date_txt[:4])
    if year is None:
        year = _parse_year_from_path(path)
    if year is None:
        return None

    context = RunEnvContext.from_game_data(data.get("gameData", {}) or {})
    buckets = context.buckets()

    samples: List[dict] = []
    cur_away = 0
    cur_home = 0
    cur_outs = 0
    cur_bases = 0
    cur_inning: Optional[int] = None
    cur_half: Optional[str] = None

    for play in all_plays:
        about = play.get("about", {}) or {}
        inning_raw = about.get("inning")
        half_raw = str(about.get("halfInning", "") or "").lower()
        if not isinstance(inning_raw, int):
            continue
        if half_raw.startswith("top"):
            half = "T"
        elif half_raw.startswith("bottom"):
            half = "B"
        else:
            continue

        if cur_inning != inning_raw or cur_half != half:
            cur_inning = inning_raw
            cur_half = half
            cur_outs = 0
            cur_bases = 0

        samples.append(
            {
                "away": cur_away,
                "home": cur_home,
                "inning_bucket": inning_bucket(inning_raw, extras_bucket),
                "half": half,
                "outs": max(0, min(2, int(cur_outs))),
                "bases": cur_bases,
            }
        )

        result = play.get("result", {}) or {}
        a_after = result.get("awayScore")
        h_after = result.get("homeScore")
        if isinstance(a_after, int):
            cur_away = a_after
        if isinstance(h_after, int):
            cur_home = h_after

        count = play.get("count", {}) or {}
        outs_after = count.get("outs")
        if isinstance(outs_after, int):
            cur_outs = max(0, min(3, outs_after))

        if cur_outs >= 3:
            cur_bases = 0
            cur_outs = 0
        else:
            cur_bases = bases_mask_from_matchup(play.get("matchup", {}) or {})

    return {
        "game_pk": game_pk,
        "year": year,
        "final_total": final_total,
        "buckets": buckets,
        "samples": samples,
    }


def _auto_train_year(files: List[Path], explicit: int) -> int:
    if explicit > 0:
        return explicit
    years = [y for y in (_parse_year_from_path(p) for p in files) if y is not None]
    if not years:
        raise RuntimeError("Unable to infer years from file paths; pass --train-end-year explicitly.")
    min_year = min(years)
    max_year = max(years)
    current_year = datetime.utcnow().year
    # Keep at least one full holdout season by default when current year is present.
    if max_year >= current_year:
        train_end = max_year - 2
    else:
        train_end = max_year - 1
    return max(min_year, train_end)


def _count_table_rows(tables: Dict[str, Dict[str, Dict[str, Dict[str, float]]]]) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    for family in FAMILY_ORDER:
        out[family] = {}
        for line, m in (tables.get(family, {}) or {}).items():
            out[family][line] = len(m)
    return out


def main() -> None:
    args = parse_args()

    with open(args.stage1_cache, encoding="utf-8") as f:
        stage1 = json.load(f)
    stage1_cells = stage1.get("cells", {}) or {}
    if not stage1_cells:
        raise RuntimeError(f"No Stage-1 cells found in {args.stage1_cache}")

    line_infos = _extract_lines(stage1_cache=stage1, lines_arg=args.lines)
    print("Lines:", ", ".join(li.line for li in line_infos))

    allowed_game_types = {x.strip().upper() for x in args.game_types.split(",") if x.strip()}
    games_root = args.data_dir / "games" / args.season_type
    files = sorted(games_root.rglob("*.json"))
    if args.max_files > 0:
        files = files[: args.max_files]
    if not files:
        raise RuntimeError(f"No game files found under {games_root}")

    train_end_year = _auto_train_year(files=files, explicit=args.train_end_year)
    validation_start_year = args.validation_start_year if args.validation_start_year > 0 else train_end_year + 1
    print(f"Train <= {train_end_year}, validation >= {validation_start_year}")

    train_stats = _new_stats(line_infos)
    full_stats = _new_stats(line_infos)
    # (park, temp, wind, park_wind, probs_by_line, outcomes_by_line)
    val_rows: List[tuple] = []

    games_loaded = 0
    samples_total = 0
    samples_with_stage1 = 0
    skipped_no_stage1 = 0

    for i, path in enumerate(files):
        if i % 1000 == 0:
            print(f"  parsing {i}/{len(files)} files ...")
        g = _extract_game_with_context(
            path=path,
            allowed_game_types=allowed_game_types,
            extras_bucket=args.extras_bucket,
        )
        if g is None:
            continue

        games_loaded += 1
        year = int(g["year"])
        final_total = int(g["final_total"])
        buckets = g["buckets"]
        outcomes = [1 if final_total >= li.threshold else 0 for li in line_infos]

        for s in g["samples"]:
            samples_total += 1
            skey = f"{s['away']}_{s['home']}_{s['inning_bucket']}_{s['half']}_{s['outs']}_{s['bases']}"
            cell = stage1_cells.get(skey)
            if not cell:
                skipped_no_stage1 += 1
                continue

            probs: List[float] = []
            line_missing = False
            for li in line_infos:
                v = cell.get(li.stage1_key)
                if v is None:
                    line_missing = True
                    break
                probs.append(_clamp01(float(v)))
            if line_missing:
                skipped_no_stage1 += 1
                continue

            samples_with_stage1 += 1
            _update_stats(
                stats=full_stats,
                line_infos=line_infos,
                buckets=buckets,
                probs=probs,
                outcomes=outcomes,
            )
            if year <= train_end_year:
                _update_stats(
                    stats=train_stats,
                    line_infos=line_infos,
                    buckets=buckets,
                    probs=probs,
                    outcomes=outcomes,
                )
            elif year >= validation_start_year:
                val_rows.append(
                    (
                        buckets.get("park", UNKNOWN_BUCKET),
                        buckets.get("temp", UNKNOWN_BUCKET),
                        buckets.get("wind", UNKNOWN_BUCKET),
                        buckets.get("park_wind", UNKNOWN_BUCKET),
                        buckets.get("density_alt", UNKNOWN_BUCKET),
                        buckets.get("hr_factor", UNKNOWN_BUCKET),
                        probs,
                        outcomes,
                    )
                )

    if games_loaded == 0:
        raise RuntimeError("No final games loaded.")
    if samples_with_stage1 == 0:
        raise RuntimeError("No samples matched Stage-1 cache cells.")

    print(
        f"Loaded games={games_loaded}, samples={samples_total}, "
        f"with_stage1={samples_with_stage1}, val_rows={len(val_rows)}"
    )

    train_tables = _fit_tables(train_stats, line_infos, args)
    weights_by_line, val_metrics = _tune_weights(
        train_tables=train_tables,
        val_rows=val_rows,
        line_infos=line_infos,
        args=args,
    )
    full_tables = _fit_tables(full_stats, line_infos, args)

    payload = {
        "meta": {
            "built": datetime.utcnow().isoformat() + "Z",
            "sport": "mlb",
            "model_type": "stage2_run_environment_residual",
            "stage1_cache": str(args.stage1_cache),
            "season_type": args.season_type,
            "game_types": sorted(allowed_game_types),
            "games_loaded": games_loaded,
            "samples_total": samples_total,
            "samples_with_stage1": samples_with_stage1,
            "samples_without_stage1": skipped_no_stage1,
            "train_end_year": train_end_year,
            "validation_start_year": validation_start_year,
            "validation_rows": len(val_rows),
            "lines": {li.line: {"threshold": li.threshold, "stage1_key": li.stage1_key} for li in line_infos},
        },
        "feature_config": {
            "families": list(FAMILY_ORDER),
            "prior_n": {f: _prior_n(args, f) for f in FAMILY_ORDER},
            "min_n": {f: _min_n(args, f) for f in FAMILY_ORDER},
            "delta_cap": {f: _delta_cap(args, f) for f in FAMILY_ORDER},
            "table_rows": _count_table_rows(full_tables),
        },
        "constraints": {
            "max_total_abs_delta": float(args.max_total_delta),
        },
        "weights": weights_by_line,
        "validation_metrics": val_metrics,
        "tables": full_tables,
    }

    # Active #16 v2 (2026-05-17): stamp build-time lineage on the
    # Stage-2 cache. Same pattern as Stage-1; lets the operator
    # trace any Stage-2 promotion (or post-hoc demotion) back to the
    # exact build sha + input data window.
    try:
        import sys as _sys
        _sys.path.insert(0, str(PROJECT_DIR / "scripts" / "analysis"))
        from artifact_lineage import compute_lineage as _compute_lineage
    except ImportError:
        _compute_lineage = None  # type: ignore[assignment]
    if _compute_lineage is not None:
        try:
            games_root = args.data_dir / "games" / args.season_type
            payload["lineage"] = _compute_lineage(
                builder_path=__file__,
                input_paths=[args.stage1_cache],
                input_dir_paths=[games_root],
                project_root=PROJECT_DIR,
                extra={
                    "cli_args_summary": {
                        "season_type": args.season_type,
                        "game_types": args.game_types,
                        "train_end_year": getattr(
                            args, "train_end_year", None,
                        ),
                        "validation_start_year": getattr(
                            args, "validation_start_year", None,
                        ),
                        "max_total_delta": getattr(
                            args, "max_total_delta", None,
                        ),
                        "stage1_cache": str(args.stage1_cache),
                        "out": str(args.out),
                    },
                },
            )
        except Exception as _lineage_exc:  # noqa: BLE001
            print(f"[lineage] warning: stamp failed: {_lineage_exc!r}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print("\nValidation summary:")
    for li in line_infos:
        m = val_metrics.get(li.line, {})
        w = weights_by_line.get(li.line, {})
        print(
            f"  O{li.line}: brier {m.get('baseline_brier')} -> {m.get('stage2_brier')} "
            f"(gain={m.get('brier_gain_abs')}, {m.get('brier_gain_pct')}%) "
            f"weights={w}"
        )
    print(f"\nSaved Stage-2 model -> {args.out}")


if __name__ == "__main__":
    main()

