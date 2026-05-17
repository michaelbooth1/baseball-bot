from pathlib import Path

from scripts.analysis import evaluate_stage1_prior_candidates as prior


def _season_stats():
    return [
        {"season": 2021, "games": 100, "runs_per_game": 9.0},
        {"season": 2022, "games": 100, "runs_per_game": 8.6},
        {"season": 2023, "games": 100, "runs_per_game": 9.2},
        {"season": 2024, "games": 100, "runs_per_game": 8.8},
        {"season": 2025, "games": 100, "runs_per_game": 8.9},
    ]


def test_generate_candidates_includes_rolling_and_blends_with_normalized_weights():
    candidates = prior.generate_candidates(_season_stats(), target_rpg=8.85)
    names = {c["candidate"] for c in candidates}

    assert "rolling3_game_weighted" in names
    assert "rolling4_game_weighted" in names
    assert "exp_hl_2p0" in names
    assert "scoring_env_hl_3p0_scale_0p35" in names
    assert "blend_recent3_scoring_env_a50" in names
    for candidate in candidates:
        assert abs(sum(candidate["weights"].values()) - 1.0) < 1e-9


def test_candidate_target_rpg_uses_weighted_season_average():
    candidate = {"weights": {2021: 0.25, 2022: 0.75}}

    assert prior.candidate_target_rpg(candidate, _season_stats()) == 8.7


def test_dedupe_game_line_family_keeps_one_row_per_game_line():
    rows = [
        {"signal_model_family": "score_event_transition", "game_pk": 1, "line": "8.5"},
        {"signal_model_family": "score_event_transition", "game_pk": 1, "line": "8.5"},
        {"signal_model_family": "no_score_drift", "game_pk": 1, "line": "8.5"},
    ]

    out = prior._dedupe(rows, "game_line_family")

    assert len(out) == 2
