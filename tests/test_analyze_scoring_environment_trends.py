import json
from pathlib import Path

from scripts.analysis import analyze_scoring_environment_trends as trends


def _write_game(root: Path, *, game_pk: int, date: str, away: int, home: int, venue_id: int = 1):
    year, month, day = date.split("-")
    path = root / year / month / day / f"{game_pk}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "gamePk": game_pk,
        "gameData": {
            "game": {"type": "R"},
            "datetime": {"officialDate": date},
            "status": {"abstractGameState": "Final", "detailedState": "Final"},
            "venue": {"id": venue_id, "name": f"Venue {venue_id}"},
        },
        "liveData": {
            "linescore": {
                "teams": {
                    "away": {"runs": away},
                    "home": {"runs": home},
                }
            },
            "plays": {"allPlays": [{"result": {"awayScore": away, "homeScore": home}}]},
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_games_dedupes_by_game_pk_and_prefers_official_date_path(tmp_path):
    root = tmp_path / "regular"
    _write_game(root, game_pk=1, date="2025-04-02", away=3, home=4)
    duplicate = _write_game(root, game_pk=1, date="2025-04-02", away=5, home=5)
    moved = root / "2025" / "04" / "01" / "1.json"
    moved.parent.mkdir(parents=True, exist_ok=True)
    moved.write_text(duplicate.read_text(encoding="utf-8"), encoding="utf-8")
    _write_game(root, game_pk=2, date="2026-04-02", away=2, home=1)

    games, meta = trends.load_games(
        root,
        min_season=2025,
        max_season=2026,
        allowed_game_types={"R"},
    )

    assert [g.game_pk for g in games] == [1, 2]
    assert meta["duplicate_game_files_skipped"] == 1
    assert games[0].official_date == "2025-04-02"


def test_build_report_contains_season_projection_and_weights(tmp_path):
    root = tmp_path / "regular"
    for i, season in enumerate(range(2021, 2026), 1):
        _write_game(root, game_pk=season * 10 + 1, date=f"{season}-04-01", away=3 + i, home=4, venue_id=1)
        _write_game(root, game_pk=season * 10 + 2, date=f"{season}-04-02", away=2, home=4 + i, venue_id=2)
    _write_game(root, game_pk=202601, date="2026-04-01", away=5, home=5, venue_id=1)
    games, meta = trends.load_games(root, min_season=2021, max_season=2026, allowed_game_types={"R"})

    report = trends.build_report(
        games,
        meta=meta,
        lines=["7.5", "8.5"],
        history_start_season=2021,
        history_end_season=2025,
        projection_season=2026,
    )

    assert report["projection"]["status"] == "ok"
    assert report["projection"]["games_ytd"] == 1
    assert report["season_stats"]
    assert report["recommended_stage1_season_weights"]
    assert abs(sum(row["weight"] for row in report["recommended_stage1_season_weights"]) - 1.0) < 1e-6


def test_weighting_backtest_ranks_schemes_with_small_minimum():
    records = [
        trends.GameRecord(
            game_pk=season,
            official_date=f"{season}-04-01",
            season=season,
            month=4,
            away_runs=4,
            home_runs=4 + (season - 2020),
            total_runs=8 + (season - 2020),
            venue_id="1",
            venue_name="Venue",
            game_type="R",
            path="",
        )
        for season in range(2021, 2026)
    ]

    rows = trends.backtest_weighting_schemes(records, min_train_seasons=3, full_season_min_games=1)

    assert rows
    assert {row["scheme"] for row in rows} >= {"recent3_uniform", "linear_trend"}
    assert all(row["test_seasons"] >= 1 for row in rows)
