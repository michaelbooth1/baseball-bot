#!/usr/bin/env python3
"""
Stage-2 run-environment residual model for MLB O/U probabilities.

This module is intentionally runtime-focused:
- load a precomputed Stage-2 model JSON,
- derive environment buckets from game context,
- apply bounded logit-delta residuals on top of Stage-1 base probabilities,
- enforce monotonicity across O/U lines.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

EPS = 1e-6
UNKNOWN_BUCKET = "__UNK__"
# 2026-05-08: density_alt + hr_factor added as Stage-2 families.
# - density_alt is elevation x temp; non-redundant with `park` because it
#   varies with same-day temperature (hot day at Coors vs cold night).
# - hr_factor is per-(park, season) HR rate vs league mean; non-redundant
#   with `park` because it captures year-over-year drift (juiced ball,
#   fence moves, humidor, etc.) that the multi-year `park` bucket averages
#   away.
FAMILY_ORDER = ("park", "temp", "wind", "park_wind", "density_alt", "hr_factor")
MAX_MONOTONIC_PASSES = 12

_WIND_MPH_RE = re.compile(r"(\d+)\s*mph", re.IGNORECASE)

# Lazy-loaded stadium metadata keyed by park name + aliases.
DEFAULT_STADIUM_METADATA_PATH = (
    Path(__file__).resolve().parent.parent
    / "data" / "reference" / "mlb_stadium_weather_metadata.json"
)
_STADIUM_METADATA_BY_PARK: Optional[Dict[str, dict]] = None

# Lazy-loaded park HR factors keyed by park -> {year_str: shrunk_factor}.
DEFAULT_PARK_HR_FACTORS_PATH = (
    Path(__file__).resolve().parent.parent
    / "cache" / "park_hr_factors.json"
)
_PARK_HR_FACTORS: Optional[Dict[str, Dict[str, float]]] = None


def _clamp01(p: float) -> float:
    return max(EPS, min(1.0 - EPS, p))


def _logit(p: float) -> float:
    p = _clamp01(p)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def parse_temp_bin(temp_f: int | None) -> str:
    if temp_f is None:
        return "unknown"
    if temp_f < 50:
        return "<50F"
    if temp_f < 65:
        return "50-64F"
    if temp_f < 80:
        return "65-79F"
    return "80+F"


def parse_temp_int(temp_raw: Any) -> Optional[int]:
    """Best-effort int parse for MLB-supplied weather.temp ('72', '72 F', etc.)."""
    if temp_raw is None:
        return None
    try:
        return int(str(temp_raw).strip().split()[0])
    except (ValueError, IndexError):
        return None


def density_altitude_ft(elevation_ft: Optional[int], temp_f: Optional[int]) -> Optional[float]:
    """Standard density-altitude approximation:
        DA = pressure_alt + 120 * (T_F - 59)
    With sea-level pressure assumed standard, pressure_alt ~= field elevation.
    Captures the "ball carries further when air is thin" effect that drives
    HR rate variation by stadium-day. Coors at 90F ~= 8200 ft DA;
    Petco at 65F ~= 290 ft DA.
    """
    if elevation_ft is None or temp_f is None:
        return None
    return float(elevation_ft) + 120.0 * (float(temp_f) - 59.0)


def parse_density_alt_bin(da_ft: Optional[float]) -> str:
    """Bucket density-altitude into 5 levels + unknown.
    Levels are chosen so each bucket has meaningful sample size across the
    league (most of MLB lives in 0-2.5k; only Coors and to a lesser extent
    Chase/Salt-River reach the upper bins on hot days).
    """
    if da_ft is None:
        return "unknown"
    if da_ft < 0:
        return "<0"
    if da_ft < 1000:
        return "0_1k"
    if da_ft < 2500:
        return "1k_2_5k"
    if da_ft < 5000:
        return "2_5k_5k"
    return "5k+"


def _load_stadium_metadata(path: Optional[Path] = None) -> Dict[str, dict]:
    """Load and memoize the stadium metadata, keyed by primary_name + aliases.
    Returns {} (and caches that) if the file is missing -- callers degrade to
    UNKNOWN_BUCKET for elevation-derived features.
    """
    global _STADIUM_METADATA_BY_PARK
    if _STADIUM_METADATA_BY_PARK is not None and path is None:
        return _STADIUM_METADATA_BY_PARK
    p = Path(path) if path else DEFAULT_STADIUM_METADATA_PATH
    if not p.exists():
        if path is None:
            _STADIUM_METADATA_BY_PARK = {}
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        if path is None:
            _STADIUM_METADATA_BY_PARK = {}
        return {}
    out: Dict[str, dict] = {}
    for s in raw.get("stadiums", []) or []:
        primary = str(s.get("primary_name") or "").strip()
        if primary:
            out[primary] = s
        for a in s.get("aliases", []) or []:
            out[str(a).strip()] = s
    if path is None:
        _STADIUM_METADATA_BY_PARK = out
    return out


def reset_stadium_metadata_cache() -> None:
    """Test-only: clear the lazy cache so a different metadata path can be loaded."""
    global _STADIUM_METADATA_BY_PARK
    _STADIUM_METADATA_BY_PARK = None


def parse_hr_factor_bin(factor: Optional[float]) -> str:
    """Bucket the (park, season) HR factor into 5 levels + unknown.
    Levels chosen so that:
      <0.85 / 0.85-0.95 = HR-suppressing parks (Oracle, Petco, Kauffman in some years)
      0.95-1.05 = neutral
      1.05-1.15 / 1.15+ = HR-friendly (Yankee, GABP, Coors in many years)
    """
    if factor is None:
        return "unknown"
    if factor < 0.85:
        return "<0.85"
    if factor < 0.95:
        return "0.85_0.95"
    if factor < 1.05:
        return "0.95_1.05"
    if factor < 1.15:
        return "1.05_1.15"
    return "1.15+"


def _load_park_hr_factors(path: Optional[Path] = None) -> Dict[str, Dict[str, float]]:
    """Load and memoize per-(park, year) HR factor cache. Returns mapping
    {park: {year_str: shrunk_factor}}. Returns {} (cached) if file is missing.
    """
    global _PARK_HR_FACTORS
    if _PARK_HR_FACTORS is not None and path is None:
        return _PARK_HR_FACTORS
    p = Path(path) if path else DEFAULT_PARK_HR_FACTORS_PATH
    if not p.exists():
        if path is None:
            _PARK_HR_FACTORS = {}
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        if path is None:
            _PARK_HR_FACTORS = {}
        return {}
    out: Dict[str, Dict[str, float]] = {}
    for park, by_year in (raw.get("by_park") or {}).items():
        if not isinstance(by_year, dict):
            continue
        flat: Dict[str, float] = {}
        for year_str, entry in by_year.items():
            if isinstance(entry, dict) and "shrunk_factor" in entry:
                try:
                    flat[str(year_str)] = float(entry["shrunk_factor"])
                except (TypeError, ValueError):
                    continue
        if flat:
            out[str(park)] = flat
    if path is None:
        _PARK_HR_FACTORS = out
    return out


def reset_park_hr_factors_cache() -> None:
    """Test-only: clear the lazy cache."""
    global _PARK_HR_FACTORS
    _PARK_HR_FACTORS = None


def lookup_hr_factor(
    park: str,
    year: Optional[str],
    hr_factors: Optional[Mapping[str, Mapping[str, float]]] = None,
) -> Optional[float]:
    """Look up the (park, year) HR factor with most-recent-year fallback.
    A 2026 game in early season may have no 2026 entry yet (or a low-N one);
    fall back to the most recent prior year's shrunk factor for stability.
    """
    factors = hr_factors if hr_factors is not None else _load_park_hr_factors()
    if not factors:
        return None
    park_map = factors.get(park)
    if not park_map:
        return None
    if year and str(year) in park_map:
        return float(park_map[str(year)])
    # Fallback: most recent year with data for this park.
    available = sorted(park_map.keys())
    if not available:
        return None
    return float(park_map[available[-1]])


def parse_wind_bin(wind_text: str | None) -> str:
    if not wind_text or not str(wind_text).strip():
        return "unknown"
    txt = str(wind_text).lower()
    mph_match = _WIND_MPH_RE.search(txt)
    mph = int(mph_match.group(1)) if mph_match else None

    if "out to" in txt:
        direction = "out"
    elif "in from" in txt:
        direction = "in"
    elif "left to right" in txt or "right to left" in txt:
        direction = "cross"
    elif "calm" in txt:
        direction = "calm"
    else:
        direction = "other"

    if mph is None:
        return direction
    if mph >= 10:
        return f"{direction}_10plus"
    if mph >= 6:
        return f"{direction}_6to9"
    return f"{direction}_0to5"


def _normalize_bucket(value: str | None) -> str:
    if not value:
        return UNKNOWN_BUCKET
    txt = str(value).strip()
    return txt if txt else UNKNOWN_BUCKET


def enforce_over_monotonic(over_probs: Mapping[str, float]) -> Dict[str, float]:
    """
    Enforce P(O6.5) >= P(O7.5) >= ... by iterative adjacent averaging.
    """
    if not over_probs:
        return {}

    ordered = sorted(((float(k), k, _clamp01(v)) for k, v in over_probs.items()), key=lambda x: x[0])
    vals = [x[2] for x in ordered]

    for _ in range(MAX_MONOTONIC_PASSES):
        changed = False
        for i in range(len(vals) - 1):
            if vals[i] < vals[i + 1]:
                avg = 0.5 * (vals[i] + vals[i + 1])
                vals[i] = avg
                vals[i + 1] = avg
                changed = True
        if not changed:
            break

    out: Dict[str, float] = {}
    for i, (_, key, _) in enumerate(ordered):
        out[key] = _clamp01(vals[i])
    return out


@dataclass(frozen=True)
class RunEnvContext:
    park: str = UNKNOWN_BUCKET
    temp_bin: str = "unknown"
    wind_bin: str = "unknown"
    roof_type: str = "unknown"
    density_alt_bin: str = "unknown"
    hr_factor_bin: str = "unknown"

    @classmethod
    def from_game_data(
        cls,
        game_data: Mapping[str, Any],
        stadium_metadata: Optional[Mapping[str, dict]] = None,
        hr_factors: Optional[Mapping[str, Mapping[str, float]]] = None,
        year: Optional[str] = None,
    ) -> "RunEnvContext":
        venue = game_data.get("venue", {}) or {}
        weather = game_data.get("weather", {}) or {}

        park = _normalize_bucket(str(venue.get("name") or UNKNOWN_BUCKET))
        roof_type = _normalize_bucket(
            str((venue.get("fieldInfo", {}) or {}).get("roofType") or "unknown")
        )

        temp_f = parse_temp_int(weather.get("temp"))
        temp_bin = parse_temp_bin(temp_f)
        wind_bin = parse_wind_bin(str(weather.get("wind") or ""))

        # density_alt: requires stadium elevation lookup + the StatsAPI temp.
        # Lazy-loads the stadium metadata file; degrades to "unknown" if missing.
        meta = stadium_metadata if stadium_metadata is not None else _load_stadium_metadata()
        elevation_ft = None
        if meta:
            stadium = meta.get(park)
            if isinstance(stadium, dict):
                ev = stadium.get("elevation_ft")
                if isinstance(ev, (int, float)):
                    elevation_ft = int(ev)
        density_alt_bin = parse_density_alt_bin(density_altitude_ft(elevation_ft, temp_f))

        # hr_factor: per-(park, season). Year inferred from game_data.datetime
        # if not explicitly passed. Falls back to most-recent prior year via
        # lookup_hr_factor.
        if year is None:
            datetime_blk = game_data.get("datetime", {}) or {}
            date_txt = str(datetime_blk.get("dateTime", "") or datetime_blk.get("originalDate", ""))
            if len(date_txt) >= 4 and date_txt[:4].isdigit():
                year = date_txt[:4]
        factor = lookup_hr_factor(park, year, hr_factors=hr_factors)
        hr_factor_bin = parse_hr_factor_bin(factor)

        return cls(
            park=park,
            temp_bin=temp_bin,
            wind_bin=wind_bin,
            roof_type=roof_type,
            density_alt_bin=density_alt_bin,
            hr_factor_bin=hr_factor_bin,
        )

    def buckets(self) -> Dict[str, str]:
        park = _normalize_bucket(self.park)
        temp = _normalize_bucket(self.temp_bin)
        wind = _normalize_bucket(self.wind_bin)
        density_alt = _normalize_bucket(self.density_alt_bin)
        hr_factor = _normalize_bucket(self.hr_factor_bin)
        return {
            "park": park,
            "temp": temp,
            "wind": wind,
            "park_wind": f"{park}__{wind}",
            "density_alt": density_alt,
            "hr_factor": hr_factor,
        }


class Stage2RunEnvModel:
    def __init__(self, payload: Mapping[str, Any]):
        self.payload = dict(payload)
        self.tables: Dict[str, Dict[str, Dict[str, Dict[str, float]]]] = (
            self.payload.get("tables", {}) or {}
        )
        self.weights: Dict[str, Dict[str, float]] = self.payload.get("weights", {}) or {}
        constraints = self.payload.get("constraints", {}) or {}
        self.max_total_abs_delta = float(constraints.get("max_total_abs_delta", 1.0))
        self.lines = sorted(self.weights.keys(), key=float)

    @classmethod
    def from_path(cls, model_path: str | Path) -> "Stage2RunEnvModel":
        path = Path(model_path)
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        return cls(payload)

    def _line_delta(self, line: str, buckets: Mapping[str, str]) -> float:
        line_weights = self.weights.get(line, {})
        total_delta = 0.0
        for family in FAMILY_ORDER:
            w = float(line_weights.get(family, 0.0))
            if w == 0.0:
                continue
            bucket = buckets.get(family, UNKNOWN_BUCKET)
            family_line_table = (self.tables.get(family, {}) or {}).get(line, {}) or {}
            bucket_row = family_line_table.get(bucket)
            if not bucket_row:
                continue
            total_delta += w * float(bucket_row.get("delta", 0.0))
        return max(-self.max_total_abs_delta, min(self.max_total_abs_delta, total_delta))

    def adjust_line(self, line: str, base_prob: float, context: RunEnvContext) -> float:
        p0 = _clamp01(float(base_prob))
        delta = self._line_delta(line=line, buckets=context.buckets())
        return _clamp01(_sigmoid(_logit(p0) + delta))

    def adjust_over_probs(
        self,
        base_over_probs: Mapping[str, float],
        context: RunEnvContext,
        enforce_monotonic: bool = True,
    ) -> Dict[str, float]:
        adjusted: Dict[str, float] = {}
        for line, p0 in base_over_probs.items():
            adjusted[line] = self.adjust_line(line=line, base_prob=float(p0), context=context)
        return enforce_over_monotonic(adjusted) if enforce_monotonic else adjusted

