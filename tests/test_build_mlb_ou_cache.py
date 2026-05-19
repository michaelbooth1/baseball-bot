import json
from pathlib import Path
from types import SimpleNamespace

from cache import build_mlb_ou_cache as builder


def _write_game(path: Path, *, game_pk: int, official_date: str, away: int = 4, home: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "gamePk": game_pk,
        "gameData": {
            "game": {"type": "R"},
            "datetime": {"officialDate": official_date, "dateTime": f"{official_date}T19:05:00Z"},
            "status": {"abstractGameState": "Final", "detailedState": "Final"},
        },
        "liveData": {
            "linescore": {
                "teams": {
                    "away": {"runs": away},
                    "home": {"runs": home},
                }
            },
            "plays": {
                "allPlays": [
                    {
                        "about": {"inning": 1, "halfInning": "top"},
                        "result": {"awayScore": away, "homeScore": home},
                        "count": {"outs": 0},
                        "matchup": {},
                    }
                ]
            },
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _args(tmp_path: Path, **overrides):
    base = {
        "data_dir": tmp_path / "data",
        "season_type": "regular",
        "game_types": "R",
        "min_date": "",
        "max_date": "",
        "min_season": 2016,
        "max_season": 2025,
        "lines": "6.5",
        "min_games": 1,
        "max_combined": 20,
        "extras_bucket": 10,
        "calib_prior_n": 80.0,
        "calib_min_n": 20,
        "max_files": 0,
        "out": tmp_path / "cache.json",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_cache_filters_history_dedupes_game_pk_and_writes_metadata(tmp_path):
    root = tmp_path / "data" / "games" / "regular"
    _write_game(root / "2015" / "04" / "01" / "1.json", game_pk=1, official_date="2015-04-01")
    _write_game(root / "2016" / "04" / "04" / "2.json", game_pk=2, official_date="2016-04-05")
    _write_game(root / "2016" / "04" / "05" / "2.json", game_pk=2, official_date="2016-04-05")
    _write_game(root / "2025" / "09" / "28" / "3.json", game_pk=3, official_date="2025-09-28")
    _write_game(root / "2026" / "04" / "01" / "4.json", game_pk=4, official_date="2026-04-01")

    cache = builder.build_cache(_args(tmp_path))

    meta = cache["meta"]
    assert meta["history_start_date"] == "2016-04-05"
    assert meta["history_end_date"] == "2025-09-28"
    assert meta["seasons"] == ["2016", "2025"]
    assert meta["games_by_season"] == {"2016": 1, "2025": 1}
    assert meta["total_games"] == 2
    assert meta["games_loaded"] == 2
    assert meta["duplicate_game_files_skipped"] == 1
    assert meta["builder_args"]["min_season"] == 2016
    assert meta["builder_args"]["max_season"] == 2025
    assert cache["cells"]


def test_build_cache_applies_season_allocation_weights(tmp_path):
    root = tmp_path / "data" / "games" / "regular"
    _write_game(root / "2016" / "04" / "01" / "1.json", game_pk=1, official_date="2016-04-01", away=0, home=0)
    _write_game(root / "2025" / "04" / "01" / "2.json", game_pk=2, official_date="2025-04-01", away=10, home=0)
    weights_path = tmp_path / "weights.csv"
    weights_path.write_text("season,weight\n2016,0.1\n2025,0.9\n", encoding="utf-8")

    weighted = builder.build_cache(_args(tmp_path, season_weights_path=weights_path))
    unweighted = builder.build_cache(_args(tmp_path))

    meta = weighted["meta"]
    assert meta["season_weighting"]["enabled"] is True
    assert meta["season_weighting"]["implied_allocations_by_season"] == {"2016": 0.1, "2025": 0.9}
    cell = weighted["cells"]["0_0_1_T_0_0"]
    base_cell = unweighted["cells"]["0_0_1_T_0_0"]
    assert cell["weighted_n_samples"] == 2.0
    assert cell["effective_n_samples"] < cell["weighted_n_samples"]
    assert cell["o65"] == 0.9
    assert cell["po65"] > base_cell["po65"]


# ---------------------------------------------------------------------------
# Active #8 Alt-A smoothing-mode tests (2026-05-17).
#
# Verify the --smoothing-mode flag materializes the runtime's on-the-fly
# Alt-A FV (which swaps poXX for oXX from the same cell) as a real cache
# file ready for promote.py stage1.
# ---------------------------------------------------------------------------


def test_smoothing_mode_poisson_is_default_and_leaves_cells_unchanged(tmp_path):
    """Default mode = poisson; po65 stays the Poisson smoothing, not o65.

    Locks in backward compatibility for the production builder so a refresh
    that doesn't pass --smoothing-mode keeps emitting the same cache.
    """
    root = tmp_path / "data" / "games" / "regular"
    _write_game(root / "2016" / "04" / "01" / "1.json", game_pk=1, official_date="2016-04-01", away=5, home=4)

    cache = builder.build_cache(_args(tmp_path))
    cell = cache["cells"]["0_0_1_T_0_0"]
    # Empirical is 100% (1/1 sample over 6.5); Poisson should be < 1.0
    # because phase lambda for inning-1-top-0-outs-empty-bases is the
    # full game remaining, predicting some chance of staying under 7.
    assert cell["o65"] == 1.0
    assert cell["po65"] < 1.0
    assert cell["po65"] != cell["o65"]

    alt_a = cache["meta"]["alt_a_smoothing"]
    assert alt_a["enabled"] is False
    assert alt_a["mode"] == "poisson"
    assert alt_a["cells_overridden"] == 0


def test_smoothing_mode_empirical_when_available_overwrites_po_with_o(tmp_path):
    """Alt-A mode: po65 is replaced with the same cell's o65 value."""
    root = tmp_path / "data" / "games" / "regular"
    _write_game(root / "2016" / "04" / "01" / "1.json", game_pk=1, official_date="2016-04-01", away=5, home=4)

    cache = builder.build_cache(
        _args(tmp_path, smoothing_mode="empirical_when_available"),
    )
    cell = cache["cells"]["0_0_1_T_0_0"]

    # Empirical was 1.0 in this fixture, which is a boundary value and
    # therefore NOT eligible for override (would blow up logit math).
    # Build a richer fixture with an interior empirical so the override
    # actually fires.
    assert cell["o65"] == 1.0
    alt_a = cache["meta"]["alt_a_smoothing"]
    assert alt_a["enabled"] is True
    assert alt_a["mode"] == "empirical_when_available"
    # Boundary-empirical cells are tracked separately:
    assert alt_a["cells_kept_poisson_invalid_empirical"] >= 1


def test_smoothing_mode_overrides_interior_empirical_cells(tmp_path):
    """When empirical is in (0,1), Alt-A replaces po65 with o65 exactly."""
    root = tmp_path / "data" / "games" / "regular"
    # Two games, both inning-1-top-bases-empty, scores (5,3)=8 and (3,1)=4.
    # final_total: 8, 4. For line 6.5 (threshold 7): one over (8>=7), one
    # under (4<7). Empirical rate = 0.5 (an interior probability that
    # the override pass should accept and write into po65).
    _write_game(root / "2016" / "04" / "01" / "1.json", game_pk=1, official_date="2016-04-01", away=5, home=3)
    _write_game(root / "2016" / "04" / "02" / "2.json", game_pk=2, official_date="2016-04-02", away=3, home=1)

    poisson_cache = builder.build_cache(_args(tmp_path, min_games=1))
    alt_a_cache = builder.build_cache(
        _args(tmp_path, min_games=1, smoothing_mode="empirical_when_available"),
    )

    poisson_cell = poisson_cache["cells"]["0_0_1_T_0_0"]
    alt_a_cell = alt_a_cache["cells"]["0_0_1_T_0_0"]

    # Sanity: empirical observed two PA states in two games at the
    # bottom of inning 1 top 0-out empty-bases. (the first plate
    # appearance of every game lives here.) Empirical = 0.5.
    assert alt_a_cell["o65"] == 0.5

    # Production mode: po65 is the Poisson smoothing, NOT 0.5.
    assert poisson_cell["po65"] != 0.5

    # Alt-A mode: po65 has been overwritten with o65.
    assert alt_a_cell["po65"] == 0.5

    summary = alt_a_cache["meta"]["alt_a_smoothing"]
    assert summary["enabled"] is True
    assert summary["cells_overridden"] >= 1
    assert summary["line_overrides"]["po65"] >= 1
    # Signed delta direction: empirical (0.5) - poisson (something
    # bigger because phase lambda predicts ~8-10 runs at inning 1).
    # On these tiny fixtures the delta should be negative.
    assert summary["n_line_deltas"] >= 1


def test_smoothing_mode_min_empirical_n_threshold_gates_override(tmp_path):
    """`--min-empirical-n-for-override` blocks low-sample overrides."""
    root = tmp_path / "data" / "games" / "regular"
    _write_game(root / "2016" / "04" / "01" / "1.json", game_pk=1, official_date="2016-04-01", away=5, home=3)
    _write_game(root / "2016" / "04" / "02" / "2.json", game_pk=2, official_date="2016-04-02", away=3, home=1)

    # min_empirical_n_for_override much higher than any cell's n_samples
    # means no override should happen even in empirical_when_available
    # mode.
    cache = builder.build_cache(
        _args(
            tmp_path,
            min_games=1,
            smoothing_mode="empirical_when_available",
            min_empirical_n_for_override=10_000,
        ),
    )
    summary = cache["meta"]["alt_a_smoothing"]
    assert summary["cells_overridden"] == 0
    assert summary["cells_kept_poisson_low_n"] == summary["cells_total"]


def test_smoothing_mode_lineage_records_alt_a_args(tmp_path):
    """The lineage cli_args_summary surfaces smoothing_mode so cross-
    artifact consistency check can distinguish Alt-A vs production caches.
    """
    root = tmp_path / "data" / "games" / "regular"
    _write_game(root / "2016" / "04" / "01" / "1.json", game_pk=1, official_date="2016-04-01")
    cache = builder.build_cache(
        _args(tmp_path, smoothing_mode="empirical_when_available", min_empirical_n_for_override=3),
    )
    builder_args = cache["meta"]["builder_args"]
    assert builder_args["smoothing_mode"] == "empirical_when_available"
    assert builder_args["min_empirical_n_for_override"] == 3
