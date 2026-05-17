#!/usr/bin/env python3
"""
Analyze Polymarket MLB O/U tick data for price overreactions.

For each scoring event in a monitored game, this script computes:
  - The fair-value change implied by the MLB O/U cache (Stage-1 empirical).
  - The actual market price move in the reaction window after the event.
  - The "overreaction" = actual_move - fair_move.

Large overreactions indicate the market pushed the price further than
historical probabilities justify, presenting potential mean-reversion edges.

Usage:
    python baseball/scripts/analysis/analyze_polymarket_overreactions.py
    python baseball/scripts/analysis/analyze_polymarket_overreactions.py --date 2026-04-05
    python baseball/scripts/analysis/analyze_polymarket_overreactions.py --date 2026-04-05 --min-overreaction 0.03
    python baseball/scripts/analysis/analyze_polymarket_overreactions.py --date 2026-04-05 --pre-event-offset 90 --reaction-window 60
    python baseball/scripts/analysis/analyze_polymarket_overreactions.py --output results.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_POLYMARKET_ROOT = PROJECT_DIR / "data" / "polymarket" / "mlb_ou"
DEFAULT_CACHE_PATH = PROJECT_DIR / "cache" / "mlb_ou_cache.json"
DEFAULT_TEAM_GAME_LOG_PATH = PROJECT_DIR / "cache" / "team_game_log.json"
DEFAULT_STAGE2_MODEL_PATH = PROJECT_DIR / "cache" / "mlb_stage2_run_env.json"
DEFAULT_GAMES_ROOT = PROJECT_DIR / "data" / "games" / "regular"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXTRAS_BUCKET = 10       # innings 10+ all map here
MAX_INDIVIDUAL_SCORE = 10  # max per-team score stored in cache cells
MAX_COMBINED_SCORE = 20    # max combined score stored in cache
BASES_MASK = 0             # no runner info in snapshots; use neutral state

# Poisson-calibrated key prefix per line (e.g. "8.5" -> "po85")
def _line_cache_key(line: str) -> str:
    """Convert a line string like '8.5' to its Poisson cache key 'po85'."""
    try:
        val = float(line)
        code = str(int(val * 10))  # 8.5 -> "85"
        return f"po{code}"
    except Exception:
        return ""


def _line_value_from_cache_key(raw_key: str) -> Optional[float]:
    """Convert cache keys like o85/po85 to 8.5."""
    key = str(raw_key or "").strip().lower()
    if key.startswith("po"):
        key = key[2:]
    elif key.startswith("o"):
        key = key[1:]
    if not key:
        return None
    try:
        return int(key) / 10.0
    except Exception:
        return None


def _clip_prob(p: float) -> float:
    return max(1e-6, min(1.0 - 1e-6, float(p)))


def _logit(p: float) -> float:
    z = _clip_prob(p)
    return math.log(z / (1.0 - z))


def _inv_logit(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


# Map inning_state from MLB API to cache half indicator.
# "Top"    -> top half of inning in progress       -> "T"
# "Middle" -> between top and bottom halves        -> "B" (next half = bottom)
# "Bottom" -> bottom half of inning in progress    -> "B"
# "End"    -> inning complete                      -> "T" next inning (treat as B end)
def _inning_state_to_half(inning_state: str) -> str:
    s = (inning_state or "").strip().lower()
    if s in ("top", ""):
        return "T"
    if s in ("middle", "bottom", "end"):
        return "B"
    return "T"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Tick:
    ts: datetime
    inning: Optional[int]
    inning_state: str
    outs: Optional[int]
    balls: Optional[int]
    strikes: Optional[int]
    runners_on: int  # bitmask: 1=1st, 2=2nd, 4=3rd (0 = empty / legacy tick)
    away_score: Optional[int]
    home_score: Optional[int]
    best_bid: Optional[float]
    best_ask: Optional[float]
    ltp: Optional[float]

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        # One-sided book: use available quote directly (bid or ask).
        # ltp can be stale from much earlier; prefer live quote.
        if self.best_bid is not None:
            return self.best_bid
        if self.best_ask is not None:
            return self.best_ask
        return self.ltp

    @property
    def total_score(self) -> int:
        return (self.away_score or 0) + (self.home_score or 0)


@dataclass
class ScoringEvent:
    tick_index: int          # index in the tick list where score changed
    ts: datetime
    away_before: int
    home_before: int
    away_after: int
    home_after: int
    inning: int
    inning_state: str
    outs: int
    runs_scored: int
    fv_before: Optional[float]   # P(over line | state before)
    fv_after: Optional[float]    # P(over line | state after)
    fv_delta: Optional[float]    # fv_after - fv_before
    mid_before: Optional[float]  # avg mid price in pre-window
    mid_peak: Optional[float]    # peak mid in reaction window
    actual_delta: Optional[float]  # mid_peak - mid_before
    overreaction: Optional[float]  # actual_delta - fv_delta
    edge_side: str               # "OVER" or "UNDER" or ""


@dataclass
class GameAnalysis:
    date: str
    game_pk: int
    away_abbrev: str
    home_abbrev: str
    line: str
    events: List[ScoringEvent] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Cache wrapper
# ---------------------------------------------------------------------------

class OUCache:
    def __init__(self, cache_path: Path):
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        self.cells: Dict[str, dict] = data.get("cells", {})
        meta = data.get("meta", {})
        self.extras_bucket: int = int(meta.get("extras_bucket", EXTRAS_BUCKET))
        self.max_individual: int = MAX_INDIVIDUAL_SCORE
        self.max_combined: int = int(meta.get("max_combined", MAX_COMBINED_SCORE))
        self._line_value_to_key: Dict[float, str] = self._build_line_key_map(meta=meta)
        self._available_line_values: List[float] = sorted(self._line_value_to_key.keys())

    def _cell_key(
        self,
        away_score: int,
        home_score: int,
        inning: int,
        half: str,
        outs: int,
        bases: int = BASES_MASK,
    ) -> str:
        # Clamp individual scores.
        a = min(away_score, self.max_individual)
        h = min(home_score, self.max_individual)
        # Clamp combined score so we don't exceed what cache was built with.
        combined = a + h
        if combined > self.max_combined:
            excess = combined - self.max_combined
            # Reduce both proportionally (simple: split excess from larger).
            if a >= h:
                a = max(0, a - excess)
            else:
                h = max(0, h - excess)
        # Inning bucket.
        inning_bucket = min(inning, self.extras_bucket)
        # Outs must be 0-2.
        o = max(0, min(2, outs))
        return f"{a}_{h}_{inning_bucket}_{half}_{o}_{bases}"

    def _build_line_key_map(self, *, meta: Dict[str, object]) -> Dict[float, str]:
        out: Dict[float, str] = {}

        # Preferred source: cache metadata line map (o65/o75/...).
        lines_meta = meta.get("lines", {}) or {}
        if isinstance(lines_meta, dict):
            for raw_key in lines_meta.keys():
                line_val = _line_value_from_cache_key(str(raw_key))
                if line_val is None:
                    continue
                out[line_val] = f"po{int(round(line_val * 10))}"

        # Fallback: infer line keys from cell payloads (poXX keys).
        if not out:
            for cell in self.cells.values():
                if not isinstance(cell, dict):
                    continue
                for key in cell.keys():
                    if not str(key).startswith("po"):
                        continue
                    line_val = _line_value_from_cache_key(str(key))
                    if line_val is None:
                        continue
                    out[line_val] = str(key)
                if out:
                    break
        return out

    def _line_prob_from_cell(
        self,
        *,
        cell: Dict[str, object],
        requested_line: str,
        requested_line_value: Optional[float],
    ) -> Tuple[Optional[float], Dict[str, object]]:
        """Resolve cell probability for line, with interpolation/extrapolation fallback."""
        requested_key = _line_cache_key(requested_line)
        if not requested_key:
            return None, {"line_fallback_mode": "invalid_line"}

        # Exact cache key hit.
        raw_exact = cell.get(requested_key)
        try:
            if raw_exact is not None:
                return float(raw_exact), {
                    "line_fallback_mode": "exact",
                    "line_source_key": requested_key,
                    "line_source_key_low": requested_key,
                    "line_source_key_high": requested_key,
                }
        except Exception:
            pass

        # Build available per-cell points.
        points: List[Tuple[float, float, str]] = []
        for line_val in self._available_line_values:
            key = self._line_value_to_key.get(line_val)
            if not key:
                continue
            raw = cell.get(key)
            if raw is None:
                continue
            try:
                points.append((line_val, float(raw), key))
            except Exception:
                continue

        if not points:
            return None, {"line_fallback_mode": "no_line_points"}
        points.sort(key=lambda x: x[0])
        target = requested_line_value
        if target is None:
            line_val = _line_value_from_cache_key(requested_key)
            target = line_val if line_val is not None else points[-1][0]

        # Lower than supported range.
        if target <= points[0][0] + 1e-9:
            if len(points) == 1:
                return points[0][1], {
                    "line_fallback_mode": "clamp_low",
                    "line_source_key": points[0][2],
                    "line_source_key_low": points[0][2],
                    "line_source_key_high": points[0][2],
                }
            x0, p0, k0 = points[0]
            x1, p1, k1 = points[1]
            slope = (_logit(p1) - _logit(p0)) / (x1 - x0) if abs(x1 - x0) > 1e-9 else 0.0
            est = _inv_logit(_logit(p0) + slope * (target - x0))
            return est, {
                "line_fallback_mode": "extrapolate_low",
                "line_source_key": k0,
                "line_source_key_low": k0,
                "line_source_key_high": k1,
            }

        # Higher than supported range (e.g. 12.5+ against an 11.5 max cache).
        if target >= points[-1][0] - 1e-9:
            if len(points) == 1:
                return points[-1][1], {
                    "line_fallback_mode": "clamp_high",
                    "line_source_key": points[-1][2],
                    "line_source_key_low": points[-1][2],
                    "line_source_key_high": points[-1][2],
                }
            x0, p0, k0 = points[-2]
            x1, p1, k1 = points[-1]
            slope = (_logit(p1) - _logit(p0)) / (x1 - x0) if abs(x1 - x0) > 1e-9 else 0.0
            est = _inv_logit(_logit(p1) + slope * (target - x1))
            return est, {
                "line_fallback_mode": "extrapolate_high",
                "line_source_key": k1,
                "line_source_key_low": k0,
                "line_source_key_high": k1,
            }

        # Interpolate between nearest lower/upper available line points.
        for i in range(1, len(points)):
            lo_x, lo_p, lo_k = points[i - 1]
            hi_x, hi_p, hi_k = points[i]
            if lo_x <= target <= hi_x:
                if abs(hi_x - lo_x) <= 1e-9:
                    return lo_p, {
                        "line_fallback_mode": "interpolate_flat",
                        "line_source_key": lo_k,
                        "line_source_key_low": lo_k,
                        "line_source_key_high": hi_k,
                    }
                w = (target - lo_x) / (hi_x - lo_x)
                est_logit = _logit(lo_p) + w * (_logit(hi_p) - _logit(lo_p))
                est = _inv_logit(est_logit)
                return est, {
                    "line_fallback_mode": "interpolate",
                    "line_source_key": lo_k,
                    "line_source_key_low": lo_k,
                    "line_source_key_high": hi_k,
                }

        return None, {"line_fallback_mode": "unresolved"}

    def _state_fallback_candidates(
        self,
        *,
        away_score: int,
        home_score: int,
        inning: int,
        half: str,
        outs: int,
        bases: int,
    ) -> List[Tuple[int, str, str]]:
        """Return progressively coarser state keys for sparse-cell fallback."""
        out: List[Tuple[int, str, str]] = []
        seen = set()
        base_inning = max(1, min(int(inning), self.extras_bucket))
        outs_pref = [outs] + [o for o in (0, 1, 2) if o != outs]
        half_pref = [half, ("B" if half == "T" else "T")]

        near_innings: List[int] = []
        for d in (1, 2):
            for cand in (base_inning - d, base_inning + d):
                if 1 <= cand <= self.extras_bucket and cand not in near_innings:
                    near_innings.append(cand)
        far_innings = [
            i
            for i in range(1, self.extras_bucket + 1)
            if i != base_inning and i not in near_innings
        ]
        inning_pref = near_innings + far_innings

        def _add(level: int, label: str, *, inn: int, hf: str, o: int, b: int) -> None:
            key = self._cell_key(
                away_score=away_score,
                home_score=home_score,
                inning=inn,
                half=hf,
                outs=o,
                bases=b,
            )
            if key in seen:
                return
            seen.add(key)
            out.append((level, label, key))

        # Level 0: exact state (highest fidelity).
        _add(0, "exact", inn=base_inning, hf=half, o=outs, b=bases)

        # Level 1: bases-empty, same inning/half/outs.
        if bases != BASES_MASK:
            _add(1, "bases_empty", inn=base_inning, hf=half, o=outs, b=BASES_MASK)

        # Level 2: same inning/half, relax outs (bases empty).
        for o in outs_pref:
            _add(2, "same_inning_half_any_outs", inn=base_inning, hf=half, o=o, b=BASES_MASK)

        # Level 3: same inning, relax half + outs (bases empty).
        for hf in half_pref:
            for o in outs_pref:
                _add(3, "same_inning_any_half_outs", inn=base_inning, hf=hf, o=o, b=BASES_MASK)

        # Level 4: nearby innings, same half, any outs (bases empty).
        for inn in near_innings:
            for o in outs_pref:
                _add(4, "near_inning_same_half", inn=inn, hf=half, o=o, b=BASES_MASK)

        # Level 5: nearby innings, any half/outs (bases empty).
        for inn in near_innings:
            for hf in half_pref:
                for o in outs_pref:
                    _add(5, "near_inning_any_half_outs", inn=inn, hf=hf, o=o, b=BASES_MASK)

        # Level 6: any remaining inning bucket, any half/outs (bases empty).
        for inn in inning_pref:
            for hf in half_pref:
                for o in outs_pref:
                    _add(6, "any_inning_any_half_outs", inn=inn, hf=hf, o=o, b=BASES_MASK)

        return out

    def lookup_with_meta(
        self,
        away_score: int,
        home_score: int,
        inning: int,
        inning_state: str,
        outs: int,
        line: str,
        runners_on: int = BASES_MASK,
    ) -> Tuple[Optional[float], Dict[str, object]]:
        """Lookup with line+state hierarchical fallback metadata."""
        half = _inning_state_to_half(inning_state)
        line_key = _line_cache_key(line)
        line_value: Optional[float]
        try:
            line_value = float(line)
        except Exception:
            line_value = _line_value_from_cache_key(line_key) if line_key else None

        meta: Dict[str, object] = {
            "used_fallback": False,
            "used_state_fallback": False,
            "used_line_fallback": False,
            "state_fallback_level": None,
            "state_fallback_label": None,
            "state_cell_key": None,
            "line_requested_key": line_key,
            "line_requested_value": line_value,
            "line_fallback_mode": "none",
            "line_source_key": None,
            "line_source_key_low": None,
            "line_source_key_high": None,
        }
        if not line_key:
            meta["line_fallback_mode"] = "invalid_line"
            return None, meta

        candidates = self._state_fallback_candidates(
            away_score=away_score,
            home_score=home_score,
            inning=inning,
            half=half,
            outs=outs,
            bases=runners_on,
        )
        for level, label, key in candidates:
            cell = self.cells.get(key)
            if not isinstance(cell, dict):
                continue
            fv, line_meta = self._line_prob_from_cell(
                cell=cell,
                requested_line=line,
                requested_line_value=line_value,
            )
            if fv is None:
                continue
            mode = str(line_meta.get("line_fallback_mode") or "none")
            used_state = level > 0
            used_line = mode != "exact"
            out_meta = dict(meta)
            out_meta.update(line_meta)
            out_meta.update(
                {
                    "used_state_fallback": used_state,
                    "used_line_fallback": used_line,
                    "used_fallback": bool(used_state or used_line),
                    "state_fallback_level": level,
                    "state_fallback_label": label,
                    "state_cell_key": key,
                }
            )
            return fv, out_meta

        meta["line_fallback_mode"] = "no_state_match"
        return None, meta

    def lookup(
        self,
        away_score: int,
        home_score: int,
        inning: int,
        inning_state: str,
        outs: int,
        line: str,
        runners_on: int = BASES_MASK,
    ) -> Optional[float]:
        """Return P(over line | game state), with sparse-cell fallback support."""
        fv, _meta = self.lookup_with_meta(
            away_score=away_score,
            home_score=home_score,
            inning=inning,
            inning_state=inning_state,
            outs=outs,
            line=line,
            runners_on=runners_on,
        )
        return fv


# ---------------------------------------------------------------------------
# Tick loading
# ---------------------------------------------------------------------------

def _parse_ts(ts_str: str) -> datetime:
    s = ts_str.rstrip("Z")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def load_ticks(jsonl_path: Path) -> List[Tick]:
    """Load all valid ticks from a JSONL file."""
    ticks: List[Tick] = []
    with open(jsonl_path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                r = json.loads(raw)
            except json.JSONDecodeError:
                continue
            book = r.get("book", {}) or {}
            if not book.get("ok", False):
                continue
            ticks.append(
                Tick(
                    ts=_parse_ts(r.get("ts", "")),
                    inning=r.get("inning"),
                    inning_state=str(r.get("inning_state") or ""),
                    outs=r.get("outs"),
                    balls=r.get("balls"),
                    strikes=r.get("strikes"),
                    runners_on=int(r.get("runners_on") or 0),
                    away_score=r.get("away_score"),
                    home_score=r.get("home_score"),
                    best_bid=book.get("best_bid"),
                    best_ask=book.get("best_ask"),
                    ltp=book.get("ltp"),
                )
            )
    ticks.sort(key=lambda t: t.ts)
    return ticks


# ---------------------------------------------------------------------------
# Overreaction analysis
# ---------------------------------------------------------------------------

def _avg_mid(ticks: List[Tick]) -> Optional[float]:
    mids = [t.mid for t in ticks if t.mid is not None]
    return sum(mids) / len(mids) if mids else None


def _peak_mid_change(ticks: List[Tick], mid_before: Optional[float]) -> Optional[float]:
    """Return the largest absolute mid deviation from mid_before in ticks."""
    if mid_before is None:
        return None
    mids = [t.mid for t in ticks if t.mid is not None]
    if not mids:
        return None
    # Return the max-magnitude signed delta (preserve direction).
    deltas = [m - mid_before for m in mids]
    if not deltas:
        return None
    # Use the delta with max absolute magnitude.
    return max(deltas, key=abs)


def analyze_line(
    ticks: List[Tick],
    line: str,
    cache: OUCache,
    pre_event_offset_s: float = 90.0,
    pre_window_secs: float = 30.0,
    reaction_window_secs: float = 60.0,
    min_overreaction: float = 0.03,
    away_abbrev: str = "",
    home_abbrev: str = "",
    game_date: str = "",
    team_offense_model=None,   # Optional[TeamOffenseModel]
    stage2_model=None,         # Optional[Stage2RunEnvModel]
    stage2_context=None,       # Optional[RunEnvContext]
) -> List[ScoringEvent]:
    """
    Detect scoring events in the tick series and measure price overreactions.

    Polymarket front-runs the MLB API by ~65-80s as live attendees trade
    immediately when runs score. To avoid capturing an already-moved price as
    the baseline, we anchor the pre-event window to ticks that fall in the
    interval [event_ts - pre_event_offset_s - pre_window_secs,
               event_ts - pre_event_offset_s].
    The default offset (90s) sits safely before the observed front-run window.

    Args:
        ticks: Chronologically sorted tick snapshots for a single line+side.
        line: O/U line string (e.g. "8.5").
        cache: Loaded MLB O/U cache.
        pre_event_offset_s: Seconds before the MLB API score change to anchor
            the baseline window. Should exceed the Polymarket front-run lag
            (observed ~65-80s); default 90s.
        pre_window_secs: Width of the baseline averaging window in seconds
            (default 30s).
        reaction_window_secs: Seconds after event to observe market reaction.
        min_overreaction: Minimum |overreaction| to include in output (abs cents).

    Returns:
        List of ScoringEvent objects, one per scoring event.
    """
    events: List[ScoringEvent] = []

    for i, tick in enumerate(ticks):
        if i == 0:
            continue
        prev = ticks[i - 1]

        # Need valid score data on both sides.
        if tick.away_score is None or tick.home_score is None:
            continue
        if prev.away_score is None or prev.home_score is None:
            continue
        if tick.inning is None or tick.outs is None:
            continue

        # Check for a scoring event.
        if tick.total_score <= prev.total_score:
            continue

        runs_scored = tick.total_score - prev.total_score

        # Pre-event baseline: ticks in the window ending `pre_event_offset_s`
        # before the MLB API score change. This avoids capturing prices that
        # have already moved due to Polymarket front-running (~65-80s lead).
        event_ts = tick.ts
        baseline_end = event_ts - timedelta(seconds=pre_event_offset_s)
        baseline_start = baseline_end - timedelta(seconds=pre_window_secs)
        pre_ticks = [
            t for t in ticks[:i]
            if baseline_start <= t.ts <= baseline_end
        ]
        mid_before = _avg_mid(pre_ticks)

        # Reaction window: ticks in the next `reaction_window_secs` seconds.
        event_ts = tick.ts
        reaction_ticks = []
        for j in range(i, len(ticks)):
            dt = (ticks[j].ts - event_ts).total_seconds()
            if dt > reaction_window_secs:
                break
            reaction_ticks.append(ticks[j])

        mid_peak_delta = _peak_mid_change(reaction_ticks, mid_before)
        mid_peak = (mid_before + mid_peak_delta) if (mid_before is not None and mid_peak_delta is not None) else None

        # Fair value before (state just before the score changed).
        fv_before = cache.lookup(
            away_score=prev.away_score,
            home_score=prev.home_score,
            inning=prev.inning if prev.inning is not None else tick.inning,
            inning_state=prev.inning_state if prev.inning_state else tick.inning_state,
            outs=prev.outs if prev.outs is not None else tick.outs,
            line=line,
            runners_on=prev.runners_on,
        )

        # Fair value after (use state from the scoring tick).
        fv_after = cache.lookup(
            away_score=tick.away_score,
            home_score=tick.home_score,
            inning=tick.inning,
            inning_state=tick.inning_state,
            outs=tick.outs,
            line=line,
            runners_on=tick.runners_on,
        )

        # [Stage-2] Apply park/weather run-environment adjustment.
        if stage2_model is not None and stage2_context is not None:
            if fv_before is not None:
                fv_before = stage2_model.adjust_line(
                    line=line, base_prob=fv_before, context=stage2_context
                )
            if fv_after is not None:
                fv_after = stage2_model.adjust_line(
                    line=line, base_prob=fv_after, context=stage2_context
                )

        # [Stage-3] Apply team offense adjustment to both before/after FVs.
        # This corrects systematic FV underestimation for high-offense matchups.
        if team_offense_model is not None and away_abbrev and home_abbrev and game_date:
            inning_for_adj = tick.inning if tick.inning is not None else 1
            if fv_before is not None:
                fv_before = team_offense_model.adjust_fv(
                    fv_before, away_abbrev, home_abbrev, game_date, inning_for_adj
                )
            if fv_after is not None:
                fv_after = team_offense_model.adjust_fv(
                    fv_after, away_abbrev, home_abbrev, game_date, inning_for_adj
                )

        fv_delta: Optional[float] = None
        if fv_before is not None and fv_after is not None:
            fv_delta = fv_after - fv_before

        # Overreaction calculation.
        overreaction: Optional[float] = None
        if fv_delta is not None and mid_peak_delta is not None:
            overreaction = mid_peak_delta - fv_delta

        # Determine edge side: if market pushed over price up more than justified,
        # fading it (selling over / buying under) is the edge.
        edge_side = ""
        if overreaction is not None and abs(overreaction) >= min_overreaction:
            if overreaction > 0:
                edge_side = "FADE_OVER"   # Over overshot upward → sell over / buy under
            else:
                edge_side = "FADE_UNDER"  # Under overshot downward → sell under / buy over

        if overreaction is not None and abs(overreaction) >= min_overreaction:
            events.append(
                ScoringEvent(
                    tick_index=i,
                    ts=tick.ts,
                    away_before=prev.away_score,
                    home_before=prev.home_score,
                    away_after=tick.away_score,
                    home_after=tick.home_score,
                    inning=tick.inning,
                    inning_state=tick.inning_state,
                    outs=tick.outs,
                    runs_scored=runs_scored,
                    fv_before=fv_before,
                    fv_after=fv_after,
                    fv_delta=fv_delta,
                    mid_before=mid_before,
                    mid_peak=mid_peak,
                    actual_delta=mid_peak_delta,
                    overreaction=overreaction,
                    edge_side=edge_side,
                )
            )

    return events


# ---------------------------------------------------------------------------
# Directory scanning
# ---------------------------------------------------------------------------

def find_game_dirs(polymarket_root: Path, date: str) -> List[Path]:
    """Return all game directories for a given date."""
    date_dir = polymarket_root / date
    if not date_dir.exists():
        return []
    return [p for p in date_dir.iterdir() if p.is_dir() and (p / "meta.json").exists()]


def load_meta(game_dir: Path) -> dict:
    meta_path = game_dir / "meta.json"
    try:
        with open(meta_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def available_lines(game_dir: Path) -> List[str]:
    """Return sorted unique lines available in game dir (e.g. ['7.5', '8.5', '9.5'])."""
    lines = set()
    for p in game_dir.glob("ou_*_over_yes.jsonl"):
        # Pattern: ou_{line}_over_yes.jsonl where line uses _ for .
        stem = p.stem  # e.g. ou_8_5_over_yes
        # Remove prefix and suffix.
        inner = stem.removeprefix("ou_").removesuffix("_over_yes")
        # Replace last _ with . to get float-ish string.
        # e.g. "8_5" -> "8.5"
        parts = inner.rsplit("_", 1)
        if len(parts) == 2:
            try:
                line = f"{parts[0]}.{parts[1]}"
                float(line)
                lines.add(line)
            except ValueError:
                # Handle integer lines like "8" (no decimal)
                try:
                    float(inner.replace("_", "."))
                    lines.add(inner.replace("_", "."))
                except ValueError:
                    pass
    return sorted(lines, key=float)


def jsonl_path_for(game_dir: Path, line: str, side: str) -> Path:
    code = line.replace(".", "_")
    return game_dir / f"ou_{code}_{side}.jsonl"


def load_game_run_env_context(
    game_pk: int, date: str, games_root: Path = DEFAULT_GAMES_ROOT
) -> Optional[dict]:
    """
    Load venue/weather fields from the raw game feed file.

    Returns a dict with keys "venue" and "weather" suitable for
    RunEnvContext.from_game_data(), or None if file not found/parseable.

    Path pattern: data/games/regular/{year}/{month}/{day}/{game_pk}.json
    """
    if not date or not game_pk:
        return None
    try:
        year, month, day = date.split("-")
    except ValueError:
        return None
    game_file = games_root / year / month / day / f"{game_pk}.json"
    if not game_file.exists():
        return None
    try:
        with open(game_file, encoding="utf-8", errors="replace") as f:
            d = json.load(f)
        gd = d.get("gameData", {}) or {}
        venue = gd.get("venue", {}) or {}
        weather = gd.get("weather", {}) or {}
        return {"venue": venue, "weather": weather}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _fmt_score(away: int, home: int, away_abbr: str, home_abbr: str) -> str:
    return f"{away_abbr} {away} - {home} {home_abbr}"


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "  N/A "
    return f"{v:+.3f}"


def _fmt_price(v: Optional[float]) -> str:
    if v is None:
        return " N/A "
    return f"{v:.3f}"


def print_report(analyses: List[GameAnalysis], min_overreaction: float) -> None:
    """Pretty-print findings to stdout."""
    all_events: List[Tuple[GameAnalysis, ScoringEvent]] = []
    for ga in analyses:
        for ev in ga.events:
            all_events.append((ga, ev))

    # Sort by absolute overreaction magnitude.
    all_events.sort(key=lambda x: abs(x[1].overreaction or 0), reverse=True)

    print("\n" + "=" * 100)
    print(f"  MLB POLYMARKET O/U OVERREACTION REPORT")
    print(f"  Min |overreaction| threshold: {min_overreaction:.3f}  ({min_overreaction * 100:.1f} cents)")
    print("=" * 100)

    if not all_events:
        print("  No overreaction events found above threshold.\n")
        return

    header = (
        f"  {'GAME':<22} {'LINE':>5}  {'INN':>4}  "
        f"{'SCORE BEFORE':>15} -> {'SCORE AFTER':<15}  "
        f"{'FV_BEF':>7} {'FV_AFT':>7} {'FV_D':>7}  "
        f"{'MID_BEF':>7} {'MID_AFT':>7} {'ACT_D':>7}  "
        f"{'OVER_REACT':>10}  {'EDGE':<12}"
    )
    print(header)
    print("  " + "-" * 97)

    for ga, ev in all_events:
        game_label = f"{ga.away_abbrev}@{ga.home_abbrev}"
        inn_label = f"{ev.inning}{ev.inning_state[:1].upper() if ev.inning_state else '?'}"
        score_before = f"{ga.away_abbrev}{ev.away_before}-{ev.home_before}{ga.home_abbrev}"
        score_after = f"{ga.away_abbrev}{ev.away_after}-{ev.home_after}{ga.home_abbrev}"

        over_str = f"{ev.overreaction:+.4f}" if ev.overreaction is not None else "  N/A"
        print(
            f"  {game_label:<22} {ga.line:>5}  {inn_label:>4}  "
            f"{score_before:>15} -> {score_after:<15}  "
            f"{_fmt_price(ev.fv_before):>7} {_fmt_price(ev.fv_after):>7} {_fmt_pct(ev.fv_delta):>7}  "
            f"{_fmt_price(ev.mid_before):>7} {_fmt_price(ev.mid_peak):>7} {_fmt_pct(ev.actual_delta):>7}  "
            f"{over_str:>10}  {ev.edge_side:<12}"
        )

    # Summary section
    print("\n" + "=" * 100)
    print("  SUMMARY BY GAME + LINE")
    print("=" * 100)

    by_game_line: Dict[Tuple[str, str], List[ScoringEvent]] = {}
    for ga, ev in all_events:
        key = (f"{ga.away_abbrev}@{ga.home_abbrev}", ga.line)
        by_game_line.setdefault(key, []).append(ev)

    for (game_label, line), evs in sorted(by_game_line.items()):
        over_vals = [e.overreaction for e in evs if e.overreaction is not None]
        avg_or = sum(over_vals) / len(over_vals) if over_vals else 0.0
        max_or = max(over_vals, key=abs) if over_vals else 0.0
        fade_over = sum(1 for e in evs if e.edge_side == "FADE_OVER")
        fade_under = sum(1 for e in evs if e.edge_side == "FADE_UNDER")
        print(
            f"  {game_label:<20} line={line:>5}  "
            f"events={len(evs):>2}  avg_over={avg_or:+.4f}  max_over={max_or:+.4f}  "
            f"FADE_OVER={fade_over}  FADE_UNDER={fade_under}"
        )

    print()


def save_json_output(analyses: List[GameAnalysis], output_path: Path) -> None:
    """Save full results to a JSON file."""
    def _ev_to_dict(ev: ScoringEvent) -> dict:
        return {
            "tick_index": ev.tick_index,
            "ts": ev.ts.isoformat(),
            "score_before": {"away": ev.away_before, "home": ev.home_before},
            "score_after": {"away": ev.away_after, "home": ev.home_after},
            "inning": ev.inning,
            "inning_state": ev.inning_state,
            "outs": ev.outs,
            "runs_scored": ev.runs_scored,
            "fv_before": ev.fv_before,
            "fv_after": ev.fv_after,
            "fv_delta": ev.fv_delta,
            "mid_before": ev.mid_before,
            "mid_peak": ev.mid_peak,
            "actual_delta": ev.actual_delta,
            "overreaction": ev.overreaction,
            "edge_side": ev.edge_side,
        }

    out = []
    for ga in analyses:
        out.append(
            {
                "date": ga.date,
                "game_pk": ga.game_pk,
                "away_abbrev": ga.away_abbrev,
                "home_abbrev": ga.home_abbrev,
                "line": ga.line,
                "events": [_ev_to_dict(ev) for ev in ga.events],
            }
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"  Saved JSON output: {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Analyze Polymarket MLB O/U ticks for price overreactions vs. fair value."
    )
    p.add_argument(
        "--date",
        type=str,
        default="",
        help="Date to analyze (YYYY-MM-DD). Default: all dates found in polymarket root.",
    )
    p.add_argument(
        "--game",
        type=str,
        default="",
        help="Filter by game directory fragment (e.g. 'MIL_at_KC' or game_pk '824134').",
    )
    p.add_argument(
        "--line",
        type=str,
        default="",
        help="Filter to a specific O/U line (e.g. '8.5'). Default: all lines.",
    )
    p.add_argument(
        "--min-overreaction",
        type=float,
        default=0.04,
        help="Minimum absolute overreaction magnitude to report (default: 0.04 = 4 cents).",
    )
    p.add_argument(
        "--reaction-window",
        type=float,
        default=60.0,
        help="Seconds after scoring event to observe market reaction (default: 60).",
    )
    p.add_argument(
        "--pre-event-offset",
        type=float,
        default=90.0,
        help=(
            "Seconds before the MLB API score change to anchor the baseline price window. "
            "Polymarket front-runs the API by ~65-80s; default 90s sits safely before that."
        ),
    )
    p.add_argument(
        "--pre-window-secs",
        type=float,
        default=30.0,
        help="Width of the baseline averaging window in seconds (default: 30).",
    )
    p.add_argument(
        "--cache-path",
        type=Path,
        default=DEFAULT_CACHE_PATH,
        help=f"Path to MLB O/U cache JSON (default: {DEFAULT_CACHE_PATH}).",
    )
    p.add_argument(
        "--polymarket-root",
        type=Path,
        default=DEFAULT_POLYMARKET_ROOT,
        help=f"Root directory for Polymarket output (default: {DEFAULT_POLYMARKET_ROOT}).",
    )
    p.add_argument(
        "--output",
        type=str,
        default="",
        help="Optional path to save JSON results.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-game progress.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Load cache.
    if not args.cache_path.exists():
        print(f"ERROR: cache not found at {args.cache_path}", file=sys.stderr)
        sys.exit(1)
    print(f"Loading cache: {args.cache_path}")
    cache = OUCache(args.cache_path)
    print(f"  {len(cache.cells)} cells loaded.")

    # Load Stage-2 park/weather run-environment model.
    stage2_model = None
    try:
        sys.path.insert(0, str(PROJECT_DIR / "cache"))
        from stage2_run_env_model import Stage2RunEnvModel, RunEnvContext  # noqa: E402
        stage2_model = Stage2RunEnvModel.from_path(DEFAULT_STAGE2_MODEL_PATH)
        print(f"  Stage-2 run-env model loaded: lines={stage2_model.lines}  "
              f"max_delta={stage2_model.max_total_abs_delta:.2f}")
    except Exception as exc:
        print(f"  WARNING: Stage-2 model not loaded ({exc}); park/weather adjustment disabled.")

    # Load team offense model (Stage-3).
    team_offense_model = None
    try:
        sys.path.insert(0, str(PROJECT_DIR / "scripts" / "analysis"))
        from team_offense_model import TeamOffenseModel  # noqa: E402
        team_offense_model = TeamOffenseModel.load(auto_rebuild=False)
        print(
            f"  Team offense model loaded: mlb_avg_total={team_offense_model.mlb_avg_total:.3f} RPG"
        )
    except Exception as exc:
        print(f"  WARNING: team offense model not loaded ({exc}); using Stage-1 FV only.")

    # Discover dates to process.
    root = args.polymarket_root
    if not root.exists():
        print(f"ERROR: polymarket root not found: {root}", file=sys.stderr)
        sys.exit(1)

    if args.date:
        dates = [args.date]
    else:
        dates = sorted(
            p.name for p in root.iterdir()
            if p.is_dir() and not p.name.startswith("schedules")
        )

    if not dates:
        print("No dates found to process.")
        return

    print(f"Processing dates: {dates}")

    all_analyses: List[GameAnalysis] = []

    for date in dates:
        game_dirs = find_game_dirs(root, date)
        if not game_dirs:
            print(f"  [{date}] No game directories found.")
            continue

        # Optional game filter.
        if args.game:
            game_dirs = [g for g in game_dirs if args.game in g.name]

        print(f"  [{date}] {len(game_dirs)} game(s) found.")

        for gdir in sorted(game_dirs):
            meta = load_meta(gdir)
            game_info = meta.get("game", {})
            game_pk = game_info.get("game_pk", 0)
            away = game_info.get("away_abbrev", gdir.name.split("_")[0])
            home = game_info.get("home_abbrev", gdir.name.split("_")[2] if "_at_" in gdir.name else "")

            # [Stage-2] Build run-env context once per game from local game file.
            stage2_context = None
            if stage2_model is not None:
                raw_env = load_game_run_env_context(game_pk, date)
                if raw_env is not None:
                    try:
                        stage2_context = RunEnvContext.from_game_data(raw_env)
                    except Exception:
                        stage2_context = None

            lines = available_lines(gdir)
            if args.line:
                lines = [l for l in lines if l == args.line]

            if args.verbose:
                park_str = (stage2_context.park if stage2_context else "unknown")
                print(f"    {away}@{home} (pk={game_pk}): lines={lines}  park='{park_str}'")

            for line in lines:
                over_path = jsonl_path_for(gdir, line, "over_yes")
                if not over_path.exists():
                    continue

                ticks = load_ticks(over_path)
                # Filter to ticks with valid game state (live games only).
                live_ticks = [t for t in ticks if t.inning is not None and t.away_score is not None]

                if len(live_ticks) < 10:
                    if args.verbose:
                        print(f"      line={line}: only {len(live_ticks)} live ticks, skipping.")
                    continue

                events = analyze_line(
                    ticks=live_ticks,
                    line=line,
                    cache=cache,
                    pre_event_offset_s=args.pre_event_offset,
                    pre_window_secs=args.pre_window_secs,
                    reaction_window_secs=args.reaction_window,
                    min_overreaction=args.min_overreaction,
                    away_abbrev=away,
                    home_abbrev=home,
                    game_date=date,
                    team_offense_model=team_offense_model,
                    stage2_model=stage2_model,
                    stage2_context=stage2_context,
                )

                if args.verbose:
                    print(
                        f"      line={line}: {len(live_ticks)} live ticks, "
                        f"{len(events)} overreaction event(s) >= {args.min_overreaction:.3f}"
                    )

                ga = GameAnalysis(
                    date=date,
                    game_pk=game_pk,
                    away_abbrev=away,
                    home_abbrev=home,
                    line=line,
                    events=events,
                )
                all_analyses.append(ga)

    # Report.
    print_report(all_analyses, args.min_overreaction)

    # Optionally save.
    if args.output:
        save_json_output(all_analyses, Path(args.output))


if __name__ == "__main__":
    main()
