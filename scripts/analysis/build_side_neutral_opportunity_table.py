#!/usr/bin/env python3
"""
Build a side-neutral O/U opportunity table from raw Polymarket tick files.

This is analysis-only research plumbing for testing whether the existing Over
fair-value stack can support side-aware selection without changing live trading.
It pairs the recorded `over_yes` and `under_no` books for each game-line,
samples the stream to control bloat, computes fair Over and fair Under values,
and joins final labels when local game files are available.

Outputs:
  data/analysis_output/side_neutral_opportunities/
    side_neutral_opportunities.jsonl
    side_neutral_opportunities.csv
    manifest.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = PROJECT_DIR / "scripts" / "analysis"
CACHE_DIR = PROJECT_DIR / "cache"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
if str(ANALYSIS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_DIR))
if str(CACHE_DIR) not in sys.path:
    sys.path.insert(0, str(CACHE_DIR))

from analyze_polymarket_overreactions import (  # noqa: E402
    OUCache,
    load_game_run_env_context,
    load_meta,
)
from scripts.trading.remaining_opportunity import compute_remaining_opportunity_fields  # noqa: E402
from scripts.trading.scoring_path_features import SCORING_PATH_FIELD_KEYS, compute_scoring_path_fields  # noqa: E402


LOGGER = logging.getLogger("build_side_neutral_opportunity_table")

DEFAULT_POLYMARKET_ROOT = PROJECT_DIR / "data" / "polymarket" / "mlb_ou"
DEFAULT_GAMES_ROOT = PROJECT_DIR / "data" / "games" / "regular"
DEFAULT_CACHE_PATH = PROJECT_DIR / "cache" / "mlb_ou_cache.json"
DEFAULT_STAGE2_MODEL_PATH = PROJECT_DIR / "cache" / "mlb_stage2_run_env.json"
DEFAULT_TEAM_GAME_LOG_PATH = PROJECT_DIR / "cache" / "team_game_log.json"
DEFAULT_OUTPUT_ROOT = PROJECT_DIR / "data" / "analysis_output" / "side_neutral_opportunities"

DEFAULT_SAMPLE_SECONDS = 30.0
DEFAULT_MAX_PAIR_LAG_SECONDS = 2.5

OUTPUT_COLUMNS = [
    "row_id",
    "schema_version",
    "source",
    "session_date",
    "ts",
    "pair_lag_seconds",
    "game_dir_name",
    "game_pk",
    "away_abbrev",
    "home_abbrev",
    "line",
    "market_id",
    "over_token_id",
    "under_token_id",
    "game_status",
    "game_detailed_status",
    "inning",
    "inning_state",
    "outs",
    "balls",
    "strikes",
    "runners_on",
    "away_score",
    "home_score",
    "current_total",
    "home_leading_late",
    "batting_team_is_home",
    "bottom9_available_if_needed",
    "expected_remaining_half_innings",
    "expected_remaining_pa_bucket",
    "home_skip_bottom9_risk",
    *SCORING_PATH_FIELD_KEYS,
    "over_bid",
    "over_ask",
    "over_mid",
    "over_ltp",
    "under_bid",
    "under_ask",
    "under_mid",
    "under_ltp",
    "over_spread",
    "under_spread",
    "over_under_ask_sum",
    "over_under_bid_sum",
    "over_mid_no_vig",
    "under_mid_no_vig",
    "fair_over_base_poisson",
    "fair_over_base_empirical",
    "fair_over",
    "fair_under",
    "stage2_run_env_delta",
    "team_offense_delta",
    "fv_used_fallback",
    "fv_state_fallback_level",
    "fv_state_fallback_label",
    "fv_line_fallback_mode",
    "fv_line_source_key",
    "over_edge_to_ask",
    "under_edge_to_ask",
    "over_edge_to_mid_no_vig",
    "under_edge_to_mid_no_vig",
    "over_market_logit_residual",
    "under_market_logit_residual",
    "best_side_by_edge",
    "best_edge_to_ask",
    "label_final_available",
    "final_away",
    "final_home",
    "final_total",
    "target_over_win",
    "target_under_win",
    "target_over_taker_profit_units",
    "target_under_taker_profit_units",
]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build side-neutral opportunity rows from raw O/U ticks.")
    p.add_argument("--polymarket-root", type=Path, default=DEFAULT_POLYMARKET_ROOT)
    p.add_argument("--games-root", type=Path, default=DEFAULT_GAMES_ROOT)
    p.add_argument("--cache-path", type=Path, default=DEFAULT_CACHE_PATH)
    p.add_argument("--stage2-model-path", type=Path, default=DEFAULT_STAGE2_MODEL_PATH)
    p.add_argument("--team-game-log-path", type=Path, default=DEFAULT_TEAM_GAME_LOG_PATH)
    p.add_argument("--min-date", type=str, default="", help="Inclusive YYYY-MM-DD.")
    p.add_argument("--max-date", type=str, default="", help="Inclusive YYYY-MM-DD.")
    p.add_argument("--sample-seconds", type=float, default=DEFAULT_SAMPLE_SECONDS)
    p.add_argument("--max-pair-lag-seconds", type=float, default=DEFAULT_MAX_PAIR_LAG_SECONDS)
    p.add_argument("--disable-stage2", action="store_true")
    p.add_argument("--disable-stage3", action="store_true")
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    p.add_argument("--strict", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        if not math.isfinite(out):
            return None
        return out
    except Exception:
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def _parse_ts(raw: Any) -> Optional[datetime]:
    if raw in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ts_epoch(raw: Any) -> Optional[float]:
    dt = _parse_ts(raw)
    return dt.timestamp() if dt else None


def _clip_prob(value: float) -> float:
    return max(1e-6, min(1.0 - 1e-6, float(value)))


def _logit(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    p = _clip_prob(value)
    return math.log(p / (1.0 - p))


def _logit_residual(model_prob: Optional[float], market_prob: Optional[float]) -> Optional[float]:
    a = _logit(model_prob)
    b = _logit(market_prob)
    if a is None or b is None:
        return None
    return a - b


def _mid(bid: Optional[float], ask: Optional[float], ltp: Optional[float] = None) -> Optional[float]:
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    if bid is not None:
        return bid
    if ask is not None:
        return ask
    return ltp


def _profit_units(won: Optional[int], price: Optional[float]) -> Optional[float]:
    if won is None or price is None or price <= 0:
        return None
    return (1.0 / price - 1.0) if won == 1 else -1.0


def _date_in_range(date_str: str, min_date: str, max_date: str) -> bool:
    if min_date and date_str < min_date:
        return False
    if max_date and date_str > max_date:
        return False
    return True


def _read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            yield row


def _valid_book(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    book = row.get("book") or {}
    if not isinstance(book, Mapping) or not book.get("ok", False):
        return None
    bid = _safe_float(book.get("best_bid"))
    ask = _safe_float(book.get("best_ask"))
    ltp = _safe_float(book.get("ltp"))
    if bid is None and ask is None and ltp is None:
        return None
    out = dict(row)
    out["_ts_epoch"] = _ts_epoch(row.get("ts"))
    if out["_ts_epoch"] is None:
        return None
    out["_best_bid"] = bid
    out["_best_ask"] = ask
    out["_ltp"] = ltp
    return out


def load_tick_rows(path: Path) -> List[Dict[str, Any]]:
    rows = [r for r in (_valid_book(row) for row in _read_jsonl(path)) if r is not None]
    rows.sort(key=lambda r: float(r["_ts_epoch"]))
    return rows


def _closest_by_ts(
    target_epoch: float,
    rows: Sequence[Dict[str, Any]],
    epochs: Sequence[float],
) -> Tuple[Optional[Dict[str, Any]], Optional[float]]:
    if not rows:
        return None, None
    idx = bisect_left(epochs, target_epoch)
    candidates: List[int] = []
    if idx < len(rows):
        candidates.append(idx)
    if idx > 0:
        candidates.append(idx - 1)
    best_row: Optional[Dict[str, Any]] = None
    best_lag: Optional[float] = None
    for i in candidates:
        lag = abs(float(rows[i]["_ts_epoch"]) - target_epoch)
        if best_lag is None or lag < best_lag:
            best_lag = lag
            best_row = rows[i]
    return best_row, best_lag


def _line_from_over_path(path: Path) -> str:
    name = path.name
    prefix = "ou_"
    suffix = "_over_yes.jsonl"
    if not name.startswith(prefix) or not name.endswith(suffix):
        return ""
    raw = name[len(prefix) : -len(suffix)]
    return raw.replace("_", ".")


def iter_game_line_files(root: Path, min_date: str = "", max_date: str = "") -> Iterable[Tuple[str, Path, Path]]:
    if not root.exists():
        return
    for date_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        date_str = date_dir.name
        if not _date_in_range(date_str, min_date, max_date):
            continue
        for over_path in sorted(date_dir.rglob("*_over_yes.jsonl")):
            under_path = over_path.with_name(over_path.name.replace("_over_yes.jsonl", "_under_no.jsonl"))
            if under_path.exists():
                yield date_str, over_path, under_path


def _date_range(min_date: str, max_date: str) -> Iterable[str]:
    if not min_date or not max_date:
        return []
    try:
        start = datetime.strptime(min_date, "%Y-%m-%d").date()
        end = datetime.strptime(max_date, "%Y-%m-%d").date()
    except Exception:
        return []
    out: List[str] = []
    value = start
    while value <= end:
        out.append(value.strftime("%Y-%m-%d"))
        value += timedelta(days=1)
    return out


def load_final_scores(games_root: Path, min_date: str = "", max_date: str = "") -> Dict[int, Dict[str, int]]:
    finals: Dict[int, Dict[str, int]] = {}
    if not games_root.exists():
        return finals
    if min_date and max_date:
        game_paths: List[Path] = []
        for date_str in _date_range(min_date, max_date):
            yyyy, mm, dd = date_str.split("-")
            game_paths.extend(sorted((games_root / yyyy / mm / dd).glob("*.json")))
    else:
        game_paths = sorted(games_root.rglob("*.json"))
    for game_path in game_paths:
        try:
            with open(game_path, encoding="utf-8") as f:
                game = json.load(f)
        except Exception:
            continue
        game_pk = _safe_int(game.get("gamePk"))
        if game_pk is None:
            continue
        teams = game.get("liveData", {}).get("linescore", {}).get("teams", {}) or {}
        away = _safe_int((teams.get("away") or {}).get("runs"))
        home = _safe_int((teams.get("home") or {}).get("runs"))
        if away is None or home is None:
            continue
        finals[game_pk] = {"final_away": away, "final_home": home, "final_total": away + home}
    return finals


def _load_stage2_model(path: Path, disabled: bool, warnings: List[str]):
    if disabled:
        return None, None
    try:
        from stage2_run_env_model import RunEnvContext, Stage2RunEnvModel  # noqa: WPS433

        return Stage2RunEnvModel.from_path(path), RunEnvContext
    except Exception as exc:
        warnings.append(f"stage2 model disabled: {exc}")
        return None, None


def _load_team_offense_model(path: Path, disabled: bool, warnings: List[str]):
    if disabled:
        return None
    try:
        from team_offense_model import TeamOffenseModel  # noqa: WPS433

        return TeamOffenseModel.load(game_log_path=path, auto_rebuild=False)
    except Exception as exc:
        warnings.append(f"stage3 team offense disabled: {exc}")
        return None


@lru_cache(maxsize=4096)
def _game_meta_fields(game_dir: Path) -> Dict[str, Any]:
    meta = load_meta(game_dir)
    game = meta.get("game", {}) or {}
    markets = meta.get("ou_markets", []) or []
    tokens_by_line: Dict[str, Dict[str, Any]] = {}
    for market in markets:
        line = str(market.get("line") or "")
        tokens_by_line[line] = {
            "market_id": market.get("market_id"),
            "over_token_id": market.get("over_token_id"),
            "under_token_id": market.get("under_token_id"),
        }
    return {
        "game_pk": _safe_int(game.get("game_pk")) or 0,
        "game_date": str(game.get("game_date") or game.get("start_time_utc") or ""),
        "away_abbrev": str(game.get("away_abbrev") or ""),
        "home_abbrev": str(game.get("home_abbrev") or ""),
        "tokens_by_line": tokens_by_line,
    }


def _lookup_fair_over(
    *,
    cache: OUCache,
    over_row: Mapping[str, Any],
    line: str,
    game_date: str,
    away_abbrev: str,
    home_abbrev: str,
    stage2_model: Any,
    run_env_context: Any,
    offense_model: Any,
) -> Dict[str, Any]:
    away_score = _safe_int(over_row.get("away_score"))
    home_score = _safe_int(over_row.get("home_score"))
    inning = _safe_int(over_row.get("inning"))
    outs = _safe_int(over_row.get("outs"))
    runners_on = _safe_int(over_row.get("runners_on")) or 0
    inning_state = str(over_row.get("inning_state") or "")
    if away_score is None or home_score is None or inning is None or outs is None:
        return {}

    base, meta = cache.lookup_with_meta(
        away_score=away_score,
        home_score=home_score,
        inning=inning,
        inning_state=inning_state,
        outs=outs,
        line=line,
        runners_on=runners_on,
    )
    if base is None:
        return {}

    fair = float(base)
    stage2_delta = 0.0
    if stage2_model is not None and run_env_context is not None:
        try:
            adjusted = stage2_model.adjust_line(line=line, base_prob=fair, context=run_env_context)
            stage2_delta = _logit(adjusted) - _logit(fair)
            fair = float(adjusted)
        except Exception:
            stage2_delta = 0.0

    team_delta = 0.0
    if offense_model is not None:
        try:
            date_key = game_date[:10]
            fair = float(
                offense_model.adjust_fv(
                    base_fv=fair,
                    away_abbrev=away_abbrev,
                    home_abbrev=home_abbrev,
                    game_date=date_key,
                    inning=inning,
                )
            )
            team_delta = float(
                offense_model.get_matchup_delta(
                    away_abbrev,
                    home_abbrev,
                    date_key,
                    inning,
                )
            )
        except Exception:
            team_delta = 0.0

    empirical = None
    try:
        code = str(int(round(float(line) * 10)))
        cell = cache.cells.get(meta.get("state_cell_key")) if isinstance(meta, Mapping) else None
        if isinstance(cell, Mapping) and cell.get(f"o{code}") is not None:
            empirical = float(cell.get(f"o{code}"))
    except Exception:
        empirical = None

    return {
        "fair_over_base_poisson": float(base),
        "fair_over_base_empirical": empirical,
        "fair_over": _clip_prob(fair),
        "fair_under": 1.0 - _clip_prob(fair),
        "stage2_run_env_delta": stage2_delta,
        "team_offense_delta": team_delta,
        "fv_used_fallback": bool(meta.get("used_fallback")) if isinstance(meta, Mapping) else None,
        "fv_state_fallback_level": meta.get("state_fallback_level") if isinstance(meta, Mapping) else None,
        "fv_state_fallback_label": meta.get("state_fallback_label") if isinstance(meta, Mapping) else None,
        "fv_line_fallback_mode": meta.get("line_fallback_mode") if isinstance(meta, Mapping) else None,
        "fv_line_source_key": meta.get("line_source_key") if isinstance(meta, Mapping) else None,
    }


def _build_row(
    *,
    row_id: str,
    session_date: str,
    game_dir: Path,
    line: str,
    over_row: Dict[str, Any],
    under_row: Dict[str, Any],
    pair_lag: float,
    cache: OUCache,
    final_scores: Mapping[int, Dict[str, int]],
    stage2_model: Any,
    run_env_context: Any,
    offense_model: Any,
) -> Optional[Dict[str, Any]]:
    meta = _game_meta_fields(game_dir)
    game_pk = _safe_int(over_row.get("game_pk")) or int(meta["game_pk"])
    away_abbrev = str(over_row.get("away_abbrev") or meta["away_abbrev"])
    home_abbrev = str(over_row.get("home_abbrev") or meta["home_abbrev"])
    game_date = str(meta.get("game_date") or session_date)
    tokens = (meta.get("tokens_by_line") or {}).get(line, {})

    over_bid = _safe_float(over_row.get("_best_bid"))
    over_ask = _safe_float(over_row.get("_best_ask"))
    over_ltp = _safe_float(over_row.get("_ltp"))
    under_bid = _safe_float(under_row.get("_best_bid"))
    under_ask = _safe_float(under_row.get("_best_ask"))
    under_ltp = _safe_float(under_row.get("_ltp"))
    if over_ask is None or under_ask is None:
        return None

    fair = _lookup_fair_over(
        cache=cache,
        over_row=over_row,
        line=line,
        game_date=game_date,
        away_abbrev=away_abbrev,
        home_abbrev=home_abbrev,
        stage2_model=stage2_model,
        run_env_context=run_env_context,
        offense_model=offense_model,
    )
    if fair.get("fair_over") is None:
        return None

    over_mid = _mid(over_bid, over_ask, over_ltp)
    under_mid = _mid(under_bid, under_ask, under_ltp)
    mid_sum = (over_mid or 0.0) + (under_mid or 0.0) if over_mid is not None and under_mid is not None else None
    over_mid_no_vig = over_mid / mid_sum if mid_sum and mid_sum > 0 else None
    under_mid_no_vig = under_mid / mid_sum if mid_sum and mid_sum > 0 else None

    fair_over = _safe_float(fair.get("fair_over"))
    fair_under = _safe_float(fair.get("fair_under"))
    if fair_over is None or fair_under is None:
        return None

    over_edge = fair_over - over_ask
    under_edge = fair_under - under_ask
    if over_edge >= under_edge and over_edge > 0:
        best_side = "over"
        best_edge = over_edge
    elif under_edge > 0:
        best_side = "under"
        best_edge = under_edge
    else:
        best_side = "none"
        best_edge = max(over_edge, under_edge)

    away_score = _safe_int(over_row.get("away_score")) or 0
    home_score = _safe_int(over_row.get("home_score")) or 0
    inning = _safe_int(over_row.get("inning"))
    inning_state = str(over_row.get("inning_state") or "")
    remaining = compute_remaining_opportunity_fields(
        away_score=away_score,
        home_score=home_score,
        inning=inning,
        inning_state=inning_state,
    )
    scoring_path = compute_scoring_path_fields(
        away_inning_runs=over_row.get("away_inning_runs") or (),
        home_inning_runs=over_row.get("home_inning_runs") or (),
        current_inning=inning,
    )

    final = final_scores.get(game_pk)
    line_value = _safe_float(line)
    target_over_win: Optional[int] = None
    if final and line_value is not None:
        target_over_win = 1 if final["final_total"] > line_value else 0
    target_under_win = 1 - target_over_win if target_over_win is not None else None

    out: Dict[str, Any] = {
        "row_id": row_id,
        "schema_version": 1,
        "source": "raw_polymarket_ticks",
        "session_date": session_date,
        "ts": over_row.get("ts"),
        "pair_lag_seconds": round(float(pair_lag), 3),
        "game_dir_name": game_dir.name,
        "game_pk": game_pk,
        "away_abbrev": away_abbrev,
        "home_abbrev": home_abbrev,
        "line": line,
        "market_id": tokens.get("market_id") or over_row.get("market_id"),
        "over_token_id": tokens.get("over_token_id") or over_row.get("token_id"),
        "under_token_id": tokens.get("under_token_id") or under_row.get("token_id"),
        "game_status": over_row.get("game_status"),
        "game_detailed_status": over_row.get("game_detailed_status"),
        "inning": inning,
        "inning_state": inning_state,
        "outs": _safe_int(over_row.get("outs")),
        "balls": _safe_int(over_row.get("balls")),
        "strikes": _safe_int(over_row.get("strikes")),
        "runners_on": _safe_int(over_row.get("runners_on")) or 0,
        "away_score": away_score,
        "home_score": home_score,
        "current_total": away_score + home_score,
        "over_bid": over_bid,
        "over_ask": over_ask,
        "over_mid": over_mid,
        "over_ltp": over_ltp,
        "under_bid": under_bid,
        "under_ask": under_ask,
        "under_mid": under_mid,
        "under_ltp": under_ltp,
        "over_spread": (over_ask - over_bid) if over_bid is not None and over_ask is not None else None,
        "under_spread": (under_ask - under_bid) if under_bid is not None and under_ask is not None else None,
        "over_under_ask_sum": over_ask + under_ask,
        "over_under_bid_sum": (
            over_bid + under_bid if over_bid is not None and under_bid is not None else None
        ),
        "over_mid_no_vig": over_mid_no_vig,
        "under_mid_no_vig": under_mid_no_vig,
        "over_edge_to_ask": over_edge,
        "under_edge_to_ask": under_edge,
        "over_edge_to_mid_no_vig": (
            fair_over - over_mid_no_vig if over_mid_no_vig is not None else None
        ),
        "under_edge_to_mid_no_vig": (
            fair_under - under_mid_no_vig if under_mid_no_vig is not None else None
        ),
        "over_market_logit_residual": _logit_residual(fair_over, over_ask),
        "under_market_logit_residual": _logit_residual(fair_under, under_ask),
        "best_side_by_edge": best_side,
        "best_edge_to_ask": best_edge,
        "label_final_available": bool(final and line_value is not None),
        "final_away": final.get("final_away") if final else None,
        "final_home": final.get("final_home") if final else None,
        "final_total": final.get("final_total") if final else None,
        "target_over_win": target_over_win,
        "target_under_win": target_under_win,
        "target_over_taker_profit_units": _profit_units(target_over_win, over_ask),
        "target_under_taker_profit_units": _profit_units(target_under_win, under_ask),
    }
    out.update(remaining)
    out.update(scoring_path)
    out.update(fair)
    return out


def build_rows(
    *,
    polymarket_root: Path,
    games_root: Path,
    cache_path: Path,
    stage2_model_path: Path = DEFAULT_STAGE2_MODEL_PATH,
    team_game_log_path: Path = DEFAULT_TEAM_GAME_LOG_PATH,
    min_date: str = "",
    max_date: str = "",
    sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
    max_pair_lag_seconds: float = DEFAULT_MAX_PAIR_LAG_SECONDS,
    disable_stage2: bool = False,
    disable_stage3: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    warnings: List[str] = []
    cache = OUCache(cache_path)
    final_scores = load_final_scores(games_root, min_date=min_date, max_date=max_date)
    stage2_model, run_env_cls = _load_stage2_model(stage2_model_path, disable_stage2, warnings)
    offense_model = _load_team_offense_model(team_game_log_path, disable_stage3, warnings)

    rows: List[Dict[str, Any]] = []
    counts: Counter = Counter()
    per_date_counts: Counter = Counter()
    for session_date, over_path, under_path in iter_game_line_files(polymarket_root, min_date, max_date):
        counts["line_pairs_seen"] += 1
        line = _line_from_over_path(over_path)
        game_dir = over_path.parent
        over_rows = load_tick_rows(over_path)
        under_rows = load_tick_rows(under_path)
        if not over_rows or not under_rows:
            counts["line_pairs_missing_valid_ticks"] += 1
            continue
        under_epochs = [float(r["_ts_epoch"]) for r in under_rows]

        run_env_context = None
        if stage2_model is not None and run_env_cls is not None:
            meta = _game_meta_fields(game_dir)
            raw_env = load_game_run_env_context(int(meta["game_pk"]), session_date, games_root=games_root)
            if raw_env is not None:
                try:
                    run_env_context = run_env_cls.from_game_data(raw_env, year=session_date[:4])
                except Exception:
                    run_env_context = None

        last_emit_epoch: Optional[float] = None
        for over_row in over_rows:
            target_epoch = float(over_row["_ts_epoch"])
            if last_emit_epoch is not None and target_epoch - last_emit_epoch < sample_seconds:
                counts["ticks_skipped_sample_interval"] += 1
                continue
            under_row, pair_lag = _closest_by_ts(target_epoch, under_rows, under_epochs)
            if under_row is None or pair_lag is None or pair_lag > max_pair_lag_seconds:
                counts["ticks_skipped_no_under_pair"] += 1
                continue
            seq = per_date_counts[session_date] + 1
            row_id = f"{session_date}_{over_row.get('game_pk')}_{line}_{seq:06d}"
            row = _build_row(
                row_id=row_id,
                session_date=session_date,
                game_dir=game_dir,
                line=line,
                over_row=over_row,
                under_row=under_row,
                pair_lag=pair_lag,
                cache=cache,
                final_scores=final_scores,
                stage2_model=stage2_model,
                run_env_context=run_env_context,
                offense_model=offense_model,
            )
            if row is None:
                counts["ticks_skipped_no_fair_value"] += 1
                continue
            rows.append(row)
            last_emit_epoch = target_epoch
            per_date_counts[session_date] += 1
            counts["rows"] += 1

    rows.sort(key=lambda r: (str(r.get("session_date")), str(r.get("ts")), str(r.get("row_id"))))
    manifest = {
        "generated_at_utc": _now_iso(),
        "schema_version": 1,
        "config": {
            "polymarket_root": str(polymarket_root),
            "games_root": str(games_root),
            "cache_path": str(cache_path),
            "stage2_model_path": str(stage2_model_path),
            "team_game_log_path": str(team_game_log_path),
            "min_date": min_date or None,
            "max_date": max_date or None,
            "sample_seconds": sample_seconds,
            "max_pair_lag_seconds": max_pair_lag_seconds,
            "disable_stage2": disable_stage2,
            "disable_stage3": disable_stage3,
        },
        "counts": dict(counts),
        "rows_by_date": dict(sorted(per_date_counts.items())),
        "rows_by_best_side": dict(sorted(Counter(str(r.get("best_side_by_edge")) for r in rows).items())),
        "final_label_rows": sum(1 for r in rows if r.get("label_final_available")),
        "warnings": warnings,
    }
    return rows, manifest


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]], columns: Sequence[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps({c: row.get(c) for c in columns}) + "\n")


def _write_csv(path: Path, rows: Sequence[Dict[str, Any]], columns: Sequence[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c) for c in columns})


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    if not args.cache_path.exists():
        raise SystemExit(f"Missing cache path: {args.cache_path}")

    rows, manifest = build_rows(
        polymarket_root=args.polymarket_root,
        games_root=args.games_root,
        cache_path=args.cache_path,
        stage2_model_path=args.stage2_model_path,
        team_game_log_path=args.team_game_log_path,
        min_date=args.min_date,
        max_date=args.max_date,
        sample_seconds=args.sample_seconds,
        max_pair_lag_seconds=args.max_pair_lag_seconds,
        disable_stage2=args.disable_stage2,
        disable_stage3=args.disable_stage3,
    )
    if args.strict and not rows:
        raise SystemExit("Strict mode failed: no side-neutral opportunity rows produced.")

    args.output_root.mkdir(parents=True, exist_ok=True)
    jsonl_path = args.output_root / "side_neutral_opportunities.jsonl"
    csv_path = args.output_root / "side_neutral_opportunities.csv"
    manifest_path = args.output_root / "manifest.json"
    _write_jsonl(jsonl_path, rows, OUTPUT_COLUMNS)
    _write_csv(csv_path, rows, OUTPUT_COLUMNS)
    _write_json(manifest_path, manifest)
    LOGGER.info("Wrote %s", jsonl_path)
    LOGGER.info("Wrote %s", csv_path)
    LOGGER.info("Wrote %s", manifest_path)
    LOGGER.info(
        "Rows=%d final_labels=%d best_side=%s",
        len(rows),
        manifest["final_label_rows"],
        manifest["rows_by_best_side"],
    )


if __name__ == "__main__":
    main()
