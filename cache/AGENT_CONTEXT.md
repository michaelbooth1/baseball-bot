# cache Agent Context

This folder holds the offline-built model artifacts that the live trading
runtime reads at startup. Treat the JSON files as **data**, not source: they
are byproducts of the Python builders next to them (and a few builders that
live in `scripts/analysis/`). When a builder changes, regenerate the JSON in
the same patch -- do not hand-edit the JSON.

Last checked against the active `cache/*.py` files: 2026-05-13
(density_alt + hr_factor families in Stage-2; park_hr_factors.json from
scripts/analysis/build_park_hr_factors.py; **staging-vs-production split**
added for both Stage-2 and Stage-3: `mlb_stage2_run_env.staging.json` and
`team_offense_v2_weights.json` are written daily by the refresh, but
production reads `mlb_stage2_run_env.json` and the compiled-in defaults in
`scripts/analysis/team_offense_model.py` until an explicit promotion runs;
Stage-1 builder now supports explicit date/season windows, dedupes duplicate
schedule-date files by `gamePk`, and writes history coverage metadata).

The two builders that live here (`build_mlb_ou_cache.py`,
`build_mlb_stage2_run_env.py`) are the Stage-1 / Stage-2 fair-value pipeline.
Other JSON caches (`pitcher_cache.json`, `team_game_log.json`, and
`weather/game_weather_<date>.json`) are written by builders in
`scripts/analysis/` and consumed by runtime/analysis alongside the Stage-1/2
artifacts.

## Cache Dataflow

1. `scripts/scrape_mlb_history.py` fetches historical MLB live feeds into
   `data/games/<season_type>/<year>/*.json`. The two cache builders below
   stream from that tree.
2. `build_mlb_ou_cache.py` (this folder) does **two passes** over the games:
   - Pass 1: build per-state empirical Over rates and a phase-keyed expected
     remaining-runs lambda (`(inning_bucket, half, outs, bases) -> lam`).
   - Pass 2: on low-support cells only, fit a logit-delta calibration table
     `(line, inning/half/out bucket, runs_needed)` with Bayesian shrinkage.
   - Writes `mlb_ou_cache.json`.
   - For 2026 startup refresh, the intended production fallback window is the
     five completed prior regular seasons (`--min-season 2021 --max-season 2025`).
     Rolling 4y (`2022-2025`) is the current lead research candidate, but it
     remains a side artifact. Use side artifacts such as
     `mlb_ou_cache_4y_2022_2025_candidate.json`,
     `mlb_ou_cache_10y_candidate.json`, and
     `mlb_ou_cache_weighted_scoring_env_candidate.json` for validation before
     changing runtime behavior. Weighted candidates use
     `--season-weights-path` plus `--season-weight-mode allocation`; weights
     are research-only unless FV ablation + walk-forward validation promote
     them explicitly.
3. `build_mlb_stage2_run_env.py` (this folder) reads `mlb_ou_cache.json`
   plus the same game tree, aligns each plate-appearance state with the
   matching Stage-1 cell, fits shrunken logit-delta tables for park / temp /
   wind / park_wind / density_alt / hr_factor, tunes per-line family weights
   on a holdout window, then refits final tables on all data. Writes
   `mlb_stage2_run_env.json`. The two new (2026-05-08) families:
   - `density_alt` requires `data/reference/mlb_stadium_weather_metadata.json`
     for stadium elevation lookups; bucketed via
     `parse_density_alt_bin(density_altitude_ft(elevation_ft, temp_f))`.
   - `hr_factor` requires `cache/park_hr_factors.json` (built by
     `scripts/analysis/build_park_hr_factors.py`); bucketed via
     `parse_hr_factor_bin(lookup_hr_factor(park, year))` with most-recent-year
     fallback when the active season has no entry yet.
4. `stage2_run_env_model.py` (this folder) is the **runtime** loader/applier
   for that Stage-2 JSON. Trading and analysis code import
   `Stage2RunEnvModel` and `RunEnvContext` from here.
5. `scripts/analysis/build_pitcher_cache.py`,
   `scripts/analysis/build_team_game_log.py`, and
   `scripts/analysis/refresh_game_weather.py` write `pitcher_cache.json`,
   `team_game_log.json`, and `weather/game_weather_<date>.json` into this
   folder. They are documented in `scripts/analysis/AGENT_CONTEXT.md`; this
   folder is just their output sink.

## Stage Invariants

- **Stage-1 is empirical-first, calibrated-Poisson-fallback.** Cells with
  `n_games >= MIN_GAMES` (default 40) and `away+home <= MAX_COMBINED`
  (default 20) ship empirical Over rates per line; everything else falls
  back to phase-lambda Poisson with the `poisson_calibration` logit-delta
  table.
- **Line keys are deterministic.** Empirical: `o<digits>` (e.g. `o85` for
  over 8.5). Poisson reference: `po<digits>`. The threshold for line `L.5`
  is `int(L)+1` (over 8.5 means final total >= 9). Do not invent new line
  encodings -- Stage-2, the trading runtime, and the analysis tables all
  parse this same shape.
- **Stage-2 is a residual on top of Stage-1, never a replacement.**
  `p2 = sigmoid(logit(p1) + sum_f w_f * delta_f(bucket_f))` with per-family
  delta caps and a global `max_total_abs_delta`. If the holdout Brier gain
  is below `--min-brier-improvement` for a line, weights collapse to zero
  for that line on purpose -- Stage-2 must earn its keep.
- **Family order is fixed:**
  `("park", "temp", "wind", "park_wind", "density_alt", "hr_factor")` --
  declared in `stage2_run_env_model.FAMILY_ORDER`. Any code that iterates
  families should import that constant rather than hardcoding the list.
  Legacy model JSONs that only have the first four families still load
  correctly: missing families just contribute 0 delta at apply time.
- **Monotonicity is enforced at apply time.** `enforce_over_monotonic()`
  guarantees `P(O6.5) >= P(O7.5) >= ...` after Stage-2 deltas via iterated
  adjacent averaging. Don't bypass this in custom callers; non-monotonic
  output breaks downstream EV math.
- **State key shape is a contract.**
  `away_home_inningBucket_half_outs_basesMask`, where `inningBucket` is
  1..9 plus `extras_bucket` (default 10), `half` is `T`/`B`, `outs` is
  0..2, and `basesMask` is a 0..7 bitmask (1=1B, 2=2B, 4=3B). Trading
  code constructs the same string when looking up cells; if you change
  the format here you must update the runtime lookup.

## File Ownership

### Builders (regenerate by running these)

- `build_mlb_ou_cache.py`: Stage-1 builder. Streams
  `data/games/<season_type>/**/*.json`, snapshots state **before** each
  plate appearance (matching baseball's discrete event structure rather
  than a time grid), filters by `--min-date` / `--max-date` /
  `--min-season` / `--max-season`, optionally applies season weights from
  `--season-weights-path`, dedupes repeated `gamePk` files, and writes
  `mlb_ou_cache.json`. Helpers worth knowing:
  `bases_mask_from_matchup`, `final_score_from_game`,
  `is_final_game`, `inning_bucket`, `line_to_threshold`,
  `line_to_emp_key`, `line_to_poisson_key`, `state_label`. The Stage-2
  builder imports several of these directly, so keep their signatures
  stable.
- `build_mlb_stage2_run_env.py`: Stage-2 builder. Reuses Stage-1 helpers
  via direct import, derives buckets from `RunEnvContext.from_game_data`,
  splits years into train (`<= --train-end-year`) vs validation
  (`>= --validation-start-year`), grid-searches per-family weights on
  validation Brier (tie-break by logloss, then by lower complexity),
  refits residual tables on all data, and writes
  `mlb_stage2_run_env.json`. Output payload includes `feature_config`
  (priors, min_n, delta caps, table-row counts), `constraints`,
  `weights`, `validation_metrics`, and `tables`.

### Runtime applier

- `stage2_run_env_model.py`: pure runtime module imported by trading and
  analysis code. Owns:
  - `FAMILY_ORDER` constant.
  - `parse_temp_bin(temp_f)` -- buckets: `<50F`, `50-64F`, `65-79F`,
    `80+F`, `unknown`.
  - `parse_wind_bin(wind_text)` -- direction (`out`/`in`/`cross`/`calm`/
    `other`) crossed with mph band (`0to5`/`6to9`/`10plus`); `unknown`
    when the weather string is empty.
  - `enforce_over_monotonic(over_probs)` -- iterated adjacent averaging
    over up to `MAX_MONOTONIC_PASSES` (12) passes.
  - `RunEnvContext` dataclass with `from_game_data` and `buckets()`.
    `buckets()` returns a 4-key dict including the
    `park__wind` interaction.
  - `Stage2RunEnvModel(payload)` / `Stage2RunEnvModel.from_path(model_path)`
    with `adjust_line(line, base_prob, context)` and
    `adjust_over_probs(base_over_probs, context, enforce_monotonic=True)`.
  - `UNKNOWN_BUCKET = "__UNK__"` -- the sentinel used both at fit time and
    apply time. If a runtime context has a missing field, it must serialize
    to this exact string so the table lookup hits the same bucket.

### Design notes (read these before changing any of the above)

- `MLB_OU_CACHE_DESIGN.md`: documents the Stage-1 state model, sampling
  strategy (per-PA, not time-based), supported lines, cell inclusion
  thresholds, and fallback calibration philosophy.
- `STAGE2_RUN_ENV_MODEL.md`: documents the residual-delta formula,
  per-family shrinkage math, weight tuning approach, and the explicit
  "no forced complexity" rule (zero out weights when validation gain is
  below the configured floor).

### JSON artifacts (do not hand-edit)

- `mlb_ou_cache.json`: Stage-1 cache. Top-level: `meta`,
  `poisson_calibration`, `cells`. Each cell carries
  `n` (unique games), `n_samples`, `lam`, human `label`, and one `o<L>`
  + one `po<L>` field per supported line. Rebuilt by the default startup
  refresh after `scrape_recent_games`; use `--startup-refresh-skip-stage1-cache`
  only when launch speed matters more than a fresh base cache.
- `mlb_stage2_run_env.json`: Stage-2 model. Top-level: `meta`,
  `feature_config`, `constraints`, `weights`, `validation_metrics`,
  `tables`. `tables[family][line][bucket]` carries
  `n`, `raw_mean`, `emp_rate`, `emp_shrunk`, `delta`. `weights[line][family]`
  is a per-line, per-family scalar from the validation grid search.
- `pitcher_cache.json`: produced by `scripts/analysis/build_pitcher_cache.py`,
  not by anything in this folder. Schema: `built_at`, `season`,
  `mlb_avg_era`, `pitcher_count`, `pitchers[mlbam_id] = {name, era, ip, gs}`.
  Read by trading runtime for SP-ERA gating and team-offense modeling.
- `team_game_log.json`: produced by `scripts/analysis/build_team_game_log.py`.
  Schema: `built_at`, `seasons`, `total_games`, `mlb_avg_rpg`, `mlb_avg_total`,
  `games[]` with `{date, away, home, away_runs, home_runs}`. Read by the
  team-offense model and several analysis builders.
- `weather/game_weather_<date>.json`: produced by
  `scripts/analysis/refresh_game_weather.py` during startup refresh. Schema:
  `schema_version`, `date`, `coverage`, `warnings`, and `games[]` with
  schedule fields, stadium metadata, provider status, raw hourly Weather v2
  fields, and derived air-density / wind-component diagnostics. Weather v2 is
  the canonical live weather input for Stage-2; provider misses degrade temp
  and wind to unknown buckets rather than legacy schedule text.
- `park_hr_factors.json`: produced by
  `scripts/analysis/build_park_hr_factors.py`. Schema: `schema_version`,
  `generated_at_utc`, `seasons_scanned`, `prior_n_shrinkage`,
  `league_hr_per_game_by_year`, `by_park[park][year] = {games, hrs,
  raw_factor, shrunk_factor, ...}`. Read by `stage2_run_env_model._load_park_hr_factors`
  to drive the `hr_factor` Stage-2 family. Rebuilt daily as the
  `park_hr_factors` step in `scripts/analysis/run_daily_refresh.py`
  (right after `team_game_log`, before `preflight_artifacts`); skip via
  `--skip-park-hr-factors` (CLI) or `--startup-refresh-skip-park-hr-factors`
  (live engine).
- `mlb_stage2_run_env.staging.json`: **staging** Stage-2 model produced
  daily by the `stage2_run_env_retrain_staging` refresh step (refits
  Stage-2 from the full 2021-2026 corpus). Same schema as
  `mlb_stage2_run_env.json` (`meta`, `feature_config`, `constraints`,
  `weights`, `validation_metrics`, `tables`). The runtime does NOT read
  this file -- it is the daily fresh fit waiting for promotion. The
  inline `model_freshness_health` step diffs validation Brier between
  staging and production at 0.001 (0.1pp) and surfaces drift; promotion
  to `mlb_stage2_run_env.json` remains a manual swap so a regression in
  the daily fit cannot silently change live FV. Staleness-checked
  against `data/games/regular/` dir mtimes (~16 min saved per run when
  the corpus hasn't changed).
- `team_offense_v2_weights.json`: **production** Stage-3 v2 weights
  (betas + shrinkage + bounds), consumed by
  `scripts/analysis/team_offense_model._load_weights_overrides` at
  runtime. Schema: `schema_version` (currently 1), `generated_at_utc`,
  `source_artifact`, `model_name`, `betas` (`prior_season`,
  `season_to_date`, `momentum_10`), optional `shrinkage`
  (`sigma_within_sq`, `tau_sq_season`, `tau_sq_prior`,
  `tau_sq_momentum`), and `bounds.max_logit_delta`. Missing file or
  wrong `schema_version` falls back cleanly to the compiled-in defaults
  in `team_offense_model.py`. Written by the manual
  `scripts/analysis/promote_team_offense_v2.py` after a human reviews
  the daily Stage-3 v2 fit at
  `data/analysis_output/team_offense_calibration/phase4_models.json`;
  the daily refresh refits but does NOT auto-promote.

### Staging-vs-production pattern

Both Stage-2 and Stage-3 v2 follow the same shape:

| Stage | Daily fit (research) | Production read |
| --- | --- | --- |
| Stage-2 | `cache/mlb_stage2_run_env.staging.json` (auto-rewritten by refresh) | `cache/mlb_stage2_run_env.json` (manual swap) |
| Stage-3 v2 | `data/analysis_output/team_offense_calibration/phase4_models.json` (auto-rewritten by refresh) | `cache/team_offense_v2_weights.json` (written by `promote_team_offense_v2.py`) |

The motivation is symmetric: a daily fit that auto-promotes itself
into the live FV path would mean a noisy day's data could silently
swap the live model. Keeping the promotion manual gives the operator
a chance to read the `model_freshness_health` Brier diff and make an
explicit "yes, swap" call.

## Cross-Folder Dependencies

- Trading runtime imports `RunEnvContext` and `Stage2RunEnvModel` directly
  from `cache/stage2_run_env_model.py`. Stage-2 inference is wired through
  the signal pipeline's "Stage-2 run env delta" phase. Schema changes to
  the model JSON or to the buckets must be coordinated with
  `scripts/trading/`.
- `scripts/trading/signal_config.py`, `scripts/analysis/analyze_polymarket_overreactions.py`,
  and `scripts/analysis/backtest_gates.py` all reference the JSON paths in
  this folder. Do not move or rename the JSON files casually -- check those
  call sites first.
- `scripts/monitor/monitor_mlb_polymarket_ou.py`,
  `scripts/analysis/build_pitcher_cache.py`,
  `scripts/analysis/refresh_game_weather.py`,
  `scripts/analysis/team_offense_model.py`, and
  `scripts/analysis/build_team_game_log.py` all read or write
  `pitcher_cache.json` / `team_game_log.json` / `weather/game_weather_<date>.json`
  here. The build cadence and refresh policy live in those modules; see
  `scripts/analysis/AGENT_CONTEXT.md`.
- The analysis-side calibration / walk-forward pipeline assumes Stage-1
  empirical and Stage-1 Poisson values are present per cell with the
  `o<L>` / `po<L>` keys. Renaming a key here ripples into training-table
  builders and report writers.

## Daily Data Efficiency

- The full Stage-1 build is two streamed passes over `data/games`. Memory
  scales with the number of distinct state keys, not games -- the heavy
  `defaultdict` is `state_stats` keyed by
  `(away, home, inning_bucket, half, outs, bases)`. For quick smoke tests
  use `--max-files`.
- Stage-2 is a single streamed pass producing `train_stats`, `full_stats`,
  and `val_rows`. `val_rows` is the heaviest list at runtime since it
  carries per-PA buckets + per-line probs/outcomes; `--max-files` is the
  knob to keep it bounded during iteration.
- Both builders fail loudly if the input tree is missing or empty
  (`No game files found`, `No final games loaded`, `No samples matched
  Stage-1 cache cells`). Don't paper over these with try/except --
  silently shipping a stale or partial JSON is worse than crashing.
- Hyperparameters are exposed as CLI flags (`--prior-n-*`, `--min-n-*`,
  `--delta-cap-*`, `--weight-grid-*`, `--min-brier-improvement`,
  `--max-total-delta`). Document in the run log when you change a
  default; the defaults in `parse_args()` are the canonical production
  config.

## Safe-Edit Checklist

- Do not hand-edit any `*.json` here. Regenerate by running the
  matching builder so `meta.built` and the validation summaries stay
  truthful.
- Do not change the state-key string format, line-key encoding
  (`o<digits>` / `po<digits>`), or `FAMILY_ORDER` without coordinated
  updates in `scripts/trading/` and `scripts/analysis/`.
- Do not promote Stage-2 weights past the
  `--min-brier-improvement` floor by hand. If a line's validation gain is
  below the floor, the builder zeros it on purpose; making it non-zero
  manually defeats the "no forced complexity" guarantee.
- Do not change `UNKNOWN_BUCKET` (`"__UNK__"`); both fit-time and apply-
  time paths look it up by exact string.
- When you change a Stage-1 helper that Stage-2 imports
  (`bases_mask_from_matchup`, `final_score_from_game`, `is_final_game`,
  `inning_bucket`, `line_to_threshold`), rebuild **both** caches in
  order: Stage-1 first, Stage-2 second. Stage-2 reads the freshly built
  Stage-1 JSON.
- After a rebuild, run the full pytest suite (analysis-side tests in
  particular load `mlb_ou_cache.json` for fixture realism) plus
  `python -m compileall -q cache` before handing off.
