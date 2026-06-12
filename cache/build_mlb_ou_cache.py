#!/usr/bin/env python3
"""
Build MLB O/U stage-1 cache from historical game feeds.

Input:
  baseball/data/games/<season_type>/**/*.json  (from scrape_mlb_history.py)

Output:
  baseball/cache/mlb_ou_cache.json

State key:
  {away}_{home}_{inning_bucket}_{half}_{outs}_{bases}
  - inning_bucket: 1..9, extras bucket = 10
  - half: T or B
  - outs: 0..2
  - bases: bitmask 0..7 (1st=1, 2nd=2, 3rd=4)

Empirical target:
  P(final_total >= threshold) for each configured O/U line.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from scipy.stats import nbinom, poisson

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_DIR / "data"
DEFAULT_OUT_PATH = PROJECT_DIR / "cache" / "mlb_ou_cache.json"

DEFAULT_LINES = "6.5,7.5,8.5,9.5,10.5,11.5"
DEFAULT_MIN_GAMES = 40
DEFAULT_MAX_COMBINED = 20
DEFAULT_EXTRAS_BUCKET = 10

DEFAULT_CALIB_PRIOR_N = 80.0
DEFAULT_CALIB_MIN_N = 20
EPS = 1e-6

# Active #8 (2026-05-17): Alt-A smoothing modes.
# `poisson` preserves the historical builder behavior: each cell's
# `poXX` value is the Poisson-CDF prediction conditioned on phase
# lambda + current state, and the sibling `oXX` value is the raw
# empirical rate observed in that cell.
# `empirical_when_available` is the Alt-A materialization: after the
# normal build pass, each cell's `poXX` is OVERWRITTEN with its
# `oXX` value when `n_samples >= MIN_EMPIRICAL_N_FOR_OVERRIDE`. The
# runtime reads only `poXX`, so this produces a cache whose FV path
# matches the on-the-fly Alt-A shadow logged on every candidate.
SMOOTHING_MODE_POISSON = "poisson"
SMOOTHING_MODE_EMPIRICAL_WHEN_AVAILABLE = "empirical_when_available"
# Hygiene #3 (2026-06-11): negative-binomial tail. The Poisson is
# structurally too thin-tailed for run scoring -- the 2026-05-19 audit
# measured poisson > empirical by 4-7pp at every line where
# poisson >= 0.85 (845-1,919 well-supported cells per line), and the
# 2026-06-06 retrain experiment proved the bias is the smoothing, not
# stale data. `negative_binomial` fits per-phase dispersion via method
# of moments on the remaining-runs samples (r = mean^2/(var-mean) when
# overdispersed) and computes poXX from the NB survival function;
# phases that are NOT overdispersed (var <= mean) or too thin keep the
# Poisson. The fallback-calibration pass (pass 2) uses the same
# distribution so the logit-delta table stays consistent with the
# smoothing it corrects.
SMOOTHING_MODE_NEGATIVE_BINOMIAL = "negative_binomial"
SMOOTHING_MODES = (
    SMOOTHING_MODE_POISSON,
    SMOOTHING_MODE_EMPIRICAL_WHEN_AVAILABLE,
    SMOOTHING_MODE_NEGATIVE_BINOMIAL,
)
DEFAULT_SMOOTHING_MODE = SMOOTHING_MODE_POISSON
DEFAULT_MIN_EMPIRICAL_N_FOR_OVERRIDE = 0
# Minimum remaining-runs samples a phase needs before its NB
# dispersion estimate is trusted; thinner phases keep Poisson.
DEFAULT_NB_MIN_PHASE_N = 200


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build MLB O/U empirical cache.")
    p.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="Base baseball data directory (default: baseball/data).",
    )
    p.add_argument(
        "--season-type",
        type=str,
        default="regular",
        help="Season folder under games/ (default: regular).",
    )
    p.add_argument(
        "--game-types",
        type=str,
        default="R",
        help="Allowed MLB game type codes from gameData.game.type (default: R).",
    )
    p.add_argument(
        "--min-date",
        type=str,
        default="",
        help="Inclusive game-file date filter YYYY-MM-DD. Applied before parsing feeds.",
    )
    p.add_argument(
        "--max-date",
        type=str,
        default="",
        help="Inclusive game-file date filter YYYY-MM-DD. Applied before parsing feeds.",
    )
    p.add_argument(
        "--min-season",
        type=int,
        default=0,
        help="Inclusive season/year filter from file path (e.g. 2016).",
    )
    p.add_argument(
        "--max-season",
        type=int,
        default=0,
        help="Inclusive season/year filter from file path (e.g. 2025).",
    )
    p.add_argument(
        "--lines",
        type=str,
        default=DEFAULT_LINES,
        help=f"Comma-separated O/U lines (default: {DEFAULT_LINES}).",
    )
    p.add_argument(
        "--min-games",
        type=int,
        default=DEFAULT_MIN_GAMES,
        help=f"Minimum unique games per cell (default: {DEFAULT_MIN_GAMES}).",
    )
    p.add_argument(
        "--max-combined",
        type=int,
        default=DEFAULT_MAX_COMBINED,
        help=f"Skip cells where away+home exceeds this value (default: {DEFAULT_MAX_COMBINED}).",
    )
    p.add_argument(
        "--extras-bucket",
        type=int,
        default=DEFAULT_EXTRAS_BUCKET,
        help=f"Inning bucket used for extras (default: {DEFAULT_EXTRAS_BUCKET}).",
    )
    p.add_argument(
        "--calib-prior-n",
        type=float,
        default=DEFAULT_CALIB_PRIOR_N,
        help=f"Bayesian prior weight for fallback calibration (default: {DEFAULT_CALIB_PRIOR_N}).",
    )
    p.add_argument(
        "--calib-min-n",
        type=int,
        default=DEFAULT_CALIB_MIN_N,
        help=f"Minimum samples for calibration key retention (default: {DEFAULT_CALIB_MIN_N}).",
    )
    p.add_argument(
        "--max-files",
        type=int,
        default=0,
        help="Optional cap on number of game files for quick testing (0 = no cap).",
    )
    p.add_argument(
        "--season-weights-path",
        type=Path,
        default=None,
        help=(
            "Optional CSV/JSON season weights. CSV must include season and a weight column. "
            "Used for research candidate caches only."
        ),
    )
    p.add_argument(
        "--season-weight-column",
        type=str,
        default="weight",
        help="Weight column name in --season-weights-path CSV (default: weight).",
    )
    p.add_argument(
        "--season-weight-mode",
        type=str,
        choices=("allocation", "multiplier"),
        default="allocation",
        help=(
            "allocation treats weights as total season allocation shares; multiplier treats "
            "weights as raw per-game multipliers. Both are normalized to preserve total game mass."
        ),
    )
    p.add_argument(
        "--smoothing-mode",
        type=str,
        choices=SMOOTHING_MODES,
        default=DEFAULT_SMOOTHING_MODE,
        help=(
            "Per-line probability source. 'poisson' keeps the historical "
            "behavior (poXX = Poisson-CDF). 'empirical_when_available' "
            "(Active #8 Alt-A) overwrites each cell's poXX with its oXX "
            "value when n_samples >= --min-empirical-n-for-override AND "
            "the empirical is a valid (0,1) probability. The runtime reads "
            "only poXX, so this materializes the on-the-fly Alt-A shadow "
            "as a real cache file ready for promote.py stage1."
        ),
    )
    p.add_argument(
        "--nb-min-phase-n",
        type=int,
        default=DEFAULT_NB_MIN_PHASE_N,
        help=(
            "Hygiene #3: minimum remaining-runs samples a phase needs "
            "before --smoothing-mode negative_binomial trusts its "
            "method-of-moments dispersion fit; thinner phases keep "
            f"Poisson (default: {DEFAULT_NB_MIN_PHASE_N})."
        ),
    )
    p.add_argument(
        "--min-empirical-n-for-override",
        type=int,
        default=DEFAULT_MIN_EMPIRICAL_N_FOR_OVERRIDE,
        help=(
            "Minimum cell n_samples required before --smoothing-mode "
            "empirical_when_available will overwrite poXX with oXX "
            "(default: 0 = always override when empirical present, "
            "matching the runtime shadow path)."
        ),
    )
    p.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_PATH,
        help=f"Output cache path (default: {DEFAULT_OUT_PATH}).",
    )
    return p.parse_args()


def _parse_date(raw: str, *, arg_name: str) -> date:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{arg_name} must be YYYY-MM-DD, got {raw!r}") from exc


def _file_date(path: Path, season_type: str) -> Optional[date]:
    parts = path.parts
    try:
        idx = parts.index(season_type)
        year = int(parts[idx + 1])
        month = int(parts[idx + 2])
        day = int(parts[idx + 3])
        return date(year, month, day)
    except Exception:
        return None


def _filter_files_by_history_window(files: List[Path], args: argparse.Namespace) -> List[Path]:
    min_date = _parse_date(args.min_date, arg_name="--min-date") if args.min_date else None
    max_date = _parse_date(args.max_date, arg_name="--max-date") if args.max_date else None
    if min_date and max_date and min_date > max_date:
        raise ValueError("--min-date must be <= --max-date")
    if args.min_season and args.max_season and int(args.min_season) > int(args.max_season):
        raise ValueError("--min-season must be <= --max-season")

    out: List[Path] = []
    for path in files:
        file_date = _file_date(path, args.season_type)
        if file_date is None:
            continue
        if min_date and file_date < min_date:
            continue
        if max_date and file_date > max_date:
            continue
        if args.min_season and file_date.year < int(args.min_season):
            continue
        if args.max_season and file_date.year > int(args.max_season):
            continue
        out.append(path)
    return out


def _load_season_weights(path: Optional[Path], weight_column: str = "weight") -> Dict[str, float]:
    if path is None:
        return {}
    if not path.exists():
        raise FileNotFoundError(f"Season weights file not found: {path}")
    suffix = path.suffix.lower()
    weights: Dict[str, float] = {}
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                season = str(row.get("season") or "").strip()
                raw_weight = row.get(weight_column)
                if not season:
                    continue
                try:
                    weight = float(raw_weight)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Invalid weight for season {season!r} in {path}: {raw_weight!r}") from exc
                if weight < 0 or not math.isfinite(weight):
                    raise ValueError(f"Season weight must be finite and non-negative for {season}: {weight}")
                weights[season] = weight
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            source = payload.get("weights", payload)
            if isinstance(source, dict):
                for season, raw_weight in source.items():
                    weight = float(raw_weight)
                    if weight < 0 or not math.isfinite(weight):
                        raise ValueError(f"Season weight must be finite and non-negative for {season}: {weight}")
                    weights[str(season)] = weight
            elif isinstance(source, list):
                for row in source:
                    if not isinstance(row, dict):
                        continue
                    season = str(row.get("season") or "").strip()
                    weight = float(row.get(weight_column))
                    if weight < 0 or not math.isfinite(weight):
                        raise ValueError(f"Season weight must be finite and non-negative for {season}: {weight}")
                    weights[season] = weight
        elif isinstance(payload, list):
            for row in payload:
                if not isinstance(row, dict):
                    continue
                season = str(row.get("season") or "").strip()
                weight = float(row.get(weight_column))
                if weight < 0 or not math.isfinite(weight):
                    raise ValueError(f"Season weight must be finite and non-negative for {season}: {weight}")
                weights[season] = weight
        else:
            raise ValueError(f"Unsupported season weight payload in {path}")
    if not weights:
        raise ValueError(f"No season weights loaded from {path}")
    if sum(weights.values()) <= 0:
        raise ValueError(f"Season weights must contain positive total weight: {path}")
    return weights


def _count_valid_games_by_season(
    files: List[Path],
    *,
    allowed_game_types: set[str],
    extras_bucket: int,
) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    seen_game_pks: set[int] = set()
    for path in files:
        game = extract_game_samples(path, allowed_game_types, extras_bucket)
        if game is None:
            continue
        game_pk = int(game["game_pk"])
        if game_pk in seen_game_pks:
            continue
        seen_game_pks.add(game_pk)
        game_date = str(game.get("game_date") or "")
        if len(game_date) >= 4:
            counts[game_date[:4]] += 1
    return dict(counts)


def _prepare_season_weighting(
    args: argparse.Namespace,
    files: List[Path],
    *,
    allowed_game_types: set[str],
) -> Tuple[Dict[str, float], Dict[str, object]]:
    weights_path = getattr(args, "season_weights_path", None)
    if not weights_path:
        return {}, {"enabled": False}
    raw_weights = _load_season_weights(weights_path, getattr(args, "season_weight_column", "weight"))
    season_counts = _count_valid_games_by_season(
        files,
        allowed_game_types=allowed_game_types,
        extras_bucket=int(args.extras_bucket),
    )
    seasons = sorted(season_counts)
    missing = [season for season in seasons if season not in raw_weights]
    if missing:
        raise ValueError(
            f"Season weights file {weights_path} is missing seasons present in build window: {', '.join(missing)}"
        )
    total_games = sum(season_counts.values())
    if total_games <= 0:
        raise RuntimeError("No valid games available for season-weight normalization.")

    mode = getattr(args, "season_weight_mode", "allocation")
    multipliers: Dict[str, float] = {}
    if mode == "allocation":
        raw_total = sum(raw_weights[season] for season in seasons)
        if raw_total <= 0:
            raise ValueError("Season allocation weights sum to zero for build seasons.")
        for season in seasons:
            allocation = raw_weights[season] / raw_total
            multipliers[season] = allocation * total_games / season_counts[season]
    else:
        weighted_total = sum(raw_weights[season] * season_counts[season] for season in seasons)
        if weighted_total <= 0:
            raise ValueError("Season multiplier weights produce zero total game mass.")
        scale = total_games / weighted_total
        for season in seasons:
            multipliers[season] = raw_weights[season] * scale

    implied_alloc = {
        season: multipliers[season] * season_counts[season] / total_games
        for season in seasons
    }
    return multipliers, {
        "enabled": True,
        "path": str(weights_path),
        "weight_column": getattr(args, "season_weight_column", "weight"),
        "mode": mode,
        "normalization": "total_weighted_game_mass_equals_total_unweighted_games",
        "raw_weights": {season: raw_weights[season] for season in seasons},
        "game_counts_by_season": dict(sorted(season_counts.items())),
        "game_multipliers_by_season": {season: round(multipliers[season], 8) for season in seasons},
        "implied_allocations_by_season": {season: round(implied_alloc[season], 8) for season in seasons},
        "total_unweighted_games": total_games,
        "total_weighted_games": round(sum(multipliers[s] * season_counts[s] for s in seasons), 6),
    }


def _effective_n(sum_w: float, sum_w2: float) -> float:
    if sum_w2 <= 0:
        return 0.0
    return (sum_w * sum_w) / sum_w2


def _clamp01(x: float) -> float:
    return max(EPS, min(1.0 - EPS, x))


def _logit(p: float) -> float:
    p = _clamp01(p)
    return math.log(p / (1.0 - p))


def line_to_threshold(line: str) -> int:
    # over 8.5 => threshold 9
    return int(float(line) + 0.5)


def line_to_emp_key(line: str) -> str:
    return "o" + line.replace(".", "")


def line_to_poisson_key(line: str) -> str:
    return "po" + line.replace(".", "")


def parse_lines(lines_csv: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for raw in lines_csv.split(","):
        line = raw.strip()
        if not line:
            continue
        # validates parse
        threshold = line_to_threshold(line)
        if not line.endswith(".5"):
            raise ValueError(f"Line must end with .5 for standard O/U markets: {line}")
        out[line] = threshold
    if not out:
        raise ValueError("No lines provided.")
    return out


def inning_bucket(inning: int, extras_bucket: int) -> int:
    if inning <= 0:
        return 1
    return inning if inning <= 9 else extras_bucket


def bases_mask_from_matchup(matchup: dict) -> int:
    mask = 0
    if matchup.get("postOnFirst"):
        mask |= 1
    if matchup.get("postOnSecond"):
        mask |= 2
    if matchup.get("postOnThird"):
        mask |= 4
    return mask


def final_score_from_game(data: dict) -> Optional[Tuple[int, int]]:
    linescore = data.get("liveData", {}).get("linescore", {})
    teams = linescore.get("teams", {})
    away_runs = (teams.get("away", {}) or {}).get("runs")
    home_runs = (teams.get("home", {}) or {}).get("runs")
    if isinstance(away_runs, int) and isinstance(home_runs, int):
        return away_runs, home_runs

    # Fallback: final result score from last play
    plays = data.get("liveData", {}).get("plays", {}).get("allPlays", [])
    if plays:
        last = plays[-1].get("result", {})
        a = last.get("awayScore")
        h = last.get("homeScore")
        if isinstance(a, int) and isinstance(h, int):
            return a, h
    return None


def official_date_from_game(data: dict) -> str:
    dt = data.get("gameData", {}).get("datetime", {}) or {}
    official = str(dt.get("officialDate") or "").strip()
    if len(official) >= 10:
        return official[:10]
    game_date = str(dt.get("dateTime") or "").strip()
    if len(game_date) >= 10:
        return game_date[:10]
    return ""


def is_final_game(data: dict) -> bool:
    status = data.get("gameData", {}).get("status", {})
    abstract = str(status.get("abstractGameState", "") or "").lower()
    detailed = str(status.get("detailedState", "") or "").lower()
    if abstract == "final":
        return True
    finalish = {"final", "game over", "completed early", "completed"}
    return detailed in finalish


def extract_game_samples(
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
    away_final, home_final = score
    final_total = away_final + home_final

    all_plays = data.get("liveData", {}).get("plays", {}).get("allPlays", [])
    if not all_plays:
        return None

    game_pk = data.get("gamePk")
    if not isinstance(game_pk, int):
        return None

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

        inning = inning_raw
        if cur_inning != inning or cur_half != half:
            cur_inning = inning
            cur_half = half
            cur_outs = 0
            cur_bases = 0

        # Snapshot BEFORE this plate appearance.
        ib = inning_bucket(inning, extras_bucket)
        samples.append(
            {
                "away": cur_away,
                "home": cur_home,
                "inning_bucket": ib,
                "half": half,
                "outs": max(0, min(2, int(cur_outs))),
                "bases": cur_bases,
                "final_total": final_total,
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
            matchup = play.get("matchup", {}) or {}
            cur_bases = bases_mask_from_matchup(matchup)

    return {
        "game_pk": game_pk,
        "game_type": game_type,
        "game_date": official_date_from_game(data),
        "final_total": final_total,
        "samples": samples,
    }


def poisson_over_prob(threshold: int, current_total: int, lam_remaining: float) -> float:
    needed = threshold - current_total
    if needed <= 0:
        return 1.0
    if lam_remaining <= 0:
        return 0.0
    return float(1.0 - poisson.cdf(needed - 1, lam_remaining))


def fit_nb_dispersion(
    mean: float, var: float, n: int, *, min_phase_n: int = DEFAULT_NB_MIN_PHASE_N,
) -> Optional[float]:
    """Method-of-moments negative-binomial size parameter `r` for a
    phase's remaining-runs distribution. Returns None when the phase
    should keep Poisson: not overdispersed (var <= mean -- NB cannot
    represent it), degenerate mean, or too few samples to trust the
    variance estimate."""
    if n < min_phase_n or mean <= 0:
        return None
    if var <= mean:
        return None
    return (mean * mean) / (var - mean)


def nb_over_prob(
    threshold: int,
    current_total: int,
    lam_remaining: float,
    nb_r: Optional[float],
) -> float:
    """P(final_total >= threshold) with remaining runs ~ NB(mean=lam,
    size=r). NB parameterization: p = r / (r + mean), so
    P(X >= k) = nbinom.sf(k - 1, r, p). Falls back to Poisson when the
    phase has no trusted dispersion fit (nb_r None) -- the NB cache is
    therefore a strict superset of the Poisson cache: identical where
    overdispersion is unmeasurable, fatter-tailed where it is."""
    needed = threshold - current_total
    if needed <= 0:
        return 1.0
    if lam_remaining <= 0:
        return 0.0
    if nb_r is None or nb_r <= 0:
        return float(1.0 - poisson.cdf(needed - 1, lam_remaining))
    p = nb_r / (nb_r + lam_remaining)
    return float(nbinom.sf(needed - 1, nb_r, p))


def _apply_alt_a_smoothing(
    cells: Dict[str, dict],
    *,
    lines: Dict[str, int],
    smoothing_mode: str,
    min_empirical_n_for_override: int,
) -> Dict[str, object]:
    """Active #8 Alt-A (2026-05-17): overwrite per-cell poXX with oXX.

    Each cell already carries both the empirical rate (`oXX` = raw
    observed over rate) and the Poisson smoothed rate (`poXX`). The
    runtime reads only `poXX`. When `smoothing_mode` is
    `empirical_when_available`, this pass replaces each cell's `poXX`
    with its sibling `oXX` value where the cell has enough sample
    support and the empirical is a valid (0,1) probability.

    Cells where the override is declined (insufficient n, empirical
    missing, boundary value) keep the Poisson value. The diagnostic
    summary returned is logged into `cache.meta["alt_a_smoothing"]`
    so the daily-review staging-health block can answer "how many
    cells flipped, by how much."
    """
    summary: Dict[str, object] = {
        "enabled": smoothing_mode == SMOOTHING_MODE_EMPIRICAL_WHEN_AVAILABLE,
        "mode": smoothing_mode,
        "min_empirical_n_for_override": int(min_empirical_n_for_override),
        "cells_total": len(cells),
        "cells_overridden": 0,
        "cells_kept_poisson_low_n": 0,
        "cells_kept_poisson_no_empirical": 0,
        "cells_kept_poisson_invalid_empirical": 0,
        "line_overrides": {line_to_poisson_key(line): 0 for line in lines},
        # Per-line boundary skips: how often each line's empirical was
        # exactly 0 or 1 (degenerate sample artifact). The aggregate
        # cells_kept_poisson_invalid_empirical only counts cells where
        # ALL lines hit the boundary; per-line is the actionable view
        # for the scoped Alt-A design (Active #17).
        "line_boundary_skips": {line_to_poisson_key(line): 0 for line in lines},
        "mean_abs_delta_logit": 0.0,
        "mean_signed_delta": 0.0,
        "n_line_deltas": 0,
    }
    # Only the empirical_when_available mode overrides poXX with oXX.
    # 2026-06-11: was `== SMOOTHING_MODE_POISSON`, which would have let
    # the new negative_binomial mode fall through and get its NB values
    # clobbered by empirical overrides.
    if smoothing_mode != SMOOTHING_MODE_EMPIRICAL_WHEN_AVAILABLE:
        return summary

    threshold = int(min_empirical_n_for_override)
    total_abs_delta_logit = 0.0
    total_signed_delta = 0.0
    n_deltas = 0

    for cell in cells.values():
        n_samples = int(cell.get("n_samples", 0))
        if n_samples < threshold:
            summary["cells_kept_poisson_low_n"] += 1
            continue

        any_override = False
        for line in lines.keys():
            emp_key = line_to_emp_key(line)
            poi_key = line_to_poisson_key(line)
            raw_emp = cell.get(emp_key)
            raw_poi = cell.get(poi_key)
            if raw_emp is None:
                continue
            try:
                emp = float(raw_emp)
            except (TypeError, ValueError):
                continue
            # Empirical rates at the (0,1) boundary blow up the logit-
            # additive FV math the runtime does (logit(0) = -inf). Keep
            # Poisson in that case; the over-prediction story Alt-A is
            # solving is about the *interior* of the probability space.
            if not (0.0 < emp < 1.0):
                summary["line_boundary_skips"][poi_key] += 1
                if not any_override:
                    summary["cells_kept_poisson_invalid_empirical"] += 1
                continue

            cell[poi_key] = round(emp, 4)
            summary["line_overrides"][poi_key] += 1
            any_override = True

            try:
                old = float(raw_poi) if raw_poi is not None else None
            except (TypeError, ValueError):
                old = None
            if old is not None and 0.0 < old < 1.0:
                delta = emp - old
                total_signed_delta += delta
                # logit-space distance is what the FV chain actually
                # consumes; report it so operators reading the daily
                # review can reason in the same units the engine uses.
                total_abs_delta_logit += abs(
                    math.log(emp / (1.0 - emp))
                    - math.log(old / (1.0 - old))
                )
                n_deltas += 1

        if any_override:
            summary["cells_overridden"] += 1
        else:
            # n_samples passed the threshold but no line had a usable
            # empirical (all None / all boundary). Track separately so
            # the daily-review block can distinguish "low-N cells" from
            # "high-N cells with no usable empirical."
            summary["cells_kept_poisson_no_empirical"] += 1

    if n_deltas > 0:
        summary["mean_abs_delta_logit"] = round(total_abs_delta_logit / n_deltas, 6)
        summary["mean_signed_delta"] = round(total_signed_delta / n_deltas, 6)
    summary["n_line_deltas"] = n_deltas
    return summary


def base_label(mask: int) -> str:
    chars = ["-", "-", "-"]
    if mask & 1:
        chars[0] = "1"
    if mask & 2:
        chars[1] = "2"
    if mask & 4:
        chars[2] = "3"
    return "".join(chars)


def state_label(ib: int, half: str, outs: int, bases: int, extras_bucket: int) -> str:
    inning_txt = "X" if ib == extras_bucket else str(ib)
    half_txt = "Top" if half == "T" else "Bot"
    return f"{half_txt}{inning_txt}  {outs} out  bases={base_label(bases)}"


def build_cache(args: argparse.Namespace) -> dict:
    lines = parse_lines(args.lines)
    allowed_game_types = {x.strip().upper() for x in args.game_types.split(",") if x.strip()}

    games_root = args.data_dir / "games" / args.season_type
    all_files = sorted(games_root.rglob("*.json"))
    files = _filter_files_by_history_window(all_files, args)
    if args.max_files and args.max_files > 0:
        files = files[: args.max_files]
    if not files:
        raise RuntimeError(f"No game files found under {games_root}")

    season_multipliers, season_weighting_meta = _prepare_season_weighting(
        args,
        files,
        allowed_game_types=allowed_game_types,
    )
    season_weighting_enabled = bool(season_weighting_meta.get("enabled"))

    print(
        f"Found {len(files)} files under {games_root} "
        f"(from {len(all_files)} total after history filters). Parsing pass 1..."
    )
    if season_weighting_enabled:
        print(
            "  Season weighting enabled: "
            f"{season_weighting_meta.get('path')} "
            f"({season_weighting_meta.get('mode')}, total weighted games "
            f"{season_weighting_meta.get('total_weighted_games')})"
        )

    def _new_state():
        return {
            "sample_n": 0,
            "games_n": 0,
            "weighted_sample_n": 0.0,
            "weighted_games_n": 0.0,
            "weighted_sample_w2": 0.0,
            "weighted_games_w2": 0.0,
            "over_hits": {line_to_emp_key(line): 0 for line in lines},
            "weighted_over_hits": {line_to_emp_key(line): 0.0 for line in lines},
        }

    state_stats: dict[tuple, dict] = defaultdict(_new_state)
    # phase lambda uses inning/half/outs/bases, independent of score
    phase_remaining_sum: dict[tuple, float] = defaultdict(float)
    phase_remaining_n: dict[tuple, int] = defaultdict(int)
    phase_weighted_remaining_sum: dict[tuple, float] = defaultdict(float)
    phase_weighted_n: dict[tuple, float] = defaultdict(float)
    # Hygiene #3 (2026-06-11): sum of squares so the NB smoothing mode
    # can fit per-phase dispersion (var = E[X^2] - mean^2). Accumulated
    # unconditionally (cheap) so the same pass serves every mode.
    phase_remaining_sumsq: dict[tuple, float] = defaultdict(float)
    phase_weighted_remaining_sumsq: dict[tuple, float] = defaultdict(float)

    games_loaded = 0
    weighted_games_loaded = 0.0
    samples_recorded = 0
    sum_final_totals = 0.0
    weighted_sum_final_totals = 0.0
    seen_game_pks: set[int] = set()
    duplicate_game_files_skipped = 0
    games_by_season: dict[str, int] = defaultdict(int)
    loaded_game_dates: List[str] = []

    for i, path in enumerate(files):
        if i % 1000 == 0:
            print(f"  pass1 {i}/{len(files)} files ...")
        game = extract_game_samples(path, allowed_game_types, args.extras_bucket)
        if game is None:
            continue
        game_pk = int(game["game_pk"])
        if game_pk in seen_game_pks:
            duplicate_game_files_skipped += 1
            continue
        seen_game_pks.add(game_pk)

        games_loaded += 1
        game_date = str(game.get("game_date") or "")
        if len(game_date) >= 4:
            games_by_season[game_date[:4]] += 1
            loaded_game_dates.append(game_date[:10])
        season = game_date[:4] if len(game_date) >= 4 else ""
        game_weight = float(season_multipliers.get(season, 1.0))
        final_total = game["final_total"]
        sum_final_totals += final_total
        weighted_games_loaded += game_weight
        weighted_sum_final_totals += final_total * game_weight

        seen_in_game: set = set()
        for s in game["samples"]:
            samples_recorded += 1
            key = (
                s["away"],
                s["home"],
                s["inning_bucket"],
                s["half"],
                s["outs"],
                s["bases"],
            )
            st = state_stats[key]
            st["sample_n"] += 1
            st["weighted_sample_n"] += game_weight
            st["weighted_sample_w2"] += game_weight * game_weight
            if key not in seen_in_game:
                st["games_n"] += 1
                st["weighted_games_n"] += game_weight
                st["weighted_games_w2"] += game_weight * game_weight
                seen_in_game.add(key)

            for line, threshold in lines.items():
                if final_total >= threshold:
                    st["over_hits"][line_to_emp_key(line)] += 1
                    st["weighted_over_hits"][line_to_emp_key(line)] += game_weight

            current_total = s["away"] + s["home"]
            remaining_runs = max(0, final_total - current_total)
            phase_key = (s["inning_bucket"], s["half"], s["outs"], s["bases"])
            phase_remaining_sum[phase_key] += remaining_runs
            phase_remaining_n[phase_key] += 1
            phase_weighted_remaining_sum[phase_key] += remaining_runs * game_weight
            phase_weighted_n[phase_key] += game_weight
            phase_remaining_sumsq[phase_key] += remaining_runs * remaining_runs
            phase_weighted_remaining_sumsq[phase_key] += (
                remaining_runs * remaining_runs * game_weight
            )

    if games_loaded == 0:
        raise RuntimeError("No valid final games loaded.")

    league_rpg = (
        weighted_sum_final_totals / weighted_games_loaded
        if season_weighting_enabled and weighted_games_loaded > 0
        else sum_final_totals / games_loaded
    )

    phase_lambda: dict[tuple, float] = {}
    for k, total_remaining in phase_remaining_sum.items():
        if season_weighting_enabled:
            n = phase_weighted_n[k]
            phase_lambda[k] = (phase_weighted_remaining_sum[k] / n) if n else 0.0
        else:
            n = phase_remaining_n[k]
            phase_lambda[k] = (total_remaining / n) if n else 0.0

    # Hygiene #3: per-phase NB dispersion (size r). None = keep Poisson
    # for that phase. Only consumed when smoothing_mode is
    # negative_binomial; computed unconditionally for the meta summary.
    smoothing_mode = str(getattr(args, "smoothing_mode", DEFAULT_SMOOTHING_MODE))
    nb_active = smoothing_mode == SMOOTHING_MODE_NEGATIVE_BINOMIAL
    nb_min_phase_n = int(getattr(args, "nb_min_phase_n", DEFAULT_NB_MIN_PHASE_N))
    phase_nb_r: dict[tuple, Optional[float]] = {}
    nb_phase_summary = {
        "phases_total": len(phase_lambda),
        "phases_nb_fit": 0,
        "phases_kept_poisson": 0,
        "dispersion_ratios": [],  # var/mean per fitted phase, summarized below
    }
    for k, mean in phase_lambda.items():
        if season_weighting_enabled:
            n_w = phase_weighted_n[k]
            ex2 = (phase_weighted_remaining_sumsq[k] / n_w) if n_w else 0.0
            n_for_fit = int(phase_remaining_n[k])  # trust raw sample count for the floor
        else:
            n_raw = phase_remaining_n[k]
            ex2 = (phase_remaining_sumsq[k] / n_raw) if n_raw else 0.0
            n_for_fit = int(n_raw)
        var = max(0.0, ex2 - mean * mean)
        r = fit_nb_dispersion(mean, var, n_for_fit, min_phase_n=nb_min_phase_n)
        phase_nb_r[k] = r
        if r is not None:
            nb_phase_summary["phases_nb_fit"] += 1
            nb_phase_summary["dispersion_ratios"].append(var / mean if mean > 0 else 0.0)
        else:
            nb_phase_summary["phases_kept_poisson"] += 1

    def _phase_over_prob(threshold: int, current_total: int, pkey: tuple) -> float:
        """Smoothing-mode-aware P(over). The fallback-calibration pass
        and the cell builder both route through this so the calibration
        deltas correct the SAME distribution the cells carry."""
        lam_ = phase_lambda.get(pkey, 0.0)
        if nb_active:
            return nb_over_prob(threshold, current_total, lam_, phase_nb_r.get(pkey))
        return poisson_over_prob(threshold, current_total, lam_)

    # Calibration pass: only on fallback-domain states (games_n < min_games)
    print("Parsing pass 2 for fallback calibration ...")
    cal_stats: dict[str, dict[str, dict[int, dict]]] = defaultdict(
        lambda: defaultdict(
            lambda: defaultdict(
                lambda: {
                    "n": 0,
                    "hits": 0,
                    "raw_sum": 0.0,
                    "weighted_n": 0.0,
                    "weighted_hits": 0.0,
                    "weighted_raw_sum": 0.0,
                    "weighted_w2": 0.0,
                }
            )
        )
    )
    cal_samples_used = 0
    weighted_cal_samples_used = 0.0
    seen_game_pks_pass2: set[int] = set()

    for i, path in enumerate(files):
        if i % 1000 == 0:
            print(f"  pass2 {i}/{len(files)} files ...")
        game = extract_game_samples(path, allowed_game_types, args.extras_bucket)
        if game is None:
            continue
        game_pk = int(game["game_pk"])
        if game_pk in seen_game_pks_pass2:
            continue
        seen_game_pks_pass2.add(game_pk)
        final_total = game["final_total"]
        game_date = str(game.get("game_date") or "")
        season = game_date[:4] if len(game_date) >= 4 else ""
        game_weight = float(season_multipliers.get(season, 1.0))

        for s in game["samples"]:
            key = (
                s["away"],
                s["home"],
                s["inning_bucket"],
                s["half"],
                s["outs"],
                s["bases"],
            )
            if state_stats[key]["games_n"] >= args.min_games:
                continue

            current_total = s["away"] + s["home"]
            pkey = (s["inning_bucket"], s["half"], s["outs"], s["bases"])
            phase_bucket = f"{s['inning_bucket']}_{s['half']}_{s['outs']}"

            cal_samples_used += 1
            weighted_cal_samples_used += game_weight
            for line, threshold in lines.items():
                needed = threshold - current_total
                if needed <= 0:
                    continue
                raw_p = _phase_over_prob(threshold, current_total, pkey)
                bucket = cal_stats[line][phase_bucket][needed]
                bucket["n"] += 1
                bucket["weighted_n"] += game_weight
                bucket["weighted_w2"] += game_weight * game_weight
                if final_total >= threshold:
                    bucket["hits"] += 1
                    bucket["weighted_hits"] += game_weight
                bucket["raw_sum"] += raw_p
                bucket["weighted_raw_sum"] += raw_p * game_weight

    cal_table: dict[str, dict[str, dict[str, dict]]] = {}
    cal_entries = 0
    for line, phase_map in sorted(cal_stats.items()):
        out_phase: dict[str, dict[str, dict]] = {}
        for phase_bucket, need_map in sorted(phase_map.items()):
            out_need: dict[str, dict] = {}
            for needed, st in sorted(need_map.items()):
                n = st["n"]
                if n < args.calib_min_n:
                    continue
                weighted_n = float(st["weighted_n"])
                denom = weighted_n if season_weighting_enabled else float(n)
                if denom <= 0:
                    continue
                raw_mean = (
                    st["weighted_raw_sum"] / weighted_n
                    if season_weighting_enabled
                    else st["raw_sum"] / n
                )
                if raw_mean <= 0.0 or raw_mean >= 1.0:
                    continue
                emp_rate = (
                    st["weighted_hits"] / weighted_n
                    if season_weighting_enabled
                    else st["hits"] / n
                )
                hits_for_shrink = st["weighted_hits"] if season_weighting_enabled else st["hits"]
                emp_shrunk = (hits_for_shrink + args.calib_prior_n * raw_mean) / (denom + args.calib_prior_n)
                delta = _logit(emp_shrunk) - _logit(raw_mean)
                out_row = {
                    "n": n,
                    "raw_mean": round(raw_mean, 6),
                    "emp_rate": round(emp_rate, 6),
                    "emp_shrunk": round(emp_shrunk, 6),
                    "delta": round(delta, 6),
                }
                if season_weighting_enabled:
                    out_row.update(
                        {
                            "weighted_n": round(weighted_n, 4),
                            "effective_n": round(_effective_n(weighted_n, st["weighted_w2"]), 4),
                        }
                    )
                out_need[str(needed)] = out_row
                cal_entries += 1
            if out_need:
                out_phase[phase_bucket] = out_need
        if out_phase:
            cal_table[line] = out_phase

    print("Building cache cells ...")
    cells = {}
    skipped_low_games = 0
    skipped_score = 0

    for key, st in sorted(state_stats.items()):
        away, home, ib, half, outs, bases = key
        combined = away + home
        if combined > args.max_combined:
            skipped_score += 1
            continue
        n_games = st["games_n"]
        n_samples = st["sample_n"]
        if n_games < args.min_games:
            skipped_low_games += 1
            continue

        pkey = (ib, half, outs, bases)
        lam = phase_lambda.get(pkey, 0.0)

        cell = {
            "n": n_games,
            "n_samples": n_samples,
            "lam": round(lam, 4),
            "label": state_label(ib, half, outs, bases, args.extras_bucket),
        }
        if nb_active:
            # Diagnostic only -- the runtime reads poXX. None means the
            # phase kept Poisson (not overdispersed or too thin).
            r_diag = phase_nb_r.get(pkey)
            cell["nb_r"] = round(r_diag, 4) if r_diag is not None else None
        if season_weighting_enabled:
            cell.update(
                {
                    "weighted_n": round(st["weighted_games_n"], 4),
                    "weighted_n_samples": round(st["weighted_sample_n"], 4),
                    "effective_n": round(_effective_n(st["weighted_games_n"], st["weighted_games_w2"]), 4),
                    "effective_n_samples": round(
                        _effective_n(st["weighted_sample_n"], st["weighted_sample_w2"]),
                        4,
                    ),
                }
            )

        for line, threshold in lines.items():
            emp_key = line_to_emp_key(line)
            poi_key = line_to_poisson_key(line)
            if season_weighting_enabled:
                hits = st["weighted_over_hits"][emp_key]
                denom = st["weighted_sample_n"]
            else:
                hits = st["over_hits"][emp_key]
                denom = n_samples
            cell[emp_key] = round(hits / denom, 4) if denom else 0.0
            cell[poi_key] = round(_phase_over_prob(threshold, combined, pkey), 4)

        skey = f"{away}_{home}_{ib}_{half}_{outs}_{bases}"
        cells[skey] = cell

    min_emp_override = int(
        getattr(args, "min_empirical_n_for_override", DEFAULT_MIN_EMPIRICAL_N_FOR_OVERRIDE)
    )
    alt_a_summary = _apply_alt_a_smoothing(
        cells,
        lines=lines,
        smoothing_mode=smoothing_mode,
        min_empirical_n_for_override=min_emp_override,
    )

    # Hygiene #3: NB smoothing diagnostics for the meta block. The
    # dispersion ratio (var/mean) quantifies HOW overdispersed run
    # scoring is per phase -- 1.0 would mean Poisson was right.
    ratios = nb_phase_summary.pop("dispersion_ratios")
    nb_meta_summary = {
        "enabled": nb_active,
        "mode": smoothing_mode,
        "min_phase_n": nb_min_phase_n,
        **nb_phase_summary,
        "mean_dispersion_ratio": (
            round(sum(ratios) / len(ratios), 4) if ratios else None
        ),
        "max_dispersion_ratio": round(max(ratios), 4) if ratios else None,
    }

    line_meta = {line_to_emp_key(line): f"over {line}" for line in lines}
    history_start = min(loaded_game_dates) if loaded_game_dates else ""
    history_end = max(loaded_game_dates) if loaded_game_dates else ""
    seasons = sorted(games_by_season.keys())
    builder_args = {
        "data_dir": str(args.data_dir),
        "season_type": args.season_type,
        "game_types": args.game_types,
        "lines": args.lines,
        "min_date": args.min_date,
        "max_date": args.max_date,
        "min_season": int(args.min_season or 0),
        "max_season": int(args.max_season or 0),
        "min_games": int(args.min_games),
        "max_combined": int(args.max_combined),
        "extras_bucket": int(args.extras_bucket),
        "calib_prior_n": float(args.calib_prior_n),
        "calib_min_n": int(args.calib_min_n),
        "max_files": int(args.max_files or 0),
        "season_weights_path": str(getattr(args, "season_weights_path", "") or ""),
        "season_weight_column": str(getattr(args, "season_weight_column", "weight")),
        "season_weight_mode": str(getattr(args, "season_weight_mode", "allocation")),
        "smoothing_mode": smoothing_mode,
        "min_empirical_n_for_override": min_emp_override,
        "out": str(args.out),
    }
    cache = {
        "meta": {
            "built": datetime.utcnow().isoformat() + "Z",
            "sport": "mlb",
            "season_type": args.season_type,
            "game_types": sorted(allowed_game_types),
            "history_start_date": history_start,
            "history_end_date": history_end,
            "seasons": seasons,
            "games_by_season": dict(sorted(games_by_season.items())),
            "total_games": games_loaded,
            "weighted_total_games": round(weighted_games_loaded, 4) if season_weighting_enabled else None,
            "builder_args": builder_args,
            "season_weighting": season_weighting_meta,
            "files_considered": len(files),
            "duplicate_game_files_skipped": duplicate_game_files_skipped,
            "games_loaded": games_loaded,
            "samples_recorded": samples_recorded,
            "valid_cells": len(cells),
            "league_runs_per_game": round(league_rpg, 4),
            "min_games": args.min_games,
            "max_combined": args.max_combined,
            "extras_bucket": args.extras_bucket,
            "state_key_format": "away_home_inningBucket_half_outs_basesMask",
            "n_definition": "unique games observed in this cell",
            "n_samples_definition": "total sampled plate-appearance states in this cell",
            "weighted_n_definition": (
                "weighted unique-game mass observed in this cell; present only when season weighting is enabled"
            ),
            "effective_n_definition": (
                "(sum weights)^2 / sum(weights^2); present only when season weighting is enabled"
            ),
            "lines": line_meta,
            "alt_a_smoothing": alt_a_summary,
            "nb_smoothing": nb_meta_summary,
        },
        "poisson_calibration": {
            "method": "logit_delta_by_line_inningHalfOut_needed",
            "prior_n": args.calib_prior_n,
            "min_n": args.calib_min_n,
            "calibration_samples": cal_samples_used,
            "weighted_calibration_samples": round(weighted_cal_samples_used, 4)
            if season_weighting_enabled
            else None,
            "table": cal_table,
        },
        "cells": cells,
    }

    print("\nCache build complete:")
    print(f"  Games loaded: {games_loaded}")
    if season_weighting_enabled:
        print(f"  Weighted game mass: {weighted_games_loaded:.2f}")
    print(f"  Seasons: {', '.join(seasons)}")
    print(f"  History window: {history_start or 'n/a'} -> {history_end or 'n/a'}")
    print(f"  Duplicate game files skipped: {duplicate_game_files_skipped}")
    print(f"  Samples recorded: {samples_recorded}")
    print(f"  Valid cells: {len(cells)}")
    print(f"  Skipped low games: {skipped_low_games}")
    print(f"  Skipped high score cells: {skipped_score}")
    print(
        f"  Calibration keys: {cal_entries} "
        f"(prior_n={args.calib_prior_n:.0f}, min_n={args.calib_min_n}, samples={cal_samples_used})"
    )
    if alt_a_summary.get("enabled"):
        print(
            f"  Alt-A smoothing: mode={alt_a_summary['mode']}, "
            f"min_empirical_n_for_override={alt_a_summary['min_empirical_n_for_override']}, "
            f"cells_overridden={alt_a_summary['cells_overridden']}/{alt_a_summary['cells_total']}, "
            f"mean_signed_delta={alt_a_summary['mean_signed_delta']:+.4f}, "
            f"mean_abs_delta_logit={alt_a_summary['mean_abs_delta_logit']:.4f}"
        )
    if nb_meta_summary.get("enabled"):
        print(
            f"  NB smoothing: phases_nb_fit={nb_meta_summary['phases_nb_fit']}/"
            f"{nb_meta_summary['phases_total']} "
            f"(kept_poisson={nb_meta_summary['phases_kept_poisson']}), "
            f"mean_dispersion_ratio={nb_meta_summary['mean_dispersion_ratio']}, "
            f"max={nb_meta_summary['max_dispersion_ratio']}"
        )

    sample_keys = [
        "0_0_1_T_0_0",
        "0_0_1_B_0_0",
        "1_0_5_T_1_3",
    ]
    print("\nSample cells:")
    for sk in sample_keys:
        c = cells.get(sk)
        if c:
            print(
                f"  {sk:18s} n={c['n']:4d} n_samples={c['n_samples']:6d} "
                f"lam={c['lam']:.2f} label={c['label']}"
            )
        else:
            print(f"  {sk:18s} (not present)")

    return cache


def main() -> None:
    args = parse_args()
    cache = build_cache(args)

    # Active #16 v2 (2026-05-17): stamp build-time lineage on the
    # Stage-1 cache. Today's loss attribution shipment identified
    # Stage-1 as owning ~100% of the 27pp aggregate over-prediction
    # bias; lineage now answers "when was this cache built, on what
    # data window, by what git_sha?" without git-log archaeology.
    try:
        import sys as _sys
        _sys.path.insert(0, str(PROJECT_DIR / "scripts" / "analysis"))
        from artifact_lineage import compute_lineage as _compute_lineage
    except ImportError:
        _compute_lineage = None  # type: ignore[assignment]
    if _compute_lineage is not None:
        try:
            games_root = args.data_dir / "games" / args.season_type
            cache["lineage"] = _compute_lineage(
                builder_path=__file__,
                input_dir_paths=[games_root],
                project_root=PROJECT_DIR,
                extra={
                    "cli_args_summary": {
                        "season_type": args.season_type,
                        "game_types": args.game_types,
                        "lines": args.lines,
                        "min_games": getattr(args, "min_games", None),
                        "max_combined": getattr(args, "max_combined", None),
                        "extras_bucket": getattr(args, "extras_bucket", None),
                        "history_start_date": str(
                            getattr(args, "history_start_date", "") or ""
                        ),
                        "history_end_date": str(
                            getattr(args, "history_end_date", "") or ""
                        ),
                        "season_weighting_path": str(
                            getattr(args, "season_weighting_path", "") or ""
                        ),
                        "smoothing_mode": str(
                            getattr(args, "smoothing_mode", DEFAULT_SMOOTHING_MODE)
                        ),
                        "min_empirical_n_for_override": int(
                            getattr(
                                args,
                                "min_empirical_n_for_override",
                                DEFAULT_MIN_EMPIRICAL_N_FOR_OVERRIDE,
                            )
                        ),
                        "max_files": getattr(args, "max_files", None),
                        "out": str(args.out),
                    },
                },
            )
        except Exception as _lineage_exc:  # noqa: BLE001
            # Lineage stamp MUST NEVER block the cache build.
            print(f"[lineage] warning: stamp failed: {_lineage_exc!r}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)
    print(f"\nSaved cache -> {args.out}")


if __name__ == "__main__":
    main()
