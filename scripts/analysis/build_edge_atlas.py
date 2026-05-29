#!/usr/bin/env python3
"""build_edge_atlas.py -- per-cell market-efficiency map (2026-05-27).

Long-form research output: for every (game-state cell × line) where we have
BOTH a 10y MLB empirical Over rate (from cache/mlb_ou_cache.json) AND an
observed Polymarket ask (from any candidate_universe stream), compute:

  bias = market_ask_median - P_empirical

A positive bias means the market historically asked MORE than the 10y
empirical Over rate -- i.e. the Over was overpriced in that cell; Under
was the natural side. A negative bias means the Over was underpriced.

The output ranks cells by a significance proxy = |bias| × sqrt(n_observations)
so cells with both a big bias AND lots of observations float to the top.

This is descriptive only. It maps WHERE structural mispricings have existed
historically; it does NOT predict whether tomorrow's market will repeat them.
Walk-forward / cohort-by-cohort validation is required before any gate change.

Data sources:
  - cache/mlb_ou_cache.json -- per-cell empirical Over rate per line
  - data/<root>/candidate_universe/*_candidates.jsonl -- per-tick (state, ask)
  - data/<root>/candidate_universe/*_outcomes.jsonl  -- realized final totals
                                                       (for the optional overlay)

Defaults read live_trading + paper_A_current + paper_trading roots so we get
the broadest unique-tick coverage without 11x duplication from the multi-
engine fleet (paper_<other> mirrors of the same shared market data).

Output:
  data/analysis_output/edge_atlas/edge_atlas.json
  data/analysis_output/edge_atlas/edge_atlas.md
  data/analysis_output/edge_atlas/edge_atlas_rows.csv
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_PATH = PROJECT_DIR / "cache" / "mlb_ou_cache.json"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "data" / "analysis_output" / "edge_atlas"
DEFAULT_ROOTS = [
    PROJECT_DIR / "data" / "live_trading",
    PROJECT_DIR / "data" / "paper_A_current",
    PROJECT_DIR / "data" / "paper_trading",
]

# Cohort filters. A cell-line pair must clear ALL three to qualify as
# "observed enough to compare on." Tuned to be conservative so the
# top-of-report cells are robust.
MIN_MLB_GAMES_FOR_CELL = 40         # matches cache builder's --min-games
MIN_MARKET_OBSERVATIONS = 10        # at least 10 ask observations in the cell
EXTRAS_INNING_BUCKET = 10           # match cache builder default

# When ranking, cap n in the sqrt(n) so a single high-volume cell can't
# dominate the entire ranking just by having 10k observations vs 50.
RANKING_N_CAP = 1000

# Max plausible bid/ask spread for an observation to count. Above this,
# the ask is typically a stale offer on a thin book (the bot's own
# `gate_wide_spread` skip identifies these in real time). Filtering by
# spread here prevents single observations like ask=0.88 / bid=0.08
# from dragging the median.
MAX_SPREAD_CENTS = 25


# ---------------------------------------------------------------------------
# Cell-key + line-key derivation (mirrors cache/build_mlb_ou_cache.py)
# ---------------------------------------------------------------------------

def line_to_emp_key(line_str: str) -> str:
    """7.5 -> 'o75', 10.5 -> 'o105'. Mirrors cache.line_to_emp_key."""
    return "o" + str(line_str).replace(".", "")


def line_to_poisson_key(line_str: str) -> str:
    """7.5 -> 'po75'."""
    return "po" + str(line_str).replace(".", "")


def normalize_inning_state(state: Optional[str]) -> Optional[str]:
    """Map MLB schedule's 4-value inning_state to T/B half label.

    Top -> T, Bottom -> B. Middle/End are between halves (no batter at the
    plate); we return None and the caller skips the row -- there is no
    matching mlb_ou_cache cell because the cache is keyed on the per-PA
    snapshot before a plate appearance.
    """
    if state is None:
        return None
    s = str(state).strip().lower()
    if s in ("top", "t"):
        return "T"
    if s in ("bottom", "b"):
        return "B"
    return None


def derive_cell_key(row: Dict[str, Any]) -> Optional[str]:
    """Build the mlb_ou_cache cell-key from a candidate row.

    Format: '{away}_{home}_{inning_bucket}_{half}_{outs}_{bases_mask}'

    Returns None when any required field is missing OR when the
    inning_state is Middle/End (no batter at the plate).
    """
    away = row.get("away_score_before")
    home = row.get("home_score_before")
    inning = row.get("inning")
    half = normalize_inning_state(row.get("inning_state"))
    outs = row.get("outs")
    bases = row.get("runners_on")
    if (
        away is None or home is None or inning is None
        or half is None or outs is None or bases is None
    ):
        return None
    try:
        away = int(away)
        home = int(home)
        inning = int(inning)
        outs = int(outs)
        bases = int(bases)
    except (TypeError, ValueError):
        return None
    if outs < 0 or outs > 2 or bases < 0 or bases > 7:
        return None
    ib = inning if inning < EXTRAS_INNING_BUCKET else EXTRAS_INNING_BUCKET
    return f"{away}_{home}_{ib}_{half}_{outs}_{bases}"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_cache(path: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (cells, meta) from mlb_ou_cache.json."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    cells = data.get("cells") or {}
    meta = data.get("meta") or {}
    return cells, meta


def iter_candidate_files(roots: Sequence[Path]) -> Iterable[Path]:
    """Yield every *_candidates.jsonl under any root's candidate_universe/."""
    for root in roots:
        if not root.exists():
            continue
        pattern = str(root / "candidate_universe" / "*_candidates.jsonl")
        for fp in sorted(glob.glob(pattern)):
            yield Path(fp)


def iter_outcome_files(roots: Sequence[Path]) -> Iterable[Path]:
    """Yield every *_outcomes.jsonl (used by the realized-outcomes overlay)."""
    for root in roots:
        if not root.exists():
            continue
        pattern = str(root / "candidate_universe" / "*_outcomes.jsonl")
        for fp in sorted(glob.glob(pattern)):
            yield Path(fp)


def load_outcomes(roots: Sequence[Path]) -> Dict[Tuple[int, str], int]:
    """Map (game_pk, line) -> final_total. Used to verify whether the
    Over actually hit in each game-line we observed market data for."""
    out: Dict[Tuple[int, str], int] = {}
    for fp in iter_outcome_files(roots):
        with open(fp, encoding="utf-8") as f:
            for raw in f:
                if not raw.strip():
                    continue
                try:
                    r = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                gpk = r.get("game_pk")
                line = r.get("line")
                final_total = r.get("final_total")
                if gpk is None or line is None or final_total is None:
                    continue
                try:
                    out[(int(gpk), str(line))] = int(final_total)
                except (TypeError, ValueError):
                    continue
    return out


# ---------------------------------------------------------------------------
# Accumulator: per (cell_key, line) collect ask observations
# ---------------------------------------------------------------------------

@dataclass
class CellLineObs:
    cell_key: str
    line: str
    asks: List[float] = field(default_factory=list)
    game_pks: set = field(default_factory=set)

    def add(self, ask: float, game_pk: Any) -> None:
        self.asks.append(float(ask))
        if game_pk is not None:
            self.game_pks.add(game_pk)


def accumulate_observations(
    roots: Sequence[Path],
    max_files: Optional[int] = None,
) -> Tuple[Dict[Tuple[str, str], CellLineObs], Dict[str, int]]:
    """Walk every candidates.jsonl under `roots` and accumulate ask
    observations indexed by (cell_key, line)."""
    obs: Dict[Tuple[str, str], CellLineObs] = {}
    stats = Counter()
    files = list(iter_candidate_files(roots))
    if max_files is not None and max_files > 0:
        files = files[:max_files]
    for fp in files:
        stats["files_read"] += 1
        with open(fp, encoding="utf-8") as f:
            for raw in f:
                if not raw.strip():
                    continue
                try:
                    r = json.loads(raw)
                except json.JSONDecodeError:
                    stats["malformed_json"] += 1
                    continue
                stats["rows_read"] += 1
                # Filter to OVER side only -- the cache's p_emp is the
                # OVER probability, so UNDER-side decision_asks would be
                # complements (1 - over_ask) and break the bias math.
                # The 2026-05-19 UNDER candidate emission added under-side
                # rows; the OVER stream remains the canonical comparison.
                if r.get("side") != "over":
                    stats["non_over_side_skipped"] += 1
                    continue
                ask = r.get("decision_ask")
                if ask is None:
                    stats["no_decision_ask"] += 1
                    continue
                # Boundary asks (0/1) are exchange "settled" markers, not
                # real quotes. Drop them so the median isn't dragged.
                try:
                    ask_f = float(ask)
                except (TypeError, ValueError):
                    stats["bad_ask"] += 1
                    continue
                if ask_f <= 0.01 or ask_f >= 0.99:
                    stats["boundary_ask"] += 1
                    continue
                # Wide-spread guard: when the book is thin / one-sided
                # the ask is often a stale offer. Bot's gate_wide_spread
                # already skips these in real time; mirror that here.
                best_bid = r.get("best_bid")
                if best_bid is not None:
                    try:
                        bid_f = float(best_bid)
                    except (TypeError, ValueError):
                        bid_f = None
                    if (
                        bid_f is not None
                        and (ask_f - bid_f) * 100 > MAX_SPREAD_CENTS
                    ):
                        stats["wide_spread_skipped"] += 1
                        continue
                # Only include rows where the bot did NOT infer a score
                # event (inferred_runs is None / 0). When inference fires,
                # the `*_score_before` fields are the PRE-inference state
                # that lags the market's belief -- joining those ticks to
                # the cache cell measures the inference lag, not market
                # mispricing. The 47k of 51k "clean" rows are plenty.
                ir = r.get("inferred_runs")
                if ir is not None and ir != 0:
                    stats["inference_active_skipped"] += 1
                    continue
                cell_key = derive_cell_key(r)
                if cell_key is None:
                    stats["no_cell_key"] += 1
                    continue
                line = r.get("line")
                if line is None:
                    stats["no_line"] += 1
                    continue
                key = (cell_key, str(line))
                if key not in obs:
                    obs[key] = CellLineObs(cell_key=cell_key, line=str(line))
                obs[key].add(ask_f, r.get("game_pk"))
                stats["observations_kept"] += 1
    return obs, dict(stats)


# ---------------------------------------------------------------------------
# Atlas row builder: join market observations to cache empirical
# ---------------------------------------------------------------------------

@dataclass
class AtlasRow:
    cell_key: str
    line: str
    inning: int
    half: str
    outs: int
    bases_mask: int
    away_score: int
    home_score: int
    score_diff: int
    cell_label: Optional[str]
    p_empirical: Optional[float]
    p_poisson: Optional[float]
    mlb_n_games: int
    mlb_n_samples: int
    market_n_ticks: int
    market_n_games: int
    market_ask_median: Optional[float]
    market_ask_p25: Optional[float]
    market_ask_p75: Optional[float]
    market_ask_min: Optional[float]
    market_ask_max: Optional[float]
    bias_market_minus_empirical: Optional[float]
    abs_bias: Optional[float]
    significance: Optional[float]  # |bias| * sqrt(min(n_ticks, RANKING_N_CAP))
    # Realized-outcome overlay (when we have outcomes joined):
    n_settled_game_lines: int = 0
    realized_over_hits: int = 0
    realized_over_rate: Optional[float] = None
    realized_minus_empirical: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cell_key": self.cell_key,
            "line": self.line,
            "inning": self.inning,
            "half": self.half,
            "outs": self.outs,
            "bases_mask": self.bases_mask,
            "away_score": self.away_score,
            "home_score": self.home_score,
            "score_diff": self.score_diff,
            "cell_label": self.cell_label,
            "p_empirical": self.p_empirical,
            "p_poisson": self.p_poisson,
            "mlb_n_games": self.mlb_n_games,
            "mlb_n_samples": self.mlb_n_samples,
            "market_n_ticks": self.market_n_ticks,
            "market_n_games": self.market_n_games,
            "market_ask_median": self.market_ask_median,
            "market_ask_p25": self.market_ask_p25,
            "market_ask_p75": self.market_ask_p75,
            "market_ask_min": self.market_ask_min,
            "market_ask_max": self.market_ask_max,
            "bias_market_minus_empirical": self.bias_market_minus_empirical,
            "abs_bias": self.abs_bias,
            "significance": self.significance,
            "n_settled_game_lines": self.n_settled_game_lines,
            "realized_over_hits": self.realized_over_hits,
            "realized_over_rate": self.realized_over_rate,
            "realized_minus_empirical": self.realized_minus_empirical,
        }


def _percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolation percentile (q in [0, 1])."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(s[lo])
    frac = pos - lo
    return float(s[lo] + (s[hi] - s[lo]) * frac)


def _parse_cell_key(cell_key: str) -> Optional[Tuple[int, int, int, str, int, int]]:
    parts = cell_key.split("_")
    if len(parts) != 6:
        return None
    try:
        away = int(parts[0]); home = int(parts[1]); ib = int(parts[2])
        outs = int(parts[4]); bases = int(parts[5])
        half = parts[3]
        if half not in ("T", "B"):
            return None
    except ValueError:
        return None
    return away, home, ib, half, outs, bases


def build_atlas_rows(
    obs: Dict[Tuple[str, str], CellLineObs],
    cells: Dict[str, Any],
    outcomes: Optional[Dict[Tuple[int, str], int]] = None,
) -> List[AtlasRow]:
    rows: List[AtlasRow] = []
    for (cell_key, line), o in obs.items():
        cell = cells.get(cell_key)
        emp_key = line_to_emp_key(line)
        poi_key = line_to_poisson_key(line)
        parsed = _parse_cell_key(cell_key)
        if parsed is None:
            continue
        away, home, ib, half, outs, bases = parsed
        if not o.asks:
            continue
        # Cache lookup: cell + line might be missing if MLB sample was thin.
        p_emp = None
        p_poi = None
        mlb_n_games = 0
        mlb_n_samples = 0
        cell_label = None
        if cell is not None:
            p_emp = cell.get(emp_key)
            p_poi = cell.get(poi_key)
            mlb_n_games = int(cell.get("n") or 0)
            mlb_n_samples = int(cell.get("n_samples") or 0)
            cell_label = cell.get("label")

        med = float(median(o.asks))
        p25 = _percentile(o.asks, 0.25)
        p75 = _percentile(o.asks, 0.75)

        bias: Optional[float] = None
        sig: Optional[float] = None
        abs_bias: Optional[float] = None
        if p_emp is not None:
            bias = round(med - float(p_emp), 4)
            abs_bias = round(abs(bias), 4)
            capped_n = min(len(o.asks), RANKING_N_CAP)
            sig = round(abs_bias * math.sqrt(capped_n), 4)

        # Realized-outcomes overlay: did the games we observed in this
        # cell actually go Over the line? Note: this conflates "cell at
        # tick observation time" with "game final total" -- a single
        # game contributes to MANY cells (each PA in the game). The
        # rate here is "of the games we saw at least one tick from in
        # THIS cell, how many ended Over?" -- a useful but noisy signal.
        n_settled = 0
        over_hits = 0
        realized_rate: Optional[float] = None
        realized_minus_emp: Optional[float] = None
        if outcomes is not None:
            try:
                threshold = int(float(line) + 0.5)
            except (TypeError, ValueError):
                threshold = None
            if threshold is not None:
                for gpk in o.game_pks:
                    ft = outcomes.get((int(gpk), str(line)))
                    if ft is None:
                        continue
                    n_settled += 1
                    if ft >= threshold:
                        over_hits += 1
                if n_settled > 0:
                    realized_rate = round(over_hits / n_settled, 4)
                    if p_emp is not None:
                        realized_minus_emp = round(
                            realized_rate - float(p_emp), 4,
                        )

        rows.append(AtlasRow(
            cell_key=cell_key,
            line=line,
            inning=ib,
            half=half,
            outs=outs,
            bases_mask=bases,
            away_score=away,
            home_score=home,
            score_diff=away - home,
            cell_label=cell_label,
            p_empirical=float(p_emp) if p_emp is not None else None,
            p_poisson=float(p_poi) if p_poi is not None else None,
            mlb_n_games=mlb_n_games,
            mlb_n_samples=mlb_n_samples,
            market_n_ticks=len(o.asks),
            market_n_games=len(o.game_pks),
            market_ask_median=round(med, 4),
            market_ask_p25=round(p25, 4) if p25 is not None else None,
            market_ask_p75=round(p75, 4) if p75 is not None else None,
            market_ask_min=round(min(o.asks), 4),
            market_ask_max=round(max(o.asks), 4),
            bias_market_minus_empirical=bias,
            abs_bias=abs_bias,
            significance=sig,
            n_settled_game_lines=n_settled,
            realized_over_hits=over_hits,
            realized_over_rate=realized_rate,
            realized_minus_empirical=realized_minus_emp,
        ))
    return rows


# ---------------------------------------------------------------------------
# Summary slicing
# ---------------------------------------------------------------------------

def _inning_band(ib: int) -> str:
    if ib <= 3:
        return "inn_1-3"
    if ib <= 5:
        return "inn_4-5"
    if ib <= 7:
        return "inn_6-7"
    if ib <= 9:
        return "inn_8-9"
    return "inn_10+"


def _score_diff_band(diff: int) -> str:
    if diff <= -4:
        return "trailing>=4"
    if diff <= -1:
        return "trailing_1-3"
    if diff == 0:
        return "tied"
    if diff <= 3:
        return "leading_1-3"
    return "leading>=4"


def summarize_by(
    rows: Sequence[AtlasRow],
    key_fn,
    *,
    min_qualifying_rows: int = 5,
) -> List[Dict[str, Any]]:
    """Aggregate atlas rows by an arbitrary key. Reports stake-weighted-
    style stats so cells with more market observations carry more weight.
    """
    buckets: Dict[Any, List[AtlasRow]] = defaultdict(list)
    for r in rows:
        if r.bias_market_minus_empirical is None:
            continue
        if r.mlb_n_games < MIN_MLB_GAMES_FOR_CELL:
            continue
        if r.market_n_ticks < MIN_MARKET_OBSERVATIONS:
            continue
        buckets[key_fn(r)].append(r)

    out: List[Dict[str, Any]] = []
    for bucket, items in buckets.items():
        if len(items) < min_qualifying_rows:
            continue
        biases = [r.bias_market_minus_empirical for r in items]
        weighted_bias_num = sum(
            (r.bias_market_minus_empirical or 0.0) * r.market_n_ticks
            for r in items
        )
        weighted_bias_den = sum(r.market_n_ticks for r in items)
        weighted_bias = (
            weighted_bias_num / weighted_bias_den
            if weighted_bias_den > 0 else None
        )
        out.append({
            "bucket": str(bucket),
            "n_cells": len(items),
            "total_market_ticks": sum(r.market_n_ticks for r in items),
            "mean_bias": round(sum(biases) / len(biases), 4),
            "median_bias": round(median(biases), 4),
            "stake_weighted_bias": round(weighted_bias, 4) if weighted_bias is not None else None,
            "cells_with_overpriced_over": sum(1 for b in biases if b > 0.01),
            "cells_with_underpriced_over": sum(1 for b in biases if b < -0.01),
            "cells_within_1pp": sum(1 for b in biases if abs(b) <= 0.01),
        })
    out.sort(key=lambda r: r["bucket"])
    return out


# ---------------------------------------------------------------------------
# Build report
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z",
    )


def build_atlas_payload(
    cache_path: Path,
    roots: Sequence[Path],
    *,
    max_files: Optional[int] = None,
) -> Dict[str, Any]:
    cells, cache_meta = load_cache(cache_path)
    obs, walk_stats = accumulate_observations(roots, max_files=max_files)
    outcomes = load_outcomes(roots)
    rows = build_atlas_rows(obs, cells, outcomes=outcomes)

    qualifying = [
        r for r in rows
        if r.mlb_n_games >= MIN_MLB_GAMES_FOR_CELL
        and r.market_n_ticks >= MIN_MARKET_OBSERVATIONS
        and r.p_empirical is not None
    ]
    qualifying.sort(key=lambda r: r.significance or 0.0, reverse=True)

    # Top mispricings in each direction.
    top_overall = [r.to_dict() for r in qualifying[:25]]
    top_overpriced = [
        r.to_dict() for r in sorted(
            [q for q in qualifying if (q.bias_market_minus_empirical or 0) > 0],
            key=lambda r: r.significance or 0.0,
            reverse=True,
        )[:15]
    ]
    top_underpriced = [
        r.to_dict() for r in sorted(
            [q for q in qualifying if (q.bias_market_minus_empirical or 0) < 0],
            key=lambda r: r.significance or 0.0,
            reverse=True,
        )[:15]
    ]

    # Aggregate slices.
    by_inning = summarize_by(qualifying, lambda r: _inning_band(r.inning))
    by_score_diff = summarize_by(qualifying, lambda r: _score_diff_band(r.score_diff))
    by_line = summarize_by(qualifying, lambda r: r.line)
    by_outs = summarize_by(qualifying, lambda r: f"outs_{r.outs}")
    by_bases = summarize_by(qualifying, lambda r: f"bases_{r.bases_mask:03b}")

    # Coverage gap: cells in the cache we never observed in market data.
    observed_cells = {r.cell_key for r in rows}
    unobserved_cells = [c for c in cells.keys() if c not in observed_cells]

    headline = {
        "n_cache_cells": len(cells),
        "n_observed_cells": len(observed_cells),
        "n_qualifying_rows": len(qualifying),
        "n_atlas_rows_total": len(rows),
        "n_unobserved_cells": len(unobserved_cells),
        "total_observations": sum(r.market_n_ticks for r in rows),
        "total_unique_game_pks": len({
            gpk for r in rows for gpk in [g for g in [
                r.cell_key]  # placeholder; counted via obs below
            ]
        }),  # NOTE: simpler count below
    }
    # Replace placeholder above with the real distinct-game count from obs.
    unique_gpks = set()
    for o in obs.values():
        unique_gpks.update(o.game_pks)
    headline["total_unique_game_pks"] = len(unique_gpks)

    return {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "active_priority": "Long-form research: edge atlas (2026-05-27)",
        "cache_path": str(cache_path),
        "cache_meta": {
            "history_start_date": cache_meta.get("history_start_date"),
            "history_end_date": cache_meta.get("history_end_date"),
            "seasons": cache_meta.get("seasons"),
            "total_games": cache_meta.get("total_games"),
        },
        "data_roots": [str(r) for r in roots],
        "filters": {
            "min_mlb_games_for_cell": MIN_MLB_GAMES_FOR_CELL,
            "min_market_observations": MIN_MARKET_OBSERVATIONS,
            "ranking_n_cap": RANKING_N_CAP,
        },
        "walk_stats": walk_stats,
        "headline": headline,
        "top_overall_significance": top_overall,
        "top_overpriced_over": top_overpriced,
        "top_underpriced_over": top_underpriced,
        "by_inning_band": by_inning,
        "by_score_diff_band": by_score_diff,
        "by_line": by_line,
        "by_outs": by_outs,
        "by_bases_mask": by_bases,
        "rows": [r.to_dict() for r in rows],
    }


# ---------------------------------------------------------------------------
# Markdown render
# ---------------------------------------------------------------------------

def _fmt_pct(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _fmt_signed_pct(v: Optional[float], digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def render_markdown(payload: Dict[str, Any]) -> str:
    out: List[str] = []
    out.append("# Edge Atlas — per-cell market efficiency map\n")
    out.append(f"_Generated {payload['generated_at_utc']}._\n")
    cm = payload.get("cache_meta") or {}
    out.append(
        f"**Cache history:** {cm.get('history_start_date')} → "
        f"{cm.get('history_end_date')} ({cm.get('total_games')} games "
        f"across seasons {cm.get('seasons')})\n"
    )
    out.append("**Data roots walked:**\n")
    for root in payload.get("data_roots") or []:
        out.append(f"- `{root}`")
    out.append("")
    h = payload["headline"]
    out.append(
        "**Coverage:** "
        f"{h['n_observed_cells']}/{h['n_cache_cells']} cache cells observed "
        f"({100 * h['n_observed_cells'] / max(1, h['n_cache_cells']):.1f}%). "
        f"{h['n_qualifying_rows']} (cell × line) pairs cleared the "
        f"{MIN_MLB_GAMES_FOR_CELL}-game + {MIN_MARKET_OBSERVATIONS}-tick floor. "
        f"{h['total_observations']} unique market ticks across "
        f"{h['total_unique_game_pks']} distinct games.\n"
    )

    # Top significance (either direction)
    out.append("## Top 25 mispricings (by |bias| × √n)\n")
    out.append(
        "_Positive bias = market ask was HIGHER than 10y empirical "
        "(Over overpriced → Under was the cheap side). Negative = opposite._\n"
    )
    out.append("| Cell label | Line | n ticks | n games | P_emp | Median ask | Bias | Significance | Realized rate (n) |")
    out.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    for r in payload["top_overall_significance"]:
        realized = (
            f"{_fmt_pct(r['realized_over_rate'])} (n={r['n_settled_game_lines']})"
            if r.get("realized_over_rate") is not None else "—"
        )
        out.append(
            f"| {r['cell_label']} | {r['line']} | {r['market_n_ticks']} | "
            f"{r['market_n_games']} | "
            f"{_fmt_pct(r['p_empirical'])} | {_fmt_pct(r['market_ask_median'])} | "
            f"{_fmt_signed_pct(r['bias_market_minus_empirical'])} | "
            f"{(r['significance'] or 0):.2f} | {realized} |"
        )
    out.append("")

    out.append("## Top 15 overpriced Over (market > empirical → bet UNDER candidate)\n")
    out.append("| Cell label | Line | n ticks | P_emp | Median ask | Bias |")
    out.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for r in payload["top_overpriced_over"]:
        out.append(
            f"| {r['cell_label']} | {r['line']} | {r['market_n_ticks']} | "
            f"{_fmt_pct(r['p_empirical'])} | {_fmt_pct(r['market_ask_median'])} | "
            f"{_fmt_signed_pct(r['bias_market_minus_empirical'])} |"
        )
    out.append("")

    out.append("## Top 15 underpriced Over (market < empirical → bet OVER candidate)\n")
    out.append("| Cell label | Line | n ticks | P_emp | Median ask | Bias |")
    out.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for r in payload["top_underpriced_over"]:
        out.append(
            f"| {r['cell_label']} | {r['line']} | {r['market_n_ticks']} | "
            f"{_fmt_pct(r['p_empirical'])} | {_fmt_pct(r['market_ask_median'])} | "
            f"{_fmt_signed_pct(r['bias_market_minus_empirical'])} |"
        )
    out.append("")

    # Slice summaries
    def _render_slice(title: str, key: str, label: str) -> None:
        slc = payload.get(key) or []
        if not slc:
            return
        out.append(f"## {title}\n")
        out.append(
            f"| {label} | n cells | total ticks | mean bias | "
            "stake-weighted bias | over-priced | under-priced | within 1pp |"
        )
        out.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for s in slc:
            out.append(
                f"| {s['bucket']} | {s['n_cells']} | {s['total_market_ticks']} | "
                f"{_fmt_signed_pct(s['mean_bias'])} | "
                f"{_fmt_signed_pct(s['stake_weighted_bias'])} | "
                f"{s['cells_with_overpriced_over']} | "
                f"{s['cells_with_underpriced_over']} | "
                f"{s['cells_within_1pp']} |"
            )
        out.append("")

    _render_slice("Bias by inning band", "by_inning_band", "Inning band")
    _render_slice("Bias by score diff band (away−home)", "by_score_diff_band", "Score diff")
    _render_slice("Bias by line", "by_line", "Line")
    _render_slice("Bias by outs", "by_outs", "Outs")
    _render_slice("Bias by bases mask", "by_bases_mask", "Bases mask (binary)")

    out.append(
        "## Coverage gap\n"
        f"{h['n_unobserved_cells']} cache cells (of {h['n_cache_cells']} total, "
        f"{100 * h['n_unobserved_cells'] / max(1, h['n_cache_cells']):.1f}%) "
        "had ZERO market ticks observed in the data roots. These are\n"
        "states where 10y MLB history says the cell is real but the market\n"
        "either didn't quote it during our 1mo window or our monitor missed it.\n"
        "Direct future market-monitoring focus here if any of these states\n"
        "have high `n_games` in the cache.\n"
    )

    out.append(
        "## How to read this report\n\n"
        "1. **`bias_market_minus_empirical`** is the headline number: the\n"
        "   median market ask MINUS the 10y empirical Over rate at the\n"
        "   matching cell. +0.05 means the market priced this cell's Over\n"
        "   ~5pp higher than history suggests. The natural side is the\n"
        "   opposite of the bias direction.\n"
        "2. **`significance`** = `|bias| × √(min(n_ticks, RANKING_N_CAP))`.\n"
        "   The cap prevents one high-volume cell from drowning the rest.\n"
        f"   N is capped at {RANKING_N_CAP}.\n"
        "3. **`realized_over_rate`** is the optional outcomes overlay --\n"
        "   it counts, over the games we OBSERVED in this cell during our\n"
        "   ~1mo window, what fraction finished Over the line. n is\n"
        "   intentionally per-game (not per-tick) to keep correlation\n"
        "   honest. A small n_settled with a wide gap to P_empirical is\n"
        "   noise; the persistent direction across many cells is signal.\n"
        "4. **Important caveat**: this is descriptive only. It maps\n"
        "   WHERE structural mispricings have existed historically; it\n"
        "   does NOT predict whether tomorrow's market will repeat them.\n"
        "   Walk-forward / cohort-by-cohort validation is required before\n"
        "   any gate change. Treat as research input, not promotion\n"
        "   evidence.\n"
    )
    return "\n".join(out) + "\n"


def write_rows_csv(payload: Dict[str, Any], csv_path: Path) -> None:
    rows = payload.get("rows") or []
    if not rows:
        csv_path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--cache-path", type=Path, default=DEFAULT_CACHE_PATH,
        help=f"Stage-1 MLB OU cache JSON (default: {DEFAULT_CACHE_PATH}).",
    )
    p.add_argument(
        "--data-root", action="append", type=Path, default=None,
        help=(
            "Repeatable. Root directory under which candidate_universe/ lives. "
            f"Default: {[str(r) for r in DEFAULT_ROOTS]}."
        ),
    )
    p.add_argument(
        "--out-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR}).",
    )
    p.add_argument(
        "--max-files", type=int, default=0,
        help="Cap files walked per root for smoke tests. 0 = no cap.",
    )
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    roots = args.data_root or DEFAULT_ROOTS
    payload = build_atlas_payload(
        args.cache_path,
        roots,
        max_files=(args.max_files or None),
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "edge_atlas.json"
    md_path = args.out_dir / "edge_atlas.md"
    csv_path = args.out_dir / "edge_atlas_rows.csv"
    json_path.write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8",
    )
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    write_rows_csv(payload, csv_path)
    h = payload["headline"]
    print(
        f"Edge atlas: {h['n_observed_cells']}/{h['n_cache_cells']} cells "
        f"observed, {h['n_qualifying_rows']} qualifying (cell × line) pairs, "
        f"{h['total_observations']} unique ticks across "
        f"{h['total_unique_game_pks']} games. Wrote {md_path}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
