"""Stage-2 runtime tests. Focused on the new density_alt + (later) hr_factor
families and their backward-compatible runtime behavior."""
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "cache"))

from stage2_run_env_model import (  # noqa: E402
    FAMILY_ORDER,
    RunEnvContext,
    Stage2RunEnvModel,
    UNKNOWN_BUCKET,
    density_altitude_ft,
    lookup_hr_factor,
    parse_density_alt_bin,
    parse_hr_factor_bin,
    parse_temp_int,
    reset_park_hr_factors_cache,
    reset_stadium_metadata_cache,
)


def test_family_order_includes_density_alt():
    assert "density_alt" in FAMILY_ORDER


def test_family_order_includes_hr_factor():
    assert "hr_factor" in FAMILY_ORDER


def test_density_altitude_basic_arithmetic():
    # Standard formula: DA = elevation + 120 * (T_F - 59)
    # Coors @ 90F: 5200 + 120*31 = 5200 + 3720 = 8920
    assert density_altitude_ft(5200, 90) == pytest.approx(8920.0)
    # Sea level @ 59F (standard temp): DA = 0
    assert density_altitude_ft(0, 59) == pytest.approx(0.0)
    # Cold day actually shrinks DA below field elevation
    assert density_altitude_ft(1000, 30) == pytest.approx(1000 + 120 * (30 - 59))


def test_density_altitude_returns_none_when_inputs_missing():
    assert density_altitude_ft(None, 80) is None
    assert density_altitude_ft(5200, None) is None
    assert density_altitude_ft(None, None) is None


def test_density_alt_bin_levels():
    assert parse_density_alt_bin(None) == "unknown"
    assert parse_density_alt_bin(-200) == "<0"
    assert parse_density_alt_bin(500) == "0_1k"
    assert parse_density_alt_bin(1500) == "1k_2_5k"
    assert parse_density_alt_bin(3000) == "2_5k_5k"
    assert parse_density_alt_bin(8920) == "5k+"
    # Boundary checks (left-inclusive, right-exclusive)
    assert parse_density_alt_bin(0) == "0_1k"
    assert parse_density_alt_bin(1000) == "1k_2_5k"
    assert parse_density_alt_bin(2500) == "2_5k_5k"
    assert parse_density_alt_bin(5000) == "5k+"


def test_parse_temp_int_handles_strings_and_units():
    assert parse_temp_int("72") == 72
    assert parse_temp_int(" 72 ") == 72
    assert parse_temp_int("72 F") == 72
    assert parse_temp_int(72) == 72
    assert parse_temp_int(None) is None
    assert parse_temp_int("clear") is None
    assert parse_temp_int("") is None


def test_runenv_context_emits_density_alt_for_known_park():
    """RunEnvContext should look up Coors elevation from stadium metadata
    and emit a 5k+ density_alt bucket on a hot day."""
    reset_stadium_metadata_cache()
    ctx = RunEnvContext.from_game_data({
        "venue": {"name": "Coors Field"},
        "weather": {"temp": "90", "wind": "10 mph, Out To CF"},
    })
    buckets = ctx.buckets()
    assert buckets["density_alt"] == "5k+"
    assert buckets["park"] == "Coors Field"
    assert ctx.density_alt_bin == "5k+"


def test_runenv_context_emits_low_density_for_sea_level_park():
    reset_stadium_metadata_cache()
    ctx = RunEnvContext.from_game_data({
        "venue": {"name": "Petco Park"},
        "weather": {"temp": "65", "wind": "calm"},
    })
    assert ctx.buckets()["density_alt"] == "0_1k"


def test_runenv_context_unknown_when_park_not_in_metadata():
    reset_stadium_metadata_cache()
    ctx = RunEnvContext.from_game_data({
        "venue": {"name": "Made Up Stadium"},
        "weather": {"temp": "72"},
    })
    assert ctx.buckets()["density_alt"] == "unknown"


def test_runenv_context_unknown_when_temp_missing():
    reset_stadium_metadata_cache()
    ctx = RunEnvContext.from_game_data({
        "venue": {"name": "Coors Field"},
        "weather": {},
    })
    assert ctx.buckets()["density_alt"] == "unknown"


def test_runenv_context_accepts_explicit_metadata_override(tmp_path):
    """Test that callers can pass their own metadata dict (used by analyzers
    that already have stadium info loaded)."""
    custom = {
        "Coors Field": {"elevation_ft": 5200},
        "Petco Park": {"elevation_ft": 62},
    }
    ctx = RunEnvContext.from_game_data(
        {"venue": {"name": "Coors Field"}, "weather": {"temp": "90"}},
        stadium_metadata=custom,
    )
    assert ctx.density_alt_bin == "5k+"


def test_stage2_runtime_backward_compatible_with_legacy_model_json(tmp_path):
    """Legacy model JSONs without density_alt in tables/weights should still
    apply correctly, with density_alt contributing zero."""
    legacy_payload = {
        "meta": {"model_type": "stage2_run_environment_residual"},
        "weights": {
            "8.5": {"park": 0.5, "temp": 0.0, "wind": 0.0, "park_wind": 0.0}
            # NOTE: no density_alt key
        },
        "tables": {
            "park": {"8.5": {"Coors Field": {"delta": 0.20}}},
            "temp": {},
            "wind": {},
            "park_wind": {},
            # NOTE: no density_alt table
        },
        "constraints": {"max_total_abs_delta": 1.0},
    }
    path = tmp_path / "legacy_model.json"
    path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    reset_stadium_metadata_cache()
    model = Stage2RunEnvModel.from_path(path)
    ctx = RunEnvContext.from_game_data({
        "venue": {"name": "Coors Field"},
        "weather": {"temp": "90"},
    })
    # Should apply 0.5 * 0.20 = 0.10 logit shift from park; density_alt zero.
    adjusted = model.adjust_line("8.5", 0.5, ctx)
    assert 0.5 < adjusted < 0.6  # logit(0.5) + 0.10 -> sigmoid ~0.525


def test_hr_factor_bin_levels():
    assert parse_hr_factor_bin(None) == "unknown"
    assert parse_hr_factor_bin(0.70) == "<0.85"
    assert parse_hr_factor_bin(0.85) == "0.85_0.95"  # left-inclusive
    assert parse_hr_factor_bin(0.92) == "0.85_0.95"
    assert parse_hr_factor_bin(0.95) == "0.95_1.05"
    assert parse_hr_factor_bin(1.00) == "0.95_1.05"
    assert parse_hr_factor_bin(1.05) == "1.05_1.15"
    assert parse_hr_factor_bin(1.10) == "1.05_1.15"
    assert parse_hr_factor_bin(1.15) == "1.15+"
    assert parse_hr_factor_bin(1.40) == "1.15+"


def test_lookup_hr_factor_uses_explicit_year_when_available():
    factors = {
        "Coors Field": {"2023": 1.10, "2024": 1.18, "2025": 1.22},
    }
    assert lookup_hr_factor("Coors Field", "2024", hr_factors=factors) == pytest.approx(1.18)


def test_lookup_hr_factor_falls_back_to_most_recent_when_year_missing():
    """A 2026 game with no 2026 entry yet should use the most recent prior year."""
    factors = {
        "Coors Field": {"2023": 1.10, "2024": 1.18, "2025": 1.22},
    }
    assert lookup_hr_factor("Coors Field", "2026", hr_factors=factors) == pytest.approx(1.22)


def test_lookup_hr_factor_returns_none_for_unknown_park():
    factors = {"Coors Field": {"2024": 1.18}}
    assert lookup_hr_factor("Made Up Stadium", "2024", hr_factors=factors) is None


def test_runenv_context_emits_hr_factor_bin_when_factors_provided():
    reset_park_hr_factors_cache()
    factors = {
        "Coors Field": {"2025": 1.22},
        "Petco Park": {"2025": 0.82},
    }
    ctx = RunEnvContext.from_game_data(
        {"venue": {"name": "Coors Field"},
         "weather": {"temp": "85"},
         "datetime": {"dateTime": "2025-07-15T19:00:00Z"}},
        hr_factors=factors,
    )
    assert ctx.hr_factor_bin == "1.15+"
    ctx2 = RunEnvContext.from_game_data(
        {"venue": {"name": "Petco Park"},
         "weather": {"temp": "65"},
         "datetime": {"dateTime": "2025-07-15T19:00:00Z"}},
        hr_factors=factors,
    )
    assert ctx2.hr_factor_bin == "<0.85"


def test_runenv_context_hr_factor_unknown_when_no_cache_or_year():
    reset_park_hr_factors_cache()
    ctx = RunEnvContext.from_game_data(
        {"venue": {"name": "Made Up Stadium"}, "weather": {"temp": "72"}},
        hr_factors={},
    )
    assert ctx.hr_factor_bin == "unknown"


def test_runenv_context_explicit_year_kwarg_takes_precedence():
    reset_park_hr_factors_cache()
    factors = {"Coors Field": {"2024": 1.10, "2025": 1.22}}
    # Even though datetime says 2025, an explicit year=2024 wins.
    ctx = RunEnvContext.from_game_data(
        {"venue": {"name": "Coors Field"},
         "weather": {"temp": "85"},
         "datetime": {"dateTime": "2025-07-15T19:00:00Z"}},
        hr_factors=factors,
        year="2024",
    )
    assert ctx.hr_factor_bin == "1.05_1.15"


def test_stage2_runtime_applies_density_alt_when_present(tmp_path):
    """When the model JSON has density_alt weights and table, the runtime
    should incorporate them on top of park."""
    payload = {
        "meta": {"model_type": "stage2_run_environment_residual"},
        "weights": {
            "8.5": {
                "park": 0.5, "temp": 0.0, "wind": 0.0,
                "park_wind": 0.0, "density_alt": 0.5,
            }
        },
        "tables": {
            "park": {"8.5": {"Coors Field": {"delta": 0.10}}},
            "temp": {}, "wind": {}, "park_wind": {},
            "density_alt": {"8.5": {"5k+": {"delta": 0.20}}},
        },
        "constraints": {"max_total_abs_delta": 1.0},
    }
    path = tmp_path / "model.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    reset_stadium_metadata_cache()
    model = Stage2RunEnvModel.from_path(path)
    ctx = RunEnvContext.from_game_data({
        "venue": {"name": "Coors Field"},
        "weather": {"temp": "90"},
    })
    # Total delta = 0.5 * 0.10 + 0.5 * 0.20 = 0.05 + 0.10 = 0.15
    # logit(0.5) + 0.15 = 0.15 -> sigmoid ~0.5374
    adjusted = model.adjust_line("8.5", 0.5, ctx)
    assert adjusted == pytest.approx(0.5374, abs=0.001)
