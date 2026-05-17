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
