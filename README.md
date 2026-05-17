# Baseball Polymarket O/U Pipeline

## Objective

This repo builds and operates a **self-improving** automated betting system for MLB Over/Under totals markets on Polymarket. "Self-improving" is the load-bearing word: every model, calibrator, gate-economics audit, and drift alert that the live runtime depends on is rebuilt and re-evaluated on the latest data by the **startup script** (`scripts/analysis/run_daily_refresh.py`, executed automatically before `LiveTradingEngine` boots). The operator's job is to read the daily refresh's health rollup and apply the small set of decisions the system explicitly flags for human approval (Stage-2 promotion, Stage-3 v2 promotion, gate threshold changes); everything else self-corrects between sessions.

### Self-improvement contract

Concretely, this means the startup script is responsible for:

- **Re-ingesting the world**: scrape yesterday's completed games + today's schedule, refresh stadium weather, refresh pitcher ERA, rebuild Stage-1/2/3 inputs.
- **Re-fitting every learned artifact** whose inputs have changed: probability calibration (with stability gate against day-over-day flip-flop), EV-policy win+fill models, Stage-2 staging cache, Stage-3 v2 weight fits, learned execution policy prototype, market-anchored alpha residual models.
- **Re-validating the model** out-of-sample: rolling walk-forward (score-event + no-score drift, kept separate), walk-forward certification scorecard with `KEEP` / `RETUNE` / `RETIRE` verdicts per enforced gate, stake-scaling promotion analyzer with `need_more_data` / `hold` / `promote` verdict.
- **Self-auditing for drift**: seven-dimension drift alerts (calibration / fill rate / signal quality / regime mix / cohort-ROI / concept-drift / drift-in-drift) gated on Wilson upper bound where applicable; Stage-2 staging-vs-production Brier diff; model-artifact age check; calibration method stability gate; orphan-fill reconciler tracking. Two of the seven are *leading* indicators -- `concept_drift_health` fires when today's input distribution differs from last month's, and `drift_in_drift_health` fits a linear trend over 30d of PSI snapshots to catch slow-creep drift that never crosses the daily threshold but accumulates past it over weeks.
- **Surfacing one operator answer per session**: `refresh_health_rollup` prints one INFO block answering "is the project healthy?"; the weekly HTML rollup bundles trailing-7-day KPIs + alerts + sparklines into one static page.

What the startup script deliberately does **not** do automatically:

- Promote Stage-2 staging to production cache (auto-fitted; manual swap via inspection of staging vs production Brier).
- Promote Stage-3 v2 research weights to `cache/team_offense_v2_weights.json` (manual via `promote_team_offense_v2.py`).
- Change enforced gate thresholds (manual after reading walk-forward certification verdicts).
- Promote shadow gates / EV-policy enforce / stake scaling enforce (manual after the corresponding promotion analyzer verdict reads `promote`).

These are the four levers the operator pulls. Everything upstream of those levers refits and self-audits each day with no human action.

All four levers are wrapped behind one CLI in both directions:
`python scripts/analysis/promote.py {status, stage2 | stage3-v2 | stake-scaling | gate-threshold, demote {stage2 | stage3-v2 | stake-scaling | gate-threshold}}`.
Each subcommand reads the relevant verdict file, refuses unless the
verdict says go (or `--force`), performs the change, and appends to
`data/analysis_output/promotion_events.jsonl` with a `direction:
"promote" | "demote"` tag. `status` is read-only and shows all four
*promotion* verdicts plus the four *demotion* verdicts (post-promotion
outcome-regression check) in one summary -- the daily check.

Promotions of file-swap levers (stage2, stage3-v2) atomically back up
the prior production to `<file>.prior_promote.json` before swapping,
so demote can roll back to a known-good state. Cohort-ROI drift alerts
get auto-tagged with `[coincides with: <lever> promotion N days ago]`
when any promotion lands in the trailing 14 days -- the fast signal
that complements the 14-day post-hoc demotion verdict.

**Auto-daemon (preview-default)**: the daily refresh's
`auto_promote_demote_daemon` step reads today's verdicts + the
promote_events log and (in `--auto-daemon-mode act`) auto-invokes
`promote.py` for file-swap levers when a verdict says go AND a
14-day cooldown has elapsed since the last action. Ships in
`preview` mode by default (logs decisions only; takes no action), so
operators can review what the daemon would do for several sessions
before flipping to `act`. CLI-flag levers (stake-scaling,
gate-threshold) stay manual under the daemon since they don't actuate
through a file swap. This is the transition from "self-improving with
human approver" to "self-improving with human reviewer."

### Current strategic objective

Within that self-improvement frame, the current trading objective is **state-value Over trading around market overreaction to score and no-score transitions**.

This is a slight pivot from the earlier framing. The original premise focused mostly on modeling the expected game state after the next scoring-relevant event while avoiding fastest-feed competition. That remains useful, but live evidence showed that "phantom post-score state" risk can make immediate score-event signals look much better than they are. The strategy now treats both sides of the transition as research targets:

- **Score-event transitions**: only buy Overs when the post-score state value remains robust and phantom-run risk is low.
- **No-score drift transitions**: study cases where the Over drifts lower during a same-score segment, but current-state value still supports scoring upside.

The goal is not to be the fastest reactor after a scoring event. We assume faster participants often win that race. The goal is to identify mispriced Over state value after market moves, combine it with execution probability, and place trades only when expected realized EV is positive after fill risk, adverse selection, and microstructure effects.

**Current status: live trading active.** Paper trading is also available for simulation and testing.

---

## How It Works (plain English)

### The core idea

During an MLB game, the fair probability for each O/U line evolves as score, inning, outs, runners, and context evolve.

Instead of assuming we can consistently capture immediate post-score lag, we treat each score or no-score market transition as a candidate and ask:
- What is the probability this bet wins if we get filled?
- What is the probability we actually get filled at our intended price?
- What is expected realized EV after combining those two probabilities?
- Is the model's edge coming from a real current-state advantage, or from a phantom inferred-score state that the market is correctly rejecting?

### Candidate generation and decision rule

Every ~2-3 seconds, the system fetches live bid/ask prices and game-state context and generates candidate events when structural conditions are met.

There are now two candidate families:
- **Score-event candidates**: ask jumps and confirmed movement suggest the market may be repricing after a scoring-relevant event.
- **No-score drift shadow candidates**: the score has not changed, the Over ask has drifted lower, and current-state value may still be attractive.

The trade decision is model-based:
1. **`p_win_if_filled`** from pre-signal game-state features
2. **`p_fill`** from execution/microstructure features
3. **`EV_realized = p_fill * EV_if_filled`**

The trigger itself is not the thesis. It is only the candidate generator. Current production trading still uses the stable gate stack; EV policy, probability calibration, no-score drift, and phantom-risk diagnostics are kept in shadow unless explicitly promoted after enough data.

### State-value transition diagnostics

The system logs additional diagnostics so each candidate can be reviewed under the current strategic objective:

- `state_value_strategy`: `score_event_transition` or `no_score_drift`
- `current_state_value_edge`: fair-value edge using the confirmed current score
- `shadow_after_event_edge`: edge after the inferred scoring event
- `shadow_fv_inferred_lift`: how much edge comes from the inferred event state
- `shadow_phantom_risk_score` / `shadow_phantom_risk_band`: non-enforcing risk tag for score-event candidates
- `shadow_post_tr20_*_pass`: non-enforcing probes for possible post-TR20 tuning
  (`extreme_020`, ask-ramp-v2, Gate-6-relax-as-enforced, and combined pass)
- live end-of-run `current_state_edge_band_diagnostics`: shadow-only score-event outcome buckets for current-state edge below 0.03, 0.03-0.08, and 0.08+
- candidate rollup sidecars (`*_candidate_rollup.json`): complete counts by decision/reason/game-line; high-volume early-gate raw rows are sampled by state/price bucket while model-bearing rows remain full fidelity

These fields are diagnostic-only today. They help separate real state-value opportunities from model artifacts before any new gate or execution rule is promoted.

### The fair-value model (3 stages)

**Stage 1 -- Poisson base probability**: A historical lookup table built from 5 years of MLB game data. Given the exact game state (score, inning, half-inning, outs, base runners), it returns the historical probability of the total going over the line. Poisson calibration smooths cells with limited data.

**Stage 2 -- Park & weather adjustment**: Adjusts the Stage-1 probability based on the venue (park factor) and current weather (temperature, wind direction and speed). A 90-degree day with wind blowing out at Wrigley is materially different from a cold night at Oakland Coliseum. The Stage-2 model has six feature families: `park` (categorical per-stadium baseline), `temp`, `wind`, `park_wind` (interaction), `density_alt` (elevation x temperature interaction -- captures ball-carry physics so a hot day at Coors -> ~8,900 ft density altitude is treated differently from a cold night at Coors), and `hr_factor` (per-(park, season) HR rate vs league mean -- captures year-over-year drift in park HR-friendliness from juiced ball, fence moves, humidor changes, etc., that the multi-year `park` bucket averages away). The `density_alt` and `hr_factor` families were added 2026-05-08 and only take effect after the next Stage-2 cache rebuild.

**Stage 3 -- Team offense adjustment**: Adjusts further for the two teams actually playing. A game between the Yankees and Dodgers has a different run expectation than the same game state between two weak offenses. The current Stage-3 (deployed 2026-05-07, TR20) blends three EB-shrunk per-team windows -- prior season (negative coefficient -0.151, regression-to-mean), season-to-date (+0.141), and trailing 10-game momentum (+0.150) -- multiplied by a linear inning-weight ramp. Replaces the original 2026-04 single-50-game-window v1 model that was found to be ~3.2x too aggressive on a 1.13M-row residual table over 2021-2026. See `model_improvements/team_offense_v2_phase4_findings_2026_05_07.txt`.

### The gates (filters that prevent bad bets)

Even when the math looks good, many game situations are known to produce unreliable signals. These filters block those:

| Gate | What it blocks | Why |
|---|---|---|
| Inning minimum | Lines < 8.5: no bets before inning 4; lines >= 8.5: before inning 5 | Too many innings remain, too much variance |
| Minimum ask | Standard lines: ask < 0.55; high lines: ask < 0.60 | Thin books / noisy pricing |
| Pace check | Current run pace x 9 innings < line - 1.5 | Game is objectively too slow-scoring |
| Runs still needed | More than 3.5 runs still needed to hit the line | Historically poor ROI at this distance |
| Close game + far from line | Lead <= 1 AND runs needed >= 4 | Both managers play defense-first in tight games |
| Inning 5 dead zone | Inning 5 AND runs needed >= 2.5 | Bullpen transition inning suppresses scoring |
| Inning 6 dead zone | Inning 6 AND runs needed >= 2.5 | Setup relievers create another scoring lull |
| Blowout / blowout-adjacent | Trailing <= 1 run AND lead >= 6 AND inning >= 6; OR trailing <= 1 AND lead >= 4 AND inning >= 7 | Poisson inflates probability 11-30pp; trailing team is functionally eliminated |
| FV saturation | Stage-1 base fair value >= 0.99 | Poisson ceiling = phantom API score update; market LTP contradicts model |
| Large FV/ask gap (late) | Model edge > 28pp AND inning >= 7 | Market is better-informed; large late-inning gaps are a phantom-run fingerprint |
| **Extreme edge (any inning) [TR17, ENFORCED 2026-05-01; threshold tightened TR19, 2026-05-03]** | **Model edge > 22pp in ANY inning** | **Originally enforced at 0.30 on 2026-05-01 (TR17). Tightened to 0.22 on 2026-05-03 (TR19) after the 2026-04-28 -> 2026-05-03 live window: edge>0.20 cohort was 4W/8L, -$79.57 on 12 bets at avg fill ~0.63 (Wilson 95% upper bound ~58%, well below break-even ~62%). All 8 unique window losses had `p_score_event_proxy = 0.000` -- no ask-jump confirmation that an actual run scored. Bets with edge<0.20 in the same window were +$24.38 (10W/1L). See `scripts/analysis/analyze_window_2026_04_28_to_05_03.py` for the full analysis.** |
| Stage-2 extreme suppression | S2 logit delta <= -0.20 AND inning >= 6 | Extreme park/weather suppression exceeds Stage-2 model's correction capacity |
| Pitcher quality boost | Current pitcher ERA < 3.75 AND inning <= 6 | Raises minimum edge by 3pp when facing a quality starter (requires pitcher cache) |

### Order execution

Execution is treated as part of the model, not just plumbing.

The current implementation places a **limit buy order** on Polymarket when a candidate passes decision thresholds. The baseline limit heuristic is:

```
limit = bid + spread x 0.65
```

This is a baseline heuristic, not a guaranteed edge. The long-term objective is to replace fixed spread heuristics with an execution policy optimized on realized EV.

**Order management after placement:**
- Every ~4-10 seconds, open orders are polled for fills
- If the model's current fair value decays to within 5% of our limit price (edge collapsed), the order is cancelled -- the thesis is no longer valid
- If the game ends before the order fills, it is cancelled and recorded as a missed signal
- A 3-hour safety-net timeout exists for abandoned orders

### Bet sizing

The system supports **quarter-Kelly** sizing, but Kelly is only appropriate when probabilities are calibrated and stable out-of-sample.

Until calibration quality is proven, conservative sizing (flat or tightly capped Kelly) should be treated as the default risk posture.

The Kelly formula for a binary prediction market is:

```
f* = (fair_value - limit_price) / (1 - limit_price)
```

Where `fair_value` is the model's probability estimate and `limit_price` is the price we're paying. This fraction represents how much of the bankroll has positive expected value at this edge. Multiplying by quarter-Kelly (0.25) and the daily budget gives the stake:

```
stake = f* x 0.25 x daily_budget
```

For example, if the model says an over has a 78% chance and our limit is 0.62, then:
- `f* = (0.78 - 0.62) / (1 - 0.62) = 0.42`
- `stake = 0.42 x 0.25 x $125 = ~$13`

A bet with tighter edge (say FV=0.72, limit=0.67) would size to ~$5. A bet with strong edge (FV=0.85, limit=0.65) would size to ~$26. The system caps any single bet at 33% of the daily budget (~$41 at the default $125 budget) and floors at $5.

**Why quarter-Kelly?** Full Kelly maximizes long-run growth rate but requires a perfectly calibrated model. Quarter-Kelly gives roughly 50% of the maximum growth rate at 1/16th the variance -- a standard choice when there is uncertainty about model precision. As live data accumulates and the model is further calibrated, this fraction can be tuned upward.

### Accounting

Filled orders contribute to realized P&L. Cancelled/unfilled orders are recorded as "missed signals" with their counterfactual outcome (would it have won?) tracked separately. This keeps three clean views:
- **Signal quality**: % of signals that would have won if filled
- **Fill rate**: % of signals that actually got a fill
- **Realized P&L**: actual money made/lost on filled orders only

### Logging

All session activity is written to `logs/real-logs/YYYY-MM-DD.log` at DEBUG level (every tick decision, FV check, order poll). The terminal shows INFO-level output only. The log file is the primary source for post-session analysis of Kelly inputs, fill rates, and FV decay patterns.

---

## Repository Layout

```text
baseball/
  README.md
  requirements.txt
  model_overview.txt          -- detailed model design notes
  buying_model_1.txt          -- limit price calibration notes
  codex_thoughts.txt          -- external code review findings
  claude_response.txt         -- response to code review with verifications

  cache/
    build_mlb_ou_cache.py     -- builds Stage-1 Poisson probability table
    build_mlb_stage2_run_env.py -- builds Stage-2 park/weather model
    stage2_run_env_model.py   -- Stage-2 runtime module
    mlb_ou_cache.json         -- Stage-1 production cache (5 completed prior seasons; metadata-audited)
    mlb_stage2_run_env.json   -- Stage-2 model weights
    team_game_log.json        -- per-team RPG history (Stage-3 input)
    MLB_OU_CACHE_DESIGN.md
    STAGE2_RUN_ENV_MODEL.md

  scripts/
    scraping/
      scrape_mlb_history.py   -- pulls historical MLB game data from StatsAPI

    monitor/
      monitor_mlb_polymarket_ou.py -- live game + market monitor (orchestrator + CLI)
      monitor_constants.py    -- URLs, defaults, TEAM_SLUGS, regex, debug cadences
      monitor_utils.py        -- _safe_float / _safe_int / slug + iso helpers
      monitor_system.py       -- performance-mode (CPU pinning) + sleep prevention
      monitor_models.py       -- ScheduleScore / ScheduledGame / OUMarket / GameMarketMatch
      monitor_recorder.py     -- LocalRecorder (per-game JSONL writer)
      monitor_stats_client.py -- MLBStatsClient (schedule fetch + pitcher ERA cache)
      monitor_discovery.py    -- PolymarketDiscoveryClient (Gamma event matching)
      monitor_book_client.py  -- PolymarketBookClient (CLOB + Gamma fallback)
      monitor_cli.py          -- parse_args

    analysis/
      evaluate_no_score_drift_policy.py   - shadow/paper no-score drift policy evaluator
      build_queue_aware_execution_replay.py - offline execution price policy replay
      analyze_polymarket_overreactions.py -- detects scoring events and market lag
      analyze_book_captures.py            -- post-session order fill diagnostics
      backtest_gates.py                   -- backtests gate stack against historical data
      team_offense_model.py               -- Stage-3 team offense runtime module
      build_team_game_log.py              -- builds team_game_log.json from history
      build_pitcher_cache.py              -- builds pitcher ERA cache for Stage-4 gate
      build_unified_signal_table.py       -- canonical event-level signal table builder
      build_calibration_opportunity_training_table.py -- model-bearing opportunity table from calibration + score-confirmation rows
      analyze_scoring_environment_trends.py -- 10-season scoring drift + season-weight research
      analyze_scoring_path_effects.py -- 10-season scoring-path timing research for FV feature candidates
      evaluate_stage1_prior_candidates.py -- broad Stage-1 prior proxy screen: rolling windows, recency weights, blends
      fair_value_stage_ablation_report.py -- FV ladder ablation: market, inference, Stage-2, Stage-3, final FV
      build_fv_gap_decomposition_report.py -- FV gap decomposition: market/no-vig vs Poisson, empirical, Stage-2/3, final FV
      build_fv_trust_shrinkage_experiment.py -- support-weighted FV shrinkage toward ask/no-vig market anchors
      audit_stage1_inferred_empirical.py -- score-event Stage-1 Poisson-vs-empirical overconfidence audit
      train_calibration_market_anchored_alpha.py -- family-separated market-anchored alpha models from calibration opportunities
      calibration_market_anchored_alpha_walk_forward.py -- rolling family alpha validation vs ask/no-vig baselines with clustered P&L CIs
      build_clv_report.py -- closing/late-price value diagnostics by family/gate/bucket and realized ROI
      build_fv_disagreement_quality_report.py -- FV-vs-market disagreement quality by family, support/trust, CLV, ROI
      fv_disagreement_quality_walk_forward.py -- rolling bucket-trust validation for FV-vs-market disagreement regimes
      build_analysis_safe_trade_table.py -- canonical session/ledger trade table; excludes order errors and labels live vs paper fallback
      build_signal_training_table.py      -- leakage-aware modeling table + date splits
      train_baseline_models.py            -- baseline signal/fill model trainer
      backtest_ev_policy.py               -- EV-ranked policy tuning and test backtest
      build_execution_diagnostics_report.py -- per-trade execution diagnostics (limit touch, first-touch, cancel reason, counterfactual)
      build_state_value_transition_report.py -- score-event vs no-score drift regime diagnostics
      walk_forward_runner.py              -- rolling walk-forward backtest harness (top roadmap item)
      no_score_drift_walk_forward.py      -- deduped no-score drift training table + walk-forward harness
      session_report.py                   -- live session analytics dashboard (P&L, gate sim, venue breakdown)

    trading/
      models.py            -- data models: BetRecord, LiveBetRecord, OrderResult, OrderStatus, TradeRecord
      signal_engine.py     -- core signal detection + gate pipeline + paper simulation (was paper_trader.py)
      live_engine.py       -- live CLOB execution engine, Kelly sizing, order lifecycle (was real_trader.py)
      polymarket_client.py -- Polymarket CLOB REST client: auth, orders, fills (was clob_order_client.py)
      ev_policy.py         -- EV policy model scoring runtime (LogisticJsonScorer)
      paper_trader.py      -- entry-point shim -> signal_engine.main() (backward-compat)
      real_trader.py       -- entry-point shim -> live_engine.main() (backward-compat)
      clob_order_client.py -- re-export shim -> polymarket_client (backward-compat)

  data/
    games/                     -- raw MLB StatsAPI game JSONs
    schedules/                 -- daily MLB schedule JSONs
    manifests/                 -- scrape run manifests
    polymarket/mlb_ou/         -- live tick data from monitor
    paper_trading/
      master_ledger.jsonl      -- all paper bets (48 records)
      sessions/                -- per-date paper session JSONs
      book_captures/           -- post-signal bid/ask snapshots
    live_trading/
      candidate_universe/*_candidate_rollup.json - compact daily candidate rollups; raw JSONL remains canonical
      master_ledger.jsonl      -- all live bets (filled + missed)
      live_orders_ledger.jsonl -- canonical live order lifecycle stream
      candidate_universe/      -- trade, skip, skip-with-features, and shadow no-score drift rows
      sessions/                -- per-date live session JSONs
      book_captures/           -- post-signal bid/ask streaming captures (legacy)
      tape_captures/           -- Family A (recent-flow) sidecars: per signal + per late-stage skip
      book_decision_snapshots/ -- Family B (book state at decision) sidecars
      velocity_snapshots/      -- Family C (book velocity / drift) sidecars

  logs/
    test-logs/                 -- TestRun1 through TestRun11 iteration notes
    real-logs/                 -- per-date live session logs (DEBUG level)
```

---

## Dependencies

```bash
pip install -r requirements.txt
```

Requires Python 3.9+. Key packages: `requests`, `python-dotenv`, `py_clob_client`.

For live trading, a `.env` file at the repo root is required with:
```
POLY_PRIVATE_KEY=<your Polymarket private key>
```

---

## 1) Historical Scraper

```bash
# Default: recent historical lookback, regular season
python scripts/scraping/scrape_mlb_history.py

# Ten completed prior seasons for Stage-1 research candidates
python scripts/scraping/scrape_mlb_history.py \
  --start-date 2016-01-01 --end-date 2025-12-31 --game-types R

# Custom range
python scripts/scraping/scrape_mlb_history.py \
  --start-date 2021-01-01 --end-date 2026-04-10

# Dry run (schedule only, no game downloads)
python scripts/scraping/scrape_mlb_history.py --dry-run
```

Writes to `data/games/`, `data/schedules/`, `data/manifests/`.

---

## 2) Cache Building

### Stage-1: Poisson probability table

```bash
python cache/build_mlb_ou_cache.py --season-type regular --min-season 2021 --max-season 2025
```

Reads all game JSONs in `data/games/`. Builds a lookup table keyed by
`{away_score}_{home_score}_{inning}_{half}_{outs}_{bases}` with empirical and
Poisson-calibrated P(over line) for each O/U line. Output: `cache/mlb_ou_cache.json`.
The builder deduplicates repeated schedule-date files by `gamePk` and writes
cache metadata (`history_start_date`, `history_end_date`, `seasons`,
`games_by_season`, `total_games`, `builder_args`). For 2026 live runs the
production fallback is the five completed prior seasons, 2021-2025. The current
lead research candidate is rolling 4y, 2022-2025; it stays a side artifact until
deduped FV ablation and walk-forward validation justify promotion:

```bash
python cache/build_mlb_ou_cache.py \
  --season-type regular --min-season 2022 --max-season 2025 \
  --out cache/mlb_ou_cache_4y_2022_2025_candidate.json
```

To validate broader-history research candidates, write them to side artifacts:

```bash
python cache/build_mlb_ou_cache.py \
  --season-type regular --min-season 2016 --max-season 2025 \
  --out cache/mlb_ou_cache_10y_candidate.json
```

Scoring-environment weighted candidate caches are also supported. The weights
file is produced by `analyze_scoring_environment_trends.py`; allocation mode
treats the weights as total season shares and normalizes total weighted game
mass back to the unweighted game count:

```bash
python cache/build_mlb_ou_cache.py \
  --season-type regular --min-season 2016 --max-season 2025 \
  --season-weights-path data/analysis_output/scoring_trends/recommended_stage1_season_weights.csv \
  --season-weight-mode allocation \
  --out cache/mlb_ou_cache_weighted_scoring_env_candidate.json
```

As of the first comparison through 2026-05-09, rolling 4y (`2022-2025`) was the
best all-family/no-score calibration candidate on deduped rows. The 5y
production fallback preserved better score-event rank ordering. Weighted 10y
improved Brier/logloss versus uniform 10y but did not beat 4y/5y on the small
labeled sample. Keep all non-5y caches research-only until deduped FV ablation
and walk-forward validation say otherwise.

### Stage-2: Park/weather model

```bash
python cache/build_mlb_stage2_run_env.py
```

Fits logistic regression residuals on venue + weather features. Output: `cache/mlb_stage2_run_env.json`.

The live startup path also refreshes a per-game stadium weather cache:

```bash
python scripts/analysis/refresh_game_weather.py --date 2026-05-06
```

Output: `cache/weather/game_weather_<date>.json`. This cache joins MLB schedule
games to `data/reference/mlb_stadium_weather_metadata.json`, fetches hourly
local stadium weather by coordinate, and stores Weather v2 fields such as
temperature, humidity, pressure, air-density index, density altitude, wind-out
component, wind-cross component, roof/exposure metadata, and source age.
Weather v2 is now the **only live weather input** for Stage-2 FV adjustment.
When provider data is missing, Stage-2 keeps venue/park context but temp/wind
degrade to unknown buckets; it does not fall back to legacy schedule text.

Flattened weather-v2 fields are now propagated into runtime candidate rows,
placed-bet diagnostics, candidate-universe exports, unified signals, signal
training tables, no-score drift policy rows, no-score paper ledgers, and the
no-score drift walk-forward feature set. This lets walk-forward tests measure
whether local weather improves calibration or P&L without reparsing cache files
or reparsing cache files. The full raw weather audit stream is preserved, but
model feature lists use a smaller `WEATHER_MODEL_FEATURE_FIELD_KEYS` subset:
cache date/time, stadium ids/names, and raw error text are excluded, while
`weather_effective_*` fields are populated only for open-air games where outside
weather is model-usable. Retractable-roof games remain auditable but are not
treated as clean weather-model examples until roof state is validated.

### Stage-3: Team game log

```bash
python scripts/analysis/build_team_game_log.py
```

Builds per-team runs-per-game history used by the team offense model. Output: `cache/team_game_log.json`.

---

## 3) Live Monitor (standalone)

The monitor alone just records market data without trading.

```bash
# Run today
python scripts/monitor/monitor_mlb_polymarket_ou.py

# Specific date
python scripts/monitor/monitor_mlb_polymarket_ou.py --date 2026-04-09

# One-cycle smoke test
python scripts/monitor/monitor_mlb_polymarket_ou.py --once --start-on-preview --poll-interval 1.0
```

Key defaults: `--poll-interval 2.5`, `--max-workers 20`.

Writes tick data to `data/polymarket/mlb_ou/YYYY-MM-DD/`.

---

## 4) Paper Trader / Signal Simulation

Runs the full signal + gate pipeline but records simulated bets instead of placing real orders.
Useful for testing gate changes before going live.

The core logic lives in `scripts/trading/signal_engine.py`. The `paper_trader.py` entry point is a thin shim that calls it -- both work identically:

```bash
# Default run (either form works)
python scripts/trading/signal_engine.py
python scripts/trading/paper_trader.py

# Adjust edge threshold or stake
python scripts/trading/signal_engine.py --edge-threshold 0.12 --stake 200

# Specific date
python scripts/trading/signal_engine.py --date 2026-04-09
```

### Current paper trading defaults

| Parameter | Default | Notes |
|---|---|---|
| `--edge-threshold` | 0.15 | Standard lines (< 8.5) |
| `--edge-threshold-high-line` | 0.16 | Lines >= 8.5 |
| `--jump-threshold` | 0.06 | Min ask jump to flag signal |
| `--max-spread` | 0.20 | Max bid/ask spread allowed |
| `--min-inning` | 4 | Standard line minimum inning |
| `--min-inning-high-line` | 5 | High-line minimum inning |
| `--min-entry-ask` | 0.55 | Standard line ask floor |
| `--min-entry-ask-high-line` | 0.60 | High-line ask floor |
| `--runs-needed-max` | 3.5 | Max runs still needed to hit line |
| `--min-close-game-rn` | 4.0 | Close-game high-rn dead zone |
| `--inn5-rn-max` | 2.5 | Inning 5 runs-needed ceiling |
| `--inn6-rn-max` | 2.5 | Inning 6 runs-needed ceiling |
| `--blowout-lead-min` | 6 | Full blowout: lead >= this AND inning >= 6 |
| `--blowout-adj-lead-min` | 4 | Blowout-adjacent: lead >= this AND inning >= 7 |
| `--max-base-fv` | 0.99 | FV saturation skip threshold |
| `--fv-ask-gap-max` | 0.28 | Large-gap skip: model edge > this AND inning >= 7 |
| `--s2-suppress-max` | -0.20 | Stage-2 extreme suppression logit threshold |
| `--s2-suppress-min-inning` | 6 | Minimum inning for S2 suppression gate |
| `--sp-era-threshold` | 3.75 | Pitcher quality gate ERA threshold |
| `--sp-era-edge-boost` | 0.03 | Extra edge required when pitcher ERA is elite |
| `--min-current-total` | 4 | Min combined score before betting |
| `--confirmation-ticks` | 3 | Ticks ask must stay elevated |
| `--event-dedup-secs` | 60 | Seconds between bets on same game |
| `--inning-dedup-gap` | 2 | Innings between bets on same line |
| `--stake` | 100 | Notional $ per paper bet |
| `--capture-duration` | 30 | Seconds of post-signal book capture |

Outputs: `data/paper_trading/master_ledger.jsonl`, `data/paper_trading/sessions/`.

---

## 5) Live Trader

Places real orders on the Polymarket CLOB. Inherits all gate and signal logic from `signal_engine.py`. Requires `.env` with `POLY_PRIVATE_KEY`.

> **TR17 (2026-05-01) -- `gate_extreme_edge` is now ENFORCED.** Any signal with
> `edge > 0.30` (configurable via `--extreme-edge-max`) is now skipped in any
> inning, not just inning >= 7. This is a direct response to the cumulative
> live evidence: edge>0.25 settled bets through 2026-04-30 are 1W/5L, -$117.56
> on $140.29 stake. See the Gate Evolution table and the Evidence Snapshot for
> the full justification. The `ltp_ask_gap` shadow tag remains shadow-only.
>
> **TR19 (2026-05-03) -- `gate_extreme_edge` threshold tightened 0.30 -> 0.22.**
> The 6-day live window 2026-04-28 -> 2026-05-03 ran 21 settled bets at 11W/8L
> for -$55.19 P&L. The edge>0.20 cohort was 4W/8L (33% WR) for -$79.57 on 12
> bets; the edge<0.20 cohort was 10W/1L (91% WR) for +$24.38 on 11 bets. All
> 8 unique losses had `p_score_event_proxy = 0.000` (no ask-jump confirmation
> of a real run). Wilson 95% upper bound on the high-edge cohort WR is ~58%,
> well below the ~62% structural break-even at the cohort's avg fill price
> (0.63). The 0.22 threshold keeps a 2pp buffer above the empirical boundary
> so signals near the edge_threshold floors (0.10 / 0.15) still pass. Override
> via `--extreme-edge-max` if needed. Full analysis:
> `scripts/analysis/analyze_window_2026_04_28_to_05_03.py`.

The core logic lives in `scripts/trading/live_engine.py`. The `real_trader.py` entry point is a shim:

```bash
# Dry run (full logic, no real orders posted)
python scripts/trading/live_engine.py --dry-run
python scripts/trading/live_engine.py --dry-run  # shim, same result

# Default live run (Kelly sizing, $125 daily budget)
python scripts/trading/live_engine.py

# Override to flat betting at a fixed stake
python scripts/trading/live_engine.py --stake-mode flat --stake 25

# Custom budget or Kelly fraction
python scripts/trading/live_engine.py --daily-budget 300 --kelly-fraction 0.30

# Adjust FV-decay cancel threshold
python scripts/trading/live_engine.py --fv-cancel-min-edge 0.08

# Disable default hardware performance mode for debugging/unusual hardware
python scripts/trading/live_engine.py --no-performance-mode
```

### Startup artifact refresh (canonical daily refresh)

Live startup runs the canonical daily refresh before `LiveTradingEngine` loads
its runtime artifacts. As of 2026-05-16 it is a **45-step base pipeline**
(plus one `daily_human_review:<date>` step per stale completed session) -- it
scrapes yesterday's completed games, refreshes today's MLB schedule, rebuilds
Stage-1/2/3 inputs, runs preflight cache checks, retrains every research
artifact whose inputs have changed (Stage-2 staging, Stage-3 v2 weight fits,
EV-policy table, probability calibration), rebuilds all completed-session
analysis outputs, audits artifact lineage/freshness, and finishes with a
one-block operator-facing health rollup. Runtime-consumed calibration / EV /
market-anchored alpha artifacts are built with `--artifact-purpose
runtime-refit`: evaluation still uses train/validation/test splits, but
exported parameters are refit on all eligible labeled rows after method or
policy selection.

Steps run in this order:

1. `preflight_env_secrets` (inline) -- verify `.env` and `POLY_PRIVATE_KEY` (warning by default; hard-fail with `--startup-refresh-require-poly-private-key`)
2. `scrape_recent_games` -- backfill the last N completed days of MLB game JSONs (default lookback 7 days, end date = yesterday)
3. `stage1_ou_cache` -- rebuild `cache/mlb_ou_cache.json` from the five completed prior regular seasons for the active year
4. `scrape_active_schedule` -- refresh the active-month schedule (`--dry-run`, no in-progress feed downloads)
5. `game_weather_cache` -- `cache/weather/game_weather_<active-date>.json` (canonical Weather v2 live Stage-2 input)
6. `pitcher_cache` -- `cache/pitcher_cache.json`
7. `team_game_log` -- explicit Stage-3 input rebuild (`cache/team_game_log.json`); replaces lazy first-tick rebuild
8. `park_hr_factors` -- Stage-2 `hr_factor` family input (`cache/park_hr_factors.json`); per-(park, season) HR rate vs league mean
9. `preflight_artifacts` (inline) -- validate Stage-1/2/3 cache loads + Stage-1 production coverage metadata + Stage-3 active-season team coverage >= 80%; warns if `park_hr_factors.json` is missing
Optional. `daily_human_review:<date>` -- one step per missing/stale completed session
10. `analysis_safe_trade_table` -- canonical session/ledger trade table; excludes `order_status=error`, dedupes ledger lifecycle rows, and labels live vs paper fallback.
11-21. `candidate_universe_table`, `calibration_opportunity_training`, `calibrate_signal_probabilities` (`--artifact-purpose runtime-refit`), `model_maturity_report`, `fair_value_stage_ablation`, `fv_gap_decomposition`, `fv_trust_shrinkage`, `calibration_market_anchored_alpha` (`--artifact-purpose runtime-refit`), `stage1_inferred_empirical_audit`, `unified_signals`, `signal_training_table`
22. `clv_report` -- entry price vs late captured mid, grouped by family/gate/bucket and compared with realized ROI. Staleness-checked.
23. `fv_disagreement_quality` -- when raw FV disagrees with market, rank buckets by outcome calibration gain, CLV, ROI, and Stage-1 support/trust. Staleness-checked.
24. `train_baseline_models` -- EV-policy win + fill models (Active #6 part 2 prerequisite). **Staleness-checked** against `signal_training_table.jsonl` mtime
25. `ev_policy_backtest` -- rebuilds `ev_policy_report.json` and runtime-safe EV model artifacts with `--artifact-purpose runtime-refit`. Staleness-checked
26. `stage2_run_env_retrain_staging` -- refits Stage-2 from the full 2021-2026 corpus to `cache/mlb_stage2_run_env.staging.json` (NOT the production cache; promotion stays manual). Staleness-checked against `data/games/regular/` dir mtimes; biggest single saver (~16 min when corpus unchanged)
27-29. `stage3_team_offense_features`, `stage3_team_offense_calibration_table`, `stage3_team_offense_v2_fit` -- three-step Stage-3 v2 retrain chain. All staleness-checked. Output is research-only (`phase4_models.json`); production weights at `cache/team_offense_v2_weights.json` require an explicit `promote_team_offense_v2.py` run
30. `model_freshness_health` (inline) -- diffs Stage-2 staging vs production validation Brier and surfaces drift alerts at the 0.001 (0.1pp) level; age-checks every model artifact and flags anything > 30 days old
31-35. `execution_diagnostics`, `queue_aware_execution_replay`, `learn_execution_policy` (Active #7 prototype), `state_value_transition_report`, `no_score_drift_policy`
36. `no_score_drift_paper_ledger`
37-40. `walk_forward_score_event` (staleness-checked), `walk_forward_no_score_drift`, `walk_forward_market_anchored_alpha` (staleness-checked; family-separated alpha validation vs ask/no-vig baselines with clustered policy-P&L CIs), `walk_forward_fv_disagreement_quality` (staleness-checked; train/validation-selected FV disagreement buckets applied out of sample)
41. `stake_scaling_promotion_analyzer`
42. `walk_forward_certification`
43. `weekly_drift_rollup`
44. `artifact_lineage_freshness` -- writes `data/analysis_output/artifact_lineage_freshness/artifact_lineage_freshness_report.{json,md,csv}` with generated/max dates, input hashes/mtimes, row/family counts, and stale-downstream flags.
45. `refresh_health_rollup` (inline) -- reads per-step results + latest daily human-review alert counts + walk-forward summary + `model_freshness_health` notes + artifact-lineage summary and prints one INFO block. The operator-facing answer to "is the project healthy?"

`StalenessCheck` policy: subprocess steps with a declared `output_path` + input
set are skipped (`status="skipped_fresh"`) when the output mtime is at least
as new as every input mtime. `--force-retrain` bypasses every check; useful
for full rebuilds. The summary line counts both `ok` and `skipped-fresh`.

By default this is **fail-open**: a failed refresh logs a warning and trading
continues, because stale research artifacts should not block order lifecycle
recovery. Use `--startup-refresh-strict` when you explicitly want bad refresh
state to abort live startup. As of 2026-05-12 the refresh **does** retrain
runtime decision artifacts in-band: probability calibration
(`calibrate_signal_probabilities.py`), the EV-policy table
(`learn_execution_policy.py`), the Stage-2 staging cache, and the Stage-3 v2
weight fits (`fit_team_offense_calibration_v2.py`). The Stage-3 v2 promotion
to production (`promote_team_offense_v2.py`) remains a deliberate manual step,
since auto-promotion of fair-value weights without a comparison gate would
risk a silent live-model swap.

The refresh writes a manifest to
`data/analysis_output/startup_refresh/<date>_startup_refresh.json` with
`summary`, `summary_status`, `failed_step_names`, `logs_dir_bytes`, and a
`phase6_reminder` field that fires after 2026-06-07 (30 days post-TR20) to
prompt re-tuning of TR19's `extreme_edge_max=0.22` against the v2 Stage-3 edge
distribution. Plan-only refreshes write
`<date>_startup_refresh_plan.json` so dry-run planning cannot overwrite the
canonical completed-run manifest.

At shutdown, live runtime performs a best-effort final scrape of the active
date's completed regular-season games before writing daily human-review and
model-maturity reports. This keeps same-day side-neutral, UNDER, and no-score
paper labels from lagging until the next manual scrape.

Useful controls:
- `--no-startup-refresh` for a fast launch (skips the entire refresh)
- `--startup-refresh-strict` to abort on refresh failure
- `--startup-refresh-max-date YYYY-MM-DD` to override the completed-session date
- `--startup-refresh-include-run-date` only after the active session is complete
- `--startup-refresh-skip-recent-games-scrape` / `--startup-refresh-skip-active-schedule-scrape` to skip scraping (Stage-3 inputs will go stale)
- `--startup-refresh-recent-games-lookback-days N` to change backfill horizon (default 7)
- `--startup-refresh-skip-stage1-cache` to skip rebuilding `cache/mlb_ou_cache.json`
- `--startup-refresh-skip-team-game-log` to skip explicit Stage-3 rebuild (lazy rebuild remains)
- `--startup-refresh-skip-park-hr-factors` to skip Stage-2 `hr_factor` input rebuild (cache will go stale)
- `--startup-refresh-skip-preflight-secrets` / `--startup-refresh-skip-preflight-artifacts` to skip preflight checks
- `--startup-refresh-require-poly-private-key` to hard-fail when `.env`/`POLY_PRIVATE_KEY` is missing (live mode)
- `--startup-refresh-skip-weather-cache` to skip local weather fetches
- `--startup-weather-provider none` to write metadata-only Weather v2 rows with unknown temp/wind buckets
- `--startup-refresh-skip-walk-forward` if startup time is more important than fresh rolling research

### Current production config (validation phase)

For reference, the actual command line being run live during the current validation
period (post-V2-cutover, flat-stake, conservative budget, all shadow diagnostics enabled):

```bash
python scripts/trading/real_trader.py \
  --timezone America/Toronto \
  --stake-mode flat --stake 10 \
  --daily-budget 80 --per-game-budget-fraction 0.40 --max-open-orders 7 \
  --fv-ask-gap-max 0.26 \
  --capture-duration 120 --capture-interval 1 --capture-depth 5 \
  --ev-policy-mode shadow --shadow-relaxed-enabled \
  --pitcher-cache-path cache/pitcher_cache.json \
  --log-level INFO
```

The 2026-04-22 P0 cancel-discipline values (`--fv-cancel-min-edge 0.03`,
`--fv-decay-min-age-secs 90`, `--fv-decay-min-ask-drop 0.03`) are now compiled
defaults, so they no longer need to appear on the command line. Same for
`--ask-reversal-drop 0.08`, `--ask-reversal-window 5`, and
`--order-timeout-secs 10800`.

Differences from the default flags table below:
- `--stake-mode flat --stake 10` (Kelly is the default but is suspended until the
  fill-model and probability calibration are validated; flat keeps sizing
  constant so calibration drift is easy to read).
- `--daily-budget 80` (sized to fund the conservative flat-$10 sizing across a
  typical 7-game slate; replaces the prior --stake 20/--daily-budget 160 pair).
- `--per-game-budget-fraction 0.40 --max-open-orders 7` (slightly looser than
  defaults to allow more concurrent same-game capacity).
- `--fv-ask-gap-max 0.26` (tightened from default 0.28 after Apr 21 phantom-run
  analysis).
- `--capture-duration 120 --capture-depth 5` (longer window, deeper book than
  defaults, to feed fill-model + execution-replay research).
- Shadow diagnostics on: `--shadow-relaxed-enabled`, `--ev-policy-mode shadow`.
- Performance mode is now default-on; use `--no-performance-mode` only for
  debugging or unusual CPU-affinity issues.

### Live trading defaults

| Parameter | Default | Notes |
|---|---|---|
| `--stake-mode` | kelly | `kelly` or `flat` |
| `--kelly-fraction` | 0.25 | Quarter-Kelly scaling factor |
| `--kelly-max-bet-fraction` | 0.33 | Max single bet as fraction of daily budget |
| `--daily-budget` | 125 | Max USDC deployed per session |
| `--per-game-budget-fraction` | 0.35 | Max same-game exposure as fraction of daily budget |
| `--stake` | 25 | USDC per bet (flat mode only) |
| `--min-order-size` | 5 | Minimum order size in USDC |
| `--spread-factor` | 0.65 | Limit price position in spread |
| `--fv-cancel-min-edge` | 0.03 | Cancel order if FV - limit < this (P0 lowered from 0.05 on 2026-04-22) |
| `--fv-decay-min-age-secs` | 90 | Minimum order age before FV-decay cancel checks (P0 raised from 30 on 2026-04-22) |
| `--fv-decay-min-ask-drop` | 0.03 | Ask must also confirm decay before cancellation |
| `--ask-reversal-drop` | 0.08 | Early cancel if ask drops sharply post-placement |
| `--ask-reversal-window` | 5 | Seconds where ask-reversal logic is active |
| `--kelly-max-edge` | 0.25 | Cap edge used in Kelly sizing (variance control) |
| `--order-timeout-secs` | 10800 | Safety-net timeout (3 hours) |
| `--max-open-orders` | 5 | Max simultaneous open orders |
| `--dry-run` | off | Logs orders without posting to CLOB |
| `--performance-mode` / `--no-performance-mode` | on | Pin process to P-cores, set HIGH priority (requires psutil); opt out with `--no-performance-mode` |
| `--pitcher-cache-path` | -- | Path to pitcher ERA cache for Stage-4 gate (from build_pitcher_cache.py) |
| `--wait-for-clob` | off | Wait for CLOB API to become available before starting (for maintenance windows) |
| `--wait-for-clob-timeout-secs` | 1800 | Max wait time for `--wait-for-clob` (30 min) |
| `--startup-refresh` / `--no-startup-refresh` | on | Rebuild completed-session analysis artifacts before live startup |
| `--startup-refresh-strict` | off | Abort startup if any refresh step fails |
| `--startup-refresh-skip-weather-cache` | off | Skip active-date local stadium weather cache refresh |
| `--startup-refresh-skip-stage1-cache` | off | Skip daily Stage-1 O/U cache rebuild |
| `--startup-weather-provider` | open-meteo | Weather v2 provider for startup cache (`none` writes metadata-only rows with unknown temp/wind) |
| `--startup-weather-timeout` | 8 | Per-request timeout for startup weather refresh |

### Order lifecycle

```
Signal fires
    v
Limit order placed at: bid + spread x 0.65
    v
Every ~4-10s: poll CLOB for fill
    v                        v
FV still high            FV decayed to within 3% of limit
(keep order alive)           v
    v                    Cancel -> record as "missed signal"
Game Final
    v                v
Filled           Still open
  v                  v
Settle P&L      Cancel -> record as "missed signal"
```

Execution notes for current logic:
- Ask-reversal guard runs first: if ask drops by `--ask-reversal-drop` within `--ask-reversal-window`, the order is cancelled.
- FV-decay cancellation only becomes eligible after `--fv-decay-min-age-secs` and requires ask confirmation via `--fv-decay-min-ask-drop`.
- Timeout cancellation (`--order-timeout-secs`) is a safety net, not the primary exit path.

Outputs:
- `data/live_trading/master_ledger.jsonl` -- all bets (filled: `_event: settled`, unfilled: `_event: missed`)
- `data/live_trading/live_orders_ledger.jsonl` -- canonical live order lifecycle stream
- `data/live_trading/candidate_universe/` -- trade, skip, and shadow state-value candidates
- `data/live_trading/sessions/YYYY-MM-DD_session.json` -- full session state
- `data/live_trading/book_captures/` -- post-signal book snapshots

---

## 6) Analysis Tools

### Session analytics report

Reads all session JSON files and prints a structured analytics dashboard. Run after each session to review performance.

```bash
python scripts/analysis/session_report.py
```

Reports:
- Overall P&L: filled win rate vs cancelled (counterfactual) win rate -- quantifies the Winner's Curse
- Fill win rate by inning, edge bucket, Stage-2 delta
- ask_drop_5s distribution for all bets (fills + cancels): market confirmation signal
- Per-session P&L with active gate flags read from session params
- Gate simulation: how many losses each current gate blocks vs wins it costs
- Per-venue fill win rate with suppressive-park flag
- Near-gate bets: signals that just passed a threshold (within 2pp) -- flags for future tuning

### Pitcher ERA cache

Required to activate Gate 8i (pitcher quality edge boost). Fetches current-season ERA from the MLB Stats API and writes `cache/pitcher_cache.json`. Run once before each session (or weekly -- ERA is stable):

```bash
python scripts/analysis/build_pitcher_cache.py --season 2026
```

Pass the cache to the live engine with:
```bash
python scripts/trading/live_engine.py --pitcher-cache-path cache/pitcher_cache.json
```

**Failure mode and recovery (TR14 hardening):** The 2026-04-28 V2-cutover session
had two consecutive StatsAPI 15s timeouts during cache build, which silently
disabled Gate 8i for the entire session. The monitor now retries cache builds 3
times with growing timeouts (15s -> 30s -> 60s) and falls back to the **stale
on-disk cache** if all rebuild attempts fail, so Gate 8i remains active across
transient StatsAPI hiccups. Stale-cache fallback is allowed up to
`PITCHER_CACHE_STALE_FALLBACK_MAX_AGE_HOURS` (currently 72h).

### Book capture analysis

Diagnoses order execution quality for a session or date range.

```bash
python scripts/analysis/analyze_book_captures.py

# Include session data for actual fill comparison
python scripts/analysis/analyze_book_captures.py \
  --sessions-root data/live_trading/sessions

# Specific date
python scripts/analysis/analyze_book_captures.py --date 2026-04-19
```

Reports:
- Fill rate by spread bucket
- Simulated P&L (taker vs limit)
- Ask velocity after signal (is market repricing before our order lands?)
- Simulated vs actual fill comparison
- Multi-line same-game correlated exposure
- Per-session breakdown

### Overreaction analysis

```bash
python scripts/analysis/analyze_polymarket_overreactions.py --date 2026-04-09
```

Detects scoring events from tick transitions and measures how much the market moved versus the fair-value model prediction.

### Gate backtest

```bash
python scripts/analysis/backtest_gates.py
```

Backtests the current gate stack against 5 years of historical data to estimate signal frequency and win rates by gate configuration.

### Unified signal table

```bash
python scripts/analysis/build_unified_signal_table.py --mode both --min-date 2026-04-01 --strict
```

Builds the canonical one-row-per-signal dataset (`signals_master`) plus lifecycle and snapshot satellite tables under `data/analysis_output/unified_signals/`.

### Leakage-aware training table

```bash
python scripts/analysis/build_signal_training_table.py --mode both --min-date 2026-04-01 --strict
```

Builds `signal_training_table` under `data/analysis_output/training_tables/` with:
- deterministic train/validation/test splits by `session_date`
- explicit pre-signal and post-signal feature groups in the manifest
- targets: `target_filled`, `target_profit`, `target_win`

### Calibration-opportunity training table

```bash
python scripts/analysis/build_calibration_opportunity_training_table.py --mode live --strict
```

Builds `calibration_opportunity_training_table` under
`data/analysis_output/calibration_opportunity_training/` from model-bearing
candidate rows, not just placed orders. It joins `*_score_confirmations.jsonl`
and `*_outcomes.jsonl` so score-event models can learn from skipped
opportunities and phantom/no-score confirmations. This is the preferred next
research table for reducing placed-bet selection bias. The builder also writes
family-specific files under `by_family/`; use those for calibration and
promotion work so score-event transition and no-score drift are never pooled by
accident.

### Fair-value stage ablation report

```bash
python scripts/analysis/fair_value_stage_ablation_report.py --mode live --max-date 2026-05-08
```

Builds `data/analysis_output/fair_value_stage_ablation/` from the
calibration-opportunity table. It compares the FV ladder against settled labels:
market ask baseline, current-state Stage-1, score-event inference, Stage-2
run-environment/weather, Stage-3 team offense, and final runtime FV. The
startup refresh runs this after `model_maturity_report` so each day starts with
a fresh view of which FV stage is helping or hurting calibration.

It can also recompute Stage-1 from a supplied cache while carrying logged
Stage-2/3 deltas forward. This is the preferred way to compare cache artifacts:

```bash
python scripts/analysis/fair_value_stage_ablation_report.py \
  --mode live --min-date 2023-01-01 --max-date 2026-05-09 \
  --stage1-cache-path cache/mlb_ou_cache_5y_baseline_2021_2025.json \
  --stage1-cache-label baseline_5y_2021_2025 \
  --output-root data/analysis_output/fair_value_stage_ablation/cache_compare/baseline_5y_2021_2025

python scripts/analysis/fair_value_stage_ablation_report.py \
  --mode live --min-date 2023-01-01 --max-date 2026-05-09 \
  --stage1-cache-path cache/mlb_ou_cache_10y_candidate.json \
  --stage1-cache-label candidate_10y_2016_2025 \
  --output-root data/analysis_output/fair_value_stage_ablation/cache_compare/candidate_10y_2016_2025
```

Latest 5y-vs-10y check on 607 labeled live opportunities through 2026-05-09:
the 10-season cache increased average FV and worsened Brier/logloss on this
small post-2023 validation sample, while score-event AUC improved. Treat the
10-season cache as useful but not automatically superior; compare by family,
ask bucket, current-state-edge bucket, and line before promoting FV changes.

### FV gap decomposition report

```bash
python scripts/analysis/build_fv_gap_decomposition_report.py \
  --mode live --max-date 2026-05-12
```

Builds `data/analysis_output/fv_gap_decomposition/` from the
calibration-opportunity table. This report treats market ask/no-vig midpoint as
the baseline and decomposes the model gap into current-state Stage-1,
inferred-state Stage-1, Stage-2/3 movement, and final runtime FV. It also
summarizes the new +1/+2/+3 run-count inference panel once fresh rows include
those runtime fields. Use it to diagnose why FV separates from market before
tuning any gate or calibration curve.

The calibration-opportunity builder carries selected `inferred_state_*`
summary fields into the training stream. For older compact sidecars, it
backfills from same-day raw candidate rows and then derives missing selected
summaries from the run-count panel, so FV-gap reports can compare selected
Poisson and empirical Stage-1 values without walking huge raw candidate files.
As of 2026-05-14 it also carries Stage-1 support diagnostics: selected
inferred-state support/trust is derived from the run-count panel or raw
candidates, while current-state support/trust can be backfilled from logged
cache cell keys. These fields are diagnostic-only and do not change FV.

### FV trust shrinkage experiment

```bash
python scripts/analysis/build_fv_trust_shrinkage_experiment.py \
  --mode live --max-date 2026-05-13
```

Builds `data/analysis_output/fv_trust_shrinkage/` from calibration
opportunities. It compares raw runtime FV against market-anchored shrinkage
variants:

- score-event transition uses selected inferred-state Stage-1 support when
  available, with current-state support as an explicit fallback variant
- no-score drift uses confirmed current-state Stage-1 support
- low-support rows are pulled toward ask or no-vig midpoint; high-support rows
  are allowed to keep more model-vs-market disagreement

The report ranks variants by family and split, then shows paper selection
results by edge threshold. It is descriptive only; any runtime shrinkage policy
requires walk-forward stability before promotion.

### Calibration market-anchored alpha

```bash
python scripts/analysis/train_calibration_market_anchored_alpha.py \
  --mode live --max-date 2026-05-12
```

Builds `data/analysis_output/calibration_market_anchored_alpha/` from
calibration opportunities. It trains family-separated research models with
`logit(p_over_win) = logit(market_price) + alpha(features)`, so the market is
the prior and the model only learns residual disagreement. This is not a live
artifact; promotion requires family-specific walk-forward stability against the
market ask/no-vig baseline.

Rolling promotion evidence lives in the dedicated walk-forward lane:

```bash
python scripts/analysis/calibration_market_anchored_alpha_walk_forward.py \
  --mode live --max-date 2026-05-12
```

This writes
`data/analysis_output/calibration_market_anchored_alpha_walk_forward/`.
It evaluates `score_event_transition` and `no_score_drift` separately, runs
both `ask` and `mid_no_vig` anchors, chooses alpha-edge thresholds on the
validation window only, and reports test P&L at executable ask with clustered
bootstrap confidence intervals by game/date/line.

The upstream calibration-opportunity training table now dedupes score-event
repeats by `(game,line,inning,half,outs,runners,score,reason)` by default. This
keeps repeated polling of the same baseball state from acting like independent
evidence. Use `--score-event-repeat-policy weight` or `none` only for targeted
audits.

### Stage-1 inferred empirical audit

```bash
python scripts/analysis/audit_stage1_inferred_empirical.py \
  --mode live --min-date 2026-05-07 --max-date 2026-05-12
```

Builds `data/analysis_output/stage1_inferred_empirical_audit/` from raw
candidate-universe rows and outcomes. The live score-event path still uses the
Stage-1 Poisson `poXX` lookup as base FV after inferred scoring; this report
reconstructs the matching empirical `oXX` sibling from the same inferred cache
cell, with support counts and line fallback metadata. It is diagnostic-only and
is now part of startup refresh so daily overconfidence checks are not manual.
Rows now include `effective_n_proxy`, `stage1_trust_weight`, support buckets,
exact-cell flags, Poisson/empirical line-exact flags, empirical sample support,
and fallback penalties where the cache metadata allows it.

### CLV / late-price diagnostics

```bash
python scripts/analysis/build_clv_report.py --mode live --max-date 2026-05-14
```

Builds `data/analysis_output/clv/`. The report compares entry price to the
last captured post-signal midpoint (`late_mid`) plus fixed horizons such as
30s/60s/120s. It groups CLV by model family, gate/reason, ask bucket, edge
bucket, inning bucket, runs-needed bucket, and phantom-risk bucket, then joins
filled trades to realized ROI. `late_mid` is a late captured mark, not a
guaranteed final market close; candidate rows without post-signal book capture
are retained as coverage rows so missing CLV coverage is visible.

### FV disagreement quality

```bash
python scripts/analysis/build_fv_disagreement_quality_report.py --mode live --max-date 2026-05-14
```

Builds `data/analysis_output/fv_disagreement_quality/`. The report starts from
calibration opportunities and asks whether runtime FV adds information when it
disagrees with the market anchor. It ranks family-specific buckets by Brier /
logloss gain over market, CLV versus entry, realized filled-trade ROI, and
Stage-1 support/trust. This is a diagnostic bridge between raw FV research and
market-anchored alpha: market price is the benchmark to beat, not the live model.

Walk-forward validation for those same disagreement regimes:

```bash
python scripts/analysis/fv_disagreement_quality_walk_forward.py --mode live --max-date 2026-05-14
```

Builds `data/analysis_output/fv_disagreement_quality_walk_forward/`. Each
rolling window selects trusted bucket keys from train data, requires those keys
to survive validation, then applies them to the next test date. The output
compares trusted test rows against all test disagreement rows by calibration
gain, CLV, and realized ROI. It is the anti-overfit lane for deciding whether
any attractive FV disagreement bucket deserves promotion into a model or policy.

First audit on 2026-05-07 through 2026-05-12 found 255 deduped score-event
states: market ask Brier `0.125`, Stage-1 Poisson Brier `0.173`, empirical
Brier `0.170`, average Poisson-minus-empirical `+0.075`, and a `>=5pp` positive
gap on 53% of states. That supports the current thesis that raw Poisson Stage-1
is overconfident, especially as a free-floating FV; test empirical-blended or
market-anchored variants in walk-forward before any live behavior change.

### Scoring environment trend report

```bash
python scripts/analysis/analyze_scoring_environment_trends.py \
  --min-season 2016 --max-season 2026 \
  --history-start-season 2016 --history-end-season 2025 \
  --projection-season 2026
```

Builds `data/analysis_output/scoring_trends/` from local regular-season game
feeds. Outputs season/month scoring trends, line-specific Over rates,
park-adjusted season effects, one-step-ahead season-weighting backtests, and a
research-only proposed Stage-1 season weight vector. The first run through
2026-05-11 showed 2026 YTD scoring below the 2016-2025 mean and recent-3-season
weighting beating all-prior uniform weighting on full-season RPG prediction.
Use this to design weighted-cache experiments; do not treat the proposed weights
as a live model change without FV ablation and walk-forward validation.

### Scoring path effects report

```bash
python scripts/analysis/analyze_scoring_path_effects.py \
  --min-season 2016 --max-season 2025 \
  --train-end-season 2022 --test-start-season 2023
```

Builds `data/analysis_output/scoring_path_effects/` from local full-length
regular-season game feeds. It tests whether prior scoring distribution
(steady scoring vs early burst, scoreless streaks, recent runs, concentration)
adds future-run or Over-probability signal after controlling for the same
current score state. First pass on 2016-2025 found the idea is directionally
visible in exact examples, but broad out-of-sample lift was tiny/negative, so
scoring-path features should be logged and tested as model features before any
FV/gate change.

Runtime now logs these fields from schedule linescore inning runs as
shadow/modeling inputs only: `scoring_inning_rate`, `scoring_half_rate`,
`burst_share`, `scoreless_streak`, `recent2_run_share`,
`weighted_run_inning_norm`, and `inning_run_slope`. They flow into candidate
rows, placed-bet/session diagnostics, unified signals, calibration opportunity
tables, no-score drift research, and alpha model feature sets, but they do not
change live FV or gates.

### Stage-1 prior candidate screen

```bash
python scripts/analysis/evaluate_stage1_prior_candidates.py \
  --mode live --min-date 2023-01-01 --max-date 2026-05-09
```

Builds `data/analysis_output/stage1_prior_candidates/`. It generates a broad
research matrix of rolling 3-10y windows, equal-season variants, exponential
recency weights, scoring-environment similarity weights, and blends. The screen
uses a fast inverse-Poisson proxy against cache-swapped FV to identify candidates
worth a full cache build. It also exports one weights CSV per candidate under
`stage1_prior_candidates/weights/`, so promising blends can be passed directly
to `cache/build_mlb_ou_cache.py --season-weights-path ...`.

First pass through 2026-05-09: proxy metrics favored target environments around
`8.85-8.88` RPG, especially rolling 4y and harsher scoring-environment weights.
Full cache tests showed rolling 4y (`2022-2025`) best on deduped all-family and
no-score calibration, while 5y/6y preserved better score-event rank ordering.
This remains a research comparison, not a production cache promotion.
Detailed notes are in
`model_improvements/stage1_prior_findings_2026_05_13.txt`.

### Baseline model training

```bash
python scripts/analysis/train_baseline_models.py --strict
```

Trains two baseline logistic models from the leakage-aware table:
- `signal_win`: predicts `target_win` from pre-signal features only
- `execution_fill`: predicts `target_filled` from pre-signal + post-signal execution features

Writes model artifacts, per-row predictions, and a metrics report to `data/analysis_output/model_baselines/`.

### EV policy backtest

```bash
python scripts/analysis/backtest_ev_policy.py --policy-mode live --strict
```

Retrains stricter EV components, then selects an EV policy on validation and reports out-of-sample test performance:
- `p_win_if_filled` runtime model (reliable decision-time features, filled-only rows)
- `p_fill_runtime` runtime model (reliable decision-time features only)
- `p_fill_strict` offline analysis model (pre + post features, with `sim_*` proxies excluded)
- EV scoring: `ev_realized = p_fill * ev_if_filled`

Outputs are written to `data/analysis_output/ev_policy/`. Live/shadow runtime
loads `ev_signal_win_if_filled_model.json` plus
`ev_execution_fill_runtime_model.json`; `ev_execution_fill_strict_model.json`
is offline-only because it intentionally includes post-signal book horizons
such as `ask_1s`/`bid_1s`.

### State-value transition report

```bash
python scripts/analysis/build_state_value_transition_report.py --mode live --min-date 2026-04-30 --max-date 2026-04-30
```

Builds a daily/regime report for the current strategy pivot:
- score-event transition rows by phantom-risk band and current-state edge
- no-score drift shadow rows by empirical support, Poisson support, inning, and drawdown regime
- ranked examples for review before deciding whether a shadow regime deserves promotion

Outputs are written to `data/analysis_output/state_value_transition/`.

### No-score drift paper policy evaluator

```bash
python scripts/analysis/evaluate_no_score_drift_policy.py --mode live --min-date 2026-04-30 --max-date 2026-04-30
```

Evaluates no-score drift as a shadow/paper policy only. It does not place live
orders. The evaluator keeps one row per game-line/same-score segment by taking
the first eligible shadow row, then joins final outcomes and reports:
- Poisson + empirical current-state support
- Poisson-only current-state support
- empirical-only and weak/missing support as diagnostic buckets

Outputs are written to `data/analysis_output/no_score_drift_policy/`.

### No-score drift walk-forward harness

```bash
python scripts/analysis/no_score_drift_walk_forward.py --mode live --stake 10 --daily-budget 80 --per-game-budget-fraction 0.40
```

Builds the separate no-score drift model-family table and walk-forward harness.
The table keeps one row per game-line/same-score segment, then joins final
outcomes and the no-score paper-ledger decision fields. The walk-forward path
trains only on past no-score drift rows and reports:
- Poisson FV, empirical FV, market ask, and learned model calibration metrics
- capped paper-policy results by support regime
- validation-selected model-edge thresholds tested on future dates

Outputs are written to `data/analysis_output/no_score_drift_walk_forward/`.
Use this before considering no-score drift promotion; it is the canonical
validation path for the no-score family.

### Walk-forward backtest harness

The single most important validation tool -- promoted to roadmap item #1 in 2026-04-30.
Trains models on past dates only, tunes on a trailing validation window, tests
on the next date, then rolls the window forward by one date. Reports
out-of-sample model calibration/discrimination plus baseline live-engine P&L,
drawdown, and trade-frequency stability across the full rolling test horizon.

```bash
# Default: walk forward across all dates with data, 14-day train / 3-day val /
# 1-day test windows, daily roll.
python scripts/analysis/walk_forward_runner.py --mode live

# Custom windows
python scripts/analysis/walk_forward_runner.py --mode live \
  --train-days 21 --val-days 5 --start-date 2026-04-15 --end-date 2026-04-30

# Dry-run (skip training, only print the planned windows)
python scripts/analysis/walk_forward_runner.py --mode live --plan-only
```

Outputs are written to `data/analysis_output/walk_forward/`:
- `summary.json` -- rolling-window aggregate metrics, including
  `baseline_live_engine_results` vs `model_policy_results`
- `per_window_results.jsonl` -- one row per (train_window, val_window, test_date)
- `calibration_drift.csv` -- Brier score / reliability metrics over time

`model_policy_results` is a skip/select simulation over historical live orders
only. It can show whether the walk-forward model would have skipped bad orders
we actually posted, but it cannot estimate fills or P&L for candidates that were
never posted by the live engine.

This is the canonical "is the model actually learning?" gate. Before promoting
any new gate, calibrator, or EV policy to enforce mode, walk-forward results on
that change must show stable out-of-sample improvement.

### Execution diagnostics report

```bash
python scripts/analysis/build_execution_diagnostics_report.py --mode live --strict
```

Builds a per-trade execution diagnostics table from unified signal artifacts:
- `limit_touch` and `first_touch_seconds` from post-signal book snapshots
- `cancel_reason` from final order lifecycle state
- `counterfactual_outcome` from settled signal direction (`final_total > line`)
- Prints a compact console view by default:
  top missed fills, cancel-reason counterfactual ROI impact, and touch-vs-fill gap

Optional console controls:
- `--no-console-report` to suppress stdout summary
- `--report-top-n 12` to control row count in compact sections

Outputs are written to `data/analysis_output/execution_diagnostics/`.

### Queue-aware execution replay

```bash
python scripts/analysis/build_queue_aware_execution_replay.py --mode live --strict
```

Compares execution price policies offline without changing live placement:
- current posted limit
- posted limit + 1c
- posted limit + 2c
- taker-like entry at decision ask

The replay reports both touch-based fills and queue-adjusted fills. The
queue-adjusted version uses a configurable buffer because top-of-book captures
do not reveal exact queue position. Use this to compare realized profit/ROI and
model EV per stake, not just fill rate.

Useful options:
- `--queue-buffer-cents 1.0`
- `--max-fill-seconds 120`
- `--min-date YYYY-MM-DD --max-date YYYY-MM-DD`

Outputs are written to `data/analysis_output/execution_replay/`.

### Daily observability rollups

Raw audit streams remain the source of truth, but daily runs now also produce
compact rollups so review starts from signal counts instead of huge logs:
- `data/live_trading/candidate_universe/YYYY-MM-DD_candidate_rollup.json`
  summarizes candidate attempts by write status, decision, reason,
  state-value strategy, and top game-line/reason buckets.
- Live and paper session JSON summaries embed the same candidate rollup plus
  `candidate_rollup_path`.
- Live shutdown/final run exit writes compact daily human-review reports to
  `data/analysis_output/daily_human_review/` so the latest run can be reviewed
  without scraping the raw log. These reports include a shadow Stage-2
  suppression dollar audit so blocked eventual winners/losers can be reviewed
  without changing gate logic.
- Startup and shutdown model-maturity reports include coverage checks for
  Under-pair/no-vig market fields and the +1/+2/+3 run-count inference panel,
  split by model family, so market-anchored and FV-gap diagnostics do not look
  healthier than their field coverage supports.
- Early pre-FV skip rows in raw candidate JSONL are compacted and sampled:
  rollups count every gate attempt, raw JSONL writes the first row per coarse
  state/price bucket plus a periodic sample, and verbose Weather v2 payload
  fields / legacy `weather_mlb_schedule_*` fields are omitted from those early
  rows. Calibration/model-bearing rows still keep full weather context, and the
  compact calibration sidecar remains the preferred modeling stream.
- No-score drift reports include Poisson-edge x empirical-edge x ask x drawdown
  regime cuts. UNDER paper reports replay shadow threshold variants at
  `min_under_edge=0.10` and `0.15` in addition to the configured baseline.
- The monitor emits one end-of-run `MONITOR ROLLUP` with schedule refresh
  counts, tick snapshot counts, retired book keys, and top retired-book details.
- Repeated successful `Schedule refreshed` messages and repeated book-retirement
  chatter move to DEBUG after the first useful INFO message.
- Repeated model DEBUG chatter for inference fallbacks and Stage-2/Stage-3
  adjustments is compacted into runtime rollups keyed by game, line, inning
  state, and source. Normal tick-buffer health is DEBUG-only every 30 minutes
  unless abnormal.

This reduces operator noise without deleting forensic data.

---

## Gate Evolution (test-run history)

| Run | Key change |
|---|---|
| TR1-TR2 | Baseline stabilization, ask-based signal, confirmation ticks |
| TR3 | Inning minimums, pace gate, ask floors, min current total |
| TR4 | Cross-inning dedup gap |
| TR5 | Edge threshold raised 0.10->0.12, high-line tier added |
| TR6 | Close-game + high-rn dead zone |
| TR7 | Stage-3 team offense model integrated, Stage-2 bug fixed |
| TR8 | Inning 5 bullpen transition gate |
| TR9 | Inning 6 setup-reliever gate, book capture duration extended |
| TR10 | Edge threshold raised 0.12->0.15, runs-needed lowered 4.0->3.5, inn5 threshold 3.0->2.5 |
| TR11 | Blowout shutout gate (trailing <= 1, lead >= 6, inning >= 6) |
| TR12 | FV saturation skip (base_fv >= 0.99), large FV/ask gap gate (edge > 0.28 AND inning >= 7); motivated by Apr 21 phantom-run analysis |
| TR13 | Blowout-adjacent gate (lead >= 4, inning >= 7); Stage-2 extreme suppression (S2 <= -0.20, inning >= 6); Stage-4 pitcher quality boost; FV saturation threshold corrected 0.98->0.99; edge gap threshold 0.30->0.28 |
| TR14 | Conditional blowout-relax (`gate_blowout_relax_mode=enforce`) -- re-allows trades on blowout-blocked candidates with ask >= 0.74 and runs_needed <= 2.5 in inning <= 7; shadow-relaxed framework added for offline gate calibration; Family A/B/C/D/E fill-model feature capture wired into both trade and late-stage skip paths; **extreme-edge** (edge > 0.30 in any inning) and **LTP-vs-ask** (`abs(ask - ltp) > 0.50`) added as **shadow risk tags** rather than enforced gates pending more sample; pitcher cache hardened (3-attempt retry + stale on-disk fallback); schedule refresh hardened (3-attempt retry with backoff); state-value transition diagnostics (`state_value_strategy`, `current_state_value_edge`, `shadow_phantom_risk_*`) added |
| TR15 (post-cutover) | Polymarket CLOB V2 SDK migration (`py-clob-client` -> `py-clob-client-v2==1.0.0`); validated in production 2026-04-28 with 3 placed orders, 2 fills, V2 cancel-by-`OrderPayload` working; collateral wrap USDC.e -> pUSD on funder wallet; `POLY_CLOB_HOST` env override added for pre-cutover testing |
| TR16 (state-value measurement) | No enforced gate changes; added no-score drift paper evaluator, current-state-edge band end-of-run diagnostics, queue-aware execution replay, candidate rollup sidecars, and monitor log rollups/deduped INFO chatter. |
| **TR17 (extreme-edge promotion, 2026-05-01)** | **`gate_extreme_edge` PROMOTED FROM SHADOW TO ENFORCE.** Skips any signal where `edge > extreme_edge_max` (default `0.30`) regardless of inning. This complements `gate_fv_ask_gap` (which is late-inning only). Justification: edge>0.25 settled filled bets through 2026-04-30 are 1W/5L, -$117.56 on $140.29 stake (loss bucket is ~120% of total realized loss). The 2026-04-28 LAA@CWS loss (edge=0.302, inning 4) bypassed `gate_fv_ask_gap` because that gate only fires inning >= 7; `gate_extreme_edge` would have blocked it. Gate is in `LATE_STAGE_SKIP_GATES` so triggered skips fire Family A-E feature capture. The `ltp_ask_gap` shadow tag remains shadow-only (weaker historical evidence). Threshold tunable via `--extreme-edge-max`. |
| **TR18 (deposit-wallet resilience, 2026-05-04)** | **No gate logic changes.** Transient-error retry with exponential backoff (2s/4s/8s, 3 attempts) added to all CLOB API operations in `polymarket_client.py` for the Polymarket Deposit Wallet rollout (May 4, 12:30 UTC). Open-order polling in `live_order_lifecycle.py` hardened to skip poll cycles on transient errors instead of triggering spurious fill-recovery. New `--wait-for-clob` startup flag blocks until the CLOB API responds (polls every 15s, 30-min timeout), allowing the bot to survive scheduled maintenance windows. `health_check()` method added to `CLOBOrderClient`. |
| **TR19 (extreme-edge threshold tightened, 2026-05-03)** | **`gate_extreme_edge` threshold lowered from 0.30 -> 0.22** (`DEFAULT_EXTREME_EDGE_MAX` in `signal_config.py`). No new gate; tightens the existing TR17 enforcement based on the 2026-04-28 -> 2026-05-03 live window analysis (`scripts/analysis/analyze_window_2026_04_28_to_05_03.py`). Window total: 21 settled, 11W/8L, -$55.19 P&L. The edge > 0.20 cohort was 4W/8L for -$79.57 on 12 bets at avg fill 0.63 (Wilson 95% upper-bound WR ~ 58% vs ~ 62% structural break-even). The edge < 0.20 cohort was 10W/1L for +$24.38 on 11 bets at avg fill 0.745. All 8 unique window losses had `p_score_event_proxy = 0.000` (no ask-jump confirmation of a real run), confirming systematic phantom-run rather than variance. Threshold choice 0.22 (not 0.20) preserves a 2pp buffer above the empirical cohort boundary so signals near the configured `edge_threshold` floors (0.10 / 0.15) still pass. Tunable via `--extreme-edge-max`. The `ltp_ask_gap` shadow tag remains shadow-only. Walk-forward certification on 30 days recommended as a follow-up before further tightening. **NOTE: TR19's empirical calibration is superseded as of 2026-05-07 (TR20) -- the TR19 threshold was tuned around v1 Stage-3's edge distribution; v2 produces a different distribution. Treat the 0.22 cap as a placeholder pending fresh post-TR20 calibration.** |
| **TR20 (Stage-3 v2 swap + fresh-test marker, 2026-05-07)** | **Stage-3 team-offense model REPLACED.** Deployed Model 3 from the V2 calibration work (`model_improvements/team_offense_v2_phase{1,4,45,5}_findings_2026_05_07.txt`). New Stage-3 blends three EB-shrunk per-team windows: prior_season_rpg (coef -0.1514), season_to_date_rpg (+0.1407), momentum_rpg_10 (+0.1503). Linear inning-weight ramp retained; per-inning weights tested but overfit on holdout. Fit on 1,129,081 leakage-free residual rows from 2021-2024, validated on 2025, holdout-tested on 2026. Replaces v1's 50-game-window + hard-clamp + LOGIT_DELTA_PER_RUN=0.20 model that Phase 1 showed was ~3.2x too aggressive. v1 code deleted in this changeset. **FRESH TESTING WINDOW STARTS 2026-05-07.** All TR1-TR19 evidence (gate thresholds, edge cohorts, win rates, P&L splits) was collected with v1 Stage-3 driving the edge distribution. v2 produces materially different edges, so prior gate calibrations may need re-tuning. Treat post-2026-05-07 data as the canonical source for any future gate-threshold or model-promotion decisions; pre-2026-05-07 data remains forensically useful but is no longer apples-to-apples. |
| **TR21 (Stage-2 park-HR feature pair, 2026-05-08)** | **Stage-2 model EXTENDED with two new feature families and REBUILT/PROMOTED.** Added `density_alt` (elevation x temperature interaction; standard density-altitude formula `DA = elev + 120*(T_F-59)`, bucketed `<0/0_1k/1k_2_5k/2_5k_5k/5k+`) and `hr_factor` (per-(park, season) HR rate vs league mean, EB-shrunk with prior n=30 games, bucketed `<0.85/0.85_0.95/0.95_1.05/1.05_1.15/1.15+`). Both are non-redundant with the existing static `park` bucket: `density_alt` varies with same-day temperature, `hr_factor` varies year-over-year (juiced ball, fence moves, humidor, etc.). Refit on 12,553 games (train <= 2024, val >= 2025, 148,270 val rows). Validation Brier improvements: O6.5 +0.42%, O7.5 +0.70%, O8.5 +0.58%, O9.5 +0.64%, O10.5 +0.42%, O11.5 +0.33%. Both features selected by the validation tuner on every line; `density_alt` weight 0.5 on 5/6 lines, `hr_factor` weight 0.25-0.5 on every line. New model promoted to `cache/mlb_stage2_run_env.json` (prior model backed up to `*.pre_density_alt_hr_factor_2026_05_08.bak`). New daily-refresh step `park_hr_factors` (in canonical startup) runs `scripts/analysis/build_park_hr_factors.py` to keep `cache/park_hr_factors.json` fresh; preflight_artifacts warns when missing. No gate threshold changes in this transaction; the Stage-2 edge distribution may shift slightly so post-TR20 fresh-test calibration audit (Phase 6, due ~2026-06-07) should account for both TR20 and TR21 together. |

---

## Fresh Testing Window (2026-05-07 onward)

On **2026-05-07** the Stage-3 team-offense model was swapped from v1 (single 50-game rolling RPG, single empirically-fit constant) to v2 (EB-shrunk blend of prior-season + season-to-date + momentum_10, with a NEGATIVE coefficient on prior-season). The v2 fit comes from 1.13M leakage-free residual rows over 2021-2026 (training 2021-2024, validation 2025, holdout 2026) and replaces a model that Phase 1 demonstrated was ~3.2x too aggressive. See the TR20 row in the Gate Evolution table and `model_improvements/team_offense_v2_*` files.

**All evidence below is pre-TR20 unless dated 2026-05-07 or later.** v2 produces a materially different edge distribution from v1, so:

- The TR19 `extreme_edge_max=0.22` cap was tuned around v1's edge distribution and may not provide equivalent protection under v2. Recalibration after 30 days of post-2026-05-07 data is recommended before the next gate-threshold change.
- Edge-cohort win rates, fill-rate splits, gate-economics audits, and walk-forward results computed on pre-2026-05-07 data should be re-run with post-TR20 data before they're used to justify any new gate or model promotion.
- `model_improvements/team_offense_v2_phase5_findings_2026_05_07.txt` documents the bidirectional gate-interaction effects observed in the dollar test on the small pre-cutover sample.
- Candidate rows now include `shadow_post_tr20_*_pass` fields so tighter
  edge-cap, ask-ramp, and Gate-6-relax hypotheses can be audited over the fresh
  window before changing live gate logic.

The pre-2026-05-07 Evidence Snapshot below remains forensically useful but is no longer apples-to-apples for forward-looking decisions.

---

## Evidence Snapshot

*As of 2026-05-02. Canonical source: `data/analysis_output/unified_signals/signals_master.jsonl` plus live session/order ledgers and daily human-review reports.*

*Pre-TR20: see Fresh Testing Window note above.*

The current objective is evidence-driven. Key observations from unified tables, session summaries, and live logs:

- Unique placed live orders in unified signals: 54 through 2026-05-02
- **Fill rate: 57.4%** (31 orders reached `filled` status / 54 placed; 30 filled rows are settled in the rebuilt table)
- **Settled filled win rate: 60.0%** (18W/12L)
- **Signal win rate (counterfactual, includes cancelled bets): 75.5%** (40/53 settled signals)
- **Cancelled-bet counterfactual win rate: 95.5%** (21W/1L on settled cancels) - the Winner's Curse gap remains large
- **Realized P&L: -$106.74** on $533.08 filled stake in the unified live table through 2026-05-02
- Current gate economics still require rolling/walk-forward validation before promotion of any new enforced gate

Edge split (still important, now through 2026-05-02):
- `edge > 0.25` settled filled bets: **1W/6L, -$127.56** on $150.29 stake. **As of 2026-05-01 (TR17), `gate_extreme_edge` was ENFORCED at 0.30; on 2026-05-03 (TR19) the threshold was tightened to 0.22 -- see Gate Evolution table for justification.** Bets in the 0.22-0.30 band are now blocked.
- `edge > 0.30` settled filled bets: **0W/2L, -$51.24** on $51.24 stake. Blocked by TR17 since 2026-05-01.
- `0.25 < edge <= 0.30` settled filled bets: **1W/4L, -$76.32** on $99.05 stake. **Now blocked by TR19 (2026-05-03)** along with the rest of the 0.22-0.30 band.
- `edge <= 0.25` settled filled bets: **17W/6L, +$20.82** on $358.50 stake. The 2026-04-28 -> 2026-05-03 window further confirmed that edge<0.20 is the profitable cohort (10W/1L, +$24.38).
- 2026-04-28 -> 2026-05-03 window edge cohort detail: edge>0.20 was 4W/8L, -$79.57 on 12 bets; edge<0.20 was 10W/1L, +$24.38 on 11 bets. All 8 unique losses had `p_score_event_proxy = 0.000` (no ask-jump confirming a real run). Source: `scripts/analysis/analyze_window_2026_04_28_to_05_03.py`.

Interpretation:
- Directional signal quality is high but fill selection quality is what matters
- Large model-to-market gaps are information asymmetry, not edge -- `gate_fv_ask_gap` (late-inning, TR12) and `gate_extreme_edge` (any-inning, TR17 + tightened TR19) jointly address this failure mode
- The TR12/TR13/TR17/TR19 gate additions directly address the worst historical losses; `ltp_ask_gap` remains shadow-only pending more sample
- `p_score_event_proxy` is a strong winner-vs-loser separator in the 2026-04-28 -> 2026-05-03 window (wins mean 0.065, losses mean 0.000); promoting it from shadow to a soft gate is the next candidate for evidence-driven enforcement after TR19's walk-forward audit

### State-value pivot evidence (through 2026-05-02)

The April 29 review refined the strategy rather than replacing the system. The key finding was that score-event candidates can be dominated by phantom post-score state assumptions: the model may infer a scoring event, assign a very high post-event FV, and still be rejected by a market that is correctly pricing the current confirmed score.

Observed 2026-04-29 to 2026-05-02 diagnostics:
- May 1 was profitable (+$2.59, 3W/1L filled), while May 2 gave back more (-$11.52, 3W/2L filled). The two-day result reinforces that 60-75% hit rates can still lose money at high Over prices.
- May 2's weakest trade was HOU@BOS O9.5: current-state edge was negative, phantom risk was medium, and the bet lost. Current-state edge below 0.03 remains a warning regime, not an enforced gate.
- No-score drift remains promising only when empirical current-state support agrees with Poisson support; Poisson-only drift is weaker and should stay paper/shadow.
- Queue-aware execution replay suggests execution price selection may be a higher-ROI research path than adding more gates, but it remains offline until more data accumulates.

Current interpretation:
- The system should keep collecting stable-filter live data.
- Score-event trades need current-state and phantom-risk diagnostics attached to every bet.
- No-score drift should remain shadow-only until there is enough sample to promote or discard it responsibly.
- EV policy and probability calibration remain shadow diagnostics, not enforced gates, until out-of-sample evidence improves.

---

## Roadmap

The roadmap (recently completed infrastructure, active priorities in priority
order, and operational guidance) is maintained in **[ROADMAP.md](ROADMAP.md)**.

---

## Key Caveats

- Live sample size remains small for strong statistical confidence.
- A few outcomes can dominate short-run P&L when prices are high and stake is concentrated.
- Long-term success depends on execution-aware selection, calibration, and disciplined no-trade behavior.
