import json
from pathlib import Path

from scripts.analysis import run_daily_refresh as rdr


def _touch(path: Path, text: str = "{}") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _minimal_config(tmp_path: Path, **overrides) -> rdr.RefreshConfig:
    """RefreshConfig with all the new (2026-05-08) startup-canonical steps off
    by default, so legacy assertions about exact step lists stay meaningful.
    Tests that exercise the new steps opt back in explicitly.
    """
    defaults = dict(
        active_date="2026-05-06",
        refresh_pitcher_cache=False,
        refresh_recent_games=False,
        refresh_active_schedule=False,
        refresh_stage1_cache=False,
        refresh_team_game_log=False,
        refresh_park_hr_factors=False,
        run_preflight_secrets=False,
        run_preflight_artifacts=False,
        sessions_dir=tmp_path / "sessions",
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        # Route stability-gate history paths under tmp so handler tests
        # never write to the canonical repo paths. Otherwise every test
        # that exercises model_freshness_health with a well-formed cache
        # pair would silently append a row to data/analysis_output/.
        stage2_brier_history_path=tmp_path / "calibration" / "stage2_brier_history.jsonl",
        stage3_v2_drift_history_path=tmp_path / "calibration" / "stage3_v2_drift_history.jsonl",
    )
    defaults.update(overrides)
    return rdr.RefreshConfig(**defaults)


def test_latest_refreshable_date_excludes_active_run_date_by_default():
    dates = ["2026-05-03", "2026-05-04", "2026-05-05", "2026-05-06"]

    assert rdr.latest_refreshable_date(dates, active_date="2026-05-06") == "2026-05-05"
    assert (
        rdr.latest_refreshable_date(dates, active_date="2026-05-06", include_run_date=True)
        == "2026-05-06"
    )
    assert (
        rdr.latest_refreshable_date(dates, active_date="2026-05-06", max_date="2026-05-04")
        == "2026-05-04"
    )


def test_daily_review_refresh_detects_missing_and_stale_outputs(tmp_path):
    sessions = tmp_path / "sessions"
    candidates = tmp_path / "candidates"
    logs = tmp_path / "logs"
    reviews = tmp_path / "reviews"

    _touch(sessions / "2026-05-04_session.json")
    _touch(candidates / "2026-05-04_candidate_rollup.json")
    _touch(logs / "2026-05-04.log", "log")

    assert rdr.daily_review_dates_needing_refresh(
        ["2026-05-04"],
        max_date="2026-05-04",
        sessions_dir=sessions,
        candidate_dir=candidates,
        log_dir=logs,
        review_dir=reviews,
    ) == ["2026-05-04"]

    _touch(reviews / "2026-05-04_human_review.json")
    _touch(reviews / "2026-05-04_human_review.md")
    assert rdr.daily_review_dates_needing_refresh(
        ["2026-05-04"],
        max_date="2026-05-04",
        sessions_dir=sessions,
        candidate_dir=candidates,
        log_dir=logs,
        review_dir=reviews,
    ) == []


def test_build_refresh_steps_includes_canonical_tables_and_research_outputs(tmp_path):
    sessions = tmp_path / "sessions"
    candidates = tmp_path / "candidates"
    logs = tmp_path / "logs"
    _touch(sessions / "2026-05-05_session.json")

    config = _minimal_config(tmp_path, sessions_dir=sessions, candidate_dir=candidates, log_dir=logs)
    steps = rdr.build_refresh_steps(config, ["2026-05-05"], "2026-05-05")
    names = [step.name for step in steps]

    assert "game_weather_cache" in names
    assert "daily_human_review:2026-05-05" in names
    assert "candidate_universe_table" in names
    assert "calibration_opportunity_training" in names
    assert "calibrate_signal_probabilities" in names
    assert "model_maturity_report" in names
    assert "fair_value_stage_ablation" in names
    assert "fv_gap_decomposition" in names
    assert "fv_trust_shrinkage" in names
    assert "calibration_market_anchored_alpha" in names
    assert "stage1_inferred_empirical_audit" in names
    assert "unified_signals" in names
    assert "signal_training_table" in names
    assert "clv_report" in names
    assert "fv_disagreement_quality" in names
    # EV-policy retraining wired into startup so the runtime always loads
    # fresh win + fill models (was a manual research step before 2026-05-12).
    assert "train_baseline_models" in names
    assert "ev_policy_backtest" in names
    # Stage-2 retrains to STAGING; the freshness handler diffs it against
    # production and surfaces any meaningful Brier drift.
    assert "stage2_run_env_retrain_staging" in names
    # Stage-3 v2 retraining: feature build -> calibration table -> fit.
    assert "stage3_team_offense_features" in names
    assert "stage3_team_offense_calibration_table" in names
    assert "stage3_team_offense_v2_fit" in names
    assert "model_freshness_health" in names
    assert "execution_diagnostics" in names
    assert "queue_aware_execution_replay" in names
    assert "learn_execution_policy" in names
    assert "state_value_transition_report" in names
    assert "no_score_drift_policy" in names
    assert "no_score_drift_paper_ledger" in names
    assert "walk_forward_score_event" in names
    assert "walk_forward_no_score_drift" in names
    assert "walk_forward_market_anchored_alpha" in names
    assert "walk_forward_fv_disagreement_quality" in names
    alpha_wf = [step for step in steps if step.name == "walk_forward_market_anchored_alpha"][0]
    assert "calibration_market_anchored_alpha_walk_forward.py" in alpha_wf.command[1]
    assert alpha_wf.staleness_check is not None
    fv_disagreement_wf = [step for step in steps if step.name == "walk_forward_fv_disagreement_quality"][0]
    assert "fv_disagreement_quality_walk_forward.py" in fv_disagreement_wf.command[1]
    assert fv_disagreement_wf.staleness_check is not None
    # UNDER single-gate-bottleneck guardrail (2026-05-30).
    assert "under_gate_bottleneck_audit" in names
    # End-of-refresh operator summary.
    assert "refresh_health_rollup" in names


def test_plan_only_writes_manifest_without_running_subprocesses(tmp_path):
    sessions = tmp_path / "sessions"
    _touch(sessions / "2026-05-05_session.json")

    config = _minimal_config(
        tmp_path,
        sessions_dir=sessions,
        refresh_daily_reviews=False,
        run_walk_forward=False,
        plan_only=True,
    )

    payload = rdr.run_startup_refresh(config)
    manifest_path = Path(payload["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest_path.name == "2026-05-06_startup_refresh_plan.json"
    assert manifest["manifest_kind"] == "plan"
    assert manifest["max_refresh_date"] == "2026-05-05"
    assert manifest["steps_failed"] == 0
    assert {step["status"] for step in manifest["steps"]} == {"planned"}
    assert "unified_signals" in {step["name"] for step in manifest["steps"]}
    # Schema-v2 manifest fields.
    assert manifest["schema_version"] == 2
    assert "summary" in manifest
    assert "logs_dir_bytes" in manifest


def test_weather_cache_step_runs_even_without_completed_session(tmp_path):
    config = _minimal_config(
        tmp_path,
        refresh_daily_reviews=False,
        run_walk_forward=False,
    )

    steps = rdr.build_refresh_steps(config, [], None)

    # game_meta_cache (Tier-2 umpire cache, 2026-05-29) rides on the same
    # refresh_weather_cache gate as the weather step.
    assert [step.name for step in steps] == ["game_weather_cache", "game_meta_cache"]


def test_weather_cache_can_be_skipped(tmp_path):
    config = _minimal_config(
        tmp_path,
        refresh_weather_cache=False,
        refresh_daily_reviews=False,
        run_walk_forward=False,
    )

    assert rdr.build_refresh_steps(config, [], None) == []


# ---------------------------------------------------------------------------
# New (2026-05-08): startup is the canonical daily refresh.
# ---------------------------------------------------------------------------


def test_canonical_startup_includes_park_hr_factors_step(tmp_path):
    """park_hr_factors must be in the default canonical pipeline, slotted
    after team_game_log (both are derived-from-games inputs) and before
    preflight_artifacts (so the preflight check can see fresh state)."""
    sessions = tmp_path / "sessions"
    _touch(sessions / "2026-05-05_session.json")
    config = rdr.RefreshConfig(
        active_date="2026-05-06",
        sessions_dir=sessions,
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        refresh_daily_reviews=False,
        run_walk_forward=False,
    )
    steps = rdr.build_refresh_steps(config, ["2026-05-05"], "2026-05-05")
    names = [s.name for s in steps]
    assert "stage1_ou_cache" in names
    assert "park_hr_factors" in names
    idx = {n: i for i, n in enumerate(names)}
    assert idx["stage1_ou_cache"] < idx["preflight_artifacts"]
    assert idx["team_game_log"] < idx["park_hr_factors"]
    assert idx["park_hr_factors"] < idx["preflight_artifacts"]


def test_park_hr_factors_can_be_skipped(tmp_path):
    config = _minimal_config(
        tmp_path,
        refresh_team_game_log=False,
        refresh_park_hr_factors=False,
        refresh_weather_cache=False,
        refresh_daily_reviews=False,
        run_walk_forward=False,
    )
    assert rdr.build_refresh_steps(config, [], None) == []


def test_stage1_cache_can_be_skipped(tmp_path):
    config = _minimal_config(
        tmp_path,
        refresh_stage1_cache=False,
        refresh_weather_cache=False,
        refresh_daily_reviews=False,
        run_walk_forward=False,
    )
    assert "stage1_ou_cache" not in [s.name for s in rdr.build_refresh_steps(config, [], None)]


def test_stage1_ou_cache_alt_a_is_present_with_correct_flags(tmp_path):
    """Active #8 (2026-05-17): stage1_ou_cache_alt_a refresh step
    rebuilds Stage-1 in Alt-A mode to the staging path. Same history
    window as the production step. NEVER auto-promoted (no companion
    inline promote step)."""
    sessions = tmp_path / "sessions"
    _touch(sessions / "2026-05-05_session.json")
    config = rdr.RefreshConfig(
        active_date="2026-05-06",
        sessions_dir=sessions,
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        refresh_daily_reviews=False,
        run_walk_forward=False,
    )
    steps = rdr.build_refresh_steps(config, ["2026-05-05"], "2026-05-05")
    alt_a_steps = [s for s in steps if s.name == "stage1_ou_cache_alt_a"]
    assert len(alt_a_steps) == 1
    step = alt_a_steps[0]
    # Same history window as the production builder.
    assert step.command[step.command.index("--min-season") + 1] == "2021"
    assert step.command[step.command.index("--max-season") + 1] == "2025"
    # Alt-A specific flag.
    assert (
        step.command[step.command.index("--smoothing-mode") + 1]
        == "empirical_when_available"
    )
    # Separate output path so production cache is never overwritten.
    out_idx = step.command.index("--out")
    assert "mlb_ou_cache_alt_a.staging.json" in step.command[out_idx + 1]
    assert step.staleness_check is not None
    # Same input root as production -- both should rebuild together
    # when game data changes.
    roots = step.staleness_check.input_dir_mtime_roots
    assert any("games" in str(r) and "regular" in str(r) for r in roots)
    # No "stage1_alt_a_cache_promote" inline step -- never auto-promotes.
    names = [s.name for s in steps]
    assert "stage1_alt_a_cache_promote" not in names


def test_unified_signals_and_training_table_use_mode_both(tmp_path):
    """2026-05-19 paper-mode propagation fix.

    Prior to this fix the refresh hardcoded `--mode live` on both
    `unified_signals` and `signal_training_table` steps, which meant
    paper sessions never reached loss-attribution or shadow-override
    reports. The user IS in paper-mode this week to validate Alt-A
    -- starving the analysis pipeline of paper data defeated the
    runway's purpose.

    Other fill-aware steps (clv_report, execution_diagnostics,
    ev_policy_backtest, queue_aware_execution_replay) stay
    `--mode live` intentionally because they read realized_executed
    / actual_fill_price, which paper mode's 100% taker assumption
    distorts.
    """
    sessions = tmp_path / "sessions"
    _touch(sessions / "2026-05-05_session.json")
    config = rdr.RefreshConfig(
        active_date="2026-05-06",
        sessions_dir=sessions,
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        refresh_daily_reviews=False,
        run_walk_forward=False,
    )
    steps = {s.name: s for s in rdr.build_refresh_steps(
        config, ["2026-05-05"], "2026-05-05",
    )}
    # The two steps that feed loss-attribution + shadow-override must
    # be both-mode.
    for step_name in ("unified_signals", "signal_training_table"):
        assert step_name in steps, f"missing step {step_name}"
        cmd = steps[step_name].command
        mode_idx = cmd.index("--mode")
        assert cmd[mode_idx + 1] == "both", (
            f"{step_name} must run --mode both for paper bets to "
            f"reach downstream analysis; got {cmd[mode_idx + 1]}"
        )
    # Fill-aware steps stay live-only. Sanity-check the two that
    # accept `--mode` to guard against an over-eager future refactor
    # that batch-converts everything to both. (ev_policy_backtest
    # accepts no --mode and is mode-agnostic; queue_aware_execution_
    # replay is gated by other config flags.)
    for step_name in ("clv_report", "execution_diagnostics"):
        if step_name not in steps:
            continue
        cmd = steps[step_name].command
        if "--mode" not in cmd:
            continue
        mode_idx = cmd.index("--mode")
        assert cmd[mode_idx + 1] == "live", (
            f"{step_name} reads fill behavior; must stay --mode live "
            f"to avoid paper's 100% taker assumption polluting the "
            f"metric. Got {cmd[mode_idx + 1]}"
        )


def test_stage1_ou_cache_alt_a_skipped_when_stage1_cache_disabled(tmp_path):
    """Alt-A step is gated by the same `refresh_stage1_cache` config
    flag as the production stage1 step -- they're paired."""
    config = _minimal_config(
        tmp_path,
        refresh_stage1_cache=False,
        refresh_weather_cache=False,
        refresh_daily_reviews=False,
        run_walk_forward=False,
    )
    names = [s.name for s in rdr.build_refresh_steps(config, [], None)]
    assert "stage1_ou_cache_alt_a" not in names


def test_preflight_artifacts_warns_when_park_hr_factors_missing(tmp_path):
    """Missing park_hr_factors.json should be a warning (Stage-2 hr_factor
    family degrades to UNKNOWN_BUCKET), not a hard failure."""
    ou = tmp_path / "ou.json"
    ou.write_text('{"states": {"x": 1}}', encoding="utf-8")
    s2 = tmp_path / "s2.json"
    s2.write_text('{"weights": {"a": 1}}', encoding="utf-8")
    games = []
    for i in range(30):
        for d in range(20):
            games.append({"date": f"2026-04-{(d % 28) + 1:02d}", "away": f"T{i:02d}",
                          "home": "NYY", "away_runs": 4, "home_runs": 5})
    tgl = tmp_path / "tgl.json"
    tgl.write_text(json.dumps({"games": games, "mlb_avg_rpg": 4.45}), encoding="utf-8")

    config = rdr.RefreshConfig(
        active_date="2026-05-08",
        sessions_dir=tmp_path / "sessions",
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        mlb_ou_cache_path=ou,
        stage2_cache_path=s2,
        team_game_log_path=tgl,
        pitcher_cache_path=tmp_path / "missing_pc.json",
        park_hr_factors_path=tmp_path / "missing_hr.json",
    )
    ok, output = rdr.INLINE_HANDLERS["preflight_artifacts"](config)
    # Stage-1/2/3 are valid -> overall ok=True. HR factors warn only.
    assert ok is True
    assert "WARNING Park HR factors" in output


def test_preflight_artifacts_passes_with_park_hr_factors_present(tmp_path):
    ou = tmp_path / "ou.json"
    ou.write_text('{"states": {"x": 1}}', encoding="utf-8")
    s2 = tmp_path / "s2.json"
    s2.write_text('{"weights": {"a": 1}}', encoding="utf-8")
    games = []
    for i in range(30):
        for d in range(20):
            games.append({"date": f"2026-04-{(d % 28) + 1:02d}", "away": f"T{i:02d}",
                          "home": "NYY", "away_runs": 4, "home_runs": 5})
    tgl = tmp_path / "tgl.json"
    tgl.write_text(json.dumps({"games": games, "mlb_avg_rpg": 4.45}), encoding="utf-8")
    hr = tmp_path / "hr.json"
    hr.write_text(json.dumps({
        "by_park": {f"Park {i:02d}": {"2026": {"shrunk_factor": 1.0}} for i in range(30)}
    }), encoding="utf-8")

    config = rdr.RefreshConfig(
        active_date="2026-05-08",
        sessions_dir=tmp_path / "sessions",
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        mlb_ou_cache_path=ou,
        stage2_cache_path=s2,
        team_game_log_path=tgl,
        pitcher_cache_path=tmp_path / "missing_pc.json",
        park_hr_factors_path=hr,
    )
    ok, output = rdr.INLINE_HANDLERS["preflight_artifacts"](config)
    assert ok is True
    assert "ok Park HR factors" in output
    assert "30 with 2026 entries" in output


def test_canonical_startup_includes_scrape_team_log_and_preflight_steps(tmp_path):
    """When all toggles default-on, startup includes scrape, team-log, and
    preflight steps in the right order."""
    sessions = tmp_path / "sessions"
    _touch(sessions / "2026-05-05_session.json")

    config = rdr.RefreshConfig(
        active_date="2026-05-06",
        sessions_dir=sessions,
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        refresh_daily_reviews=False,
        run_walk_forward=False,
    )
    steps = rdr.build_refresh_steps(config, ["2026-05-05"], "2026-05-05")
    names = [step.name for step in steps]

    # New steps are present.
    assert "preflight_env_secrets" in names
    assert "scrape_recent_games" in names
    assert "stage1_ou_cache" in names
    assert "scrape_active_schedule" in names
    assert "team_game_log" in names
    assert "preflight_artifacts" in names

    # Order: secrets first, scrapes before caches, team_game_log + artifacts after pitcher cache.
    idx = {n: i for i, n in enumerate(names)}
    assert idx["preflight_env_secrets"] < idx["scrape_recent_games"]
    assert idx["scrape_recent_games"] < idx["stage1_ou_cache"]
    assert idx["stage1_ou_cache"] < idx["scrape_active_schedule"]
    assert idx["scrape_active_schedule"] < idx["game_weather_cache"]
    assert idx["pitcher_cache"] < idx["team_game_log"]
    assert idx["team_game_log"] < idx["preflight_artifacts"]
    assert idx["preflight_artifacts"] < idx["candidate_universe_table"]
    assert idx["candidate_universe_table"] < idx["calibration_opportunity_training"]
    assert idx["calibration_opportunity_training"] < idx["calibrate_signal_probabilities"]
    # 2026-05-20 audit reorder: concept_drift_report MUST run before
    # calibrate_signal_probabilities so the calibrator records the FRESH
    # drift-report hash in its lineage. Previously calibrate ran first
    # and stamped yesterday's drift hash, producing daily stale-input
    # alerts in cross_artifact_consistency_health.
    assert idx["concept_drift_report"] < idx["calibrate_signal_probabilities"]
    assert idx["calibrate_signal_probabilities"] < idx["calibrate_signal_probabilities_under"]
    assert idx["calibrate_signal_probabilities_under"] < idx["drift_in_drift_report"]
    assert idx["model_maturity_report"] < idx["fair_value_stage_ablation"]
    assert idx["fair_value_stage_ablation"] < idx["unified_signals"]
    # Concept drift now precedes calibrate; model_maturity, ablation, and
    # unified_signals all run BEFORE concept_drift_report so they don't
    # bracket the new ordering.
    assert idx["unified_signals"] < idx["concept_drift_report"]
    assert idx["signal_training_table"] < idx["clv_report"]
    assert idx["clv_report"] < idx["fv_disagreement_quality"]
    assert idx["fv_disagreement_quality"] < idx["train_baseline_models"]
    if "walk_forward_market_anchored_alpha" in idx:
        assert idx["walk_forward_market_anchored_alpha"] < idx["walk_forward_fv_disagreement_quality"]
        assert idx["walk_forward_fv_disagreement_quality"] < idx["stake_scaling_promotion_analyzer"]
    assert idx["weekly_drift_rollup"] < idx["artifact_lineage_freshness"]
    assert idx["artifact_lineage_freshness"] < idx["refresh_health_rollup"]
    stage1 = [step for step in steps if step.name == "stage1_ou_cache"][0]
    assert stage1.command[stage1.command.index("--min-season") + 1] == "2021"
    assert stage1.command[stage1.command.index("--max-season") + 1] == "2025"
    calibration = [step for step in steps if step.name == "calibrate_signal_probabilities"][0]
    assert calibration.command[calibration.command.index("--artifact-purpose") + 1] == "runtime-refit"
    maturity = [step for step in steps if step.name == "model_maturity_report"][0]
    assert "--artifact-purpose" not in maturity.command
    ev_policy = [step for step in steps if step.name == "ev_policy_backtest"][0]
    assert ev_policy.command[ev_policy.command.index("--artifact-purpose") + 1] == "runtime-refit"
    alpha = [step for step in steps if step.name == "calibration_market_anchored_alpha"][0]
    assert alpha.command[alpha.command.index("--artifact-purpose") + 1] == "runtime-refit"
    clv_report = [step for step in steps if step.name == "clv_report"][0]
    assert "build_clv_report.py" in clv_report.command[1]
    assert clv_report.staleness_check is not None
    fv_disagreement = [step for step in steps if step.name == "fv_disagreement_quality"][0]
    assert "build_fv_disagreement_quality_report.py" in fv_disagreement.command[1]
    assert fv_disagreement.staleness_check is not None


def test_scrape_recent_games_uses_yesterday_as_end_date(tmp_path):
    config = rdr.RefreshConfig(
        active_date="2026-05-08",
        recent_games_lookback_days=3,
        sessions_dir=tmp_path / "sessions",
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        refresh_pitcher_cache=False,
        refresh_weather_cache=False,
        refresh_team_game_log=False,
        refresh_active_schedule=False,
        run_preflight_secrets=False,
        run_preflight_artifacts=False,
        refresh_daily_reviews=False,
        run_walk_forward=False,
    )
    steps = rdr.build_refresh_steps(config, [], None)
    [step] = [s for s in steps if s.name == "scrape_recent_games"]
    cmd = step.command
    # End date is yesterday (active_date - 1 day), start date is N days back.
    assert "--end-date" in cmd
    assert cmd[cmd.index("--end-date") + 1] == "2026-05-07"
    assert "--start-date" in cmd
    assert cmd[cmd.index("--start-date") + 1] == "2026-05-05"


def test_scrape_active_schedule_covers_prior_plus_active_month(tmp_path):
    """Schedule scrape window starts at first-of-PRIOR-month so games
    added late to the prior month are picked up. End is last-of-
    active-month. Active #12 fix (2026-05-17) -- previously the
    window was active month only, which created a permanent data
    gap for late-added games near month boundaries."""
    config = rdr.RefreshConfig(
        active_date="2026-05-08",
        sessions_dir=tmp_path / "sessions",
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        refresh_pitcher_cache=False,
        refresh_weather_cache=False,
        refresh_recent_games=False,
        refresh_team_game_log=False,
        run_preflight_secrets=False,
        run_preflight_artifacts=False,
        refresh_daily_reviews=False,
        run_walk_forward=False,
    )
    steps = rdr.build_refresh_steps(config, [], None)
    [step] = [s for s in steps if s.name == "scrape_active_schedule"]
    cmd = step.command
    assert cmd[cmd.index("--start-date") + 1] == "2026-04-01"
    assert cmd[cmd.index("--end-date") + 1] == "2026-05-31"
    assert "--dry-run" in cmd


def test_first_of_prior_month_wraps_january_to_december(tmp_path):
    """January active date -> December of prior year for the prior
    month. Catches the off-by-one wrap-around bug class."""
    assert rdr._first_of_prior_month("2026-01-15") == "2025-12-01"
    assert rdr._first_of_prior_month("2026-02-01") == "2026-01-01"
    assert rdr._first_of_prior_month("2026-05-08") == "2026-04-01"


def test_recent_games_lookback_default_is_45_days(tmp_path):
    """Default lookback was raised from 7 -> 45 on 2026-05-17 after
    Active #12 verification found 22 missing MLB JSONs clustered in
    a 17-30-day-old window."""
    config = rdr.RefreshConfig(active_date="2026-05-17")
    assert config.recent_games_lookback_days == 45


def test_preflight_env_secrets_warns_without_env_file(tmp_path):
    """Missing .env is a warning by default (paper mode runs fine)."""
    config = rdr.RefreshConfig(
        active_date="2026-05-08",
        sessions_dir=tmp_path / "sessions",
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        env_path=tmp_path / "missing.env",
    )
    ok, output = rdr.INLINE_HANDLERS["preflight_env_secrets"](config)
    assert ok is True
    assert "WARNING" in output


def test_preflight_env_secrets_fails_when_required(tmp_path):
    config = rdr.RefreshConfig(
        active_date="2026-05-08",
        sessions_dir=tmp_path / "sessions",
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        env_path=tmp_path / "missing.env",
        require_poly_private_key=True,
    )
    ok, output = rdr.INLINE_HANDLERS["preflight_env_secrets"](config)
    assert ok is False
    assert "WARNING" in output


def test_preflight_env_secrets_passes_with_poly_key(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("POLY_PRIVATE_KEY=0xdeadbeef\nOTHER=value\n", encoding="utf-8")
    config = rdr.RefreshConfig(
        active_date="2026-05-08",
        sessions_dir=tmp_path / "sessions",
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        env_path=env_file,
    )
    ok, output = rdr.INLINE_HANDLERS["preflight_env_secrets"](config)
    assert ok is True
    assert "POLY_PRIVATE_KEY present" in output
    # Never echo the secret value itself.
    assert "0xdeadbeef" not in output


def test_preflight_artifacts_fails_on_missing_caches(tmp_path):
    config = rdr.RefreshConfig(
        active_date="2026-05-08",
        sessions_dir=tmp_path / "sessions",
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        mlb_ou_cache_path=tmp_path / "missing_ou.json",
        stage2_cache_path=tmp_path / "missing_s2.json",
        team_game_log_path=tmp_path / "missing_tgl.json",
        pitcher_cache_path=tmp_path / "missing_pc.json",
    )
    ok, output = rdr.INLINE_HANDLERS["preflight_artifacts"](config)
    assert ok is False
    assert "FAIL Stage-1" in output
    assert "FAIL Stage-2" in output
    assert "FAIL Stage-3" in output


def test_preflight_artifacts_flags_stage3_corpus_rot(tmp_path):
    """A team_game_log that is technically valid but has no active-season
    games should fail preflight — Stage-3 v2 needs season_to_date / momentum."""
    ou = tmp_path / "ou.json"
    ou.write_text('{"states": {}}', encoding="utf-8")
    s2 = tmp_path / "s2.json"
    s2.write_text('{"weights": {}}', encoding="utf-8")
    # Stage-3 cache: 30 teams, no 2026 games.
    games = []
    for i in range(30):
        games.append({"date": "2025-09-01", "away": f"T{i:02d}", "home": "NYY",
                      "away_runs": 4, "home_runs": 5})
    tgl = tmp_path / "tgl.json"
    tgl.write_text(json.dumps({"games": games, "mlb_avg_rpg": 4.45}), encoding="utf-8")

    config = rdr.RefreshConfig(
        active_date="2026-05-08",
        sessions_dir=tmp_path / "sessions",
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        mlb_ou_cache_path=ou,
        stage2_cache_path=s2,
        team_game_log_path=tgl,
        pitcher_cache_path=tmp_path / "missing_pc.json",
    )
    ok, output = rdr.INLINE_HANDLERS["preflight_artifacts"](config)
    assert ok is False
    assert "Stage-3" in output
    assert "2026" in output


def test_preflight_artifacts_passes_with_healthy_caches(tmp_path):
    ou = tmp_path / "ou.json"
    ou.write_text(json.dumps({
        "meta": {
            "history_start_date": "2021-04-01",
            "history_end_date": "2025-09-28",
            "seasons": [str(y) for y in range(2021, 2026)],
            "games_by_season": {str(y): 2400 for y in range(2021, 2026)},
            "total_games": 12000,
            "duplicate_game_files_skipped": 0,
        },
        "cells": {"x": 1},
    }), encoding="utf-8")
    s2 = tmp_path / "s2.json"
    s2.write_text('{"weights": {"a": 1}}', encoding="utf-8")
    games = []
    teams = [f"T{i:02d}" for i in range(30)]
    for t in teams:
        for d in range(20):
            games.append({"date": f"2026-04-{(d % 28) + 1:02d}", "away": t, "home": "NYY",
                          "away_runs": 4, "home_runs": 5})
    tgl = tmp_path / "tgl.json"
    tgl.write_text(json.dumps({"games": games, "mlb_avg_rpg": 4.45}), encoding="utf-8")
    pc = tmp_path / "pc.json"
    pc.write_text('{"pitchers": {}}', encoding="utf-8")

    config = rdr.RefreshConfig(
        active_date="2026-05-08",
        sessions_dir=tmp_path / "sessions",
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        mlb_ou_cache_path=ou,
        stage2_cache_path=s2,
        team_game_log_path=tgl,
        pitcher_cache_path=pc,
    )
    ok, output = rdr.INLINE_HANDLERS["preflight_artifacts"](config)
    assert ok is True
    assert "ok Stage-1" in output
    assert "ok Stage-1 production coverage" in output
    assert "ok Stage-2" in output
    assert "ok Stage-3" in output


def test_preflight_artifacts_warns_on_stage1_history_gap(tmp_path):
    ou = tmp_path / "ou.json"
    ou.write_text(json.dumps({
        "meta": {
            "history_start_date": "2022-04-01",
            "history_end_date": "2025-09-28",
            "seasons": ["2022", "2023", "2024", "2025"],
            "games_by_season": {str(y): 2400 for y in range(2022, 2026)},
            "total_games": 9600,
        },
        "cells": {"x": 1},
    }), encoding="utf-8")
    s2 = tmp_path / "s2.json"
    s2.write_text('{"weights": {"a": 1}}', encoding="utf-8")
    games = []
    teams = [f"T{i:02d}" for i in range(30)]
    for t in teams:
        for d in range(20):
            games.append({"date": f"2026-04-{(d % 28) + 1:02d}", "away": t, "home": "NYY",
                          "away_runs": 4, "home_runs": 5})
    tgl = tmp_path / "tgl.json"
    tgl.write_text(json.dumps({"games": games, "mlb_avg_rpg": 4.45}), encoding="utf-8")

    config = rdr.RefreshConfig(
        active_date="2026-05-08",
        sessions_dir=tmp_path / "sessions",
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        mlb_ou_cache_path=ou,
        stage2_cache_path=s2,
        team_game_log_path=tgl,
        pitcher_cache_path=tmp_path / "missing_pc.json",
    )
    ok, output = rdr.INLINE_HANDLERS["preflight_artifacts"](config)
    assert ok is True
    assert "WARNING Stage-1 coverage" in output
    assert "2021-2025" in output


def test_phase6_reminder_fires_after_due_date():
    assert rdr._phase6_reminder("2026-05-06") is None
    msg = rdr._phase6_reminder("2026-06-07")
    assert msg is not None
    assert "Phase 6" in msg
    msg2 = rdr._phase6_reminder("2026-09-01")
    assert msg2 is not None


def test_manifest_summary_line_counts_steps(tmp_path):
    sessions = tmp_path / "sessions"
    _touch(sessions / "2026-05-05_session.json")

    config = rdr.RefreshConfig(
        active_date="2026-05-06",
        sessions_dir=sessions,
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        refresh_pitcher_cache=False,
        refresh_weather_cache=False,
        refresh_recent_games=False,
        refresh_active_schedule=False,
        refresh_team_game_log=False,
        run_preflight_secrets=False,
        run_preflight_artifacts=False,
        refresh_daily_reviews=False,
        run_walk_forward=False,
        plan_only=True,
    )
    payload = rdr.run_startup_refresh(config)
    assert "summary" in payload
    # Plan-only summary leads with planned count (no steps actually ran);
    # avoids the misleading "0/N steps ok" that prior format produced.
    assert "steps planned" in payload["summary"]
    assert str(payload["steps_planned"]) in payload["summary"]
    assert payload["summary_status"] == "ok"
    assert payload["manifest_kind"] == "plan"


def test_run_manifest_uses_canonical_startup_refresh_name(tmp_path):
    config = rdr.RefreshConfig(
        active_date="2026-05-06",
        sessions_dir=tmp_path / "sessions",
        candidate_dir=tmp_path / "candidates",
        log_dir=tmp_path / "logs",
        output_root=tmp_path / "out",
        refresh_pitcher_cache=False,
        refresh_weather_cache=False,
        refresh_recent_games=False,
        refresh_active_schedule=False,
        refresh_team_game_log=False,
        refresh_stage1_cache=False,
        refresh_park_hr_factors=False,
        run_preflight_secrets=False,
        run_preflight_artifacts=False,
        refresh_daily_reviews=False,
        run_walk_forward=False,
        plan_only=False,
    )

    payload = rdr.run_startup_refresh(config)
    manifest_path = Path(payload["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest_path.name == "2026-05-06_startup_refresh.json"
    assert manifest["manifest_kind"] == "run"


# ---------------------------------------------------------------------------
# model_freshness_health: Stage-2 staging diff + stale-artifact alerts.
# ---------------------------------------------------------------------------


def test_model_freshness_handler_alerts_on_stage2_brier_drift(tmp_path, monkeypatch):
    """Stage-2 staging artifact with materially better validation Brier
    should surface as a refresh-time IMPROVES alert -- the operator
    sees it without scrolling through every step's output."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    # Production: validation_brier 0.220 across two lines.
    (cache_dir / "mlb_stage2_run_env.json").write_text(json.dumps({
        "lines": {
            "o7.5": {"validation_brier": 0.220},
            "o8.5": {"validation_brier": 0.220},
        },
    }), encoding="utf-8")
    # Staging: validation_brier 0.210 -- 0.010 improvement, well past threshold.
    (cache_dir / "mlb_stage2_run_env.staging.json").write_text(json.dumps({
        "lines": {
            "o7.5": {"validation_brier": 0.210},
            "o8.5": {"validation_brier": 0.210},
        },
    }), encoding="utf-8")

    monkeypatch.setattr(rdr, "PROJECT_DIR", tmp_path)
    config = _minimal_config(
        tmp_path,
        mlb_ou_cache_path=cache_dir / "mlb_ou_cache.json",  # missing -> WARNING
        stage2_cache_path=cache_dir / "mlb_stage2_run_env.json",
    )
    ok, notes = rdr._handle_model_freshness_health(config)
    assert ok is True
    assert "ALERT Stage-2 staging IMPROVES validation Brier by 0.0100" in notes
    # Promotion hint included.
    assert "Promote with:" in notes


def test_model_freshness_handler_no_alert_when_within_tolerance(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "mlb_stage2_run_env.json").write_text(
        json.dumps({"lines": {"o7.5": {"validation_brier": 0.2205}}}),
        encoding="utf-8",
    )
    # Drift of 0.0001 -- well below the 0.001 threshold.
    (cache_dir / "mlb_stage2_run_env.staging.json").write_text(
        json.dumps({"lines": {"o7.5": {"validation_brier": 0.2206}}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(rdr, "PROJECT_DIR", tmp_path)
    config = _minimal_config(
        tmp_path,
        mlb_ou_cache_path=cache_dir / "mlb_ou_cache.json",
        stage2_cache_path=cache_dir / "mlb_stage2_run_env.json",
    )
    ok, notes = rdr._handle_model_freshness_health(config)
    assert ok is True
    assert "ALERT Stage-2" not in notes
    assert "ok Stage-2 staging matches production within tolerance" in notes


def test_staleness_check_skips_when_output_newer_than_inputs(tmp_path):
    output = tmp_path / "out.json"
    input_a = tmp_path / "in_a.json"
    input_b = tmp_path / "in_b.json"
    input_a.write_text("a", encoding="utf-8")
    input_b.write_text("b", encoding="utf-8")
    # Output written AFTER inputs -> step should be considered fresh.
    import os
    older = input_a.stat().st_mtime
    output.write_text("out", encoding="utf-8")
    os.utime(output, (older + 100, older + 100))

    check = rdr.StalenessCheck(
        output_path=output,
        input_paths=(input_a, input_b),
    )
    fresh, note = rdr._is_step_fresh(check)
    assert fresh is True
    assert "newer than newest input" in note


def test_staleness_check_runs_when_input_newer_than_output(tmp_path):
    import os
    output = tmp_path / "out.json"
    input_a = tmp_path / "in_a.json"
    output.write_text("out", encoding="utf-8")
    input_a.write_text("a", encoding="utf-8")
    # Bump input mtime forward.
    out_mt = output.stat().st_mtime
    os.utime(input_a, (out_mt + 100, out_mt + 100))

    check = rdr.StalenessCheck(
        output_path=output,
        input_paths=(input_a,),
    )
    fresh, note = rdr._is_step_fresh(check)
    assert fresh is False
    assert "older than newest input" in note


def test_staleness_check_treats_missing_output_as_not_fresh(tmp_path):
    output = tmp_path / "missing.json"
    input_a = tmp_path / "in_a.json"
    input_a.write_text("a", encoding="utf-8")
    check = rdr.StalenessCheck(output_path=output, input_paths=(input_a,))
    fresh, note = rdr._is_step_fresh(check)
    assert fresh is False
    assert "missing" in note


def test_staleness_check_uses_dir_mtime_for_corpora(tmp_path):
    """Heavy corpora like data/games/* use directory-mtime scan, not leaf files."""
    import os
    output = tmp_path / "stage2.json"
    games_dir = tmp_path / "games" / "regular" / "2026" / "5" / "12"
    games_dir.mkdir(parents=True)
    # Newer directory mtime than the output -> step is stale.
    output.write_text("out", encoding="utf-8")
    out_mt = output.stat().st_mtime
    os.utime(games_dir, (out_mt + 1000, out_mt + 1000))

    check = rdr.StalenessCheck(
        output_path=output,
        input_dir_mtime_roots=(tmp_path / "games" / "regular",),
    )
    fresh, _ = rdr._is_step_fresh(check)
    assert fresh is False


def test_run_step_skips_fresh_subprocess_step(tmp_path, monkeypatch):
    """End-to-end: a subprocess step with a fresh staleness_check returns
    status='skipped_fresh' WITHOUT running the subprocess."""
    output = tmp_path / "out.json"
    input_a = tmp_path / "in.json"
    input_a.write_text("a", encoding="utf-8")
    import os
    output.write_text("out", encoding="utf-8")
    os.utime(output, (input_a.stat().st_mtime + 100, input_a.stat().st_mtime + 100))

    step = rdr.RefreshStep(
        name="fake_heavy_step",
        command=[rdr._python(), "-c", "raise SystemExit('must not run!')"],
        staleness_check=rdr.StalenessCheck(
            output_path=output, input_paths=(input_a,),
        ),
    )
    config = _minimal_config(tmp_path)
    result = rdr._run_step(step, config)
    assert result.status == "skipped_fresh"
    assert result.elapsed_secs == 0.0
    assert "skip:" in result.output_tail


def test_force_retrain_bypasses_staleness_check(tmp_path):
    """--force-retrain runs the subprocess even when the artifact is fresh."""
    output = tmp_path / "out.json"
    input_a = tmp_path / "in.json"
    input_a.write_text("a", encoding="utf-8")
    import os
    output.write_text("out", encoding="utf-8")
    os.utime(output, (input_a.stat().st_mtime + 100, input_a.stat().st_mtime + 100))

    step = rdr.RefreshStep(
        name="fake_heavy_step",
        # Use a script that exits cleanly so the step succeeds.
        command=[rdr._python(), "-c", "pass"],
        staleness_check=rdr.StalenessCheck(
            output_path=output, input_paths=(input_a,),
        ),
    )
    config = _minimal_config(tmp_path, force_retrain=True)
    result = rdr._run_step(step, config)
    assert result.status == "ok"  # ran, not skipped


def test_heavy_steps_have_staleness_checks(tmp_path):
    """Wiring sanity: every step that takes >30s on real data carries a
    staleness_check. Caught a regression where ev_policy_backtest was
    missing one and ran unnecessarily every refresh."""
    sessions = tmp_path / "sessions"
    candidates = tmp_path / "candidates"
    logs = tmp_path / "logs"
    sessions.mkdir(); candidates.mkdir(); logs.mkdir()
    _touch(sessions / "2026-05-05_session.json")

    config = _minimal_config(tmp_path, sessions_dir=sessions, candidate_dir=candidates, log_dir=logs)
    steps = rdr.build_refresh_steps(config, ["2026-05-05"], "2026-05-05")
    by_name = {s.name: s for s in steps}
    must_be_stale_aware = [
        "train_baseline_models",
        "ev_policy_backtest",
        "stage2_run_env_retrain_staging",
        "stage3_team_offense_features",
        "stage3_team_offense_calibration_table",
        "stage3_team_offense_v2_fit",
    ]
    for name in must_be_stale_aware:
        step = by_name.get(name)
        assert step is not None, f"missing step {name}"
        assert step.staleness_check is not None, f"step {name} missing staleness_check"


def test_model_freshness_handler_flags_missing_staging(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "mlb_stage2_run_env.json").write_text(
        json.dumps({"lines": {"o7.5": {"validation_brier": 0.22}}}),
        encoding="utf-8",
    )
    # No staging file.

    monkeypatch.setattr(rdr, "PROJECT_DIR", tmp_path)
    config = _minimal_config(
        tmp_path,
        mlb_ou_cache_path=cache_dir / "mlb_ou_cache.json",
        stage2_cache_path=cache_dir / "mlb_stage2_run_env.json",
    )
    ok, notes = rdr._handle_model_freshness_health(config)
    assert ok is True
    assert "Stage-2 staging artifact not present" in notes
