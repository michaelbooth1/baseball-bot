#!/usr/bin/env python3
"""
weather_client.py -- Stadium-level MLB Weather v2 cache builder.

Weather v2 is the canonical live weather source for Stage-2 fair-value
adjustments and downstream model tuning. It fetches local forecast weather by
stadium coordinate, joins it to the MLB schedule, and writes compact cache rows
that trading and analysis consume.

Design goals:
- use static stadium metadata for stable coordinate/orientation joins,
- use provider weather only; do not fall back to legacy schedule text,
- derive physically meaningful diagnostics (air density, wind-out component),
- fail open at startup with explicit coverage/warning fields.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import requests


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_STADIUM_METADATA_PATH = PROJECT_DIR / "data" / "reference" / "mlb_stadium_weather_metadata.json"
DEFAULT_WEATHER_CACHE_DIR = PROJECT_DIR / "cache" / "weather"
MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

LOGGER = logging.getLogger("weather_client")

WEATHER_FEATURE_FIELD_KEYS: Tuple[str, ...] = (
    "weather_cache_available",
    "weather_cache_date",
    "weather_source_provider",
    "weather_source_status",
    "weather_source_error",
    "weather_selected_time_utc",
    "weather_selected_offset_minutes",
    "stadium_id",
    "stadium_primary_name",
    "stadium_roof_type",
    "stadium_weather_exposure",
    "stadium_weather_sensitivity",
    "stadium_elevation_ft",
    "stadium_outfield_bearing_deg",
    "weather_active_default",
    "weather_roof_state_assumption",
    "weather_roof_uncertain",
    "weather_model_usable",
    "weather_temp_f",
    "weather_relative_humidity_pct",
    "weather_dewpoint_f",
    "weather_pressure_msl_hpa",
    "weather_surface_pressure_hpa",
    "weather_precipitation_probability_pct",
    "weather_precipitation_in",
    "weather_code",
    "weather_wind_speed_mph",
    "weather_wind_from_deg",
    "weather_wind_gust_mph",
    "weather_air_density_kg_m3",
    "weather_air_density_index",
    "weather_density_altitude_ft",
    "weather_wind_out_component_mph",
    "weather_wind_cross_component_mph",
    "weather_effective_temp_f",
    "weather_effective_wind_speed_mph",
    "weather_effective_wind_gust_mph",
    "weather_effective_air_density_index",
    "weather_effective_density_altitude_ft",
    "weather_effective_wind_out_component_mph",
    "weather_effective_wind_cross_component_mph",
)

WEATHER_MODEL_FEATURE_FIELD_KEYS: Tuple[str, ...] = (
    "stadium_roof_type",
    "stadium_weather_exposure",
    "stadium_weather_sensitivity",
    "stadium_elevation_ft",
    "weather_roof_state_assumption",
    "weather_roof_uncertain",
    "weather_model_usable",
    "weather_effective_temp_f",
    "weather_effective_wind_speed_mph",
    "weather_effective_wind_gust_mph",
    "weather_effective_air_density_index",
    "weather_effective_density_altitude_ft",
    "weather_effective_wind_out_component_mph",
    "weather_effective_wind_cross_component_mph",
    "weather_precipitation_probability_pct",
    "weather_precipitation_in",
    "weather_code",
)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class StadiumWeatherMetadata:
    stadium_id: str
    primary_name: str
    aliases: Tuple[str, ...]
    mlb_team: str
    city: str
    state: str
    latitude: float
    longitude: float
    elevation_ft: float
    roof_type: str
    weather_exposure: str
    weather_sensitivity: str
    outfield_bearing_deg: Optional[float]

    def as_cache_dict(self) -> Dict[str, Any]:
        return {
            "stadium_id": self.stadium_id,
            "primary_name": self.primary_name,
            "mlb_team": self.mlb_team,
            "city": self.city,
            "state": self.state,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "elevation_ft": self.elevation_ft,
            "roof_type": self.roof_type,
            "weather_exposure": self.weather_exposure,
            "weather_sensitivity": self.weather_sensitivity,
            "outfield_bearing_deg": self.outfield_bearing_deg,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_name(value: str) -> str:
    return _NON_ALNUM_RE.sub("", str(value or "").lower())


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _parse_iso_utc(value: str) -> Optional[datetime]:
    try:
        text = str(value or "").strip()
        if not text:
            return None
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _date_plus_one(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d").date()
    return (dt + timedelta(days=1)).isoformat()


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except Exception:
        return None


def load_stadium_metadata(path: Path = DEFAULT_STADIUM_METADATA_PATH) -> List[StadiumWeatherMetadata]:
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    out: List[StadiumWeatherMetadata] = []
    for row in payload.get("stadiums", []) or []:
        aliases = tuple(str(v) for v in row.get("aliases", []) or [])
        out.append(
            StadiumWeatherMetadata(
                stadium_id=str(row.get("stadium_id") or ""),
                primary_name=str(row.get("primary_name") or ""),
                aliases=aliases,
                mlb_team=str(row.get("mlb_team") or ""),
                city=str(row.get("city") or ""),
                state=str(row.get("state") or ""),
                latitude=float(row.get("latitude")),
                longitude=float(row.get("longitude")),
                elevation_ft=float(row.get("elevation_ft") or 0.0),
                roof_type=str(row.get("roof_type") or "unknown"),
                weather_exposure=str(row.get("weather_exposure") or "unknown"),
                weather_sensitivity=str(row.get("weather_sensitivity") or "unknown"),
                outfield_bearing_deg=_safe_float(row.get("outfield_bearing_deg")),
            )
        )
    return out


class StadiumMetadataIndex:
    def __init__(self, stadiums: Sequence[StadiumWeatherMetadata]):
        self.stadiums = list(stadiums)
        self._by_name: Dict[str, StadiumWeatherMetadata] = {}
        for stadium in self.stadiums:
            for name in (stadium.primary_name, *stadium.aliases):
                norm = _normalize_name(name)
                if norm:
                    self._by_name[norm] = stadium

    @classmethod
    def from_path(cls, path: Path = DEFAULT_STADIUM_METADATA_PATH) -> "StadiumMetadataIndex":
        return cls(load_stadium_metadata(path))

    def match(self, venue_name: str) -> Optional[StadiumWeatherMetadata]:
        norm = _normalize_name(venue_name)
        if not norm:
            return None
        direct = self._by_name.get(norm)
        if direct:
            return direct
        for key, stadium in self._by_name.items():
            if key and (key in norm or norm in key):
                return stadium
        return None


def fetch_mlb_schedule_payload(date_str: str, *, timeout: float = 8.0) -> Dict[str, Any]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "MLB-Poly-Weather-Refresh/1.0",
        "Accept": "application/json",
    })
    resp = session.get(
        MLB_SCHEDULE_URL,
        params={
            "sportId": 1,
            "date": date_str,
            "hydrate": "team,linescore,venue",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def parse_schedule_games(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    games: List[Dict[str, Any]] = []
    for date_row in payload.get("dates", []) or []:
        for game in date_row.get("games", []) or []:
            game_pk = _safe_int(game.get("gamePk"))
            if game_pk is None:
                continue
            teams = game.get("teams", {}) or {}
            away_team = ((teams.get("away", {}) or {}).get("team", {}) or {})
            home_team = ((teams.get("home", {}) or {}).get("team", {}) or {})
            venue = game.get("venue", {}) or {}
            status = game.get("status", {}) or {}
            games.append(
                {
                    "game_pk": game_pk,
                    "game_date_utc": str(game.get("gameDate") or ""),
                    "away_abbrev": str(away_team.get("abbreviation") or "").upper(),
                    "home_abbrev": str(home_team.get("abbreviation") or "").upper(),
                    "away_name": str(away_team.get("name") or away_team.get("teamName") or ""),
                    "home_name": str(home_team.get("name") or home_team.get("teamName") or ""),
                    "venue_name": str(venue.get("name") or ""),
                    "venue_roof_type": str(((venue.get("fieldInfo", {}) or {}).get("roofType")) or ""),
                    "status_abstract": str(status.get("abstractGameState") or ""),
                    "status_detailed": str(status.get("detailedState") or ""),
                }
            )
    return games


class OpenMeteoWeatherClient:
    def __init__(self, *, timeout: float = 8.0, session: Optional[requests.Session] = None):
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "MLB-Poly-Weather-Refresh/1.0",
            "Accept": "application/json",
        })

    def fetch_hourly_forecast(
        self,
        *,
        latitude: float,
        longitude: float,
        start_date: str,
        end_date: str,
    ) -> Dict[str, Any]:
        params = {
            "latitude": f"{latitude:.5f}",
            "longitude": f"{longitude:.5f}",
            "hourly": ",".join(
                [
                    "temperature_2m",
                    "relative_humidity_2m",
                    "dew_point_2m",
                    "pressure_msl",
                    "surface_pressure",
                    "precipitation_probability",
                    "precipitation",
                    "weather_code",
                    "wind_speed_10m",
                    "wind_direction_10m",
                    "wind_gusts_10m",
                ]
            ),
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": "UTC",
            "start_date": start_date,
            "end_date": end_date,
        }
        resp = self.session.get(OPEN_METEO_FORECAST_URL, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()


def _nearest_hourly_record(payload: Mapping[str, Any], target_dt: Optional[datetime]) -> Dict[str, Any]:
    hourly = payload.get("hourly", {}) or {}
    times = hourly.get("time", []) or []
    if not times or target_dt is None:
        return {"selected_time_utc": "", "selected_offset_minutes": None}

    best_idx: Optional[int] = None
    best_abs_secs: Optional[float] = None
    for idx, raw_time in enumerate(times):
        dt = _parse_iso_utc(str(raw_time))
        if dt is None:
            continue
        abs_secs = abs((dt - target_dt).total_seconds())
        if best_abs_secs is None or abs_secs < best_abs_secs:
            best_abs_secs = abs_secs
            best_idx = idx
    if best_idx is None:
        return {"selected_time_utc": "", "selected_offset_minutes": None}

    out: Dict[str, Any] = {
        "selected_time_utc": _parse_iso_utc(str(times[best_idx])).isoformat().replace("+00:00", "Z"),
        "selected_offset_minutes": round((best_abs_secs or 0.0) / 60.0, 3),
    }
    for key, values in hourly.items():
        if key == "time" or not isinstance(values, list):
            continue
        out[key] = values[best_idx] if best_idx < len(values) else None
    return out


def _fahrenheit_to_c(temp_f: Optional[float]) -> Optional[float]:
    if temp_f is None:
        return None
    return (temp_f - 32.0) * 5.0 / 9.0


def calculate_air_density_kg_m3(
    *,
    temp_f: Optional[float],
    surface_pressure_hpa: Optional[float],
    dewpoint_f: Optional[float],
    relative_humidity_pct: Optional[float] = None,
) -> Optional[float]:
    temp_c = _fahrenheit_to_c(temp_f)
    if temp_c is None or surface_pressure_hpa is None:
        return None
    temp_k = temp_c + 273.15
    if temp_k <= 0:
        return None

    vapor_pressure_hpa: Optional[float] = None
    dew_c = _fahrenheit_to_c(dewpoint_f)
    if dew_c is not None:
        vapor_pressure_hpa = 6.112 * math.exp((17.67 * dew_c) / (dew_c + 243.5))
    elif relative_humidity_pct is not None:
        sat = 6.112 * math.exp((17.67 * temp_c) / (temp_c + 243.5))
        vapor_pressure_hpa = sat * max(0.0, min(100.0, relative_humidity_pct)) / 100.0

    vapor_pressure_pa = max(0.0, float(vapor_pressure_hpa or 0.0) * 100.0)
    total_pressure_pa = float(surface_pressure_hpa) * 100.0
    dry_pressure_pa = max(0.0, total_pressure_pa - vapor_pressure_pa)
    rho = dry_pressure_pa / (287.05 * temp_k) + vapor_pressure_pa / (461.495 * temp_k)
    return rho if math.isfinite(rho) else None


def calculate_density_altitude_ft(
    *,
    temp_f: Optional[float],
    elevation_ft: Optional[float],
) -> Optional[float]:
    temp_c = _fahrenheit_to_c(temp_f)
    if temp_c is None or elevation_ft is None:
        return None
    isa_temp_c = 15.0 - 0.0019812 * float(elevation_ft)
    return float(elevation_ft) + 118.8 * (temp_c - isa_temp_c)


def calculate_wind_components(
    *,
    wind_speed_mph: Optional[float],
    wind_from_deg: Optional[float],
    outfield_bearing_deg: Optional[float],
) -> Dict[str, Optional[float]]:
    if wind_speed_mph is None or wind_from_deg is None or outfield_bearing_deg is None:
        return {"wind_out_component_mph": None, "wind_cross_component_mph": None}
    wind_to_deg = (float(wind_from_deg) + 180.0) % 360.0
    diff_rad = math.radians((wind_to_deg - float(outfield_bearing_deg) + 540.0) % 360.0 - 180.0)
    out_component = float(wind_speed_mph) * math.cos(diff_rad)
    cross_component = float(wind_speed_mph) * math.sin(diff_rad)
    return {
        "wind_out_component_mph": round(out_component, 4),
        "wind_cross_component_mph": round(cross_component, 4),
    }


def _weather_active_default(stadium: Optional[StadiumWeatherMetadata]) -> bool:
    if stadium is None:
        return True
    return stadium.weather_exposure != "fixed_roof"


def _weather_roof_state_assumption(stadium: Optional[StadiumWeatherMetadata]) -> str:
    if stadium is None:
        return "missing_metadata"
    exposure = str(stadium.weather_exposure or "").lower()
    if exposure == "fixed_roof":
        return "fixed_roof_inactive"
    if exposure == "retractable":
        return "retractable_unknown"
    if exposure == "open":
        return "open_air_active"
    return "unknown_exposure"


def _weather_roof_uncertain(stadium: Optional[StadiumWeatherMetadata]) -> bool:
    if stadium is None:
        return True
    return str(stadium.weather_exposure or "").lower() in {"retractable", "unknown"}


def _weather_model_usable(
    *,
    stadium: Optional[StadiumWeatherMetadata],
    source_status: str,
) -> bool:
    if stadium is None:
        return False
    if str(stadium.weather_exposure or "").lower() != "open":
        return False
    return str(source_status or "") == "ok"


def _open_meteo_cache_key(stadium: StadiumWeatherMetadata, date_str: str) -> str:
    return f"{stadium.stadium_id}:{date_str}"


def build_game_weather_cache(
    *,
    date_str: str,
    metadata_path: Path = DEFAULT_STADIUM_METADATA_PATH,
    provider: str = "open-meteo",
    timeout: float = 8.0,
    schedule_payload: Optional[Mapping[str, Any]] = None,
    open_meteo_client: Optional[OpenMeteoWeatherClient] = None,
) -> Dict[str, Any]:
    if provider not in {"open-meteo", "none"}:
        raise ValueError(f"Unsupported weather provider: {provider}")

    metadata_index = StadiumMetadataIndex.from_path(metadata_path)
    payload = dict(schedule_payload or fetch_mlb_schedule_payload(date_str, timeout=timeout))
    games = parse_schedule_games(payload)
    meteo_client = open_meteo_client or OpenMeteoWeatherClient(timeout=timeout)

    forecast_by_stadium: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    rows: List[Dict[str, Any]] = []

    for game in games:
        venue_name = str(game.get("venue_name") or "")
        stadium = metadata_index.match(venue_name)
        game_start_dt = _parse_iso_utc(str(game.get("game_date_utc") or ""))
        source_status = "provider_missing"
        source_error = ""
        selected_weather: Dict[str, Any] = {}

        if stadium and provider == "open-meteo":
            cache_key = _open_meteo_cache_key(stadium, date_str)
            if cache_key not in forecast_by_stadium:
                try:
                    forecast_by_stadium[cache_key] = meteo_client.fetch_hourly_forecast(
                        latitude=stadium.latitude,
                        longitude=stadium.longitude,
                        start_date=date_str,
                        end_date=_date_plus_one(date_str),
                    )
                except Exception as exc:
                    forecast_by_stadium[cache_key] = {"_provider_error": str(exc)}
                    warnings.append(f"{venue_name or game.get('home_abbrev')}: weather fetch failed: {exc}")
            forecast = forecast_by_stadium[cache_key]
            if forecast.get("_provider_error"):
                source_error = str(forecast.get("_provider_error"))
            else:
                selected_weather = _nearest_hourly_record(forecast, game_start_dt)
                source_status = "ok" if selected_weather.get("selected_time_utc") else "provider_missing"
        elif not stadium:
            source_status = "missing_metadata"
            warnings.append(f"{venue_name or game.get('home_abbrev')}: no stadium weather metadata match")
        elif provider == "none":
            source_status = "provider_disabled"

        temp_f = _safe_float(selected_weather.get("temperature_2m"))
        wind_speed = _safe_float(selected_weather.get("wind_speed_10m"))
        wind_from = _safe_float(selected_weather.get("wind_direction_10m"))

        humidity = _safe_float(selected_weather.get("relative_humidity_2m"))
        dewpoint_f = _safe_float(selected_weather.get("dew_point_2m"))
        pressure_hpa = _safe_float(selected_weather.get("surface_pressure"))
        pressure_msl_hpa = _safe_float(selected_weather.get("pressure_msl"))
        density = calculate_air_density_kg_m3(
            temp_f=temp_f,
            surface_pressure_hpa=pressure_hpa or pressure_msl_hpa,
            dewpoint_f=dewpoint_f,
            relative_humidity_pct=humidity,
        )
        density_altitude = calculate_density_altitude_ft(
            temp_f=temp_f,
            elevation_ft=stadium.elevation_ft if stadium else None,
        )
        wind_components = calculate_wind_components(
            wind_speed_mph=wind_speed,
            wind_from_deg=wind_from,
            outfield_bearing_deg=stadium.outfield_bearing_deg if stadium else None,
        )
        weather_active_default = _weather_active_default(stadium)
        roof_state_assumption = _weather_roof_state_assumption(stadium)
        roof_uncertain = _weather_roof_uncertain(stadium)
        model_usable = _weather_model_usable(
            stadium=stadium,
            source_status=source_status,
        )
        effective = {
            "effective_temp_f": _round(temp_f, 2) if model_usable else None,
            "effective_wind_speed_mph": _round(wind_speed, 3) if model_usable else None,
            "effective_wind_gust_mph": (
                _round(_safe_float(selected_weather.get("wind_gusts_10m")), 3)
                if model_usable
                else None
            ),
            "effective_air_density_index": (
                _round((density / 1.225) if density is not None else None, 6)
                if model_usable
                else None
            ),
            "effective_density_altitude_ft": _round(density_altitude, 1) if model_usable else None,
            "effective_wind_out_component_mph": (
                wind_components.get("wind_out_component_mph") if model_usable else None
            ),
            "effective_wind_cross_component_mph": (
                wind_components.get("wind_cross_component_mph") if model_usable else None
            ),
        }

        rows.append(
            {
                "game_pk": game.get("game_pk"),
                "game_date_utc": game.get("game_date_utc"),
                "away_abbrev": game.get("away_abbrev"),
                "home_abbrev": game.get("home_abbrev"),
                "away_name": game.get("away_name"),
                "home_name": game.get("home_name"),
                "venue_name": venue_name,
                "status_abstract": game.get("status_abstract"),
                "status_detailed": game.get("status_detailed"),
                "stadium": stadium.as_cache_dict() if stadium else None,
                "source": {
                    "provider": provider,
                    "status": source_status,
                    "error": source_error,
                    "selected_time_utc": selected_weather.get("selected_time_utc", ""),
                    "selected_offset_minutes": selected_weather.get("selected_offset_minutes"),
                },
                "weather": {
                    "temp_f": _round(temp_f, 2),
                    "relative_humidity_pct": _round(humidity, 2),
                    "dewpoint_f": _round(dewpoint_f, 2),
                    "pressure_msl_hpa": _round(pressure_msl_hpa, 3),
                    "surface_pressure_hpa": _round(pressure_hpa, 3),
                    "precipitation_probability_pct": _safe_float(selected_weather.get("precipitation_probability")),
                    "precipitation_in": _safe_float(selected_weather.get("precipitation")),
                    "weather_code": selected_weather.get("weather_code"),
                    "wind_speed_mph": _round(wind_speed, 3),
                    "wind_from_deg": _round(wind_from, 3),
                    "wind_gust_mph": _round(_safe_float(selected_weather.get("wind_gusts_10m")), 3),
                },
                "derived": {
                    "weather_active_default": weather_active_default,
                    "weather_roof_state_assumption": roof_state_assumption,
                    "weather_roof_uncertain": roof_uncertain,
                    "weather_model_usable": model_usable,
                    "air_density_kg_m3": _round(density, 6),
                    "air_density_index": _round((density / 1.225) if density is not None else None, 6),
                    "density_altitude_ft": _round(density_altitude, 1),
                    **wind_components,
                    **effective,
                },
            }
        )

    coverage = {
        "scheduled_games": len(rows),
        "metadata_matched": sum(1 for row in rows if row.get("stadium")),
        "provider_ok": sum(1 for row in rows if (row.get("source") or {}).get("status") == "ok"),
        "provider_missing": sum(1 for row in rows if (row.get("source") or {}).get("status") == "provider_missing"),
        "provider_disabled": sum(1 for row in rows if (row.get("source") or {}).get("status") == "provider_disabled"),
        "missing_metadata": sum(1 for row in rows if (row.get("source") or {}).get("status") == "missing_metadata"),
        "fixed_roof_default": sum(
            1 for row in rows
            if ((row.get("stadium") or {}).get("weather_exposure") == "fixed_roof")
        ),
        "retractable_roof_default": sum(
            1 for row in rows
            if ((row.get("stadium") or {}).get("weather_exposure") == "retractable")
        ),
    }
    return {
        "schema_version": 1,
        "generated_at_utc": _now_iso(),
        "date": date_str,
        "provider": provider,
        "metadata_path": str(metadata_path),
        "coverage": coverage,
        "warnings": sorted(set(warnings)),
        "games": rows,
        "notes": [
            "Weather v2 is the canonical live weather source for Stage-2 fair-value adjustments.",
            "Retractable-roof games use static exposure until explicit roof-state data is validated.",
            "wind_out_component_mph is positive when modeled wind blows toward center field.",
        ],
    }


def write_game_weather_cache(payload: Mapping[str, Any], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return output_path


def default_weather_cache_path(date_str: str, cache_dir: Path = DEFAULT_WEATHER_CACHE_DIR) -> Path:
    return cache_dir / f"game_weather_{date_str}.json"


def flatten_weather_cache_game(row: Mapping[str, Any], *, cache_date: str = "") -> Dict[str, Any]:
    """Return compact pre-signal weather features for candidate/training rows."""
    source = row.get("source", {}) or {}
    stadium = row.get("stadium", {}) or {}
    weather = row.get("weather", {}) or {}
    derived = row.get("derived", {}) or {}
    out = {
        "weather_cache_available": True,
        "weather_cache_date": cache_date,
        "weather_source_provider": source.get("provider"),
        "weather_source_status": source.get("status"),
        "weather_source_error": source.get("error"),
        "weather_selected_time_utc": source.get("selected_time_utc"),
        "weather_selected_offset_minutes": source.get("selected_offset_minutes"),
        "stadium_id": stadium.get("stadium_id"),
        "stadium_primary_name": stadium.get("primary_name"),
        "stadium_roof_type": stadium.get("roof_type"),
        "stadium_weather_exposure": stadium.get("weather_exposure"),
        "stadium_weather_sensitivity": stadium.get("weather_sensitivity"),
        "stadium_elevation_ft": stadium.get("elevation_ft"),
        "stadium_outfield_bearing_deg": stadium.get("outfield_bearing_deg"),
        "weather_active_default": derived.get("weather_active_default"),
        "weather_roof_state_assumption": derived.get("weather_roof_state_assumption"),
        "weather_roof_uncertain": derived.get("weather_roof_uncertain"),
        "weather_model_usable": derived.get("weather_model_usable"),
        "weather_temp_f": weather.get("temp_f"),
        "weather_relative_humidity_pct": weather.get("relative_humidity_pct"),
        "weather_dewpoint_f": weather.get("dewpoint_f"),
        "weather_pressure_msl_hpa": weather.get("pressure_msl_hpa"),
        "weather_surface_pressure_hpa": weather.get("surface_pressure_hpa"),
        "weather_precipitation_probability_pct": weather.get("precipitation_probability_pct"),
        "weather_precipitation_in": weather.get("precipitation_in"),
        "weather_code": weather.get("weather_code"),
        "weather_wind_speed_mph": weather.get("wind_speed_mph"),
        "weather_wind_from_deg": weather.get("wind_from_deg"),
        "weather_wind_gust_mph": weather.get("wind_gust_mph"),
        "weather_air_density_kg_m3": derived.get("air_density_kg_m3"),
        "weather_air_density_index": derived.get("air_density_index"),
        "weather_density_altitude_ft": derived.get("density_altitude_ft"),
        "weather_wind_out_component_mph": derived.get("wind_out_component_mph"),
        "weather_wind_cross_component_mph": derived.get("wind_cross_component_mph"),
        "weather_effective_temp_f": derived.get("effective_temp_f"),
        "weather_effective_wind_speed_mph": derived.get("effective_wind_speed_mph"),
        "weather_effective_wind_gust_mph": derived.get("effective_wind_gust_mph"),
        "weather_effective_air_density_index": derived.get("effective_air_density_index"),
        "weather_effective_density_altitude_ft": derived.get("effective_density_altitude_ft"),
        "weather_effective_wind_out_component_mph": derived.get("effective_wind_out_component_mph"),
        "weather_effective_wind_cross_component_mph": derived.get("effective_wind_cross_component_mph"),
    }
    return {key: out.get(key) for key in WEATHER_FEATURE_FIELD_KEYS}


def weather_v2_run_env_game_data(
    weather_features: Optional[Mapping[str, Any]],
    *,
    venue_name: str = "",
    game_date_utc: str = "",
) -> Dict[str, Any]:
    """Build the Stage-2 RunEnvContext input from Weather v2 features only.

    Missing Weather v2 rows intentionally produce unknown temp/wind buckets
    instead of falling back to legacy schedule text.
    """
    features = dict(weather_features or {})
    park_name = str(features.get("stadium_primary_name") or venue_name or "")
    roof_type = str(features.get("stadium_roof_type") or "unknown")
    model_usable = weather_v2_features_model_usable(features)

    temp = _safe_float(features.get("weather_effective_temp_f")) if model_usable else None
    wind_text = _weather_v2_wind_text(features) if model_usable else ""

    return {
        "venue": {
            "name": park_name,
            "fieldInfo": {"roofType": roof_type},
        },
        "weather": {
            "temp": None if temp is None else int(round(float(temp))),
            "wind": wind_text,
        },
        "datetime": {
            "dateTime": str(game_date_utc or ""),
        },
    }


def weather_v2_source_label(weather_features: Optional[Mapping[str, Any]]) -> str:
    """Return a compact source label for Stage-2 runtime diagnostics."""
    features = dict(weather_features or {})
    if not features:
        return "weather_v2_missing"
    if features.get("weather_cache_available") is False:
        return "weather_v2_missing_game"
    status = str(features.get("weather_source_status") or "unknown")
    provider = str(features.get("weather_source_provider") or "unknown")
    usable = weather_v2_features_model_usable(features)
    return f"weather_v2:{provider}:{status}:{'usable' if usable else 'not_usable'}"


def weather_v2_features_model_usable(features: Mapping[str, Any]) -> bool:
    provider = str(features.get("weather_source_provider") or "").strip().lower()
    status = str(features.get("weather_source_status") or "").strip().lower()
    return bool(features.get("weather_model_usable")) and provider not in {"", "none"} and status == "ok"


def _weather_v2_wind_text(features: Mapping[str, Any]) -> str:
    speed = _safe_float(features.get("weather_effective_wind_speed_mph"))
    if speed is None:
        return ""
    out_component = _safe_float(features.get("weather_effective_wind_out_component_mph"))
    cross_component = _safe_float(features.get("weather_effective_wind_cross_component_mph")) or 0.0
    mph = max(0, int(round(float(speed))))
    if mph <= 0:
        return "0 mph, Calm"
    if out_component is None:
        direction = "Other"
    elif out_component >= 2.0:
        direction = "Out To CF"
    elif out_component <= -2.0:
        direction = "In From CF"
    elif cross_component >= 0:
        direction = "Left To Right"
    else:
        direction = "Right To Left"
    return f"{mph} mph, {direction}"


def load_weather_features_by_game(
    cache_path: Path,
) -> Dict[int, Dict[str, Any]]:
    """Load a weather cache file keyed by game_pk with flattened feature rows."""
    with open(cache_path, encoding="utf-8") as f:
        payload = json.load(f)
    cache_date = str(payload.get("date") or "")
    out: Dict[int, Dict[str, Any]] = {}
    for row in payload.get("games", []) or []:
        game_pk = _safe_int(row.get("game_pk")) if isinstance(row, Mapping) else None
        if game_pk is None:
            continue
        out[int(game_pk)] = flatten_weather_cache_game(row, cache_date=cache_date)
    return out
