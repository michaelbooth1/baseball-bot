import json
from pathlib import Path

from scripts.trading import weather_client as wc


def _schedule_payload():
    return {
        "dates": [
            {
                "games": [
                    {
                        "gamePk": 123,
                        "gameDate": "2026-05-06T23:05:00Z",
                        "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
                        "venue": {"name": "Wrigley Field", "fieldInfo": {"roofType": "Open"}},
                        "weather": {
                            "temp": "70",
                            "condition": "Partly Cloudy",
                            "wind": "12 mph, Out To CF",
                        },
                        "teams": {
                            "away": {"team": {"abbreviation": "STL", "name": "St. Louis Cardinals"}},
                            "home": {"team": {"abbreviation": "CHC", "name": "Chicago Cubs"}},
                        },
                    }
                ]
            }
        ]
    }


class _FakeOpenMeteo:
    def __init__(self):
        self.calls = []

    def fetch_hourly_forecast(self, *, latitude, longitude, start_date, end_date):
        self.calls.append(
            {
                "latitude": latitude,
                "longitude": longitude,
                "start_date": start_date,
                "end_date": end_date,
            }
        )
        return {
            "hourly": {
                "time": ["2026-05-06T22:00", "2026-05-06T23:00", "2026-05-07T00:00"],
                "temperature_2m": [66.0, 72.0, 71.0],
                "relative_humidity_2m": [55, 50, 52],
                "dew_point_2m": [49.0, 52.0, 52.0],
                "pressure_msl": [1012.0, 1011.0, 1011.0],
                "surface_pressure": [990.0, 989.0, 989.0],
                "precipitation_probability": [5, 5, 10],
                "precipitation": [0.0, 0.0, 0.0],
                "weather_code": [1, 1, 2],
                "wind_speed_10m": [8.0, 12.0, 11.0],
                "wind_direction_10m": [220.0, 220.0, 230.0],
                "wind_gusts_10m": [14.0, 18.0, 17.0],
            }
        }


def test_metadata_matches_aliases_and_primary_names():
    index = wc.StadiumMetadataIndex.from_path(wc.DEFAULT_STADIUM_METADATA_PATH)

    assert index.match("Wrigley Field").stadium_id == "wrigley_field"
    assert index.match("Minute Maid Park").stadium_id == "daikin_park"
    assert index.match("Guaranteed Rate Field").stadium_id == "rate_field"


def test_build_game_weather_cache_derives_density_and_wind_components():
    fake = _FakeOpenMeteo()

    payload = wc.build_game_weather_cache(
        date_str="2026-05-06",
        provider="open-meteo",
        schedule_payload=_schedule_payload(),
        open_meteo_client=fake,
    )

    assert payload["coverage"]["scheduled_games"] == 1
    assert payload["coverage"]["metadata_matched"] == 1
    assert payload["coverage"]["provider_ok"] == 1
    assert len(fake.calls) == 1

    game = payload["games"][0]
    assert game["stadium"]["stadium_id"] == "wrigley_field"
    assert game["source"]["selected_time_utc"] == "2026-05-06T23:00:00Z"
    assert game["weather"]["temp_f"] == 72.0
    assert game["derived"]["air_density_kg_m3"] is not None
    assert game["derived"]["air_density_index"] is not None
    assert game["derived"]["wind_out_component_mph"] > 0
    assert game["derived"]["weather_roof_state_assumption"] == "open_air_active"
    assert game["derived"]["weather_roof_uncertain"] is False
    assert game["derived"]["weather_model_usable"] is True
    assert game["derived"]["effective_temp_f"] == 72.0
    assert game["derived"]["effective_wind_out_component_mph"] > 0


def test_write_game_weather_cache(tmp_path):
    out = tmp_path / "weather" / "game_weather_2026-05-06.json"
    payload = {"schema_version": 1, "date": "2026-05-06", "games": []}

    wc.write_game_weather_cache(payload, out)

    assert json.loads(out.read_text(encoding="utf-8"))["date"] == "2026-05-06"


def test_provider_none_uses_unknown_weather_buckets():
    payload = wc.build_game_weather_cache(
        date_str="2026-05-06",
        provider="none",
        schedule_payload=_schedule_payload(),
    )

    game = payload["games"][0]
    assert payload["coverage"]["provider_disabled"] == 1
    assert game["weather"]["temp_f"] is None
    assert game["weather"]["wind_speed_mph"] is None
    assert game["derived"]["wind_out_component_mph"] is None
    assert game["derived"]["weather_model_usable"] is False


def test_flatten_and_load_weather_features_by_game(tmp_path):
    fake = _FakeOpenMeteo()
    payload = wc.build_game_weather_cache(
        date_str="2026-05-06",
        provider="open-meteo",
        schedule_payload=_schedule_payload(),
        open_meteo_client=fake,
    )
    cache_path = tmp_path / "game_weather_2026-05-06.json"
    wc.write_game_weather_cache(payload, cache_path)

    by_game = wc.load_weather_features_by_game(cache_path)

    assert set(by_game) == {123}
    row = by_game[123]
    assert set(row) == set(wc.WEATHER_FEATURE_FIELD_KEYS)
    assert row["weather_cache_available"] is True
    assert row["weather_cache_date"] == "2026-05-06"
    assert row["stadium_id"] == "wrigley_field"
    assert row["weather_temp_f"] == 72.0
    assert row["weather_wind_out_component_mph"] > 0
    assert row["weather_model_usable"] is True
    assert row["weather_roof_state_assumption"] == "open_air_active"
    assert row["weather_effective_temp_f"] == 72.0
    assert row["weather_effective_wind_out_component_mph"] > 0
    assert "weather_cache_date" not in wc.WEATHER_MODEL_FEATURE_FIELD_KEYS
    assert "stadium_id" not in wc.WEATHER_MODEL_FEATURE_FIELD_KEYS
    assert "weather_effective_wind_out_component_mph" in wc.WEATHER_MODEL_FEATURE_FIELD_KEYS


def test_weather_v2_run_env_game_data_uses_provider_features_only():
    fake = _FakeOpenMeteo()
    payload = wc.build_game_weather_cache(
        date_str="2026-05-06",
        provider="open-meteo",
        schedule_payload=_schedule_payload(),
        open_meteo_client=fake,
    )
    row = wc.flatten_weather_cache_game(payload["games"][0], cache_date="2026-05-06")

    game_data = wc.weather_v2_run_env_game_data(
        row,
        venue_name="Ignored Park",
        game_date_utc="2026-05-06T23:05:00Z",
    )

    assert game_data["venue"]["name"] == "Wrigley Field"
    assert game_data["venue"]["fieldInfo"]["roofType"] == "open"
    assert game_data["weather"]["temp"] == 72
    assert "Out To CF" in game_data["weather"]["wind"]


def test_weather_v2_run_env_game_data_uses_unknown_buckets_when_unusable():
    row = {
        "weather_cache_available": True,
        "weather_source_status": "provider_disabled",
        "stadium_primary_name": "Wrigley Field",
        "stadium_roof_type": "open",
        "weather_model_usable": False,
    }

    game_data = wc.weather_v2_run_env_game_data(row, venue_name="Wrigley Field")

    assert game_data["weather"]["temp"] is None
    assert game_data["weather"]["wind"] == ""
